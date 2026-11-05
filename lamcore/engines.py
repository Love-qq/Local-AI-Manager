"""Движки: адаптеры, менеджер и честная совместимость.

UI общается с движком только через EngineAdapter. Проверка совместимости и
сборка командной строки отделены от запуска процесса: несовместимую модель
нельзя запустить даже случайно, потому что launch() получает готовый план,
который строит только plan_launch() после проверки.

Три состояния совместимости:
* COMPATIBLE   — движок умеет запускать эту модель;
* INCOMPATIBLE — формат поддерживается, но конкретная модель не запустится
                 (нет шардов, битый файл, нет контекста);
* UNSUPPORTED  — движок не работает с этим форматом вообще;
* UNVERIFIED   — проверить нельзя; предупреждение не показывается.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from . import __version__
from .modelinfo import ModelInfo, inspect_path
from .paths import AppPaths, is_inside, safe_join
from .storage import ModelKind
from .tls import https_handler


class CompatibilityStatus(str, Enum):
    COMPATIBLE = "compatible"
    INCOMPATIBLE = "incompatible"
    UNSUPPORTED = "unsupported"
    UNVERIFIED = "unverified"


@dataclass(frozen=True)
class Compatibility:
    status: CompatibilityStatus
    engine: str
    code: str = ""
    detail: str = ""

    @property
    def is_blocking(self) -> bool:
        # UNVERIFIED не блокирует запуск и не показывает предупреждение.
        return self.status in (CompatibilityStatus.INCOMPATIBLE, CompatibilityStatus.UNSUPPORTED)

    def __bool__(self):
        return self.status == CompatibilityStatus.COMPATIBLE


def compatible(engine, code="ok", detail="") -> Compatibility:
    return Compatibility(CompatibilityStatus.COMPATIBLE, engine, code, detail)


def incompatible(engine, code, detail) -> Compatibility:
    return Compatibility(CompatibilityStatus.INCOMPATIBLE, engine, code, detail)


def unsupported(engine, code, detail) -> Compatibility:
    return Compatibility(CompatibilityStatus.UNSUPPORTED, engine, code, detail)


def unverified(engine, code, detail) -> Compatibility:
    return Compatibility(CompatibilityStatus.UNVERIFIED, engine, code, detail)


class LaunchError(RuntimeError):
    pass


class ConfirmationRequired(RuntimeError):
    """Установка/удаление без явного согласия пользователя запрещены."""


class IncompatibleModelError(RuntimeError):
    def __init__(self, compatibility: Compatibility):
        self.compatibility = compatibility
        super().__init__(compatibility.detail or compatibility.code)


@dataclass(frozen=True)
class LaunchParams:
    model_path: str
    ctx: int = 4096
    gpu_layers: int = 99
    threads: int = 4
    port: int = 8080
    alias: str = "local-model"
    host: str = "127.0.0.1"
    extra_args: tuple = ()

    def validated(self, *, engine_max_ctx=None) -> "LaunchParams":
        if not self.model_path:
            raise LaunchError("не выбрана модель")
        if not (512 <= int(self.ctx) <= (engine_max_ctx or 4_000_000)):
            raise LaunchError(f"контекст вне допустимого диапазона: {self.ctx}")
        if int(self.gpu_layers) < 0:
            raise LaunchError("число слоёв GPU не может быть отрицательным")
        if int(self.threads) < 1:
            raise LaunchError("число потоков CPU должно быть >= 1")
        if not (1 <= int(self.port) <= 65535):
            raise LaunchError("порт вне диапазона 1..65535")
        return self


@dataclass(frozen=True)
class LaunchPlan:
    argv: tuple
    cwd: str
    alias: str
    port: int
    host: str

    def command_line(self) -> str:
        return subprocess.list2cmdline(list(self.argv))


def plan_llama_launch(server, params: LaunchParams, *, engine_max_ctx=None) -> LaunchPlan:
    """Чистая сборка argv: процесса ещё нет, поэтому проверить всё можно тестом."""
    params = params.validated(engine_max_ctx=engine_max_ctx)
    server = str(server or "")
    if not server or not Path(server).is_file():
        raise LaunchError("llama-server.exe не найден")
    model = Path(params.model_path)
    if not model.is_file():
        raise LaunchError("файл модели не найден")
    argv = [server, "-m", str(model), "-c", str(int(params.ctx)),
            "-ngl", str(int(params.gpu_layers)), "-a", params.alias,
            "-t", str(int(params.threads)), "--host", params.host,
            "--port", str(int(params.port))]
    argv.extend(str(item) for item in params.extra_args)
    return LaunchPlan(argv=tuple(argv), cwd=str(Path(server).parent), alias=params.alias,
                      port=int(params.port), host=params.host)


def prevent_unsafe_launch(adapter, info: ModelInfo) -> Compatibility:
    """Единственная точка проверки перед запуском любого движка."""
    verdict = adapter.can_run(info)
    if verdict.is_blocking:
        raise IncompatibleModelError(verdict)
    return verdict


class EngineAdapter:
    """Интерфейс движка. Ни один вызов не трогает UI."""

    engine_id = "base"
    display_name = "Base"

    def detect(self) -> bool:
        raise NotImplementedError

    def version(self) -> str:
        raise NotImplementedError

    def can_run(self, model_info: ModelInfo) -> Compatibility:
        raise NotImplementedError

    def plan(self, params: LaunchParams):
        raise NotImplementedError

    def launch(self, params: LaunchParams, model_info: ModelInfo, *, spawn=None):
        raise NotImplementedError

    def stop(self, process, *, timeout=8.0):
        return stop_process(process, timeout=timeout)

    def generate(self, params: LaunchParams, messages, *, on_chunk=None, cancel_event=None,
                 timeout=600):
        raise NotImplementedError

    def cancel(self, cancel_event):
        if cancel_event is not None:
            cancel_event.set()

    def status(self, process=None):
        if process is None:
            return "stopped"
        return "running" if process.poll() is None else f"exited:{process.returncode}"

    def resources(self, pid):
        return {}


class LlamaCppAdapter(EngineAdapter):
    engine_id = "llama.cpp"
    display_name = "llama.cpp (llama-server)"
    ENGINE_MAX_CTX = 1_048_576

    def __init__(self, server_path="", *, runner=None):
        self.server_path = str(server_path or "")
        self._runner = runner or run_process

    # --- определение ------------------------------------------------------
    def detect(self) -> bool:
        return bool(self.server_path) and Path(self.server_path).is_file()

    def version(self) -> str:
        if not self.detect():
            return ""
        try:
            code, output = self._runner([self.server_path, "--version"], 15)
        except Exception as exc:  # не смогли запустить — версия неизвестна
            return f"unknown ({exc})"
        text = (output or "").strip().splitlines()
        for line in text:
            if "version" in line.lower():
                return line.strip()[:120]
        return text[0][:120] if text else ("" if code == 0 else f"exit {code}")

    # --- совместимость ----------------------------------------------------
    def can_run(self, model_info: ModelInfo) -> Compatibility:
        if model_info.is_supporting or model_info.kind == ModelKind.SUPPORTING_FILE:
            if "clip-projector" in (model_info.architecture or "") or "mmproj" in model_info.name:
                return unsupported(self.engine_id, "mmproj",
                                   "mmproj — CLIP-проектор, а не языковая модель: "
                                   "llama.cpp принимает его только через --mmproj вместе с основной GGUF.")
            return unsupported(self.engine_id, "supporting_file",
                               "Это вспомогательный файл, а не запускаемая модель.")
        if model_info.format == "gguf":
            if model_info.completeness == "incomplete":
                missing = ", ".join(model_info.missing_files[:4]) or "часть файлов"
                return incompatible(self.engine_id, "incomplete",
                                    f"Набор GGUF неполный: не хватает {missing}.")
            if model_info.completeness == "unknown":
                return unverified(self.engine_id, "header_unreadable",
                                  "Заголовок GGUF не прочитан — совместимость неизвестна.")
            return compatible(self.engine_id, "gguf")
        if model_info.format == "ggml":
            return unverified(self.engine_id, "legacy_ggml",
                              "GGML — устаревший формат; современные сборки llama.cpp "
                              "могут его не открыть.")
        if model_info.format in ("safetensors", "pytorch", "onnx"):
            return unsupported(self.engine_id, f"format_{model_info.format}",
                               f"llama.cpp не читает {model_info.format}: нужна GGUF-модель "
                               "или другой движок.")
        return unsupported(self.engine_id, "unknown_format",
                           "Формат модели не распознан, запуск невозможен.")

    # --- запуск -----------------------------------------------------------
    def plan(self, params: LaunchParams) -> LaunchPlan:
        return plan_llama_launch(self.server_path, params,
                                 engine_max_ctx=self.ENGINE_MAX_CTX)

    def launch(self, params: LaunchParams, model_info: ModelInfo, *, spawn=None):
        prevent_unsafe_launch(self, model_info)   # до plan() и до процесса
        plan = self.plan(params)
        spawn = spawn or default_spawn
        return spawn(plan)

    # --- генерация --------------------------------------------------------
    def generate(self, params: LaunchParams, messages, *, on_chunk=None, cancel_event=None,
                 timeout=600, temperature=0.7, max_tokens=1024):
        payload = {"model": params.alias or "local-model", "messages": list(messages),
                   "temperature": float(temperature), "max_tokens": int(max_tokens),
                   "stream": True}
        url = f"http://{params.host}:{int(params.port)}/v1/chat/completions"
        return stream_chat_completion(url, payload, on_chunk=on_chunk,
                                      cancel_event=cancel_event, timeout=timeout)


class _NoEngineAdapter(EngineAdapter):
    """Заглушка для форматов, которые движок не умеет; честно возвращает UNSUPPORTED."""

    def __init__(self, engine_id, display_name, reason, formats=()):
        self.engine_id = engine_id
        self.display_name = display_name
        self.reason = reason
        self.formats = tuple(formats)

    def detect(self) -> bool:
        return False

    def version(self) -> str:
        return ""

    def can_run(self, model_info: ModelInfo) -> Compatibility:
        if self.formats and model_info.format not in self.formats:
            return unsupported(self.engine_id, "format",
                               f"{self.display_name} не работает с форматом {model_info.format}.")
        return unsupported(self.engine_id, "no_verified_path", self.reason)


ONNX_UNVERIFIED_REASON = (
    "ONNX Runtime установлен, но в приложении нет проверенного пути запуска "
    "ONNX-модели (нужны токенизатор и генерация токенов под конкретную модель). "
    "Поэтому запуск ONNX здесь не поддерживается.")
TRANSFORMERS_UNVERIFIED_REASON = (
    "Transformers + PyTorch установлен, но приложение не умеет проверенно "
    "запускать генерацию для этой модели: архитектура, токенизатор и параметры "
    "генерации не проверяются. Запуск через этот движок не поддерживается.")


class EngineManager:
    """Поиск, проверка, установка и удаление движков."""

    def __init__(self, paths: AppPaths, *, hardware=None, runner=None, spawn=None):
        self.paths = AppPaths(paths.root) if not isinstance(paths, AppPaths) else paths
        self.hardware = hardware
        self.runner = runner or run_process
        self.spawn = spawn
        self._system_server = ""

    # --- адаптеры ---------------------------------------------------------
    def adapter(self, engine_id, server_path="") -> EngineAdapter:
        if engine_id == "llama.cpp":
            return LlamaCppAdapter(server_path or self.llama_server() or "", runner=self.runner)
        if engine_id == "ONNX Runtime":
            return _NoEngineAdapter("onnx", "ONNX Runtime", ONNX_UNVERIFIED_REASON, ("onnx",))
        if engine_id == "Transformers + PyTorch":
            return _NoEngineAdapter("transformers", "Transformers + PyTorch",
                                    TRANSFORMERS_UNVERIFIED_REASON, ("safetensors", "pytorch"))
        raise KeyError(engine_id)

    def known_engines(self) -> list:
        return ["llama.cpp", "ONNX Runtime", "Transformers + PyTorch", "Ollama"]

    def set_system_server(self, path):
        self._system_server = str(path or "")
        return self

    def llama_server(self) -> str:
        """Ищет llama-server.exe: engines/, затем PATH, затем путь из настроек."""
        engine_root = self.paths.engines / "llama.cpp"
        if engine_root.is_dir():
            found = sorted(engine_root.rglob("llama-server.exe"),
                           key=lambda p: (0 if p.parent.name.lower() == "bin" else 1, len(str(p))))
            if found:
                return str(found[0])
        which = shutil.which("llama-server")
        if which:
            return which
        if self._system_server and Path(self._system_server).is_file():
            return self._system_server
        return ""

    def detect_installed(self) -> dict:
        result = {}
        for engine_id in self.known_engines():
            if engine_id == "llama.cpp":
                path = self.llama_server()
                installed = bool(path)
                status = EngineStatus(engine_id=engine_id, display_name="llama.cpp",
                                      installed=installed, path=path,
                                      version=self.adapter(engine_id, path).version() if installed else "",
                                      detail="" if installed else "не найден llama-server.exe")
            elif engine_id == "Ollama":
                exe = shutil.which("ollama") or ""
                status = EngineStatus(engine_id=engine_id, display_name="Ollama",
                                      installed=bool(exe), path=exe, version="",
                                      detail="" if exe else "Ollama не найден в PATH")
            else:
                folder = "onnx" if engine_id == "ONNX Runtime" else "transformers"
                env = self.paths.engines / "venvs" / folder
                python = env / "Scripts" / "python.exe"
                installed = python.is_file()
                status = EngineStatus(engine_id=engine_id, display_name=engine_id,
                                      installed=installed, path=str(env),
                                      version="", detail="" if installed else "нет отдельного окружения")
            result[engine_id] = status
        return result

    # --- установка / удаление --------------------------------------------
    def install(self, engine_id, *, confirm=False, downloader=None, verify=True):
        """Устанавливает движок. Без confirm=True ничего не скачивается."""
        if not confirm:
            raise ConfirmationRequired("Установка движка требует подтверждения пользователя.")
        if engine_id not in self.known_engines():
            raise KeyError(engine_id)
        if downloader is None:
            raise LaunchError(f"Для {engine_id} не задан способ установки.")
        detail = downloader()
        status = None
        if verify and engine_id == "llama.cpp":
            status = EngineStatus(engine_id=engine_id, display_name="llama.cpp",
                                  installed=bool(self.llama_server()), path=self.llama_server(),
                                  version=self.adapter(engine_id).version(),
                                  detail="проверено после установки")
            if not status.installed:
                raise LaunchError("После установки llama-server.exe не найден — установка не подтверждена.")
        return detail, status

    def remove(self, engine_id, *, confirm=False):
        """Удаляет только то, что лежит в ENGINES_DIR. Модели не трогает никогда."""
        if not confirm:
            raise ConfirmationRequired("Удаление движка требует подтверждения пользователя.")
        if engine_id == "llama.cpp":
            target = self.paths.engines / "llama.cpp"
        elif engine_id == "ONNX Runtime":
            target = self.paths.engines / "venvs" / "onnx"
        elif engine_id == "Transformers + PyTorch":
            target = self.paths.engines / "venvs" / "transformers"
        else:
            raise LaunchError(f"Удаление {engine_id} вручную не поддерживается.")
        if not target.exists():
            return False
        if not is_inside(target, self.paths.engines):
            raise LaunchError("Отказ: путь движка вне каталога движков.")
        shutil.rmtree(target)
        return True

    # --- совместимость ----------------------------------------------------
    def models_for_engine(self, engine_id, model_infos) -> list:
        adapter = self.adapter(engine_id)
        return [info for info in model_infos
                if adapter.can_run(info).status == CompatibilityStatus.COMPATIBLE]

    def best_engine(self, model_info: ModelInfo, *, server_path="") -> Compatibility:
        """Первый подходящий движок или самое информативное объяснение."""
        verdicts = []
        for engine_id in self.known_engines():
            if engine_id in ("ONNX Runtime", "Transformers + PyTorch", "Ollama"):
                if engine_id == "Ollama":
                    continue
                adapter = self.adapter(engine_id)
            else:
                adapter = self.adapter(engine_id, server_path)
            verdict = adapter.can_run(model_info)
            if verdict.status == CompatibilityStatus.COMPATIBLE:
                return verdict
            verdicts.append(verdict)
        for verdict in verdicts:
            if verdict.status == CompatibilityStatus.INCOMPATIBLE:
                return verdict
        if verdicts:
            return verdicts[0]
        return unsupported("none", "no_engine", "Не найдено ни одного движка.")

    def require_launchable(self, model_info, *, engine_id="llama.cpp", server_path="") -> EngineAdapter:
        adapter = self.adapter(engine_id, server_path)
        prevent_unsafe_launch(adapter, model_info)   # бросает до создания процесса
        return adapter


@dataclass
class EngineStatus:
    engine_id: str
    display_name: str = ""
    installed: bool = False
    path: str = ""
    version: str = ""
    detail: str = ""

    def as_dict(self):
        return self.__dict__.copy()


# --- общие помощники -------------------------------------------------------

def run_process(argv, timeout=20):
    result = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                            errors="replace", timeout=timeout,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return result.returncode, ((result.stdout or "") + (result.stderr or "")).strip()


def default_spawn(plan: LaunchPlan):
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    return subprocess.Popen(list(plan.argv), cwd=plan.cwd, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                            errors="replace", creationflags=flags)


def stop_process(process, *, timeout=8.0):
    if process is None:
        return None
    try:
        if process.poll() is not None:
            return process.returncode
        process.terminate()
        return process.wait(timeout=timeout)
    except Exception:
        try:
            process.kill()
        except Exception:
            pass
        return None


def stream_chat_completion(url, payload, *, on_chunk=None, cancel_event=None, timeout=600,
                           opener=None):
    """SSE-поток OpenAI-compatible API. Возвращает собранный текст.

    Каждый delta.content добавляется ровно один раз; повторы содержимого не
    схлопываются, потому что одинаковые куски могут быть частью ответа.
    """
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=data, method="POST", headers={
        "Content-Type": "application/json", "Accept": "text/event-stream",
        "User-Agent": f"Local-AI-Manager/{__version__}"})
    open_url = opener or urllib.request.build_opener(https_handler()).open
    parts = []
    try:
        with open_url(request, timeout=timeout) as response:
            for raw_line in response:
                if cancel_event is not None and cancel_event.is_set():
                    break
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                item = line[5:].strip()
                if item == "[DONE]":
                    break
                try:
                    event = json.loads(item)
                except json.JSONDecodeError:
                    continue
                choices = event.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                content = delta.get("content", "")
                if isinstance(content, list):
                    content = "".join(str(x.get("text", x)) if isinstance(x, dict) else str(x)
                                      for x in content)
                if content:
                    parts.append(str(content))
                    if on_chunk:
                        on_chunk(str(content))
    except urllib.error.HTTPError as exc:
        raise LaunchError(f"HTTP {exc.code}: {exc.reason}") from exc
    return "".join(parts)
