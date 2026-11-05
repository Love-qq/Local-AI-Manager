import json
import os
import subprocess
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from pathlib import Path
import ctypes
import hashlib
import queue
import tkinter.font as tkfont
import re
import shutil
import sys
import time
import platform
import uuid
import urllib.parse
import urllib.request
import urllib.error
import zipfile
from datetime import datetime

from lamcore import timers as lam_timers
from lamcore.tls import create_ssl_context, https_handler
from lamcore import uirules
from lamcore.chat import ChatTranscript, EventSubscription
from lamcore.engines import (CompatibilityStatus, ConfirmationRequired, EngineManager,
                             IncompatibleModelError, LaunchError, LaunchParams,
                             stream_chat_completion)
from lamcore.hardware import (CONTEXT_STOPS, HardwareInfo, kv_cache_bytes,
                              manual_context, max_output_tokens, recommend_context)
from lamcore.huggingface import (DownloadCancelled, FilePicker, HFError,
                                 InvalidRepoURL, build_file_list, default_part_dir,
                                 download_file, fetch_json, format_size,
                                 parse_repo_url, plan_download, row_label)
from lamcore.i18n import LOCALES, Translator
from lamcore.modelinfo import ModelInfo, inspect_path as inspect_model
from lamcore.paths import AppPaths, UnsafePathError, safe_join
from lamcore.storage import (ModelCatalog, ModelKind, classify_file, discover_models,
                             merge_user_models, unique_path)

APP_DIR = Path(os.getenv("APPDATA", str(Path.home()))) / "LocalAIManager"
CONFIG_FILE = APP_DIR / "config.json"
DEFAULT_CONTEXTS = ["2048", "4096", "8192", "16384", "32768", "65536", "131072"]

# --- Надзор за процессом llama-server (Job Object + CTRL_BREAK) -------------
IS_WINDOWS = os.name == "nt"
APP_USER_AGENT = "Local-AI-Manager/48.1.2"
CREATE_SUSPENDED = 0x00000004  # в subprocess этой константы нет
CTRL_BREAK_EXIT_CODE = 0xC000013A  # STATUS_CONTROL_C_EXIT: llama.cpp вышел по CTRL_BREAK
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
CTRL_BREAK_EVENT = 1
PROCESS_SET_QUOTA = 0x0100
PROCESS_TERMINATE = 0x0001
PROCESS_SUSPEND_RESUME = 0x0800
PROCESS_QUERY_INFORMATION = 0x0400


class _PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong),
        ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
        ("PrivateUsage", ctypes.c_size_t),
    ]


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _JOB_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _JOB_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOB_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


_K32_API = None
_NTDLL_RESUME = None


def _kernel32():
    """Возвращает kernel32 с явными прототипами (важно для 64-битных HANDLE)."""
    global _K32_API
    if _K32_API is not None:
        return _K32_API
    if not IS_WINDOWS:
        _K32_API = False
        return False
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except OSError:
        _K32_API = False
        return False
    k32.CreateJobObjectW.restype = ctypes.c_void_p
    k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
    k32.SetInformationJobObject.restype = ctypes.c_int
    k32.SetInformationJobObject.argtypes = [
        ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    k32.AssignProcessToJobObject.restype = ctypes.c_int
    k32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    k32.TerminateJobObject.restype = ctypes.c_int
    k32.TerminateJobObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    k32.OpenProcess.restype = ctypes.c_void_p
    k32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    k32.CloseHandle.restype = ctypes.c_int
    k32.CloseHandle.argtypes = [ctypes.c_void_p]
    k32.AttachConsole.restype = ctypes.c_int
    k32.AttachConsole.argtypes = [ctypes.c_uint32]
    k32.FreeConsole.restype = ctypes.c_int
    k32.FreeConsole.argtypes = []
    k32.GenerateConsoleCtrlEvent.restype = ctypes.c_int
    k32.GenerateConsoleCtrlEvent.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
    _K32_API = k32
    return k32


def _ntdll_resume():
    """NtResumeProcess нужен, чтобы снять CREATE_SUSPENDED у процесса."""
    global _NTDLL_RESUME
    if _NTDLL_RESUME is not None:
        return _NTDLL_RESUME
    if not IS_WINDOWS:
        _NTDLL_RESUME = False
        return False
    try:
        ntdll = ctypes.WinDLL("ntdll")
        ntdll.NtResumeProcess.restype = ctypes.c_long
        ntdll.NtResumeProcess.argtypes = [ctypes.c_void_p]
    except OSError:
        _NTDLL_RESUME = False
        return False
    _NTDLL_RESUME = ntdll
    return ntdll


def _last_error():
    return ctypes.get_last_error() if IS_WINDOWS else 0


def close_handle(handle):
    """Закрывает handle Job Object. Закрытие job с KILL_ON_JOB_CLOSE убивает процесс."""
    k32 = _kernel32()
    if k32 and handle:
        try:
            k32.CloseHandle(ctypes.c_void_p(handle))
        except Exception:
            pass


def create_process_job():
    """Job Object: при закрытии последнего handle (в том числе при падении менеджера)
    Windows завершит сервер и всех его потомков."""
    k32 = _kernel32()
    if not k32:
        return None
    try:
        handle = k32.CreateJobObjectW(None, None)
        if not handle:
            return None
        info = _JOB_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not k32.SetInformationJobObject(
                ctypes.c_void_p(handle),
                JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                ctypes.byref(info),
                ctypes.sizeof(info)):
            close_handle(handle)
            return None
        return handle
    except Exception:
        return None


def detach_job(job):
    """Снимает KILL_ON_JOB_CLOSE и закрывает handle: сервер продолжит работать без менеджера."""
    k32 = _kernel32()
    if not k32 or not job:
        return False
    try:
        info = _JOB_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = 0
        ok = k32.SetInformationJobObject(
            ctypes.c_void_p(job),
            JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(info),
            ctypes.sizeof(info))
        close_handle(job)
        return bool(ok)
    except Exception:
        close_handle(job)
        return False


def assign_process_to_job(job, pid):
    """Помещает процесс в Job Object. Возвращает True при успехе."""
    k32 = _kernel32()
    if not k32 or not job or not pid:
        return False
    process_handle = None
    try:
        process_handle = k32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, int(pid))
        if not process_handle:
            return False
        return bool(k32.AssignProcessToJobObject(
            ctypes.c_void_p(job), ctypes.c_void_p(process_handle)))
    except Exception:
        return False
    finally:
        if process_handle:
            try:
                k32.CloseHandle(ctypes.c_void_p(process_handle))
            except Exception:
                pass


def resume_suspended_process(pid):
    """Снимает CREATE_SUSPENDED. Возвращает True, если процесс снова выполняется."""
    if not IS_WINDOWS:
        return True
    ntdll = _ntdll_resume()
    k32 = _kernel32()
    if not ntdll or not k32 or not pid:
        return False
    handle = None
    try:
        handle = k32.OpenProcess(PROCESS_SUSPEND_RESUME, False, int(pid))
        if not handle:
            return False
        return ntdll.NtResumeProcess(ctypes.c_void_p(handle)) == 0
    except Exception:
        return False
    finally:
        if handle:
            try:
                k32.CloseHandle(ctypes.c_void_p(handle))
            except Exception:
                pass


def terminate_job(job, exit_code=1):
    """Принудительно завершает все процессы Job Object."""
    k32 = _kernel32()
    if not k32 or not job:
        return False
    try:
        return bool(k32.TerminateJobObject(ctypes.c_void_p(job), int(exit_code)))
    except Exception:
        return False


def send_ctrl_break(pid):
    """Пытается отправить CTRL_BREAK в консоль процесса llama-server.

    Работает, если у текущего процесса нет собственной консоли (pythonw.exe/GUI).
    Если менеджер запущен из консоли, AttachConsole вернёт ошибку — тогда
    остаётся только принудительная остановка.
    """
    k32 = _kernel32()
    if not k32 or not pid:
        return False
    try:
        if not k32.AttachConsole(int(pid)):
            return False
        try:
            # Группа задаётся явно (pid = id группы, т.к. процесс создан с
            # CREATE_NEW_PROCESS_GROUP). Вещать на 0 нельзя: мы сами уже
            # присоединились к этой консоли и получили бы CTRL_BREAK.
            ok = bool(k32.GenerateConsoleCtrlEvent(CTRL_BREAK_EVENT, int(pid)))
            time.sleep(0.2)  # дать обработчику llama.cpp время среагировать
            return ok
        finally:
            k32.FreeConsole()
    except Exception:
        return False


def stop_process_gracefully(process, job=None, timeout=8.0, log=None):
    """Останавливает процесс: сначала штатно (CTRL_BREAK), потом принудительно.

    Возвращает словарь {"graceful": bool, "forced": bool, "exit_code": int|None}.
    Job Object закрывается в любом случае.
    """
    result = {"graceful": False, "forced": False, "exit_code": None}
    note = log if callable(log) else (lambda _text: None)

    if process is None:
        if job:
            close_handle(job)
        return result

    if process.poll() is not None:
        result["exit_code"] = process.returncode
        if job:
            close_handle(job)
        return result

    if send_ctrl_break(process.pid):
        note("Отправлен CTRL_BREAK: ждём штатного завершения сервера…")
        deadline = time.monotonic() + max(0.0, timeout)
        while time.monotonic() < deadline:
            if process.poll() is not None:
                break
            time.sleep(0.1)
        if process.poll() is not None:
            result["graceful"] = True
            result["exit_code"] = process.returncode
    else:
        note("Штатный сигнал недоступен (менеджер запущен из консоли) — остановка принудительная.")

    if process.poll() is None:
        result["forced"] = True
        if job:
            terminate_job(job)
        try:
            process.kill()
        except Exception:
            pass
        try:
            process.wait(timeout=5)
        except Exception:
            pass
        result["exit_code"] = process.returncode

    if job:
        close_handle(job)
    return result


def available_ram_gb():
    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]
    try:
        m = MEMORYSTATUSEX()
        m.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
        return m.ullAvailPhys / (1024 ** 3), m.ullTotalPhys / (1024 ** 3)
    except Exception:
        return None, None



def hidden_process_kwargs():
    """Safe subprocess defaults for GUI/no-console Windows applications.

    pythonw.exe and PyInstaller --windowed builds may have invalid standard
    handles. Inheriting STDIN can raise OSError [WinError 6] in subprocess.
    Always route child stdin to DEVNULL; callers choose stdout/stderr.
    """
    kwargs = {
        "stdin": subprocess.DEVNULL,
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0),
    }
    if IS_WINDOWS:
        if hasattr(subprocess, "STARTUPINFO"):
            startup = subprocess.STARTUPINFO()
            startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startup.wShowWindow = getattr(subprocess, "SW_HIDE", 0)
            kwargs["startupinfo"] = startup
    return kwargs


def find_nvidia_smi():
    """Find nvidia-smi through PATH and standard NVIDIA Windows driver locations."""
    command = shutil.which("nvidia-smi")
    if command:
        return command
    if IS_WINDOWS:
        candidates = [
            Path(os.environ.get("ProgramW6432", r"C:\Program Files")) / "NVIDIA Corporation" / "NVSMI" / "nvidia-smi.exe",
            Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "NVIDIA Corporation" / "NVSMI" / "nvidia-smi.exe",
            Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "nvidia-smi.exe",
        ]
        for candidate in candidates:
            if candidate.is_file():
                return str(candidate)
    return None


def gpu_process_vram_mb(pid):
    """Query per-process NVIDIA VRAM when nvidia-smi and driver support it."""
    command = find_nvidia_smi()
    if not command:
        return None
    try:
        result = subprocess.run(
            [command, "--query-compute-apps=pid,used_memory", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=3, **hidden_process_kwargs()
        )
        if result.returncode != 0:
            return None
        total = 0
        found = False
        for line in result.stdout.splitlines():
            parts = [part.strip() for part in line.split(",", 1)]
            if len(parts) == 2 and parts[0].isdigit() and int(parts[0]) == int(pid):
                try:
                    total += int(re.sub(r"[^0-9]", "", parts[1]))
                    found = True
                except ValueError:
                    pass
        return total if found else None
    except Exception:
        return None


def gpu_system_stats():
    """Return NVIDIA driver/device telemetry. None means unavailable or unsupported."""
    command = find_nvidia_smi()
    if not command:
        return None
    query = ("--query-gpu=name,driver_version,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw"
             " --format=csv,noheader,nounits")
    try:
        result = subprocess.run([command, *query.split()], capture_output=True, text=True,
                                timeout=3, **hidden_process_kwargs())
        if result.returncode != 0 or not result.stdout.strip():
            return None
        rows=[]
        for line in result.stdout.splitlines():
            parts=[x.strip() for x in line.split(",")]
            if len(parts) >= 7:
                rows.append({"name":parts[0], "driver":parts[1], "util":parts[2],
                             "used":parts[3], "total":parts[4], "temp":parts[5], "power":parts[6]})
        return rows or None
    except Exception:
        return None


def process_memory_stats(pid):
    """Return process working-set/private bytes without requiring psutil on Windows."""
    if IS_WINDOWS:
        k32 = _kernel32()
        if not k32:
            return None
        handle = k32.OpenProcess(PROCESS_QUERY_INFORMATION, False, int(pid))
        if not handle:
            return None
        try:
            psapi = ctypes.WinDLL("psapi", use_last_error=True)
            psapi.GetProcessMemoryInfo.restype = ctypes.c_int
            psapi.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(_PROCESS_MEMORY_COUNTERS_EX), ctypes.c_ulong]
            counters = _PROCESS_MEMORY_COUNTERS_EX()
            counters.cb = ctypes.sizeof(counters)
            if not psapi.GetProcessMemoryInfo(ctypes.c_void_p(handle), ctypes.byref(counters), counters.cb):
                return None
            return {"working_set": counters.WorkingSetSize, "private": counters.PrivateUsage,
                    "peak_working_set": counters.PeakWorkingSetSize}
        except Exception:
            return None
        finally:
            close_handle(handle)
    try:
        import psutil
        info = psutil.Process(int(pid)).memory_info()
        return {"working_set": info.rss, "private": getattr(info, "private", 0),
                "peak_working_set": getattr(info, "peak_wset", 0)}
    except Exception:
        return None


def _powershell_value(command, timeout=8):
    """Read Windows hardware metadata without requiring third-party packages."""
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
            capture_output=True, text=True, timeout=timeout, **hidden_process_kwargs()
        )
        if result.returncode == 0:
            return [line.strip() for line in result.stdout.splitlines() if line.strip()]
    except Exception:
        pass
    return []


_GPU_REGISTRY_SCRIPT = (
    "$base='HKLM:\\SYSTEM\\CurrentControlSet\\Control\\Class\\{4d36e968-e325-11ce-bfc1-08002be10318}'; "
    "Get-ChildItem $base -ErrorAction SilentlyContinue | ForEach-Object { "
    "$d=$_.GetValue('DriverDesc'); $q=$_.GetValue('HardwareInformation.qwMemorySize'); "
    "if($q -is [byte[]]){ $q=[BitConverter]::ToInt64($q,0) }; "
    "if($d -and $q){ $d + '|' + $q } }"
)


def _gpu_key(name):
    return re.sub(r"\s+", " ", str(name or "")).strip().lower()


def merge_gpu_vram(gpus, nvidia_rows=None, registry_rows=None):
    """Исправляет объём VRAM, который Win32_VideoController.AdapterRAM режет до 4 GiB.

    Приоритет: nvidia-smi (memory.total, MiB) → реестр драйвера (qwMemorySize, байты) →
    исходное значение. Список видеокарт и их порядок не меняются: встроенная графика
    не пропадает, даже когда есть NVIDIA.
    """
    for gpu in gpus:
        key = _gpu_key(gpu.get("name"))
        value = None
        for row in nvidia_rows or []:
            rkey = _gpu_key(row.get("name"))
            try:
                total_mib = float(row.get("total"))
            except (TypeError, ValueError):
                continue
            if rkey and (rkey == key or rkey in key or key in rkey):
                value = total_mib / 1024
                break
        if value is None:
            for desc, size_bytes in registry_rows or []:
                dkey = _gpu_key(desc)
                if dkey and (dkey == key or dkey in key or key in dkey):
                    value = size_bytes / 1024 ** 3
                    break
        if value:
            gpu["vram"] = round(value, 1)
    return gpus


def _parse_registry_gpu_rows(lines):
    rows = []
    for line in lines:
        name, sep, raw = line.rpartition("|")
        if sep and raw.strip().isdigit() and int(raw.strip()) > 0:
            rows.append((name, int(raw.strip())))
    return rows


def detect_system_hardware():
    cpu_lines = _powershell_value(
        "(Get-CimInstance Win32_Processor | Select-Object -First 1 -ExpandProperty Name)"
    )
    # Имена берём из Win32_VideoController; AdapterRAM — 32-битное поле, поэтому
    # реальный объём VRAM уточняется через nvidia-smi и реестр драйвера (merge_gpu_vram).
    gpu_lines = _powershell_value(
        "Get-CimInstance Win32_VideoController | ForEach-Object { "
        "$n=$_.Name; $m=$_.AdapterRAM; if($m -gt 0){$g=[math]::Round($m/1GB,1)}else{$g='?'}; "
        "$n + '|' + $g }"
    )
    free, total = available_ram_gb()
    gpus = []
    for line in gpu_lines:
        parts = line.rsplit("|", 1)
        if len(parts) == 2:
            try:
                vram = float(parts[1])
            except ValueError:
                vram = None
            gpus.append({"name": parts[0], "vram": vram})
        else:
            gpus.append({"name": line, "vram": None})
    try:
        registry_rows = _parse_registry_gpu_rows(_powershell_value(_GPU_REGISTRY_SCRIPT))
        merge_gpu_vram(gpus, gpu_system_stats(), registry_rows)
    except Exception:
        pass
    return {
        "cpu": cpu_lines[0] if cpu_lines else platform.processor() or "Не определён",
        "cores": os.cpu_count() or 1,
        "gpus": gpus,
        "ram_total": total,
        "ram_free": free,
    }


def assess_model_requirements(model_gb, context, ram_total, ram_free, max_vram=0,
                              gpu_offload=False, gpu_layers=0):
    """Return a conservative memory-fit verdict, never a speed prediction."""
    kv_gb = max(0.35, context / 4096 * 0.7)
    overhead_gb = 1.2
    cpu_required_gb = model_gb + kv_gb + overhead_gb
    gpu_enabled = bool(gpu_offload and gpu_layers > 0 and max_vram > 0)
    gpu_weight_gb = 0.0
    if gpu_enabled:
        # Keep a fixed VRAM reserve: layer and KV placement depend on llama.cpp/backend.
        gpu_weight_gb = min(model_gb, max(0.0, max_vram - kv_gb - 0.75))
    required_gb = max(0.0, model_gb - gpu_weight_gb) + kv_gb + overhead_gb

    if ram_total is None or ram_free is None:
        verdict = "неизвестно"
        message = "Нельзя оценить запуск: Windows не вернула данные о свободной RAM."
    elif ram_free >= required_gb + 2.0:
        verdict = "ok"
        message = "Запустится без заметных проблем по памяти."
    elif ram_free >= required_gb:
        verdict = "limited"
        message = "Запустится с ограничениями: запаса свободной RAM почти нет."
    elif ram_total >= required_gb:
        verdict = "limited"
        message = "Может запуститься с проблемами: сейчас свободной RAM недостаточно."
    else:
        verdict = "fail"
        message = "Не рекомендуется: доступной RAM недостаточно даже для расчётной загрузки."

    return {
        "verdict": verdict,
        "message": message,
        "kv_gb": kv_gb,
        "overhead_gb": overhead_gb,
        "cpu_required_gb": cpu_required_gb,
        "required_gb": required_gb,
        "gpu_weight_gb": gpu_weight_gb,
    }


def choose_next_actions(state):
    """Правилами выбирает главное действие и до двух дополнительных.

    Приоритет: нет модели → не GGUF → сервер запущен → нет llama-server →
    проблемы с памятью → устаревший экспорт → не экспортировано → готово к запуску.
    Каждый пункт несёт id и ключ каталога (key/reason_id); label/reason оставлены
    как русский текст для совместимости и тестов.
    """
    has_model = bool(state.get("has_model"))
    is_gguf = bool(state.get("is_gguf"))
    server_ready = bool(state.get("server_ready"))
    running = bool(state.get("server_running"))
    memory_issue = bool(state.get("memory_issue"))
    export_state = state.get("export_state") or "none"

    def action(ident, label, key=None):
        return {"id": ident, "label": label, "key": key or f"next.action.{ident}"}

    if not has_model:
        return {
            "primary": action("add_model", "Добавить модель"),
            "secondary": [action("hf_dialog", "Скачать с Hugging Face")],
            "reason": "Библиотека пуста: добавь файл модели или скачай GGUF с Hugging Face.",
            "reason_id": "next.reason.no_model",
        }
    if not is_gguf:
        return {
            "primary": action("add_model", "Выбрать GGUF-модель", "next.action.choose_gguf"),
            "secondary": [action("hf_dialog", "Скачать с Hugging Face")],
            "reason": "Для запуска через llama.cpp нужен файл .gguf — выбери его в библиотеке.",
            "reason_id": "next.reason.not_gguf",
        }
    if running:
        return {
            "primary": action("stop", "Остановить сервер"),
            "secondary": [action("chat", "Открыть чат"),
                          action("api_check", "Проверить API")],
            "reason": "Сервер отвечает на запросы: можно открыть встроенный чат или проверить API.",
            "reason_id": "next.reason.running",
        }
    if not server_ready:
        return {
            "primary": action("choose_server", "Указать llama-server"),
            "secondary": [action("download_llama", "Скачать llama.cpp")],
            "reason": "Модель выбрана, но путь к llama-server.exe ещё не задан.",
            "reason_id": "next.reason.no_server",
        }
    if memory_issue:
        return {
            "primary": action("recommendations", "Открыть рекомендации"),
            "secondary": [action("smaller_ctx", "Уменьшить контекст"),
                          action("gpu_setup", "Настроить GPU Offload")],
            "reason": "Оценка памяти неблагоприятная: сначала уменьши аппетит модели.",
            "reason_id": "next.reason.memory",
        }
    if export_state == "stale":
        return {
            "primary": action("re_export", "Обновить запись OpenCode"),
            "secondary": [action("unexport", "Удалить из OpenCode")],
            "reason": "Порт или контекст изменились — запись в OpenCode устарела.",
            "reason_id": "next.reason.stale",
        }
    if export_state == "none":
        return {
            "primary": action("export", "Экспортировать в OpenCode"),
            "secondary": [action("start", "Запустить модель")],
            "reason": "Модель ещё не добавлена в конфиг OpenCode.",
            "reason_id": "next.reason.export",
        }
    return {
        "primary": action("start", "Запустить модель"),
        "secondary": [action("gpu_check", "Проверить GPU"),
                      action("export", "Экспортировать в OpenCode")],
        "reason": "Модель и движок готовы — можно запускать сервер.",
        "reason_id": "next.reason.ready",
    }


def context_for_index(index):
    """Значение контекста по позиции ползунка; индекс зажимается в допустимый диапазон."""
    try:
        position = int(round(float(index)))
    except (TypeError, ValueError):
        position = 0
    position = max(0, min(len(DEFAULT_CONTEXTS) - 1, position))
    return DEFAULT_CONTEXTS[position]


def context_slider_needs_reset(current_maximum, new_maximum, current_value, target):
    """Whether a range refresh represents a real external slider change."""
    if int(new_maximum) != int(current_maximum):
        return True
    return target is not None and int(target) != int(round(float(current_value)))


def verdict_fits(verdict):
    """Запустится ли модель на этом ПК: «без проблем» и «с ограничениями» — да."""
    return verdict in ("ok", "limited")


def wheel_scroll_units(delta, step=60):
    """Превращает delta колеса Windows в смещение прокрутки в пикселях.

    delta > 0 — колесо вверх (содержимое идёт вверх), delta < 0 — вниз.
    Шаг 60 px соответствует одному щелчку колеса.
    """
    if not delta:
        return 0
    return -step if delta > 0 else step


def bytes_to_gb(num_bytes):
    """Переводит байты в гигабайты (1 GB = 1024**3)."""
    try:
        return float(num_bytes) / (1024 ** 3)
    except (TypeError, ValueError):
        return 0.0


def format_bytes(num_bytes):
    """Человекочитаемый размер: 812 MB, 4.21 GB."""
    value = float(num_bytes or 0)
    if value >= 1024 ** 3:
        return f"{value / 1024 ** 3:.2f} GB"
    if value >= 1024 ** 2:
        return f"{value / 1024 ** 2:.0f} MB"
    if value >= 1024:
        return f"{value / 1024:.0f} KB"
    return f"{int(value)} B"


def hf_api_url(repo_id):
    """URL метаданных репозитория Hugging Face."""
    repo = (repo_id or "").strip().strip("/")
    if not repo:
        raise ValueError("Не указан репозиторий Hugging Face")
    return f"https://huggingface.co/api/models/{repo}"



class HFTokenRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Never forward a Hugging Face bearer token to a different host/CDN."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is None:
            return None
        old_host = urllib.parse.urlsplit(req.full_url).netloc.lower()
        new_host = urllib.parse.urlsplit(newurl).netloc.lower()
        if old_host != new_host:
            redirected.remove_header("Authorization")
            redirected.unredirected_hdrs.pop("Authorization", None)
            redirected.unredirected_hdrs.pop("authorization", None)
        return redirected


def pick_llama_cpp_asset(assets, gpu_name=""):
    """Windows x64 ZIP со сборкой llama.cpp, подходящий под видеокарту (пусто — CPU)."""
    def is_build(asset):
        name = (asset.get("name") or "").lower()
        return (name.endswith(".zip") and "win" in name and "x64" in name
                and "cudart" not in name)  # cudart — только DLL, llama-server.exe внутри нет

    candidates = [asset for asset in (assets or []) if is_build(asset)]
    if not candidates:
        return None
    gpu_name = (gpu_name or "").lower()
    if "nvidia" in gpu_name:
        order = ("cuda", "vulkan", "rocm", "sycl", "openvino", "cpu")
    elif gpu_name:
        # Vulkan работает и на AMD, и на Intel, и на старых NVIDIA.
        order = ("vulkan", "cuda", "rocm", "sycl", "openvino", "cpu")
    else:
        order = ("cpu", "vulkan", "cuda", "rocm", "sycl", "openvino")

    def rank(asset):
        name = (asset.get("name") or "").lower()
        device = next((i for i, token in enumerate(order) if token in name), len(order))
        # CUDA 12.4 запускается и на старых, и на новых драйверах; 13.4 требует свежих.
        return (device, 0 if "12.4" in name else 1)

    return min(candidates, key=rank)


def stream_download_resumable(url, destination, *, headers=None, timeout=60,
                              cancel_event=None, progress=None):
    """Download to .part with HTTP Range resume and atomic final rename."""
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = Path(str(destination) + ".part")
    existing = temp.stat().st_size if temp.exists() else 0
    request_headers = {"User-Agent": APP_USER_AGENT, "Accept": "*/*"}
    request_headers.update(headers or {})
    if existing:
        request_headers["Range"] = f"bytes={existing}-"
    request = urllib.request.Request(url, headers=request_headers)
    opener = urllib.request.build_opener(HFTokenRedirectHandler(), https_handler())
    try:
        response_cm = opener.open(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        if exc.code == 416 and existing:
            # Range вне файла: .part полный или устарел — начинаем с нуля.
            temp.unlink(missing_ok=True)
            return stream_download_resumable(url, destination, headers=headers, timeout=timeout,
                                             cancel_event=cancel_event, progress=progress)
        raise
    with response_cm as response:
        status = getattr(response, "status", response.getcode())
        content_length = int(response.headers.get("Content-Length") or 0)
        content_range = response.headers.get("Content-Range", "")
        append = bool(existing and status == 206)
        if append:
            # 206 без точного Content-Range нельзя безопасно дописывать к .part.
            match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", content_range)
            if not match or int(match.group(1)) != existing:
                raise IOError("Некорректный Content-Range при докачке; файл .part сохранён.")
            total = int(match.group(3))
        else:
            if existing:
                existing = 0
            total = content_length
        if total:
            free_bytes = shutil.disk_usage(destination.parent).free
            remaining = max(0, total - existing)
            if free_bytes < remaining + 64 * 1024 * 1024:
                raise OSError(f"Недостаточно места на диске: нужно ещё примерно {remaining/1024**3:.2f} GB.")
        mode = "ab" if append else "wb"
        done = existing
        with open(temp, mode) as output:
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    raise DownloadCancelled("Загрузка остановлена. Частичный файл сохранён для продолжения.")
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                output.write(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)
            output.flush()
            os.fsync(output.fileno())
    # http.client на обрыве отдаёт b"" без исключения: сверяем факт с ожидаемым размером.
    # shortcut: chunked-ответ без Content-Length не проверить, обновить при переходе на внешний загрузчик.
    if total and done < total:
        raise IOError(
            f"Загрузка прервана: получено {done} из {total} байт. "
            "Файл .part сохранён — повтори загрузку, она продолжится с места обрыва."
        )
    if cancel_event is not None and cancel_event.is_set():
        raise DownloadCancelled("Загрузка остановлена. Частичный файл сохранён для продолжения.")
    temp.replace(destination)
    return destination



def hf_resolve_url(repo_id, filename, revision="main"):
    """Прямая ссылка на файл внутри репозитория Hugging Face."""
    repo = (repo_id or "").strip().strip("/")
    name = urllib.parse.quote((filename or "").strip().lstrip("/"), safe="/")
    if not repo or not name:
        raise ValueError("Нужны репозиторий и имя файла")
    return f"https://huggingface.co/{repo}/resolve/{urllib.parse.quote(revision, safe='')}/{name}"


def gguf_files(payload):
    """Список .gguf-файлов с размерами из ответа HF API (?blobs=true)."""
    siblings = (payload or {}).get("siblings") or []
    files = []
    for item in siblings:
        if not isinstance(item, dict):
            continue
        name = item.get("rfilename")
        if not name or not name.lower().endswith(".gguf") or is_mmproj_gguf(name):
            continue
        size = item.get("size")
        if not size and isinstance(item.get("lfs"), dict):
            size = item["lfs"].get("size")
        files.append({"name": name, "size": int(size or 0)})
    files.sort(key=lambda entry: entry["name"])
    return files


def is_mmproj_gguf(path):
    """Это CLIP-проектор, который нельзя запускать как основную LLM через -m."""
    # shortcut: определяет стандартное имя mmproj; при поддержке vision-моделей читать метаданные GGUF.
    name = Path(path).name.lower()
    return bool(re.search(r"(?:^|[-_.])mmproj(?:[-_.]|$)", name))


def gguf_siblings(payload):
    """Вытаскивает имена .gguf-файлов из ответа Hugging Face API."""
    siblings = (payload or {}).get("siblings") or []
    names = []
    for item in siblings:
        if isinstance(item, dict):
            name = item.get("rfilename")
            if name and name.lower().endswith(".gguf") and not is_mmproj_gguf(name):
                names.append(name)
    return sorted(names)


def matches_model_query(name, query):
    """Поиск по имени модели без учёта регистра; пустой запрос пропускает всё."""
    text = (query or "").strip().lower()
    return not text or text in (name or "").lower()


HF_SECTIONS = {
    "Популярные GGUF": {"sort": "downloads", "query": False},
    "Новые GGUF": {"sort": "createdAt", "query": False},
    "Поиск по названию": {"sort": "downloads", "query": True},
}


def hf_search_url(query="", sort="downloads", limit=40):
    """URL поиска GGUF-репозиториев на Hugging Face."""
    params = [("filter", "gguf"), ("sort", sort), ("direction", "-1"),
              ("limit", str(int(limit)))]
    if (query or "").strip():
        params.insert(0, ("search", query.strip()))
    return "https://huggingface.co/api/models?" + urllib.parse.urlencode(params)


def hf_repo_list(payload):
    """Превращает ответ поиска HF в список репозиториев, отсортированный по скачиваниям."""
    repos = []
    for item in payload or []:
        if isinstance(item, dict) and item.get("id"):
            repos.append({
                "id": item["id"],
                "downloads": item.get("downloads") or 0,
                "likes": item.get("likes") or 0,
            })
    repos.sort(key=lambda repo: repo["downloads"], reverse=True)
    return repos


class DownloadCancelled(Exception):
    """Raised when the user cancels an engine download or installation."""



class ContextSlider(tk.Canvas):
    """Animated token-context slider with a luminous thumb and discrete stops."""
    def __init__(self, parent, *, from_=0, to=6, command=None, bg="#272942",
                 accent="#a653f5", accent_hover="#bd79ff", track="#1f2134", **kwargs):
        super().__init__(parent, width=280, height=42, bg=bg, bd=0,
                         highlightthickness=0, cursor="hand2", **kwargs)
        self.minimum, self.maximum = int(from_), int(to)
        self.stops = list(DEFAULT_CONTEXTS)   # значения стопов: подписи концов идут от них
        self.command = command
        self.bg_color, self.accent = bg, accent
        self.accent_hover, self.track = accent_hover, track
        self.value = 0.0
        self.display_value = 0.0
        self._anim_job = None
        self._visual_job = None
        self._hover = False
        self._hover_amount = 0.0
        self._dragging = False
        self.bind("<Configure>", lambda _e: self.redraw())
        self.bind("<Button-1>", self._press)
        self.bind("<B1-Motion>", self._drag)
        self.bind("<ButtonRelease-1>", self._release)
        self.bind("<Enter>", self._enter)
        self.bind("<Leave>", self._leave)
        self.bind("<Left>", lambda _e: self._step(-1))
        self.bind("<Right>", lambda _e: self._step(1))
        self.bind("<Home>", lambda _e: self.set(self.minimum))
        self.bind("<End>", lambda _e: self.set(self.maximum))
        self.configure(takefocus=1)
        self.redraw()

    def _bounds(self):
        w = max(80, self.winfo_width())
        return 13.0, w - 13.0

    def _stop_label(self, index):
        try:
            value = int(self.stops[index])
        except (IndexError, TypeError, ValueError):
            return ""
        return f"{value // 1024}K" if value >= 1024 else str(value)

    def _x(self, value):
        left, right = self._bounds()
        span = max(1, self.maximum - self.minimum)
        return left + (right-left) * (float(value)-self.minimum) / span

    def _value_at(self, x):
        left, right = self._bounds()
        ratio = max(0.0, min(1.0, (x-left)/max(1.0, right-left)))
        return round(self.minimum + ratio*(self.maximum-self.minimum))

    def get(self):
        return int(round(self.value))

    def set_range(self, maximum, value=None):
        new_maximum = max(self.minimum, int(maximum))
        target = None
        if value is not None:
            target = max(self.minimum, min(new_maximum, int(value)))

        # update_estimate() refreshes the available context range while the user
        # drags the slider. Do not reset the thumb on every mouse event when the
        # range and selected stop have not actually changed: doing so caused the
        # thumb to visibly jump back and forth during dragging.
        if not context_slider_needs_reset(
                self.maximum, new_maximum, self.get(), target):
            return

        self.maximum = new_maximum
        if target is not None:
            self.value = float(target)
            self.display_value = float(target)
        else:
            self.value = max(float(self.minimum), min(float(self.maximum), self.value))
            self.display_value = max(float(self.minimum), min(float(self.maximum), self.display_value))

        # A genuine external range/value change supersedes any pending animation.
        if self._anim_job:
            try:
                self.after_cancel(self._anim_job)
            except Exception:
                pass
            self._anim_job = None
        self.redraw()

    def set(self, value):
        value = max(self.minimum, min(self.maximum, int(round(float(value)))))
        if value == self.get():
            self.redraw()
            return
        self.value = float(value)
        self._animate_to(float(value))
        if self.command:
            self.command(str(value))

    def _animate_to(self, target):
        """Time-based, refresh-rate-independent easing for smooth thumb movement."""
        if self._anim_job:
            try:
                self.after_cancel(self._anim_job)
            except Exception:
                pass
        start = float(self.display_value)
        distance = abs(target - start)
        if distance < 0.001:
            self.display_value = target
            self.redraw()
            self._anim_job = None
            return
        # A little more travel time for long jumps, without making the control sluggish.
        duration = min(0.34, 0.18 + distance * 0.018)
        started = time.perf_counter()

        def frame():
            elapsed = time.perf_counter() - started
            t = min(1.0, elapsed / duration)
            # Quintic smoothstep: zero velocity at both ends, no abrupt stop.
            eased = t * t * t * (t * (t * 6 - 15) + 10)
            self.display_value = start + (target - start) * eased
            self.redraw()
            if t < 1.0:
                self._anim_job = self.after(16, frame)
            else:
                self.display_value = target
                self._anim_job = None
                self.redraw()
        frame()

    def _animate_hover(self, target):
        if self._visual_job:
            try:
                self.after_cancel(self._visual_job)
            except Exception:
                pass
        start = self._hover_amount
        started = time.perf_counter()
        duration = 0.16

        def frame():
            t = min(1.0, (time.perf_counter() - started) / duration)
            eased = t * t * (3 - 2 * t)
            self._hover_amount = start + (target - start) * eased
            self.redraw()
            if t < 1.0:
                self._visual_job = self.after(16, frame)
            else:
                self._hover_amount = target
                self._visual_job = None
                self.redraw()
        frame()

    def _update_from_event(self, event):
        value = self._value_at(event.x)
        if value != self.get():
            self.value = float(value)
            self.display_value = float(value)
            self.redraw()
            if self.command:
                self.command(str(value))

    def _press(self, event):
        self.focus_set()
        self._dragging = True
        self._update_from_event(event)

    def _drag(self, event):
        if self._dragging:
            self._update_from_event(event)

    def _release(self, _event):
        self._dragging = False
        self.redraw()

    def _step(self, amount):
        self.set(self.get()+amount)
        return "break"

    def _enter(self, _event):
        self._hover = True
        self._animate_hover(1.0)

    def _leave(self, _event):
        self._hover = False
        if not self._dragging:
            self._animate_hover(0.0)

    def redraw(self):
        if not self.winfo_exists(): return
        self.delete("all")
        left, right = self._bounds()
        cy = 17
        x = self._x(self.display_value)
        # Track and progress rail.
        self.create_line(left, cy, right, cy, fill=self.track, width=7, capstyle="round")
        if x > left + 1:
            # Gradient rail inspired by the supplied CSS slider, adapted to Tk Canvas.
            gradient = ["#6650c8", self.accent, self.accent_hover]
            segments = min(100, max(2, int(x-left)))
            for segment in range(segments):
                t = segment / max(1, segments-1)
                if t < .5:
                    color = _mix_color(gradient[0], gradient[1], t*2)
                else:
                    color = _mix_color(gradient[1], gradient[2], (t-.5)*2)
                x0 = left + (x-left) * segment / segments
                x1 = left + (x-left) * (segment+1) / segments
                self.create_line(x0, cy, x1+1, cy, fill=color, width=7)
        # Discrete context stops.
        for i in range(self.minimum, self.maximum+1):
            px = self._x(i)
            active = i <= self.get()
            color = self.bg_color if active else "#77758f"
            self.create_oval(px-2.2, cy-2.2, px+2.2, cy+2.2,
                             fill=color if active else self.track, outline="")
            if i in (self.minimum, self.maximum):
                label = "2K" if i == self.minimum else self._stop_label(i)
                self.create_text(px, 35, text=label, fill="#8f8da9",
                                 font=("Segoe UI", 7))
        # Layered halo and subtly expanding thumb, animated on hover/focus.
        hover = max(self._hover_amount, 0.65 if self._dragging else 0.0)
        halo_color = _mix_color(self.accent, self.accent_hover, hover)
        glow_radius = 10.0 + 3.0 * hover
        ring_radius = 7.0 + 1.2 * hover
        self.create_oval(x-glow_radius, cy-glow_radius, x+glow_radius, cy+glow_radius,
                         fill=self.track, outline="")
        self.create_oval(x-ring_radius, cy-ring_radius, x+ring_radius, cy+ring_radius,
                         fill=halo_color, outline="")
        self.create_oval(x-4, cy-4, x+4, cy+4, fill="#fbf8ff", outline="")

class NeumorphicSwitch(tk.Canvas):
    """Animated pill switch with a soft violet gradient and spring-like thumb."""
    def __init__(self, parent, variable, command=None, **kwargs):
        self.variable = variable
        self.command = command
        self.track_on = "#a653f5"
        self.track_off = "#1b1e32"
        self.thumb = "#f4f1fb"
        self._thumb_x = 14.0 if not bool(variable.get()) else 38.0
        self._anim_job = None
        super().__init__(parent, width=52, height=26, bg=kwargs.pop("bg", "#24263d"),
                         bd=0, highlightthickness=0, cursor="hand2", takefocus=1, **kwargs)
        self._focused = False
        self.bind("<Button-1>", self._toggle)
        self.bind("<Enter>", lambda _e: self.configure(cursor="hand2"))
        self.bind("<FocusIn>", self._focus_in)
        self.bind("<FocusOut>", self._focus_out)
        self.bind("<space>", self._toggle)
        self.bind("<Return>", self._toggle)
        self.variable.trace_add("write", lambda *_: self._animate_to(38.0 if self.variable.get() else 14.0))
        self.redraw()

    def _focus_in(self, _event=None):
        self._focused = True
        self.redraw()

    def _focus_out(self, _event=None):
        self._focused = False
        self.redraw()

    def _toggle(self, _event=None):
        self.variable.set(not bool(self.variable.get()))
        if self.command:
            self.command()
        return "break"

    def _animate_to(self, target):
        if self._anim_job:
            try: self.after_cancel(self._anim_job)
            except Exception: pass
        start = self._thumb_x
        began = time.perf_counter()
        duration = 0.20
        def frame():
            t = min(1.0, (time.perf_counter() - began) / duration)
            eased = 1 - (1-t)**3
            self._thumb_x = start + (target-start)*eased
            self.redraw()
            if t < 1:
                self._anim_job = self.after(16, frame)
            else:
                self._thumb_x = target
                self._anim_job = None
                self.redraw()
        frame()

    def redraw(self):
        self.delete("all")
        active = bool(self.variable.get())
        track = self.track_on if active else self.track_off
        if self._focused:
            self.create_oval(0, 0, 52, 26, fill="", outline="#bd79ff", width=2)
        self.create_oval(3, 3, 49, 23, fill="#17192b", outline="")
        self.create_oval(2, 2, 22, 22, fill=track, outline="")
        self.create_oval(30, 2, 50, 22, fill=track, outline="")
        self.create_rectangle(12, 2, 40, 22, fill=track, outline="")
        if active:
            self.create_line(13, 5, 38, 5, fill="#d9b7ff", width=1)
        x = self._thumb_x
        self.create_oval(x-9, 4, x+9, 22, fill="#17192b", outline="")
        self.create_oval(x-7, 4, x+7, 18, fill=self.thumb, outline="#ffffff")


def _mix_color(color_a, color_b, ratio):
    """Смешивает два цвета вида #rrggbb."""
    a = [int(color_a[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(color_b[i:i + 2], 16) for i in (1, 3, 5)]
    mixed = [int(round(a[i] + (b[i] - a[i]) * ratio)) for i in range(3)]
    return "#%02x%02x%02x" % tuple(max(0, min(255, value)) for value in mixed)



def fade_in(window, duration=180, steps=10):
    """Плавное появление окна через -alpha (без эффекта там, где прозрачность не поддерживается)."""
    try:
        window.attributes("-alpha", 0.0)
    except (tk.TclError, AttributeError):
        return

    def step(index=1):
        try:
            if not window.winfo_exists():
                return
            window.attributes("-alpha", min(1.0, index / steps))
            if index < steps:
                window.after(max(8, duration // steps), step, index + 1)
        except tk.TclError:
            pass

    try:
        window.after(12, step)
    except tk.TclError:
        pass


def animate_place_y(widget, start, end, duration=220, done=None, steps=14):
    """Плавно двигает виджет, размещённый через place(), по вертикали (ease-out)."""
    def step(index=0):
        try:
            if not widget.winfo_exists():
                return
            t = index / steps
            eased = 1 - (1 - t) ** 3
            widget.place_configure(y=int(round(start + (end - start) * eased)))
            if index < steps:
                widget.after(max(8, duration // steps), step, index + 1)
            elif done:
                done()
        except tk.TclError:
            pass
    step(0)


class ToolTip:
    """Compact dark-violet tooltip for controls with non-obvious behavior."""
    def __init__(self, widget, text, colors=None):
        self.widget = widget
        self.text = text
        self.colors = colors or {}
        self.window = None
        self.job = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self.hide, add="+")
        widget.bind("<ButtonPress>", self.hide, add="+")

    def set_text(self, text):
        """Новый текст подсказки (используется при смене языка)."""
        self.text = str(text)
        if self.window:
            self.hide()

    def _schedule(self, _event=None):
        self.hide()
        try:
            self.job = self.widget.after(520, self.show)
        except tk.TclError:
            self.job = None

    def show(self):
        self.job = None
        if self.window or not self.widget.winfo_exists():
            return
        c = self.colors
        self.window = tk.Toplevel(self.widget)
        self.window.wm_overrideredirect(True)
        self.window.attributes("-topmost", True)
        page = c.get("page", "#171522")
        fill = c.get("card_alt", "#2d2f4a")
        panel = RoundedPanel(self.window, page=page, fill=fill,
                             shadow=c.get("shadow", "#09080f"),
                             radius=13, padding=10, lateral=5)
        panel.pack()
        tk.Label(panel.inner, text=self.text, bg=fill,
                 fg=c.get("text", "#f2effa"), justify="left",
                 wraplength=290, font=("Segoe UI", 9, "bold")).pack(anchor="w")
        x = self.widget.winfo_rootx() + 8
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 8
        self.window.geometry(f"+{x}+{y}")

    def hide(self, _event=None):
        if self.job:
            try: self.widget.after_cancel(self.job)
            except Exception: pass
            self.job = None
        if self.window:
            try: self.window.destroy()
            except tk.TclError: pass
            self.window = None


class RadarWidget(tk.Canvas):
    """Small, low-distraction radar motif for the hardware-analysis panel."""
    def __init__(self, parent, bg, accent="#a653f5", size=62):
        super().__init__(parent, width=size, height=size, bg=bg,
                         highlightthickness=0, bd=0)
        self.size = size
        self.accent = accent
        self.angle = 0.0
        self._job = None
        self.bind("<Destroy>", self._on_destroy, add="+")
        self._draw()

    def _on_destroy(self, event):
        if event.widget is self and self._job:
            try: self.after_cancel(self._job)
            except Exception: pass
            self._job = None

    def _draw(self):
        self.delete("all")
        c = self.size / 2
        radius = self.size * 0.40
        for r in (radius, radius * .66, radius * .33):
            self.create_oval(c-r, c-r, c+r, c+r,
                             outline="#454064", width=1)
        self.create_line(c-radius, c, c+radius, c, fill="#343451")
        self.create_line(c, c-radius, c, c+radius, fill="#343451")
        import math
        end_x = c + radius * math.cos(self.angle)
        end_y = c + radius * math.sin(self.angle)
        self.create_line(c, c, end_x, end_y, fill=self.accent, width=2)
        self.create_oval(c-3, c-3, c+3, c+3, fill="#d8b8ff", outline="")
        self.angle = (self.angle + .075) % (2 * math.pi)
        self._job = self.after(35, self._draw)


def rounded_points(x1, y1, x2, y2, radius):
    """Точки сглаженного прямоугольника со скруглёнными углами."""
    r = max(0.0, min(radius, (x2 - x1) / 2, (y2 - y1) / 2))
    return [
        x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
        x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
        x1, y2, x1, y2 - r, x1, y1 + r, x1, y1,
    ]


class GradientLine(tk.Canvas):
    """Тонкая градиентная акцентная линия (украшение верхнего края окна)."""

    def __init__(self, parent, accent, page, height=2):
        super().__init__(parent, height=height, bg=page, bd=0, highlightthickness=0, takefocus=0)
        self._accent = accent
        self._page = page
        self.bind("<Configure>", lambda _e: self._draw())

    def _draw(self):
        width = max(2, self.winfo_width())
        height = max(1, int(float(self.cget("height"))))
        self.delete("all")
        steps = min(width, 160)
        for i in range(steps):
            t = i / max(1, steps - 1)
            # акцент в начале, к правому краю растворяется в цвет фона
            color = _mix_color(self._accent, self._page, min(1.0, t * 1.15) ** 1.6)
            x1 = int(width * i / steps)
            x2 = int(width * (i + 1) / steps) + 1
            self.create_rectangle(x1, 0, x2, height, fill=color, outline="")


def fit_toplevel_to_content(window, margin=70):
    """Увеличивает окно до размера содержимого (но не больше экрана).

    Шрифты и масштаб Windows различаются: фиксированная геометрия обрезала
    нижние строки на части экранов.
    """
    try:
        if not window.winfo_exists():
            return
        window.update_idletasks()
        screen_w, screen_h = window.winfo_screenwidth(), window.winfo_screenheight()
        req_w, req_h = window.winfo_reqwidth(), window.winfo_reqheight()
        cur_w, cur_h = window.winfo_width(), window.winfo_height()
        new_w = min(max(cur_w, req_w), screen_w - margin)
        new_h = min(max(cur_h, req_h), screen_h - margin - 40)
        if (new_w, new_h) != (cur_w, cur_h):
            x = max(0, min(window.winfo_x(), screen_w - new_w))
            y = max(0, min(window.winfo_y(), screen_h - new_h - 40))
            window.geometry(f"{new_w}x{new_h}+{x}+{y}")
    except tk.TclError:
        pass


class RoundIconButton(tk.Canvas):
    """Скруглённая кнопка-иконка для шапки окна."""

    def __init__(self, parent, text, command, *, size=30, bg, fg,
                 hover_bg, hover_fg="#ffffff", font=None, glyph=None):
        super().__init__(parent, width=size, height=size, bg=bg,
                         highlightthickness=0, bd=0, cursor="hand2", takefocus=0)
        self._size = size
        self._glyph = glyph
        self._bg = bg
        self._fg = fg
        self._hover_bg = hover_bg
        self._hover_fg = hover_fg
        self._text = text
        self._command = command
        self._font = font
        self._t = 0.0
        self._target = 0.0
        self._job = None
        self.bind("<Enter>", lambda _e: self._go(1.0))
        self.bind("<Leave>", lambda _e: self._go(0.0))
        self.bind("<Button-1>", lambda _e: self._command())
        self._draw()

    def _go(self, target):
        self._target = target
        if self._job is None:
            self._job = self.after(16, self._tick)

    def _tick(self):
        self._job = None
        try:
            if not self.winfo_exists():
                return
            self._t += (self._target - self._t) * 0.34
            if abs(self._target - self._t) < 0.02:
                self._t = self._target
            self._draw()
            if self._t != self._target:
                self._job = self.after(16, self._tick)
        except tk.TclError:
            pass

    def set_glyph(self, glyph):
        self._glyph = glyph
        self._draw()

    def destroy(self):
        if self._job is not None:
            try:
                self.after_cancel(self._job)
            except Exception:
                pass
            self._job = None
        super().destroy()

    def _draw(self, hover=None):
        t = self._t if hover is None else (1.0 if hover else 0.0)
        self.delete("all")
        size = self._size
        pad = 3
        self.create_polygon(rounded_points(pad, pad, size - pad, size - pad, 9),
                            smooth=True, outline="",
                            fill=_mix_color(self._bg, self._hover_bg, t))
        color = _mix_color(self._fg, self._hover_fg, t)
        if self._glyph:
            self._draw_glyph(color)
            return
        self.create_text(size / 2, size / 2, text=self._text,
                         fill=color,
                         font=self._font or ("Inter", 12))

    def _draw_glyph(self, color):
        """Символы рисуются линиями: текстовые глифы «—» и «×» разной ширины."""
        mid, arm = self._size / 2, 4.5
        stroke = {"fill": color, "width": 1.6, "capstyle": "round"}
        if self._glyph == "minimize":
            self.create_line(mid - arm, mid, mid + arm, mid, **stroke)
        elif self._glyph == "maximize":
            self.create_rectangle(mid - arm, mid - arm, mid + arm, mid + arm,
                                  outline=color, width=1.6)
        elif self._glyph == "close":
            self.create_line(mid - arm, mid - arm, mid + arm, mid + arm, **stroke)
            self.create_line(mid - arm, mid + arm, mid + arm, mid - arm, **stroke)
        elif self._glyph == "restore":
            self.create_rectangle(mid - arm, mid - arm + 1.5, mid + arm - 1.5, mid + arm,
                                  outline=color, width=1.6)
            self.create_line(mid - arm + 1.5, mid - arm, mid + arm,
                             mid - arm, **stroke)
            self.create_line(mid + arm, mid - arm, mid + arm, mid + arm - 1.5, **stroke)


class RoundedButton(tk.Canvas):
    """Кнопка со скруглёнными углами и мягкой 3D-тенью."""

    RADIUS = 14
    PAD_X = 14
    PAD_Y = 6

    def __init__(self, parent, text, command, colors, *, variant="default",
                 state="normal", font=None, bg=None, width=None, pad=None):
        self.colors = colors
        self.variant = variant
        self.text = text
        self.command = command
        self.state = state
        self.font_spec = font or ("Inter", 9, "bold")
        self._parent_bg = bg or colors["card"]
        self._btn_pad = pad or (self.PAD_X, self.PAD_Y)
        self._btn_hover = False
        self._btn_pressed = False
        self._btn_focused = False
        self._btn_anim = 0.0          # 0..1 — плавный прогресс наведения
        self._btn_anim_target = 0.0
        self._btn_anim_job = None
        font_object = tkfont.Font(font=self.font_spec)
        self._btn_w = width or font_object.measure(text) + self._btn_pad[0] * 2
        self._btn_h = font_object.metrics("linespace") + self._btn_pad[1] * 2
        super().__init__(parent, width=self._btn_w, height=self._btn_h, bg=self._parent_bg,
                         highlightthickness=0, bd=0, cursor="hand2", takefocus=1)
        self.bind("<Enter>", self._btn_enter)
        self.bind("<Leave>", self._btn_leave)
        self.bind("<ButtonPress-1>", self._btn_press)
        self.bind("<ButtonRelease-1>", self._btn_release)
        self.bind("<Return>", lambda _e: self._btn_invoke())
        self.bind("<space>", self._btn_key_press)
        self.bind("<KeyRelease-space>", self._btn_key_release)
        self.bind("<FocusIn>", self._btn_focus_in)
        self.bind("<FocusOut>", self._btn_focus_out)
        self._btn_draw()

    # --- состояние ---------------------------------------------------------
    def configure(self, cnf=None, **kwargs):
        if "state" in kwargs:
            self.state = kwargs.pop("state")
            self._btn_draw()
        if "text" in kwargs:
            self.text = kwargs.pop("text")
            self._btn_draw()
        if cnf is None and not kwargs:
            return super().configure()
        return super().configure(cnf, **kwargs)

    def cget(self, key):
        if key == "state":
            return self.state
        if key == "text":
            return self.text
        return super().cget(key)

    def _btn_invoke(self):
        if self.state != "disabled" and callable(self.command):
            self.command()

    def _btn_key_press(self, _event=None):
        if self.state != "disabled":
            self._btn_pressed = True
            self._btn_draw()
        return "break"

    def _btn_key_release(self, _event=None):
        if self.state != "disabled" and self._btn_pressed:
            self._btn_pressed = False
            self._btn_draw()
            self._btn_invoke()
        return "break"

    def _btn_focus_in(self, _event=None):
        self._btn_focused = True
        self._btn_draw()

    def _btn_focus_out(self, _event=None):
        self._btn_focused = False
        self._btn_pressed = False
        self._btn_draw()

    # --- события -----------------------------------------------------------
    def _btn_animate(self, target):
        self._btn_anim_target = target
        if self._btn_anim_job is None:
            self._btn_anim_job = self.after(16, self._btn_tick)

    def _btn_tick(self):
        self._btn_anim_job = None
        try:
            if not self.winfo_exists():
                return
            self._btn_anim += (self._btn_anim_target - self._btn_anim) * 0.34
            if abs(self._btn_anim_target - self._btn_anim) < 0.02:
                self._btn_anim = self._btn_anim_target
            self._btn_draw()
            if self._btn_anim != self._btn_anim_target:
                self._btn_anim_job = self.after(16, self._btn_tick)
        except tk.TclError:
            pass

    def destroy(self):
        if self._btn_anim_job is not None:
            try:
                self.after_cancel(self._btn_anim_job)
            except Exception:
                pass
            self._btn_anim_job = None
        super().destroy()

    def _btn_enter(self, _event=None):
        self._btn_hover = True
        self._btn_animate(1.0)

    def _btn_leave(self, _event=None):
        self._btn_hover = False
        self._btn_pressed = False
        self._btn_animate(0.0)
        self._btn_draw()

    def _btn_press(self, _event=None):
        if self.state == "disabled":
            return
        self._btn_pressed = True
        self._btn_draw()

    def _btn_release(self, _event=None):
        if self.state == "disabled":
            return
        was_pressed = self._btn_pressed
        self._btn_pressed = False
        self._btn_draw()
        if was_pressed:
            self._btn_invoke()

    # --- отрисовка ---------------------------------------------------------
    def _palette(self):
        c = self.colors
        return {
            "default": (c["card"], c["card_alt"], c["field"], c["text"]),
            "accent": (c["accent_deep"], c["accent_hover"], c["accent_pressed"], "#ffffff"),
            "export": ("#6b4198", "#8050ae", "#51306f", "#ffffff"),
            "danger": ("#4d3850", "#62445f", "#3b2a3b", "#f4e8f0"),
        }.get(self.variant, (c["card"], c["card_alt"], c["field"], c["text"]))

    def _btn_draw(self):
        self.delete("all")
        c = self.colors
        fill, hover_fill, press_fill, fg = self._palette()
        if self.state == "disabled":
            fill = _mix_color(fill, c["card"], 0.55)
            fg = _mix_color(fg, c["card"], 0.45)
            outline = ""
        else:
            if self._btn_pressed:
                fill = press_fill
            elif self._btn_anim > 0.01:
                fill = _mix_color(fill, hover_fill, self._btn_anim)
            outline = _mix_color(fill, "#ffffff", 0.14)
        # Keyboard focus is visible without competing with the hover state.
        if self._btn_focused and self.state != "disabled":
            self.create_polygon(rounded_points(0, 0, self._btn_w, self._btn_h, self.RADIUS+2),
                                smooth=True, fill="", outline=_mix_color(c["accent_hover"], c["card"], .18), width=2)
        # Soft layered halo on hover, then the original raised surface.
        if self._btn_anim > 0.08 and self.state != "disabled":
            glow = c["accent_hover"] if self.variant == "accent" else c["accent"]
            t = self._btn_anim
            self.create_polygon(rounded_points(0, 0, self._btn_w, self._btn_h, self.RADIUS+2),
                                smooth=True, fill="", outline=_mix_color(glow, c["card"], 1 - .65 * t), width=3)
            self.create_polygon(rounded_points(2, 2, self._btn_w-2, self._btn_h-2, self.RADIUS),
                                smooth=True, fill="", outline=_mix_color(glow, c["card"], 1 - .45 * t), width=2)
        # мягкая тень-подложка создаёт объём
        self.create_polygon(rounded_points(1, 3, self._btn_w - 1, self._btn_h - 1, self.RADIUS),
                            smooth=True, outline="",
                            fill=_mix_color(fill, c["shadow"], 0.6))
        self.create_polygon(rounded_points(1, 1, self._btn_w - 1, self._btn_h - 2, self.RADIUS),
                            smooth=True, fill=fill, outline=outline)
        self.create_text(self._btn_w / 2, self._btn_h / 2 - 0.5, text=self.text, fill=fg,
                         font=self.font_spec)


class RoundedPanel(tk.Canvas):
    """Скруглённая панель с мягкой 3D-тенью и фаской; содержимое — во внутреннем фрейме."""

    def __init__(self, parent, *, page, fill, shadow, radius=18, padding=14,
                 lateral=12, stretch=False, rail=False):
        super().__init__(parent, bg=page, highlightthickness=0, bd=0, takefocus=0)
        self.rail = rail
        if stretch:
            self.configure(height=40)
        self.page = page
        self.fill = fill
        self.shadow = shadow
        self.radius = radius
        self.padding = padding
        self.lateral = lateral
        self.stretch = stretch
        self.inner = tk.Frame(self, bg=fill, bd=0, highlightthickness=0)
        self._window = self.create_window(padding, padding, window=self.inner, anchor="nw")
        self._redraw_job = None
        self.bind("<Configure>", self._schedule_redraw)
        self.inner.bind("<Configure>", self._fit_height)

    def _schedule_redraw(self, _event=None):
        # Во время растягивания окна приходит поток Configure: рисуем один раз за цикл.
        if self._redraw_job is None:
            self._redraw_job = self.after_idle(self._redraw_now)

    def _redraw_now(self):
        self._redraw_job = None
        try:
            self._redraw()
        except tk.TclError:
            pass

    def _fit_height(self, _event=None):
        # stretch-панели тоже сообщают естественную высоту: с expand они могут
        # быть выше, но не ниже содержимого (раньше нижние кнопки обрезались).
        needed = self.inner.winfo_reqheight() + self.padding * 2 + 2
        try:
            current = int(float(self.cget("height")))
        except (TypeError, ValueError):
            current = 0
        if needed > 1 and abs(current - needed) > 1:
            self.configure(height=needed)

    def _redraw(self, event=None):
        width = max(self.winfo_width(), 40)
        height = max(self.winfo_height(), 24)
        self.delete("deco")
        body_w = width - self.lateral
        body_h = height - 4

        # Мягкая многослойная тень: от почти тёмного к цвету фона.
        for layer in range(5, 0, -1):
            ratio = 0.1 + 0.1 * (5 - layer)
            self.create_polygon(
                rounded_points(2 + layer, 4 + layer, body_w + layer - 2, body_h, self.radius),
                smooth=True, outline="", tags="deco",
                fill=_mix_color(self.page, self.shadow, ratio))

        # Нижне-правая фаска (объём) и тело карточки.
        self.create_polygon(
            rounded_points(2, 3, body_w + 1, body_h + 1, self.radius),
            smooth=True, fill="", width=1, tags="deco",
            outline=_mix_color(self.fill, self.shadow, 0.8))
        self.create_polygon(
            rounded_points(2, 2, body_w, body_h, self.radius),
            smooth=True, fill=self.fill, outline="", tags="deco")
        # Верхний светлый блик — «выпуклость».
        self.create_polygon(
            rounded_points(4, 4, body_w - 1, body_h - 1, max(4, self.radius - 2)),
            smooth=True, fill="", width=1, tags="deco",
            outline=_mix_color(self.fill, "#ffffff", 0.08))
        # Короткая градиентная линия делает карточки визуально собраннее,
        # не превращая весь интерфейс в сплошную фиолетовую подсветку.
        if self.rail:
            rail_y = 4
            rail_start = 24
            rail_width = min(112, max(44, body_w * 0.18))
            rail_colors = ("#bd79ff", "#a653f5", "#7658c8", "#454b7c")
            for index, color in enumerate(rail_colors):
                x0 = rail_start + rail_width * index / len(rail_colors)
                x1 = rail_start + rail_width * (index + 1) / len(rail_colors)
                self.create_line(x0, rail_y, x1, rail_y, fill=color, width=2,
                                 capstyle="round", tags="deco")

        self.coords(self._window, self.padding, self.padding)
        self.itemconfigure(self._window, width=max(20, body_w - self.padding * 2))
        if self.stretch:
            self.itemconfigure(self._window, height=max(20, height - self.padding * 2 - 4))
        self.tag_lower("deco")


class HuggingFaceDialog(tk.Toplevel):
    """Универсальная загрузка файлов Hugging Face: GGUF, safetensors, ONNX, JSON.

    Список файлов показывает всё, что лежит в репозитории; выбор — несколько
    файлов сразу; загрузка идёт потоком с докачкой .part, проверкой размера и
    SHA256 (когда метаданные его дают) и без молчаливой перезаписи.
    """

    SECTION_KEYS = {"Популярные GGUF": "hf.section_popular",
                    "Новые GGUF": "hf.section_new",
                    "Поиск по названию": "hf.section_search"}

    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.t = app.t
        self.title(self.t("hf.title"))
        self.configure(bg=app.colors["page"])
        # Как и главное окно — без белой системной полосы, со своей тёмной шапкой.
        self.overrideredirect(True)
        self.minsize(780, 620)
        self.transient(app)
        self.results = queue.Queue()
        self.repos = []
        self.picker = FilePicker()
        self.repo_ref = None
        self.entries = []
        self.current_size = 0
        self.download_cancel_event = None
        self._bindings = []
        self._tooltips = []
        self._row_names = []
        self.repo_var = tk.StringVar(value=app.config_data.get("hf_repo", ""))
        first_section = next(iter(HF_SECTIONS))
        self.section_var = tk.StringVar(value=self.t(self.SECTION_KEYS[first_section]))
        self.query_var = tk.StringVar(value="")
        self.filter_var = tk.StringVar(value=self.t("hf.all_ext"))
        self.fit_var = tk.BooleanVar(value=False)
        self.status_var = tk.StringVar(value=self.t("hf.status_hint"))
        self.requirements_var = tk.StringVar(value=self.t("hf.requirements"))
        self.selected_var = tk.StringVar(value="")
        self.dest_var = tk.StringVar(value=str(app.hf_download_dir()))
        self._build()
        self._center_on_app()
        self.lift()
        self.focus_force()
        self._poll_job = self.after(150, self._poll)
        self.after(300, self.load_repos)

    # --- шапка и локализация ------------------------------------------------
    def _i18n_bind(self, widget, key, **params):
        self._bindings.append((widget, key, params))
        try:
            widget.configure(text=self.t(key, **params))
        except tk.TclError:
            pass
        return widget

    def _tip(self, widget, key):
        tip = ToolTip(widget, self.t(key), self.app.colors)
        self._tooltips.append((tip, key))
        return tip

    def retranslate(self):
        try:
            self.title(self.t("hf.title"))
            for widget, key, params in self._bindings:
                widget.configure(text=self.t(key, **params))
            for tip, key in self._tooltips:
                tip.set_text(self.t(key))
            self.section_combo.configure(values=self._section_display())
            self.section_var.set(self.t(self.SECTION_KEYS.get(self._current_section(),
                                                               "hf.section_popular")))
            self.filter_combo.configure(values=self._filter_values())
            if self.filter_var.get() in ("", "Все расширения", "All files"):
                self.filter_var.set(self.t("hf.all_ext"))
            self.render_files()
        except tk.TclError:
            pass

    def _section_display(self):
        return [self.t(self.SECTION_KEYS[name]) for name in HF_SECTIONS]

    def _current_section(self):
        display = self.section_var.get()
        for name, key in self.SECTION_KEYS.items():
            if self.t(key) == display:
                return name
        return next(iter(HF_SECTIONS))

    def _center_on_app(self):
        # Высоты 700 px не хватало: нижняя строка с кнопкой «Скачать» обрезалась.
        width, height = 900, 760
        try:
            x = self.app.winfo_rootx() + (self.app.winfo_width() - width) // 2
            y = self.app.winfo_rooty() + (self.app.winfo_height() - height) // 2
        except tk.TclError:
            x = y = 100
        self.geometry(f"{width}x{height}+{max(0, x)}+{max(0, y)}")
        # После построения подгоняем высоту под реальное содержимое.
        self.after(150, self._fit_to_content)

    def _fit_to_content(self):
        try:
            pad_h = self._dialog_canvas.bbox("all")[3] + 40 + 4
            screen_h = self.winfo_screenheight()
            want = min(pad_h + 40, screen_h - 110)
            if want > self.winfo_height():
                self.geometry(f"{self.winfo_width()}x{want}+{self.winfo_x()}+"
                              f"{max(0, min(self.winfo_y(), screen_h - want - 60))}")
        except (tk.TclError, TypeError):
            pass

    def _create_titlebar(self, parent):
        c = self.app.colors
        bar = tk.Frame(parent, bg=c["page"], height=36)
        bar.pack(side="top", fill="x")
        bar.pack_propagate(False)
        title = tk.Label(bar, text=self.t("hf.header"), bg=c["page"],
                         fg=c["muted"], font=(self.app.ui_font, 9, "bold"))
        title.pack(side="left", padx=(14, 0))
        close_button = RoundIconButton(
            bar, "×", self.close, size=30, bg=c["page"], fg=c["muted"],
            hover_bg=c["danger"], hover_fg="#f4edf0",
            font=(self.app.ui_font, 15))
        close_button.pack(side="right", padx=6, pady=4)
        for widget in (bar, title):
            widget.bind("<ButtonPress-1>", self._start_move)
            widget.bind("<B1-Motion>", self._move_window)
        self.bind("<Escape>", lambda _e: self.close())
        self._title_label = title

    def _start_move(self, event):
        self._drag_offset = (event.x_root - self.winfo_x(), event.y_root - self.winfo_y())

    def _move_window(self, event):
        offset = getattr(self, "_drag_offset", None)
        if not offset:
            return
        dx, dy = offset
        self.geometry(f"+{event.x_root - dx}+{event.y_root - dy}")

    # --- интерфейс ---------------------------------------------------------
    def _build(self):
        c = self.app.colors
        shell = tk.Frame(self, bg=c["page"], highlightthickness=1,
                         highlightbackground=_mix_color(c["accent"], c["page"], 0.62))
        shell.pack(fill="both", expand=True)
        GradientLine(shell, c["accent"], c["page"]).pack(side="top", fill="x")
        self._create_titlebar(shell)
        # Весь диалог прокручивается: на небольшом экране нижняя панель
        # загрузки остаётся доступной, а не обрезается.
        scroll_host = tk.Frame(shell, bg=c["page"])
        scroll_host.pack(fill="both", expand=True)
        self._dialog_canvas = tk.Canvas(scroll_host, bg=c["page"], bd=0,
                                        highlightthickness=0, yscrollincrement=1)
        dialog_scroll = ttk.Scrollbar(scroll_host, orient="vertical",
                                      command=self._dialog_canvas.yview)
        def on_scroll_range(first, last):
            # Полоса прокрутки показывается только когда содержимое не помещается.
            dialog_scroll.set(first, last)
            if float(first) <= 0.0 and float(last) >= 1.0:
                dialog_scroll.pack_forget()
            elif not dialog_scroll.winfo_manager():
                dialog_scroll.pack(side="right", fill="y", before=self._dialog_canvas)

        self._dialog_canvas.configure(yscrollcommand=on_scroll_range)
        self._dialog_canvas.pack(side="left", fill="both", expand=True)
        pad = tk.Frame(self._dialog_canvas, bg=c["page"], padx=14, pady=10)
        self._dialog_window = self._dialog_canvas.create_window((0, 0), window=pad,
                                                                  anchor="nw")

        def update_scroll_region(_event=None):
            self._dialog_canvas.configure(scrollregion=self._dialog_canvas.bbox("all"))

        def fit_scroll_width(event):
            self._dialog_canvas.itemconfigure(self._dialog_window, width=event.width)

        pad.bind("<Configure>", update_scroll_region)
        self._dialog_canvas.bind("<Configure>", fit_scroll_width)

        repo_panel = RoundedPanel(pad, page=c["page"], fill=c["card"],
                                  shadow=c["shadow"], radius=18, padding=10, lateral=7)
        repo_panel.pack(fill="x", pady=(0, 8))
        repo_body = tk.Frame(repo_panel.inner, bg=c["card"])
        repo_body.pack(fill="x")
        self._i18n_bind(tk.Label(repo_body, text=self.t("hf.repo"), bg=c["card"],
                            fg=c["muted"], font=(self.app.ui_font, 8, "bold")),
                   "hf.repo").pack(anchor="w", pady=(0, 4))
        repo_row = tk.Frame(repo_body, bg=c["card"])
        repo_row.pack(fill="x")
        self.repo_entry = tk.Entry(
            repo_row, textvariable=self.repo_var, relief="flat", bd=0,
            bg=c["field"], fg=c["text"], insertbackground=c["text"],
            selectbackground=c["accent"], selectforeground="#171225",
            highlightthickness=1, highlightbackground=c["border"],
            highlightcolor=c["accent"], font=(self.app.ui_font, 9))
        self.repo_entry.pack(side="left", fill="x", expand=True, ipady=6)
        self.repo_entry.bind("<Return>", lambda _e: self.load_files())
        self._tip(self.repo_entry, "hf.repo_ph")
        self.load_button = self.app.rbutton(repo_row, self.t("hf.load"), self.load_files,
                                            bg=c["card"])
        self.load_button.pack(side="left", padx=(8, 0))
        self._i18n_bind(self.load_button, "hf.load")
        token_row = tk.Frame(repo_body, bg=c["card"])
        token_row.pack(fill="x", pady=(7, 0))
        self._i18n_bind(ttk.Label(token_row, text=self.t("hf.token"),
                                  style="Muted.TLabel"), "hf.token").pack(side="left")
        self.token_var = tk.StringVar(value=os.environ.get("HF_TOKEN", ""))
        tk.Entry(token_row, textvariable=self.token_var, show="•", relief="flat", bd=0,
                 bg=c["field"], fg=c["text"], insertbackground=c["text"],
                 highlightthickness=1, highlightbackground=c["border"],
                 highlightcolor=c["accent"], font=(self.app.ui_font, 9)).pack(
            side="left", fill="x", expand=True, ipady=6, padx=(10, 0))

        search_panel = RoundedPanel(pad, page=c["page"], fill=c["card"],
                                    shadow=c["shadow"], radius=18, padding=10, lateral=7)
        search_panel.pack(fill="x", pady=(0, 8))
        search_body = tk.Frame(search_panel.inner, bg=c["card"])
        search_body.pack(fill="x")
        self._i18n_bind(tk.Label(search_body, text=self.t("hf.search_section"), bg=c["card"],
                            fg=c["muted"], font=(self.app.ui_font, 8, "bold")),
                   "hf.search_section").pack(anchor="w", pady=(0, 5))
        sections_row = tk.Frame(search_body, bg=c["card"])
        sections_row.pack(fill="x")
        self.section_combo = ttk.Combobox(sections_row, textvariable=self.section_var,
                                          state="readonly", width=20,
                                          values=self._section_display())
        self.section_combo.pack(side="left")
        self.query_entry = tk.Entry(
            sections_row, textvariable=self.query_var, relief="flat", bd=0,
            bg=c["field"], fg=c["text"], insertbackground=c["text"],
            selectbackground=c["accent"], selectforeground="#171225",
            highlightthickness=1, highlightbackground=c["border"],
            highlightcolor=c["accent"], font=(self.app.ui_font, 9))
        self.query_entry.pack(side="left", fill="x", expand=True, ipady=6, padx=(8, 8))
        self.query_entry.bind("<Return>", lambda _e: self.search_by_name())
        self.find_button = self.app.rbutton(sections_row, self.t("hf.find"),
                                            self.search_by_name, bg=c["card"])
        self.find_button.pack(side="left")
        self._i18n_bind(self.find_button, "hf.find")
        self.section_combo.bind("<<ComboboxSelected>>", lambda _e: self.load_repos())
        self._i18n_bind(ttk.Label(search_body, text=self.t("hf.search_hint"),
                             style="Muted.TLabel", wraplength=760),
                   "hf.search_hint").pack(anchor="w", pady=(6, 0))

        panes = tk.Frame(pad, bg=c["page"])
        panes.pack(fill="both", expand=True, pady=(0, 8))
        panes.configure(height=260)
        panes.pack_propagate(False)
        left_panel = RoundedPanel(panes, page=c["page"], fill=c["card"],
                                  shadow=c["shadow"], radius=18, padding=10, lateral=7,
                                  stretch=True)
        left_panel.pack(side="left", fill="both", expand=True, padx=(0, 5))
        left_col = tk.Frame(left_panel.inner, bg=c["card"])
        left_col.pack(fill="both", expand=True)
        self._i18n_bind(tk.Label(left_col, text=self.t("hf.repos"), bg=c["card"], fg=c["muted"],
                            font=(self.app.ui_font, 8, "bold")), "hf.repos").pack(anchor="w")
        repos_frame = tk.Frame(left_col, bg=c["card"])
        repos_frame.pack(fill="both", expand=True, pady=(5, 0))
        self.repos_listbox = tk.Listbox(
            repos_frame, relief="flat", bd=0, bg=c["field"], fg=c["text"],
            selectbackground=c["accent"], selectforeground="#171225",
            highlightthickness=1, highlightbackground=c["border"],
            highlightcolor=c["accent"], font=(self.app.ui_font, 9),
            activestyle="none", exportselection=False)
        repos_scroll = ttk.Scrollbar(repos_frame, orient="vertical",
                                     command=self.repos_listbox.yview)
        self.repos_listbox.configure(yscrollcommand=repos_scroll.set)
        repos_scroll.pack(side="right", fill="y")
        self.repos_listbox.pack(side="left", fill="both", expand=True)
        self.repos_listbox.bind("<<ListboxSelect>>", lambda _e: self.on_repo_selected())

        right_panel = RoundedPanel(panes, page=c["page"], fill=c["card"],
                                   shadow=c["shadow"], radius=18, padding=10, lateral=7,
                                   stretch=True)
        right_panel.pack(side="left", fill="both", expand=True, padx=(5, 0))
        right_col = tk.Frame(right_panel.inner, bg=c["card"])
        right_col.pack(fill="both", expand=True)
        files_header = tk.Frame(right_col, bg=c["card"])
        files_header.pack(fill="x")
        self._i18n_bind(tk.Label(files_header, text=self.t("hf.files"), bg=c["card"], fg=c["muted"],
                            font=(self.app.ui_font, 8, "bold")), "hf.files").pack(side="left")
        tk.Label(files_header, text=self.t("hf.fit"), bg=c["card"], fg=c["muted"],
                 font=(self.app.ui_font, 8)).pack(side="right", padx=(0, 7))
        self.fit_check = NeumorphicSwitch(files_header, self.fit_var,
                                          command=self.render_files, bg=c["card"])
        self.fit_check.pack(side="right")
        filters = tk.Frame(right_col, bg=c["card"])
        filters.pack(fill="x", pady=(5, 3))
        self.file_query_var = tk.StringVar(value="")
        self.file_query_entry = tk.Entry(
            filters, textvariable=self.file_query_var, relief="flat", bd=0,
            bg=c["field"], fg=c["text"], insertbackground=c["text"],
            highlightthickness=1, highlightbackground=c["border"],
            highlightcolor=c["accent"], font=(self.app.ui_font, 9))
        self.file_query_entry.pack(side="left", fill="x", expand=True, ipady=5)
        self.file_query_entry.bind("<KeyRelease>", lambda _e: self.apply_filter())
        self._tip(self.file_query_entry, "hf.filter_hint")
        self.filter_combo = ttk.Combobox(filters, textvariable=self.filter_var,
                                         state="readonly", width=13,
                                         values=self._filter_values())
        self.filter_combo.pack(side="left", padx=(7, 0))
        self.filter_combo.bind("<<ComboboxSelected>>", lambda _e: self.apply_filter())
        list_frame = tk.Frame(right_col, bg=c["card"])
        list_frame.pack(fill="both", expand=True)
        self.file_text = tk.Text(
            list_frame, height=8, wrap="none", bg=c["field"], fg=c["text"],
            relief="flat", bd=0, highlightthickness=1, highlightbackground=c["border"],
            highlightcolor=c["accent"], font=(self.app.ui_font, 9), padx=8, pady=6,
            cursor="hand2", spacing1=1, spacing3=2)
        y_scroll = ttk.Scrollbar(list_frame, orient="vertical", command=self.file_text.yview)
        x_scroll = ttk.Scrollbar(list_frame, orient="horizontal", command=self.file_text.xview)
        self.file_text.configure(yscrollcommand=y_scroll.set, xscrollcommand=x_scroll.set)
        y_scroll.pack(side="right", fill="y")
        x_scroll.pack(side="bottom", fill="x")
        self.file_text.pack(side="left", fill="both", expand=True)
        # Выбранные файлы должны быть заметны даже в длинном списке.
        self.file_text.tag_configure("sel", foreground="#171225", background=c["accent"],
                                     font=(self.app.ui_font, 9, "bold"))
        self.file_text.tag_configure("plain", foreground=c["text"])
        self.file_text.bind("<Button-1>", self._on_file_click)
        self.file_text.configure(state="disabled")

        footer_panel = RoundedPanel(pad, page=c["page"], fill=c["card"],
                                    shadow=c["shadow"], radius=18, padding=10, lateral=7)
        footer_panel.pack(fill="x")
        footer = tk.Frame(footer_panel.inner, bg=c["card"])
        footer.pack(fill="x")
        ttk.Label(footer, textvariable=self.status_var, style="Muted.TLabel",
                  wraplength=820).pack(anchor="w")
        self.requirements_label = tk.Label(
            footer, textvariable=self.requirements_var, bg=c["card"], fg=c["accent_hover"],
            justify="left", anchor="w", wraplength=820,
            font=(self.app.ui_font, 8, "bold"))
        self.requirements_label.pack(fill="x", pady=(5, 7))
        self._autowrap(self.requirements_label)

        dest_row = tk.Frame(footer, bg=c["card"])
        dest_row.pack(fill="x")
        self._i18n_bind(ttk.Label(dest_row, text=self.t("hf.dest"), style="Muted.TLabel"),
                   "hf.dest").pack(side="left")
        tk.Entry(dest_row, textvariable=self.dest_var, relief="flat", bd=0,
                 bg=c["field"], fg=c["text"], insertbackground=c["text"],
                 highlightthickness=1, highlightbackground=c["border"],
                 highlightcolor=c["accent"], font=(self.app.ui_font, 9)).pack(
            side="left", fill="x", expand=True, ipady=6, padx=(8, 8))
        browse = self.app.rbutton(dest_row, self.t("common.browse"), self.choose_dir,
                                  bg=c["card"])
        browse.pack(side="left")
        self._i18n_bind(browse, "common.browse")

        self.progress = ttk.Progressbar(footer, mode="determinate", maximum=100)
        self.progress.pack(fill="x", pady=(9, 7))
        actions = tk.Frame(footer, bg=c["card"])
        actions.pack(fill="x")
        ttk.Label(actions, textvariable=self.selected_var, style="Muted.TLabel").pack(side="left")
        self.download_button = self.app.rbutton(
            actions, self.t("hf.download"), self.start_download, variant="accent",
            bg=c["card"])
        self.download_button.pack(side="left", padx=(10, 0))
        self.cancel_button = self.app.rbutton(
            actions, self.t("common.cancel"), self.cancel_download, variant="danger",
            state="disabled", bg=c["card"])
        self.cancel_button.pack(side="left", padx=(7, 0))
        close = self.app.rbutton(actions, self.t("common.close"), self.close, bg=c["card"])
        close.pack(side="right")
        self._i18n_bind(self.download_button, "hf.download")
        self._i18n_bind(self.cancel_button, "common.cancel")
        self._i18n_bind(close, "common.close")

    @staticmethod
    def _autowrap(label, padding=6):
        def fit(event):
            width = max(120, event.width - padding)
            try:
                if int(float(str(label.cget("wraplength")) or 0)) != width:
                    label.configure(wraplength=width)
            except tk.TclError:
                pass
        label.bind("<Configure>", fit, add="+")
        return label

    def _filter_values(self):
        extensions = sorted({entry.extension for entry in self.entries if entry.extension})
        return [self.t("hf.all_ext")] + extensions

    def _selected_extension(self):
        value = self.filter_var.get()
        if value in (self.t("hf.all_ext"), "", "Все расширения", "All files"):
            return ""
        return value

    def choose_dir(self):
        folder = filedialog.askdirectory(title=self.t("hf.dest"))
        if folder:
            self.dest_var.set(folder)

    # --- подразделы Hugging Face ------------------------------------------
    def search_by_name(self):
        if not self.query_var.get().strip():
            messagebox.showinfo(self.t("hf.find"), self.t("hf.need_query"))
            return
        search_section = "Поиск по названию"
        self.section_var.set(self.t(self.SECTION_KEYS[search_section]))
        self.load_repos()

    def load_repos(self):
        section = self._current_section()
        config = HF_SECTIONS.get(section, {})
        query = self.query_var.get().strip() if config.get("query") else ""
        if config.get("query") and not query:
            messagebox.showinfo(self.t("hf.find"), self.t("hf.need_query"))
            return
        self.status_var.set(self.t("hf.repos_loading"))
        self.repos_listbox.delete(0, "end")
        self.repos = []
        threading.Thread(target=self._load_repos_worker,
                         args=(query, config.get("sort", "downloads"), self.token_var.get()),
                         daemon=True).start()

    def _load_repos_worker(self, query, sort, token):
        try:
            payload = fetch_json(hf_search_url(query, sort=sort), token=token, timeout=30)
            self.results.put(("repos", hf_repo_list(payload)))
        except HFError as exc:
            self.results.put(("error", self._error_text(exc)))
        except Exception as exc:
            self.results.put(("error", str(exc)))

    def on_repo_selected(self):
        selection = self.repos_listbox.curselection()
        if not selection:
            return
        self.repo_var.set(self.repos[selection[0]]["id"])
        self.load_files()

    # --- список файлов ----------------------------------------------------
    def load_files(self):
        raw = self.repo_var.get().strip()
        try:
            ref = parse_repo_url(raw)
        except (InvalidRepoURL, HFError) as exc:
            text = exc.message() if isinstance(exc, HFError) else str(exc)
            self.status_var.set(text)
            self.requirements_var.set(self.t("hf.need_repo_example"))
            return
        self.repo_ref = ref
        self.repo_var.set(ref.repo_id)
        self.status_var.set(self.t("hf.reading", repo=ref.repo_id))
        self.picker.clear()
        self.entries = []
        self._row_names = []
        self.file_text.configure(state="normal")
        self.file_text.delete("1.0", "end")
        self.file_text.configure(state="disabled")
        self.selected_var.set("")
        threading.Thread(target=self._load_files_worker, args=(ref, self.token_var.get()),
                         daemon=True).start()

    def _load_files_worker(self, ref, token):
        try:
            payload = fetch_json(ref.api_url() + "?blobs=true", token=token, timeout=30)
            self.results.put(("files", (ref, build_file_list(payload))))
        except HFError as exc:
            self.results.put(("failed", (exc.kind, exc.detail, exc.code,
                                         exc.retry_after, exc.extra)))
        except Exception as exc:
            self.results.put(("failed", ("other", str(exc), None, None, {})))

    def _fits_for_size(self, size):
        """Помещается ли файл такого размера в память этого ПК."""
        if not size:
            return False  # размер неизвестен — обещать запуск нельзя
        assessment = self.app.assess_requirements_for_size(bytes_to_gb(size))
        return verdict_fits(assessment["verdict"])

    def apply_filter(self):
        self.picker.query = self.file_query_var.get().strip()
        self.picker.extension = self._selected_extension()
        self.render_files()

    def render_files(self):
        """Перерисовывает список: поиск, расширение, галочка «подходит ПК»."""
        only_ready = bool(self.fit_var.get())
        visible = []
        for entry in self.picker.visible():
            if only_ready and entry.category == "weights" and not self._fits_for_size(entry.size):
                continue
            visible.append(entry)
        self.file_text.configure(state="normal")
        self.file_text.delete("1.0", "end")
        self._row_names = []
        for entry in visible:
            checked = entry.name in self.picker.selected
            self.file_text.insert("end", row_label(entry, checked) + "\n",
                                  ("sel" if checked else "plain"))
            self._row_names.append(entry.name)
        self.file_text.configure(state="disabled")
        hidden = len(self.entries) - len(visible)
        if only_ready and hidden > 0:
            self.status_var.set(self.t("hf.hidden", count=hidden))
        elif self.entries:
            self.status_var.set(self.t("hf.file_count", count=len(visible)))
        else:
            self.status_var.set(self.t("hf.status_hint"))
        self._update_selection()

    def _on_file_click(self, event):
        try:
            index = self.file_text.index(f"@{event.x},{event.y}")
            line = int(str(index).split(".")[0])
        except (tk.TclError, ValueError):
            return
        if line < 1 or line > len(self._row_names):
            return
        name = self._row_names[line - 1]
        self.picker.toggle(name)
        self.current_size = (self.picker.by_name(name).size if self.picker.by_name(name) else 0)
        self.render_files()
        entry = self.picker.by_name(name)
        if entry is not None:
            self._show_requirements(entry)

    def _update_selection(self):
        chosen = self.picker.selected_entries()
        if chosen:
            total = sum(entry.size for entry in chosen)
            self.selected_var.set(f"{self.t('hf.selected', count=len(chosen))} · {format_size(total)}")
        elif self.picker.current:
            self.selected_var.set(self.picker.current)
        else:
            self.selected_var.set(self.t("hf.select_one"))

    # --- загрузка ---------------------------------------------------------
    def start_download(self):
        targets = self.picker.targets()
        if not targets:
            self.status_var.set(self.t("hf.select_one"))
            return
        if self.repo_ref is None:
            self.status_var.set(self.t("hf.need_repo"))
            return
        destination = Path(self.dest_var.get().strip() or self.app.hf_download_dir())
        try:
            plan = plan_download(destination, self.repo_ref, [entry.name for entry in targets])
        except (HFError, OSError) as exc:
            self.status_var.set(str(exc))
            return
        renamed = [name for name, target in plan
                   if target.name != Path(name).name]
        self.download_cancel_event = threading.Event()
        self.download_button.configure(state="disabled")
        self.cancel_button.configure(state="normal")
        self.progress.configure(value=0)
        self.app.config_data["hf_repo"] = self.repo_ref.repo_id
        self.app.config_data["hf_dir"] = str(destination)
        # Never persist HF credentials in plaintext config.json.
        self.app.config_data.pop("hf_token", None)
        self.app.save_config_safely()
        total_size = sum(entry.size for entry in targets)
        self.status_var.set(self.t("hf.plan", count=len(plan), size=format_size(total_size)))
        if renamed:
            self.status_var.set(self.t("hf.renamed", name=renamed[0]))
        threading.Thread(target=self._download_worker,
                         args=(self.repo_ref, plan, targets, self.token_var.get()),
                         daemon=True).start()

    def _download_worker(self, ref, plan, entries, token):
        by_name = {entry.name: entry for entry in entries}
        completed = []
        try:
            for index, (name, target) in enumerate(plan):
                entry = by_name[name]

                def report(done, total, index=index, name=name):
                    fraction = (done / total) if total else 0.0
                    percent = ((index + fraction) / max(1, len(plan))) * 100
                    self.results.put(("progress", (percent, f"{name}: "
                                                   f"{format_bytes(done)} / {format_bytes(total)}")))

                url = ref.resolve_url(name)
                part_dir = default_part_dir(self.app.paths, ref.repo_id)
                download_file(url, target,
                              expected_size=entry.size,
                              expected_sha256=entry.sha256,
                              token=token,
                              cancel_event=self.download_cancel_event,
                              progress=report,
                              part_dir=part_dir)
                completed.append(str(target))
            self.results.put(("done", (ref.repo_id, completed)))
        except DownloadCancelled:
            self.results.put(("cancelled", None))
        except HFError as exc:
            self.results.put(("failed", (exc.kind, exc.detail, exc.code,
                                         exc.retry_after, exc.extra)))
        except FileExistsError:
            self.results.put(("failed", ("exists", "", None, None, {})))
        except Exception as exc:
            self.results.put(("failed", ("other", str(exc), None, None, {})))

    def cancel_download(self):
        if self.download_cancel_event:
            self.download_cancel_event.set()
        try:
            self.status_var.set(self.t("status.stopping"))
        except Exception:
            self.status_var.set("Останавливаю загрузку…")

    def _error_text(self, exc):
        key = f"dl.{exc.kind}"
        message = self.t(key, detail=exc.detail or "", code=exc.code or "",
                         retry_after=exc.retry_after or 30, **exc.extra)
        return message if message != key else self.t("dl.other", detail=str(exc))

    # --- очередь событий из потоков ---------------------------------------
    def _poll(self):
        try:
            while True:
                kind, payload = self.results.get_nowait()
                if kind == "repos":
                    self.repos = payload
                    self.repos_listbox.delete(0, "end")
                    for repo in payload:
                        self.repos_listbox.insert("end", f"{repo['id']}   ↓{repo['downloads']}")
                    self.status_var.set(self.t("hf.repos_found", count=len(payload))
                                        if payload else self.t("hf.repos_empty"))
                elif kind == "files":
                    ref, entries = payload
                    self.repo_ref = ref
                    self.entries = entries
                    self.picker.set_entries(entries)
                    if ref.file_path:
                        self.picker.check(ref.file_path)
                    self.filter_combo.configure(values=self._filter_values())
                    self.render_files()
                    if not entries:
                        self.status_var.set(self.t("hf.no_files"))
                    elif ref.file_path:
                        entry = self.picker.by_name(ref.file_path)
                        if entry is not None:
                            self._show_requirements(entry)
                elif kind == "error":
                    self.status_var.set(payload)
                elif kind == "progress":
                    percent, label = payload
                    self.progress.configure(value=percent)
                    self.status_var.set(label)
                elif kind == "done":
                    repo_id, downloaded = payload
                    self.progress.configure(value=100)
                    self.cancel_button.configure(state="disabled")
                    self.download_button.configure(state="normal")
                    self.download_cancel_event = None
                    self.status_var.set(self.t("hf.done", path=downloaded[0] if downloaded else ""))
                    self.app.register_repo_download(repo_id, downloaded)
                elif kind == "cancelled":
                    self.progress.configure(value=0)
                    self.cancel_button.configure(state="disabled")
                    self.download_button.configure(state="normal")
                    self.download_cancel_event = None
                    self.status_var.set(self.t("hf.cancelled"))
                elif kind == "failed":
                    failure = payload if isinstance(payload, tuple) else ("other", payload, None, None, {})
                    kind_name, detail, code, retry_after, extra = failure
                    self.progress.configure(value=0)
                    self.cancel_button.configure(state="disabled")
                    self.download_button.configure(state="normal")
                    self.download_cancel_event = None
                    key = f"dl.{kind_name}"
                    message = self.t(key, detail=detail or "", retry_after=retry_after or 30,
                                     **{k: v for k, v in (extra or {}).items() if k != "retry_after"})
                    if message == key:
                        message = self.t("dl.other", detail=detail or "")
                    self.status_var.set(message)
        except queue.Empty:
            pass
        try:
            if self.winfo_exists():
                self._poll_job = self.after(150, self._poll)
        except tk.TclError:
            pass

    def _show_requirements(self, entry):
        if entry.category != "weights":
            self.requirements_var.set(self.t("hf.supporting"))
            self.requirements_label.configure(fg=self.app.colors["muted"])
            return
        if not entry.size:
            self.requirements_var.set(self.t("hf.size_unknown", name=entry.name))
            self.requirements_label.configure(fg=self.app.colors["muted"])
            return
        gb = bytes_to_gb(entry.size)
        assessment = self.app.assess_requirements_for_size(gb)
        colors = {"ok": self.app.colors["success"],
                  "limited": self.app.colors["accent_hover"],
                  "fail": self.app.colors["danger"]}
        self.requirements_var.set(
            self.t("hf.requirement_line", name=entry.name, size=format_size(entry.size),
                   required=f"{assessment['required_gb']:.1f}",
                   model=f"{gb:.1f}", kv=f"{assessment['kv_gb']:.1f}",
                   overhead=f"{assessment['overhead_gb']:.1f}",
                   verdict=assessment["message"]))
        self.requirements_label.configure(
            fg=colors.get(assessment["verdict"], self.app.colors["muted"]))

    def close(self):
        if self.download_cancel_event and self.cancel_button["state"] == "normal":
            if not messagebox.askyesno(self.t("hf.download_running"),
                                       self.t("hf.close_while_downloading")):
                return
            self.cancel_download()
        self.app.config_data["hf_repo"] = self.repo_var.get().strip()
        self.app.config_data["hf_dir"] = self.dest_var.get().strip()
        # Never persist HF credentials in plaintext config.json.
        self.app.config_data.pop("hf_token", None)
        self.app.save_config_safely()
        try:
            self.after_cancel(self._poll_job)
        except Exception:
            pass
        self.destroy()


def opencode_root_key(config):
    """Ключ списка провайдеров: V2 (`providers`) для OpenCode 2.x, иначе `provider`.

    Если в конфиге нет ни одного ключа, пишем V2 — это схема OpenCode 2.x.
    """
    has_v1 = isinstance(config.get("provider"), dict)
    has_v2 = isinstance(config.get("providers"), dict)
    return "providers" if (has_v2 or not has_v1) else "provider"


def upsert_opencode_local_provider(config, port, alias, model_name, context, output_limit=8192):
    """Add/update the local llama.cpp model using the user's OpenCode schema."""
    root_key = opencode_root_key(config)
    use_v2 = root_key == "providers"
    root = config.setdefault(root_key, {})
    if not isinstance(root, dict):
        raise ValueError(f'Поле "{root_key}" в OpenCode должно быть объектом.')
    provider = root.setdefault("local", {})
    if not isinstance(provider, dict):
        raise ValueError(f'Поле "{root_key}.local" должно быть объектом.')
    provider["name"] = "llama.cpp (local)"
    models = provider.setdefault("models", {})
    if not isinstance(models, dict):
        models = {}
        provider["models"] = models
    model_entry = {
        "name": f"{model_name} (local)",
        "limit": {"context": int(context), "output": max(4096, int(output_limit))},
    }
    if use_v2:
        provider["package"] = "@opencode/ai/providers/openai-compatible"
        settings = provider.setdefault("settings", {})
        if not isinstance(settings, dict):
            settings = {}
            provider["settings"] = settings
        settings["baseURL"] = f"http://127.0.0.1:{int(port)}/v1"
        model_entry["modelID"] = alias
        model_entry["capabilities"] = {"tools": True}
    else:
        provider["npm"] = "@ai-sdk/openai-compatible"
        options = provider.setdefault("options", {})
        if not isinstance(options, dict):
            options = {}
            provider["options"] = options
        options["baseURL"] = f"http://127.0.0.1:{int(port)}/v1"
    models[alias] = model_entry
    return "V2" if use_v2 else "stable"


def remove_opencode_local_model(config, alias):
    root_key = opencode_root_key(config)
    root = config.get(root_key)
    if not isinstance(root, dict):
        return False
    provider = root.get("local")
    if not isinstance(provider, dict) or not isinstance(provider.get("models"), dict):
        return False
    return provider["models"].pop(alias, None) is not None



class LocalAIManager(tk.Tk):
    def __init__(self):
        # Set a stable Windows AppUserModelID before creating any HWND. Windows
        # uses it to group the process, shortcut and taskbar button as one app.
        if IS_WINDOWS:
            try:
                ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                    "LocalAIManager.Desktop"
                )
            except Exception:
                pass
            # Opt into native per-monitor DPI before creating the first Tk window.
            try:
                ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
            except Exception:
                try:
                    ctypes.windll.shcore.SetProcessDpiAwareness(2)
                except Exception:
                    pass
        super().__init__()
        self.title("Local AI Manager")
        # Apply the same branded icon to the titlebar, Alt+Tab and taskbar.
        try:
            icon_file = Path(__file__).resolve().parent / "assets" / "local_ai_manager_icon.ico"
            if icon_file.is_file():
                self.iconbitmap(default=str(icon_file))
        except Exception:
            pass
        # Fit the borderless window to the current display. The previous fixed
        # 1440x900 size could exceed smaller screens and leave an exposed strip
        # of the desktop beside the application.
        screen_w = max(800, self.winfo_screenwidth())
        screen_h = max(600, self.winfo_screenheight())
        window_w = min(1440, screen_w)
        window_h = min(900, screen_h)
        self.geometry(f"{window_w}x{window_h}+0+0")
        self.minsize(min(1120, window_w), min(760, window_h))
        self.configure(bg="#171520")
        # Borderless window: the native Windows title bar is removed with
        # overrideredirect (no WS_CAPTION/WS_THICKFRAME hacks, which left black
        # and duplicated regions because Tk caches the frame size). The taskbar
        # button, minimize support and resize edges are restored explicitly.
        self._taskbar_refreshed = False
        if IS_WINDOWS:
            self.overrideredirect(True)
            # A mapped borderless Tk window can lose its taskbar entry when
            # minimized. Restore the normal Tk window manager briefly for the
            # minimize operation, then re-enable the frameless UI on restore.
            self.bind("<Map>", self._restore_frameless_after_map, add="+")
            self.after(60, self._init_taskbar_button)
        self._maximized = False
        self._restore_geometry = "1440x900"
        self._borderless_job = None
        available_fonts = set(tkfont.families(self))
        # Современный нейтральный гротеск; используем первый доступный в системе.
        self.ui_font = next((name for name in ("Inter", "Aptos", "Segoe UI Variable", "Segoe UI", "Arial")
                             if name in available_fonts), "TkDefaultFont")
        self.option_add("*Font", (self.ui_font, 10))
        try:
            if IS_WINDOWS:
                dpi = ctypes.windll.user32.GetDpiForSystem()
                self.tk.call("tk", "scaling", dpi / 72.0)
        except Exception:
            pass
        self.process = None
        self.job = None
        self.process_started_at = None
        self._process_monitor_started = False
        self._stopping = False
        self.download_cancel_event = None
        self.download_process = None
        self.download_thread = None
        self.log_queue = queue.Queue()
        self.models = []
        self.config_data = self.load_config()
        # Legacy versions stored HF tokens in plaintext. Remove and rewrite config immediately.
        legacy_hf_token = self.config_data.pop("hf_token", None)
        if legacy_hf_token:
            self.save_config_safely()
        # --- storage layout (phase B): everything lives under one root ---------
        self.paths = AppPaths(APP_DIR).ensure()
        self.catalog = ModelCatalog(self.paths.root / ModelCatalog.FILENAME)
        self._startup_added = self._discover_existing_models()
        # --- localization (phase I) -------------------------------------------
        language = str(self.config_data.get("language") or "ru").lower()
        self.language = language if language in LOCALES else "ru"
        self.trans = Translator(self.language)
        self.t = self.trans.t
        self.trans.on_change(self._on_language_changed)
        self._i18n_bindings = []
        # --- engines (phase E) ------------------------------------------------
        self.engine_manager = EngineManager(self.paths)
        self.engine_manager.set_system_server(self.config_data.get("server", ""))
        self._style()
        self._build()
        self.refresh_models()
        self.refresh_ram()
        self.system_hardware = None
        self.after(250, self.analyze_system)
        self.after(150, self.poll_log_queue)
        if self._startup_added:
            self.write_log(self.t("lib.found_on_disk", count=len(self._startup_added)))
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self._create_resize_grips()

    def _discover_existing_models(self):
        """Дочитывает модели из каталога model/, не перемещая файлы пользователя."""
        saved = [m for m in self.config_data.get("models", []) if isinstance(m, str)]
        try:
            found = discover_models(self.paths.models)
        except OSError as exc:
            self.write_config_note(f"Не удалось прочитать папку моделей: {exc}")
            return []
        merged, added = merge_user_models(saved, found, self.paths.models)
        self.config_data["models"] = merged
        for record in found:
            self.catalog.add(record, persist=False)
        try:
            self.catalog.save()
        except OSError:
            pass
        return added

    def write_config_note(self, text):
        # Журнал ещё не создан на этапе инициализации: текст уйдёт в stderr.
        try:
            sys.stderr.write(str(text) + "\n")
        except Exception:
            pass

    def load_config(self):
        APP_DIR.mkdir(parents=True, exist_ok=True)
        defaults = {"models": [], "server": "", "selected": "", "port": 8080,
                    "skills": [], "agents": [], "active_agent": ""}
        if CONFIG_FILE.exists():
            try:
                loaded = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
                if not isinstance(loaded, dict):
                    raise ValueError("корень файла — не JSON-объект")
            except Exception:
                # Битый конфиг не затираем молча: пользователь сможет вытащить данные вручную.
                broken = CONFIG_FILE.with_name(f"config.json.broken-{datetime.now():%Y%m%d-%H%M%S}")
                try:
                    CONFIG_FILE.replace(broken)
                except OSError:
                    pass
                return defaults
            types = {"models": list, "skills": list, "agents": list, "server": str,
                     "selected": str, "active_agent": str, "port": int}
            for key, kind in types.items():
                if key in loaded and not isinstance(loaded[key], kind):
                    loaded.pop(key)
            loaded["models"] = [m for m in loaded.get("models", []) if isinstance(m, str)]
            defaults.update(loaded)
        return defaults

    def save_config(self):
        self.config_data["models"] = self.models
        self.config_data["server"] = self.server_var.get().strip()
        self.config_data["selected"] = self.model_var.get()
        try:
            port = int(self.port_var.get() or 8080)
            self.config_data["port"] = port if 1 <= port <= 65535 else 8080
        except (TypeError, ValueError):
            self.config_data["port"] = 8080
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        temp_file = CONFIG_FILE.with_suffix(".json.tmp")
        temp_file.write_text(json.dumps(self.config_data, ensure_ascii=False, indent=2), encoding="utf-8")
        temp_file.replace(CONFIG_FILE)

    def _style(self):
        # Soft dark-violet palette with stronger contrast for editable fields.
        self.colors = {
            # Dark violet neumorphism palette inspired by the supplied reference.
            "page": "#212337",
            "card": "#272942",
            "card_alt": "#2d2f4a",
            "field": "#1f2134",
            "field_hover": "#323455",
            "border": "#343757",
            "accent": "#a653f5",
            "accent_hover": "#bd79ff",
            "accent_pressed": "#8640d5",
            "accent_deep": "#8f49e8",
            "text": "#f2effa",
            "muted": "#adaac8",
            "danger": "#b65f83",
            "log": "#1c1e31",
            "shadow": "#151726",
            "highlight": "#3d4062",
            "success": "#8cc0ae",
            "warning": "#d9b26a",
        }
        c = self.colors
        s = ttk.Style(self)
        s.theme_use("clam")
        s.configure(".", background=c["page"], foreground=c["text"],
                    fieldbackground=c["field"], bordercolor=c["border"],
                    font=(self.ui_font, 10))
        s.configure("TFrame", background=c["page"])
        s.configure("Card.TFrame", background=c["card"], relief="flat", borderwidth=0)
        s.configure("TLabel", background=c["page"], foreground=c["text"],
                    font=(self.ui_font, 10))
        s.configure("Muted.TLabel", background=c["card"], foreground=c["muted"],
                    font=(self.ui_font, 9))
        s.configure("Title.TLabel", background=c["page"], foreground=c["text"],
                    font=(self.ui_font, 22, "bold"))
        s.configure("Subsection.TLabel", background=c["card"], foreground=c["accent_hover"],
                    font=(self.ui_font, 9, "bold"))
        s.configure("Section.TLabel", background=c["card"], foreground="#ddd7e7",
                    font=(self.ui_font, 9, "bold"))
        s.configure("Body.TLabel", background=c["card"], foreground=c["text"],
                    font=(self.ui_font, 10))
        s.configure("Hint.TLabel", background=c["card"], foreground=c["muted"],
                    font=(self.ui_font, 8))
        # Soft raised controls: low-contrast bevels and violet active states.
        soft_light = _mix_color(c["card"], "#ffffff", 0.10)
        soft_dark = _mix_color(c["card"], c["shadow"], 0.55)
        s.configure("TButton", background=c["card"], foreground=c["text"],
                    padding=(16, 10), borderwidth=1, relief="raised",
                    lightcolor=soft_light, darkcolor=soft_dark,
                    font=(self.ui_font, 9, "bold"))
        s.map("TButton",
              background=[("active", c["field_hover"]), ("pressed", c["field"])],
              relief=[("pressed", "sunken"), ("!pressed", "raised")],
              foreground=[("disabled", "#777990")])
        s.configure("Accent.TButton", background=c["accent_deep"], foreground="#ffffff",
                    padding=(18, 11), borderwidth=1, relief="raised",
                    lightcolor=_mix_color(c["accent_deep"], "#ffffff", 0.35),
                    darkcolor=_mix_color(c["accent_deep"], c["shadow"], 0.6),
                    font=(self.ui_font, 9, "bold"))
        s.map("Accent.TButton",
              background=[("active", c["accent_hover"]), ("pressed", c["accent_pressed"])],
              relief=[("pressed", "sunken"), ("!pressed", "raised")],
              foreground=[("active", "#ffffff")])
        s.configure("Export.TButton", background="#6b4198", foreground="#ffffff",
                    padding=(16, 10), borderwidth=1, relief="raised",
                    lightcolor=_mix_color("#6b4198", "#ffffff", 0.3),
                    darkcolor=_mix_color("#6b4198", c["shadow"], 0.6),
                    font=(self.ui_font, 9, "bold"))
        s.map("Export.TButton", background=[("active", "#8050ae"), ("pressed", "#51306f")])
        s.configure("Danger.TButton", background="#4d3850", foreground="#f4e8f0",
                    padding=(16, 10), borderwidth=1, relief="raised",
                    lightcolor=_mix_color("#4d3850", "#ffffff", 0.18),
                    darkcolor=_mix_color("#4d3850", c["shadow"], 0.6),
                    font=(self.ui_font, 9, "bold"))
        s.map("Danger.TButton", background=[("active", "#62445f"), ("pressed", "#3b2a3b")])

        # Matching checkbox/radio styling for any native toggle controls.
        s.configure("TCheckbutton", background=c["card"], foreground=c["muted"],
                    indicatorcolor=c["field"], padding=5, focuscolor=c["accent"])
        s.map("TCheckbutton",
              background=[("active", c["card"])],
              foreground=[("selected", c["accent_hover"]), ("active", c["text"])],
              indicatorcolor=[("selected", c["accent"]), ("active", c["field_hover"])])
        s.configure("TRadiobutton", background=c["card"], foreground=c["muted"],
                    indicatorcolor=c["field"], padding=5, focuscolor=c["accent"])
        s.map("TRadiobutton",
              background=[("active", c["card"])],
              foreground=[("selected", c["accent_hover"]), ("active", c["text"])],
              indicatorcolor=[("selected", c["accent"]), ("active", c["field_hover"])])

        soft_border = _mix_color(c["border"], c["card"], 0.35)
        s.configure("TEntry", fieldbackground=c["field"], foreground=c["text"],
                    insertcolor=c["text"], bordercolor=soft_border,
                    lightcolor=soft_border, darkcolor=soft_border,
                    padding=7, relief="flat")
        s.configure("Compact.TEntry", padding=5)
        s.map("TEntry",
              fieldbackground=[("disabled", c["page"]), ("focus", c["field_hover"]), ("!focus", c["field"])],
              foreground=[("disabled", c["muted"])],
              bordercolor=[("focus", c["accent"]), ("!focus", soft_border)])
        quiet_border = _mix_color(c["border"], c["field"], 0.55)
        s.configure("TCombobox", fieldbackground=c["field"], background=c["field"],
                    foreground=c["text"], arrowcolor=c["accent_hover"], arrowsize=16,
                    bordercolor=quiet_border, lightcolor=c["field"],
                    darkcolor=c["field"], padding=6, relief="flat")
        s.map("TCombobox",
              fieldbackground=[("disabled", c["page"]), ("readonly", c["field"]), ("focus", c["field_hover"])],
              foreground=[("disabled", c["muted"]), ("readonly", c["text"])],
              bordercolor=[("focus", c["accent"]), ("!focus", quiet_border)],
              arrowcolor=[("active", "#ffffff"), ("focus", "#ffffff")],
              selectbackground=[("readonly", c["accent"])],
              selectforeground=[("readonly", "#171225")],
              background=[("readonly", c["field"]), ("active", c["field_hover"])])
        for name, key in (("Ok", "success"), ("Warn", "warning"), ("Bad", "danger")):
            s.configure(f"{name}.Horizontal.TProgressbar", troughcolor=c["field"],
                        background=c[key], bordercolor=c["field"],
                        lightcolor=_mix_color(c[key], "#ffffff", 0.25),
                        darkcolor=_mix_color(c[key], "#000000", 0.25), thickness=7)
        s.configure("Horizontal.TProgressbar", troughcolor=c["field"],
                    background=c["accent"], bordercolor=c["field"],
                    lightcolor=c["accent_hover"], darkcolor=c["accent_deep"], thickness=7)
        # Тёмный скроллбар вместо системного светлого.
        # Тонкая полоса без стрелок: ползунок подсвечивается при наведении.
        thumb = _mix_color(c["border"], c["highlight"], 0.5)
        for orient, sticky in (("Vertical", "ns"), ("Horizontal", "ew")):
            name = f"{orient}.TScrollbar"
            s.layout(name, [(f"{orient}.Scrollbar.trough", {
                "sticky": sticky,
                "children": [(f"{orient}.Scrollbar.thumb", {"expand": "1", "sticky": "nswe"})]})])
            s.configure(name, background=thumb, troughcolor=c["page"],
                        bordercolor=c["page"], lightcolor=thumb, darkcolor=thumb,
                        relief="flat", borderwidth=0, gripcount=0, width=11)
            s.map(name, background=[("pressed", c["accent_pressed"]), ("active", c["accent"])],
                  lightcolor=[("pressed", c["accent_pressed"]), ("active", c["accent"])],
                  darkcolor=[("pressed", c["accent_pressed"]), ("active", c["accent"])])
        s.configure("Toast.TFrame", background=c["card"], relief="raised", borderwidth=2)
        s.configure("ToastTitle.TLabel", background=c["card"], foreground=c["text"],
                    font=(self.ui_font, 10, "bold"))
        s.configure("ToastBody.TLabel", background=c["card"], foreground=c["muted"],
                    font=(self.ui_font, 9))
        # Единый стиль вкладок и табличных списков во всех дополнительных окнах.
        s.configure("App.TNotebook", background=c["page"], borderwidth=0, tabmargins=(2, 4, 2, 0),
                    bordercolor=c["page"], lightcolor=c["page"], darkcolor=c["page"])
        s.configure("App.TNotebook.Tab", background=c["card_alt"], foreground=c["muted"],
                    padding=(16, 7), borderwidth=0, font=(self.ui_font, 9, "bold"),
                    bordercolor=c["page"], lightcolor=c["card_alt"], darkcolor=c["card_alt"])
        s.map("App.TNotebook.Tab",
              lightcolor=[("selected", c["accent_deep"])], darkcolor=[("selected", c["accent_deep"])],
              bordercolor=[("selected", c["accent_deep"])],
              background=[("selected", c["accent_deep"]), ("active", c["field_hover"])],
              foreground=[("selected", "#ffffff"), ("active", c["text"])])
        s.configure("Treeview", background=c["field"], fieldbackground=c["field"],
                    foreground=c["text"], borderwidth=0, rowheight=30,
                    font=(self.ui_font, 9))
        s.configure("Treeview.Heading", background=c["card_alt"], foreground=c["muted"],
                    relief="flat", font=(self.ui_font, 9, "bold"))
        s.map("Treeview", background=[("selected", c["accent_deep"])],
              foreground=[("selected", "#ffffff")])
        self.option_add("*TCombobox*Listbox.background", c["field"])
        self.option_add("*TCombobox*Listbox.foreground", c["text"])
        self.option_add("*TCombobox*Listbox.selectBackground", c["accent"])
        self.option_add("*TCombobox*Listbox.selectForeground", "#171225")

    def rbutton(self, parent, text, command, variant="default", **kwargs):
        """Скруглённая кнопка в цветах текущей темы."""
        return RoundedButton(parent, text, command, self.colors,
                             variant=variant, font=(self.ui_font, 10, "bold"), **kwargs)

    # --- локализация (phase I) ---------------------------------------------
    def _bind_text(self, widget, key, kind="text", **params):
        """Регистрирует виджет для t(key) и сразу ставит текст."""
        try:
            self._i18n_bindings.append([widget, key, kind, params])
            self._apply_binding(widget, key, kind, params)
        except Exception:
            pass
        return widget

    def _apply_binding(self, widget, key, kind, params):
        text = self.t(key, **params)
        try:
            if kind == "tooltip":
                widget.set_text(text)
                return
            if not widget.winfo_exists():
                return
            widget.configure(text=text)
        except tk.TclError:
            pass

    def retranslate(self):
        """Перерисовывает подписи главного окна без перезапуска."""
        alive = []
        for item in list(self._i18n_bindings):
            widget, key, kind, params = item
            if kind == "tooltip":
                alive.append(item)
                self._apply_binding(widget, key, kind, params)
                continue
            try:
                if not widget.winfo_exists():
                    continue
            except tk.TclError:
                continue
            alive.append(item)
            self._apply_binding(widget, key, kind, params)
        self._i18n_bindings = alive
        try:
            self.language_var.set(LOCALES.get(self.language, self.language))
        except Exception:
            pass
        try:
            prefix = "▾  " if getattr(self, "_advanced_open", False) else "▸  "
            self.advanced_toggle.configure(text=prefix + self.t("launch.advanced"))
            self._set_advanced(getattr(self, "_advanced_open", False))
        except (AttributeError, tk.TclError):
            pass
        # Статус в шапке тоже переводится.
        try:
            self.set_status(getattr(self, "_status_kind", "idle"),
                            getattr(self, "_status_key", None),
                            **getattr(self, "_status_params", {}))
            self._set_log_open(getattr(self, "_log_open", False))
            self._set_engine_section(getattr(self, "_engine_section_open", False))
            self.refresh_ram()
        except Exception:
            pass

    def _on_language_changed(self, locale):
        self.retranslate()
        monitor = getattr(self, "main_monitor", None)
        if monitor is not None and hasattr(monitor, "retranslate"):
            try:
                monitor.retranslate()
            except tk.TclError:
                pass
        for attribute in ("chat_window", "hf_dialog", "agent_skill_window", "dashboard_window"):
            window = getattr(self, attribute, None)
            try:
                if window is not None and window.winfo_exists() and hasattr(window, "retranslate"):
                    window.retranslate()
            except tk.TclError:
                continue

    def set_language(self, code):
        names = {name: locale for locale, name in LOCALES.items()}
        code = names.get(code, code)
        if code not in LOCALES or code == self.language:
            return self.language
        self.language = code
        self.config_data["language"] = code
        self.save_config_safely()
        self.trans.set_locale(code)
        return code

    STATUS_COLORS = {"running": "success", "error": "danger", "busy": "warning", "idle": "muted"}

    def set_status(self, kind="idle", key=None, **params):
        """Статус в шапке: kind задаёт цвет, key — текст (по умолчанию status.<kind>)."""
        self._status_kind = kind
        self._status_key = key or f"status.{kind}"
        self._status_params = params
        try:
            self.status_var.set(self.t("app.status",
                                       text=self.t(self._status_key, **params)))
        except Exception:
            pass
        self._paint_status_dot()
        self._sync_header_buttons()

    def _sync_header_buttons(self):
        """Кнопка «Стоп» активна, пока есть живой процесс сервера или идёт запуск."""
        stop_btn = getattr(self, "header_stop_btn", None)
        if stop_btn is None:
            return
        proc = getattr(self, "process", None)
        alive = bool(proc and proc.poll() is None)
        busy_stopping = bool(getattr(self, "_stopping", False))
        try:
            stop_btn.configure(state="normal" if alive and not busy_stopping else "disabled")
        except tk.TclError:
            pass

    def _paint_status_dot(self):
        color = self.colors.get(self.STATUS_COLORS.get(getattr(self, "_status_kind", "idle"), "muted"),
                                self.colors["muted"])
        try:
            self.status_dot.configure(fg=color)
        except tk.TclError:
            pass

    def _draw_header_rail(self, canvas, width):
        """Draw a restrained violet-to-indigo segmented accent under the hero."""
        canvas.delete("all")
        colors = ["#8f49e8", "#a653f5", "#bd79ff", "#7658c8", "#454b7c"]
        total = max(1, int(width))
        segment = total / len(colors)
        for index, color in enumerate(colors):
            x0 = index * segment
            x1 = (index + 1) * segment
            canvas.create_rectangle(x0, 0, x1, 3, fill=color, outline="")

    def card(self, parent, title, stretch=False):
        # Компактная карточка: заголовок с тонкой акцентной чертой, без иконок и разделителей.
        c = self.colors
        panel = RoundedPanel(parent, page=c["page"], fill=c["card"], shadow=c["shadow"],
                             radius=16, padding=10, lateral=8, stretch=stretch)
        content = ttk.Frame(panel.inner, style="Card.TFrame")
        content.pack(fill="both", expand=True)
        head = tk.Frame(content, bg=c["card"])
        head.pack(fill="x", pady=(0, 5))
        tk.Frame(head, bg=c["accent"], width=3, height=13).pack(side="left", padx=(0, 8))
        title_label = ttk.Label(head, text=self.t(title), style="Section.TLabel")
        title_label.pack(side="left", anchor="center")
        self._bind_text(title_label, title)
        return panel, content

    def draw_3d_logo(self, parent):
        # Small isometric cube, rendered as real vector polygons in Tk Canvas.
        c = self.colors
        canvas = tk.Canvas(parent, width=58, height=54, bg=c["page"],
                           bd=0, highlightthickness=0)
        canvas.pack(side="left", padx=(0, 12))
        canvas.create_polygon(29, 4, 49, 15, 29, 27, 9, 15,
                              fill="#766b91", outline="#b7accb", width=1.5)
        canvas.create_polygon(9, 15, 29, 27, 29, 49, 9, 37,
                              fill="#4c435f", outline="#9b90ad", width=1.5)
        canvas.create_polygon(49, 15, 29, 27, 29, 49, 49, 37,
                              fill="#625675", outline="#b0a4c4", width=1.5)
        canvas.create_line(29, 27, 29, 49, fill="#d0c5df", width=1)
        canvas.create_line(9, 15, 29, 4, 49, 15, fill="#d0c5df", width=1)
        return canvas

    def show_notification(self, title, message, kind="info", duration=7500):
        """Show a compact in-app notification using the app's muted 3D panel style."""
        if getattr(self, "_notification_frame", None):
            try:
                self._notification_frame.destroy()
            except Exception:
                pass

        c = self.colors
        shadow = tk.Frame(self, bg=c["shadow"], bd=0, highlightthickness=0)
        accent = c["success"] if kind == "success" else c["danger"] if kind == "error" else c["accent"]
        panel = tk.Frame(shadow, bg=c["card"], bd=0, relief="flat",
                         highlightthickness=1, highlightbackground=_mix_color(accent, c["card"], .35),
                         highlightcolor=accent)
        panel.pack(padx=(0, 8), pady=(0, 8))
        tk.Frame(panel, bg=c["highlight"], height=2, bd=0).pack(fill="x", side="top")
        stripe = tk.Frame(panel, bg=accent, width=4)
        stripe.pack(side="left", fill="y")
        body = tk.Frame(panel, bg=c["card"], padx=13, pady=10)
        body.pack(side="left", fill="both", expand=True)
        head = tk.Frame(body, bg=c["card"])
        head.pack(fill="x")
        tk.Label(head, text=title, bg=c["card"], fg=c["text"],
                 font=(self.ui_font, 10, "bold")).pack(side="left", anchor="w")
        close_btn = RoundIconButton(
            head, "×", shadow.destroy, size=25, bg=c["card"], fg=c["muted"],
            hover_bg=c["field_hover"], hover_fg=c["text"],
            font=(self.ui_font, 12, "bold"))
        close_btn.pack(side="right")
        tk.Label(body, text=message, bg=c["card"], fg=c["muted"],
                 justify="left", anchor="w", wraplength=390,
                 font=(self.ui_font, 9)).pack(fill="x", pady=(5, 0))
        shadow.place(relx=0.985, y=-60, anchor="ne")
        shadow.lift()
        self._notification_frame = shadow
        animate_place_y(shadow, -60, 46, duration=260)

        def dismiss():
            if shadow.winfo_exists():
                animate_place_y(shadow, 46, -70, duration=220,
                                done=lambda: shadow.destroy() if shadow.winfo_exists() else None)
        self.after(duration, dismiss)

    def _win_hwnd(self, user32=None):
        """Native top-level (wrapper) HWND of the Tk root window."""
        from ctypes import wintypes
        user32 = user32 or ctypes.WinDLL("user32", use_last_error=True)
        get_ancestor = user32.GetAncestor
        get_ancestor.argtypes = (wintypes.HWND, wintypes.UINT)
        get_ancestor.restype = wintypes.HWND
        child = wintypes.HWND(self.winfo_id())
        return get_ancestor(child, 2) or child.value  # GA_ROOT

    def _init_taskbar_button(self):
        """Give the borderless window a normal taskbar button (once)."""
        if not IS_WINDOWS or self._taskbar_refreshed:
            return
        self._taskbar_refreshed = True
        self._apply_windows_window_style()
        try:
            # Re-show once so Explorer picks up WS_EX_APPWINDOW.
            self.withdraw()
            self.after(30, self._reshow_after_taskbar_init)
        except tk.TclError:
            pass

    def _reshow_after_taskbar_init(self):
        try:
            self.deiconify()
            self.lift()
            self._raise_resize_grips()
        except tk.TclError:
            pass

    def _apply_windows_window_style(self):
        """Make the borderless window a regular taskbar window.

        Sets WS_EX_APPWINDOW (clears WS_EX_TOOLWINDOW) so the app keeps its
        taskbar button, and WS_MINIMIZEBOX/WS_SYSMENU so the window can be
        minimized and restored from the taskbar. No caption or frame styles
        are touched, so there is no title bar and no DWM/Tk size mismatch.
        """
        if not IS_WINDOWS:
            return False
        try:
            from ctypes import wintypes

            user32 = ctypes.WinDLL("user32", use_last_error=True)
            get_window_long = getattr(user32, "GetWindowLongPtrW", None) or user32.GetWindowLongW
            set_window_long = getattr(user32, "SetWindowLongPtrW", None) or user32.SetWindowLongW
            get_window_long.argtypes = (wintypes.HWND, ctypes.c_int)
            get_window_long.restype = ctypes.c_ssize_t
            set_window_long.argtypes = (wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t)
            set_window_long.restype = ctypes.c_ssize_t
            hwnd = self._win_hwnd(user32)
            if not hwnd:
                return False

            GWL_STYLE = -16
            GWL_EXSTYLE = -20
            WS_SYSMENU = 0x00080000
            WS_MINIMIZEBOX = 0x00020000
            WS_EX_TOOLWINDOW = 0x00000080
            WS_EX_APPWINDOW = 0x00040000

            style = get_window_long(hwnd, GWL_STYLE)
            new_style = style | WS_SYSMENU | WS_MINIMIZEBOX
            if new_style != style:
                set_window_long(hwnd, GWL_STYLE, new_style)
            ex_style = get_window_long(hwnd, GWL_EXSTYLE)
            new_ex = (ex_style & ~WS_EX_TOOLWINDOW) | WS_EX_APPWINDOW
            if new_ex != ex_style:
                set_window_long(hwnd, GWL_EXSTYLE, new_ex)

            # Tk can cache the original extended styles for an overrideredirect
            # window. Force Windows to recalculate the frame and notify the
            # shell after WS_EX_APPWINDOW has been applied; otherwise Explorer
            # may omit the taskbar button even though the style bits look right.
            set_window_pos = user32.SetWindowPos
            set_window_pos.argtypes = (
                wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                ctypes.c_int, ctypes.c_int, ctypes.c_uint,
            )
            set_window_pos.restype = wintypes.BOOL
            SWP_NOSIZE = 0x0001
            SWP_NOMOVE = 0x0002
            SWP_NOZORDER = 0x0004
            SWP_NOACTIVATE = 0x0010
            SWP_FRAMECHANGED = 0x0020
            set_window_pos(
                hwnd, None, 0, 0, 0, 0,
                SWP_NOSIZE | SWP_NOMOVE | SWP_NOZORDER |
                SWP_NOACTIVATE | SWP_FRAMECHANGED,
            )
            return True
        except Exception:
            return False

    def _work_area(self):
        """(x, y, w, h) of the monitor work area (screen minus taskbar)."""
        if IS_WINDOWS:
            try:
                from ctypes import wintypes

                class MONITORINFO(ctypes.Structure):
                    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                                ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]

                user32 = ctypes.WinDLL("user32", use_last_error=True)
                user32.MonitorFromWindow.argtypes = (wintypes.HWND, wintypes.DWORD)
                user32.MonitorFromWindow.restype = wintypes.HANDLE
                user32.GetMonitorInfoW.argtypes = (wintypes.HANDLE, ctypes.POINTER(MONITORINFO))
                user32.GetMonitorInfoW.restype = wintypes.BOOL
                monitor = user32.MonitorFromWindow(self._win_hwnd(user32), 2)  # NEAREST
                info = MONITORINFO()
                info.cbSize = ctypes.sizeof(MONITORINFO)
                if monitor and user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
                    r = info.rcWork
                    return r.left, r.top, r.right - r.left, r.bottom - r.top
            except Exception:
                pass
        return 0, 0, self.winfo_screenwidth(), self.winfo_screenheight()

    def _create_resize_grips(self):
        """Thin invisible edges/corners so the borderless window can be resized."""
        if not IS_WINDOWS:
            return
        edge, corner = 5, 10
        specs = {
            "n": ("sb_v_double_arrow", dict(x=corner, y=0, relwidth=1.0, width=-2 * corner, height=edge)),
            "s": ("sb_v_double_arrow", dict(x=corner, rely=1.0, y=-edge, relwidth=1.0, width=-2 * corner, height=edge)),
            "w": ("sb_h_double_arrow", dict(x=0, y=corner, width=edge, relheight=1.0, height=-2 * corner)),
            "e": ("sb_h_double_arrow", dict(relx=1.0, x=-edge, y=corner, width=edge, relheight=1.0, height=-2 * corner)),
            "nw": ("size_nw_se", dict(x=0, y=0, width=corner, height=corner)),
            "ne": ("size_ne_sw", dict(relx=1.0, x=-corner, y=0, width=corner, height=corner)),
            "sw": ("size_ne_sw", dict(x=0, rely=1.0, y=-corner, width=corner, height=corner)),
            "se": ("size_nw_se", dict(relx=1.0, x=-corner, rely=1.0, y=-corner, width=corner, height=corner)),
        }
        self._resize_grips = []
        for side, (cursor, place) in specs.items():
            grip = tk.Frame(self, cursor=cursor, bd=0, highlightthickness=0,
                             bg=self.cget("bg"))
            grip._place_kw = place
            grip.bind("<ButtonPress-1>", lambda e, sd=side: self._start_resize(e, sd))
            grip.bind("<B1-Motion>", lambda e, sd=side: self._do_resize(e, sd))
            self._resize_grips.append(grip)
        self._raise_resize_grips()

    def _raise_resize_grips(self):
        for grip in getattr(self, "_resize_grips", []):
            try:
                if self._maximized:
                    grip.place_forget()
                else:
                    grip.place(**grip._place_kw)
                    grip.lift()
            except tk.TclError:
                pass

    def _start_resize(self, event, side):
        self._resize_start = (event.x_root, event.y_root, self.winfo_x(), self.winfo_y(),
                              self.winfo_width(), self.winfo_height())

    def _do_resize(self, event, side):
        start = getattr(self, "_resize_start", None)
        if not start or self._maximized:
            return
        sx, sy, x, y, w, h = start
        dx, dy = event.x_root - sx, event.y_root - sy
        min_w, min_h = self.wm_minsize()
        nx, ny, nw, nh = x, y, w, h
        if "e" in side:
            nw = max(min_w, w + dx)
        if "s" in side:
            nh = max(min_h, h + dy)
        if "w" in side:
            nw = max(min_w, w - dx)
            nx = x + (w - nw)
        if "n" in side:
            nh = max(min_h, h - dy)
            ny = y + (h - nh)
        self.geometry(f"{nw}x{nh}+{nx}+{ny}")

    def _create_custom_titlebar(self):
        c = self.colors
        bar = tk.Frame(self, bg=c["page"], height=38, bd=0,
                       highlightthickness=0)
        bar.pack(side="top", fill="x")
        bar.pack_propagate(False)

        # Левая надпись убрана: освободили место под элементы интерфейса.

        # Кнопки окна — самые правые; левее них переключатель языка.
        controls = tk.Frame(bar, bg=c["page"])
        controls.pack(side="right", fill="y")
        self._titlebar_button(controls, "—", self.minimize_window)
        self._max_button = self._titlebar_button(controls, "□", self.toggle_maximize)
        self._titlebar_button(controls, "×", self.close_window, danger=True)

        # Язык переключается сразу, без перезапуска (phase I).
        self.language_display = LOCALES.get(self.language) or LOCALES["ru"]
        self.language_var = tk.StringVar(value=self.language_display)
        self.language_combo = ttk.Combobox(
            bar, textvariable=self.language_var, state="readonly", width=9,
            values=[LOCALES["ru"], LOCALES["en"]])
        self.language_combo.pack(side="right", padx=(0, 10), pady=6)
        self.language_combo.bind("<<ComboboxSelected>>",
                                 lambda _e: self.set_language(self.language_var.get()))
        label = tk.Label(bar, text=self.t("lang.label"), bg=c["page"], fg=c["muted"],
                         font=(self.ui_font, 8, "bold"))
        label.pack(side="right", padx=(0, 6))
        self._bind_text(label, "lang.label")
        ToolTip(self.language_combo, self.t("lang.label"), c)

        # Перетаскивание безрамочного окна за свободную область верхней полосы.
        # События на кнопках и комбобоксе остаются их собственными.
        for drag_target in (bar, label):
            drag_target.bind("<ButtonPress-1>", self._start_window_drag)
            drag_target.bind("<B1-Motion>", self._drag_window)
            drag_target.bind("<Double-Button-1>", lambda e: self.toggle_maximize())

    def _titlebar_button(self, parent, label, command, danger=False):
        # Один размер и шрифт для всех трёх кнопок: иначе «×» выглядит крупнее.
        c = self.colors
        btn = RoundIconButton(
            parent, label, command, size=28,
            bg=c["page"], fg=c["danger"] if danger else c["muted"],
            hover_bg=c["danger"] if danger else c["card_alt"],
            hover_fg="#f4edf0" if danger else c["text"],
            font=(self.ui_font, 13),
            glyph={"—": "minimize", "□": "maximize", "×": "close"}.get(label))
        btn.pack(side="left", padx=2, pady=5)
        return btn

    def _start_window_drag(self, event):
        if self._maximized:
            return
        self._drag_x = event.x_root - self.winfo_x()
        self._drag_y = event.y_root - self.winfo_y()

    def _drag_window(self, event):
        if self._maximized:
            return
        self.geometry(f"+{event.x_root-self._drag_x}+{event.y_root-self._drag_y}")

    def _restore_frameless_after_map(self, event=None):
        """Reapply the custom borderless frame after Windows restores the window."""
        if not IS_WINDOWS:
            return
        try:
            if event is not None and event.widget is not self:
                return
            if self.state() == "iconic":
                return
            self.after_idle(self._finish_frameless_restore)
        except tk.TclError:
            pass

    def _finish_frameless_restore(self):
        try:
            if self.state() == "iconic":
                return
            self.overrideredirect(True)
            self._apply_windows_window_style()
            self.lift()
            self._raise_resize_grips()
        except tk.TclError:
            pass

    def minimize_window(self):
        # Let the Windows shell manage the minimize transition. Tk's
        # overrideredirect windows are treated as tool windows by some Windows
        # builds, so temporarily restore WM ownership before iconifying.
        try:
            if IS_WINDOWS:
                self.overrideredirect(False)
                self.update_idletasks()
                self._apply_windows_window_style()
            self.iconify()
        except tk.TclError:
            pass

    def toggle_maximize(self):
        if not self._maximized:
            self._restore_geometry = self.geometry()
            if IS_WINDOWS:
                x, y, w, h = self._work_area()
                self.geometry(f"{w}x{h}+{x}+{y}")
            else:
                try:
                    self.state("zoomed")
                except tk.TclError:
                    sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
                    self.geometry(f"{sw}x{sh}+0+0")
            self._maximized = True
        else:
            if not IS_WINDOWS:
                try:
                    self.state("normal")
                except tk.TclError:
                    pass
            self.geometry(self._restore_geometry)
            self._maximized = False
        self._max_button.set_glyph("restore" if self._maximized else "maximize")
        self._raise_resize_grips()

    def close_window(self):
        self.on_close()

    def _build(self):
        border = _mix_color(self.colors["accent"], self.colors["page"], 0.62)
        self.configure(highlightthickness=1, highlightbackground=border, highlightcolor=border)
        GradientLine(self, self.colors["accent"], self.colors["page"]).pack(side="top", fill="x")
        self._create_custom_titlebar()
        root = self._create_scroll_area()
        c = self.colors
        header_panel = RoundedPanel(root, page=c["page"], fill=c["card"],
                                    shadow=c["shadow"], radius=16, padding=7, lateral=10)
        header_panel.pack(fill="x", pady=(0, 4))
        header = tk.Frame(header_panel.inner, bg=c["card"])
        header.pack(fill="x")
        logo = tk.Canvas(header, width=34, height=34, bg=c["card"], bd=0, highlightthickness=0)
        logo.pack(side="left", padx=(0, 10))
        logo.create_polygon(rounded_points(1, 1, 33, 33, 10), smooth=True,
                            fill=c["field"], outline=c["border"])
        logo.create_text(17, 17, text="◆", fill=c["accent_hover"], font=(self.ui_font, 13, "bold"))
        title_box = tk.Frame(header, bg=c["card"])
        title_box.pack(side="left", fill="y")
        self._bind_text(tk.Label(title_box, text=self.t("app.brand"), bg=c["card"], fg=c["text"],
                                 font=(self.ui_font, 14, "bold")), "app.brand").pack(anchor="w")
        title_box.pack_configure(pady=2)
        self.status_var = tk.StringVar()
        self._status_kind = "idle"
        header_actions = tk.Frame(header, bg=c["card"])
        header_actions.pack(side="right", padx=(8, 0))
        self.header_start_btn = self.rbutton(header_actions, self.t("app.start"), self.start_model,
                                             variant="accent", bg=c["card"], pad=(14, 7))
        self.header_start_btn.pack(side="left", padx=(0, 6))
        self.header_stop_btn = self.rbutton(header_actions, self.t("app.stop"), self.stop_model,
                                            bg=c["card"], pad=(12, 7))
        self.header_stop_btn.pack(side="left", padx=(0, 6))
        self.header_stop_btn.configure(state="disabled")
        self.header_chat_btn = self.rbutton(header_actions, self.t("app.chat"), self.open_chat,
                                            bg=c["card"], pad=(12, 7))
        self.header_chat_btn.pack(side="left")
        self._bind_text(self.header_start_btn, "app.start")
        self._bind_text(self.header_stop_btn, "app.stop")
        self._bind_text(self.header_chat_btn, "app.chat")
        status_chip = tk.Frame(header, bg=c["field"], padx=10, pady=5,
                               highlightthickness=1, highlightbackground=c["border"])
        status_chip.pack(side="right", padx=(0, 8))
        self.status_dot = tk.Label(status_chip, text="●", bg=c["field"],
                                   fg=c["muted"], font=(self.ui_font, 8))
        self.status_dot.pack(side="left", padx=(0, 6))
        tk.Label(status_chip, textvariable=self.status_var, bg=c["field"],
                 fg=c["text"], font=(self.ui_font, 9, "bold")).pack(side="left")
        self.set_status("idle")

        # Живой мониторинг — компактная полоса под шапкой.
        self.main_monitor = MainMonitorPanel(root, self)
        self.main_monitor.pack(fill="x", pady=(0, 4))

        # «Следующее действие» — узкая полоса: подсказка слева, кнопки справа.
        next_panel = RoundedPanel(root, page=c["page"], fill=c["card"], shadow=c["shadow"],
                                  radius=16, padding=6, lateral=8)
        next_panel.pack(fill="x", pady=(0, 4))
        next_row = tk.Frame(next_panel.inner, bg=c["card"])
        next_row.pack(fill="x")
        tk.Label(next_row, text="✦", bg=c["card"], fg=c["accent_hover"],
                 font=(self.ui_font, 12, "bold")).pack(side="left", padx=(2, 8))
        self.next_reason_var = tk.StringVar(value="")
        self._autowrap(ttk.Label(next_row, textvariable=self.next_reason_var, style="Muted.TLabel",
                                 justify="left")).pack(side="left", fill="x", expand=True)
        self.next_actions_row = tk.Frame(next_row, bg=c["card"])
        self.next_actions_row.pack(side="right", padx=(10, 0))

        top = ttk.Frame(root)
        top.pack(fill="x")
        self.r_layout = top
        left_shell, left = self.card(top, "card.library")
        left_shell.pack(side="left", fill="both", expand=True, padx=(0, 5))
        self.launch_shell, right = self.card(top, "card.launch")
        self.launch_shell.pack(side="left", fill="both", expand=True, padx=(5, 0))
        if not uirules.launch_params_visible(len(self.models)):
            # Пока в библиотеке нет модели, параметров запуска нет вовсе.
            self.launch_shell.pack_forget()

        # --- Библиотека -------------------------------------------------------
        self.model_var = tk.StringVar(value=self.config_data.get("selected", ""))
        self.model_combo = ttk.Combobox(left, textvariable=self.model_var, state="readonly")
        self.model_combo.pack(fill="x", pady=(0, 7))
        btns = ttk.Frame(left, style="Card.TFrame")
        btns.pack(fill="x")
        add_button = self.rbutton(btns, self.t("lib.add"), self.add_model)
        add_button.pack(side="left")
        self._bind_text(add_button, "lib.add")
        remove_button = self.rbutton(btns, self.t("lib.remove"), self.remove_model,
                                     variant="danger")
        remove_button.pack(side="left", padx=5)
        self._bind_text(remove_button, "lib.remove")
        hf_button = self.rbutton(btns, self.t("lib.download_hf"), self.open_hf_dialog)
        hf_button.pack(side="left")
        self._bind_text(hf_button, "lib.download_hf")
        self.model_info = tk.StringVar(value=self.t("lib.hint"))
        self._autowrap(ttk.Label(left, textvariable=self.model_info, style="Hint.TLabel", justify="left")).pack(fill="x", anchor="w", pady=(7, 0))

        self.format_var = tk.StringVar(
            value=self.t("lib.format", value=self.t("lib.format_unknown")))
        self.engine_var = tk.StringVar(
            value=self.t("lib.engine", value=self.t("lib.engine_none")))
        self.engine_status_var = tk.StringVar(value=self.t("lib.select_model"))
        self.engine_summary_var = tk.StringVar(value="")
        def update_engine_summary(*_):
            self.engine_summary_var.set(f"{self.format_var.get()}  ·  {self.engine_var.get()}")
        self.format_var.trace_add("write", update_engine_summary)
        self.engine_var.trace_add("write", update_engine_summary)
        update_engine_summary()
        self._autowrap(ttk.Label(left, textvariable=self.engine_summary_var, style="Muted.TLabel", justify="left")).pack(fill="x", anchor="w", pady=(6, 0))

        # Загрузка и обновление движков спрятаны в сворачиваемый блок.
        self._engine_toggle = tk.Label(left, text="▸  " + self.t("lib.engine_toggle").strip("▸ "),
                                      bg=c["card"],
                                       fg=c["accent_hover"], cursor="hand2",
                                       font=(self.ui_font, 9, "bold"))
        self._engine_toggle.pack(anchor="w", pady=(8, 0))
        self._engine_toggle.bind("<Button-1>", lambda _e: self._set_engine_section(
            not self._engine_section_open))
        self._engine_section_open = False
        engine_box = ttk.Frame(left, style="Card.TFrame")
        self.engine_box = engine_box
        self._autowrap(ttk.Label(engine_box, textvariable=self.engine_status_var, style="Hint.TLabel", justify="left")).pack(fill="x", anchor="w", pady=(6, 5))
        self.engine_choice_var = tk.StringVar(value="llama.cpp")
        self.engine_choice = ttk.Combobox(
            engine_box, textvariable=self.engine_choice_var, state="readonly",
            values=["llama.cpp", "Ollama", "Transformers + PyTorch", "ONNX Runtime"]
        )
        self.engine_choice.pack(fill="x", pady=(0, 6))
        self.engine_choice.bind("<<ComboboxSelected>>", self.on_engine_choice)
        engine_actions = ttk.Frame(engine_box, style="Card.TFrame")
        engine_actions.pack(fill="x")
        self.engine_button = self.rbutton(
            engine_actions, self.t("lib.engine_download"),
            self.download_selected_engine, state="disabled")
        self.engine_button.pack(side="left")
        self.cancel_download_button = self.rbutton(
            engine_actions, self.t("lib.engine_stop"), self.cancel_engine_download,
            state="disabled")
        self.cancel_download_button.pack(side="left", padx=(5, 0))
        self.update_check_button = self.rbutton(
            engine_actions, self.t("lib.engine_updates"), self.check_engine_updates)
        self.update_check_button.pack(side="left", padx=(5, 0))
        self.download_status_var = tk.StringVar(value=self.t("lib.engine_idle"))
        self._autowrap(ttk.Label(engine_box, textvariable=self.download_status_var,
                                 style="Hint.TLabel")).pack(fill="x", anchor="w", pady=(6, 3))
        self.download_progress = ttk.Progressbar(engine_box, mode="determinate", maximum=100)
        self.download_progress.pack(fill="x")
        self.model_combo.bind("<<ComboboxSelected>>", self.on_model_change)

        # --- Параметры запуска (скрыты, пока в библиотеке нет ни одной модели) --
        self.param_widgets = {}
        self.available_contexts = list(DEFAULT_CONTEXTS)
        self.ctx_var = tk.StringVar(value="4096")
        self.max_tokens_var = tk.StringVar(value="1024")
        self.recommendation = None

        host = tk.Frame(right, bg=c["card"])
        host.pack(fill="x")
        host.columnconfigure(0, weight=1)

        ctx_block = tk.Frame(host, bg=c["card"])
        ctx_label = ttk.Label(ctx_block, text=self.t("launch.ctx"), style="Hint.TLabel")
        ctx_label.pack(anchor="w")
        self._bind_text(ctx_label, "launch.ctx")
        ctx_row = tk.Frame(ctx_block, bg=c["card"])
        ctx_row.pack(fill="x", pady=(2, 0))
        self.ctx_scale = ContextSlider(
            ctx_row, from_=0, to=len(DEFAULT_CONTEXTS) - 1,
            command=self.on_ctx_scale, bg=c["card"],
            accent=c["accent"], accent_hover=c["accent_hover"],
            track=c["field"])
        self.ctx_scale.pack(side="left", fill="x", expand=True)
        self.ctx_entry = tk.Entry(
            ctx_row, textvariable=self.ctx_var, width=7, justify="right",
            bg=c["field"], fg=c["accent_hover"], insertbackground=c["text"],
            relief="flat", bd=0, highlightthickness=1,
            highlightbackground=c["border"], highlightcolor=c["accent"],
            font=(self.ui_font, 10, "bold"))
        self.ctx_entry.pack(side="right", padx=(8, 0), ipady=4)
        self.ctx_entry.bind("<Return>", self.apply_manual_context)
        self.ctx_entry.bind("<FocusOut>", self.apply_manual_context)
        self.ctx_scale.set(self.available_contexts.index("4096"))
        self._bind_text(ToolTip(self.ctx_scale, self.t("launch.ctx_tooltip"), c),
                        "launch.ctx_tooltip", kind="tooltip")
        self.context_hint_var = tk.StringVar(value=self.t("launch.ctx_hint_default"))
        hint_row = tk.Frame(ctx_block, bg=c["card"])
        hint_row.pack(fill="x", pady=(2, 0))
        ctx_hint = tk.Label(hint_row, textvariable=self.context_hint_var, bg=c["card"],
                            fg=c["muted"], justify="left", anchor="w",
                            font=(self.ui_font, 8))
        self._autowrap(ctx_hint).pack(side="left", fill="x", expand=True)
        self.param_widgets["ctx"] = ctx_block

        speed_block = tk.Frame(host, bg=c["card"])
        self.speed_hint_var = tk.StringVar(value=self.t("launch.speed_default"))
        speed_label = tk.Label(speed_block, textvariable=self.speed_hint_var, bg=c["card"],
                               fg=c["muted"], justify="left", anchor="w",
                               font=(self.ui_font, 8))
        self._autowrap(speed_label).pack(fill="x", anchor="w")

        gpu_block = tk.Frame(host, bg=c["card"])
        gpu_label = ttk.Label(gpu_block, text=self.t("launch.offload"), style="Muted.TLabel")
        gpu_label.pack(side="left")
        self._bind_text(gpu_label, "launch.offload")
        self.gpu_offload_var = tk.BooleanVar(value=True)
        self._gpu_last_layers = "99"
        gpu_switch = NeumorphicSwitch(gpu_block, self.gpu_offload_var,
                                      command=self.toggle_gpu_offload, bg=c["card"])
        gpu_switch.pack(side="right")
        self._bind_text(ToolTip(gpu_switch, self.t("launch.offload_tip"), c),
                        "launch.offload_tip", kind="tooltip")
        self.param_widgets["gpu_offload"] = gpu_block

        self.gpu_var = tk.StringVar(value="99")
        self.threads_var = tk.StringVar(value=str(os.cpu_count() or 4))
        self.port_var = tk.StringVar(value=str(self.config_data.get("port", 8080)))
        fields = tk.Frame(host, bg=c["card"])
        layers_cell = tk.Frame(fields, bg=c["card"])
        layers_cell.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        port_cell = tk.Frame(fields, bg=c["card"])
        port_cell.grid(row=0, column=1, sticky="ew")
        fields.columnconfigure(0, weight=1, uniform="params")
        fields.columnconfigure(1, weight=1, uniform="params")
        for cell, label_key, var, tip_key in (
                (layers_cell, "launch.layers", self.gpu_var, "launch.layers_tip"),
                (port_cell, "launch.port", self.port_var, "launch.port_tip")):
            cell_label = ttk.Label(cell, text=self.t(label_key), style="Hint.TLabel")
            cell_label.pack(anchor="w")
            self._bind_text(cell_label, label_key)
            entry = ttk.Entry(cell, textvariable=var, style="Compact.TEntry")
            entry.pack(fill="x", pady=(3, 0))
            self._bind_text(ToolTip(entry, self.t(tip_key), c), tip_key, kind="tooltip")
        self.param_widgets["gpu_layers_cell"] = layers_cell
        self.param_widgets["port"] = port_cell

        self._advanced_open = False
        self.advanced_toggle = tk.Label(hint_row, text="▸  " + self.t("launch.advanced"),
                                        bg=c["card"], fg=c["accent_hover"], cursor="hand2",
                                        font=(self.ui_font, 8, "bold"))
        self.advanced_toggle.pack(side="right", padx=(8, 0))
        self.advanced_toggle.bind("<Button-1>",
                                  lambda _e: self._set_advanced(not self._advanced_open))
        self.advanced_box = tk.Frame(host, bg=c["card"])
        self.advanced_inner = tk.Frame(self.advanced_box, bg=c["card"])
        adv_fields = tk.Frame(self.advanced_inner, bg=c["card"])
        adv_fields.pack(fill="x")
        threads_cell = tk.Frame(adv_fields, bg=c["card"])
        threads_cell.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        tokens_cell = tk.Frame(adv_fields, bg=c["card"])
        tokens_cell.grid(row=0, column=1, sticky="ew")
        adv_fields.columnconfigure(0, weight=1, uniform="adv")
        adv_fields.columnconfigure(1, weight=1, uniform="adv")
        for cell, label_key, var, tip_key in (
                (threads_cell, "launch.threads", self.threads_var, "launch.threads_tip"),
                (tokens_cell, "launch.max_tokens", self.max_tokens_var,
                 "launch.max_tokens_tip")):
            cell_label = ttk.Label(cell, text=self.t(label_key), style="Hint.TLabel")
            cell_label.pack(anchor="w")
            self._bind_text(cell_label, label_key)
            entry = ttk.Entry(cell, textvariable=var, style="Compact.TEntry")
            entry.pack(fill="x", pady=(3, 0))
            self._bind_text(ToolTip(entry, self.t(tip_key), c), tip_key, kind="tooltip")
        self.param_widgets["threads"] = threads_cell
        self.param_widgets["max_tokens"] = tokens_cell

        ctx_block.grid(row=0, column=0, sticky="ew", pady=(0, 2))
        speed_block.grid(row=1, column=0, sticky="ew", pady=(0, 4))
        gpu_block.grid(row=2, column=0, sticky="ew", pady=(0, 2))
        fields.grid(row=3, column=0, sticky="ew", pady=(0, 2))
        self.advanced_box.grid(row=4, column=0, sticky="ew", pady=(0, 2))


        # --- Память и движок --------------------------------------------------
        mid = ttk.Frame(root)
        mid.pack(fill="x", pady=2)
        ram_shell, ram = self.card(mid, "card.memory")
        ram_shell.pack(side="left", fill="both", expand=True, padx=(0, 5))
        self.ram_var = tk.StringVar(value=self.t("mem.checking"))
        ttk.Label(ram, textvariable=self.ram_var, style="Body.TLabel").pack(anchor="w")
        self.estimate_var = tk.StringVar(value=self.t("mem.pick_model"))
        self._autowrap(ttk.Label(ram, textvariable=self.estimate_var, style="Hint.TLabel", justify="left")).pack(fill="x", anchor="w", pady=(4, 0))
        self.hardware_var = tk.StringVar(value=self.t("mem.hardware_hint"))
        self._autowrap(ttk.Label(ram, textvariable=self.hardware_var, style="Hint.TLabel", justify="left")).pack(fill="x", anchor="w", pady=(6, 0))
        score_row = tk.Frame(ram, bg=c["card"])
        score_row.pack(fill="x", pady=(6, 0))
        self.system_score_var = tk.StringVar(value=self.t("mem.score", value=self.t("common.none")))
        tk.Label(score_row, textvariable=self.system_score_var, bg=c["card"],
                 fg=c["accent_hover"], font=(self.ui_font, 11, "bold")).pack(side="left")
        self.system_score_bar = ttk.Progressbar(ram, mode="determinate", maximum=100, value=0)
        self.system_score_bar.pack(fill="x", pady=(4, 0))
        self.compatibility_var = tk.StringVar(
            value="•  " + self.t("mem.compat_model"))
        self.compatibility_label = tk.Label(
            ram, textvariable=self.compatibility_var, bg=c["card"],
            fg=c["muted"], justify="left", anchor="w",
            font=(self.ui_font, 9, "bold"), wraplength=430
        )
        self._autowrap(self.compatibility_label)
        self.compatibility_label.pack(fill="x", pady=(6, 0))
        self.compatibility_note_var = tk.StringVar(value="")
        self._autowrap(ttk.Label(ram, textvariable=self.compatibility_note_var, style="Hint.TLabel", justify="left")).pack(fill="x", anchor="w", pady=(2, 0))
        ram_actions = tk.Frame(ram, bg=c["card"])
        ram_actions.pack(fill="x", pady=(8, 0))
        analyze_button = self.rbutton(ram_actions, self.t("mem.analyze"), self.analyze_system)
        analyze_button.pack(side="left")
        self._bind_text(analyze_button, "mem.analyze")
        requirements_button = self.rbutton(ram_actions, self.t("mem.requirements"),
                                           self.check_requirements)
        requirements_button.pack(side="left", padx=5)
        self._bind_text(requirements_button, "mem.requirements")
        ram_button = self.rbutton(ram_actions, self.t("mem.refresh"), self.refresh_ram)
        ram_button.pack(side="left")
        self._bind_text(ram_button, "mem.refresh")

        mid_right = ttk.Frame(mid)
        mid_right.pack(side="left", fill="both", expand=True, padx=(5, 0))
        srv_shell, srv = self.card(mid_right, "card.engine")
        srv_shell.pack(fill="x")
        self.server_var = tk.StringVar(value=self.config_data.get("server", ""))
        ent = ttk.Frame(srv, style="Card.TFrame")
        ent.pack(fill="x")
        self.server_entry = tk.Entry(
            ent, textvariable=self.server_var, relief="flat", bd=0,
            bg=c["field"], fg=c["text"],
            insertbackground=c["text"], selectbackground=c["accent"],
            selectforeground="#171225", highlightthickness=1,
            highlightbackground=c["border"], highlightcolor=c["accent"],
            font=(self.ui_font, 9)
        )
        self.server_entry.pack(side="left", fill="x", expand=True, ipady=6, padx=(0, 6))
        browse_server = self.rbutton(ent, self.t("common.browse"), self.choose_server)
        browse_server.pack(side="left")
        self._bind_text(browse_server, "common.browse")
        # Полный путь виден в поле и во всплывающей подсказке; отдельная строка-дубликат не нужна.
        self.server_path_preview = tk.StringVar(
            value=self.server_var.get() or self.t("engine.path_missing"))
        self.server_path_label = ttk.Label(
            srv, textvariable=self.server_path_preview, style="Hint.TLabel",
            wraplength=430, justify="left"
        )
        self._bind_text(ToolTip(self.server_entry, self.t("engine.path_tip"), c),
                        "engine.path_tip", kind="tooltip")
        self.server_var.trace_add(
            "write",
            lambda *_: self.server_path_preview.set(
                self.server_var.get() or self.t("engine.path_missing")
            )
        )
        self.server_var.trace_add("write", lambda *_: self.refresh_next_actions())
        srv_actions = tk.Frame(srv, bg=c["card"])
        srv_actions.pack(fill="x", pady=(8, 0))
        find_button = self.rbutton(srv_actions, self.t("engine.find"), self.auto_find_server)
        find_button.pack(side="left")
        self._bind_text(find_button, "engine.find")
        gpu_button = self.rbutton(srv_actions, self.t("engine.check_gpu"),
                                  self.check_gpu_support)
        gpu_button.pack(side="left", padx=5)
        self._bind_text(gpu_button, "engine.check_gpu")

        op_shell, op = self.card(mid_right, "card.opencode", stretch=True)
        op_shell.pack(fill="both", expand=True, pady=(6, 0))
        export_button = self.rbutton(op, self.t("oc.export"), self.export_to_opencode,
                                     variant="export", pad=(16, 8))
        export_button.pack(fill="x")
        self._bind_text(export_button, "oc.export")
        self._autowrap(ttk.Label(
            op, text=self.t("oc.export_hint"),
            style="Hint.TLabel", justify="left")).pack(fill="x", anchor="w", pady=(5, 8))
        op_row = tk.Frame(op, bg=c["card"])
        op_row.pack(fill="x")
        agents_button = self.rbutton(op_row, self.t("oc.agents"),
                                     self.open_agent_skill_manager)
        agents_button.pack(side="left")
        self._bind_text(agents_button, "oc.agents")
        unexport_button = self.rbutton(op_row, self.t("oc.remove"),
                                       self.remove_from_opencode, variant="danger")
        unexport_button.pack(side="left", padx=5)
        self._bind_text(unexport_button, "oc.remove")

        # Журнал свёрнут в одну строку; клик по заголовку раскрывает полный вывод.
        log_panel = RoundedPanel(root, page=c["page"], fill=c["card"], shadow=c["shadow"],
                                 radius=16, padding=5, lateral=8)
        log_panel.pack(fill="both", expand=False)
        self._log_panel = log_panel
        log_head = tk.Frame(log_panel.inner, bg=c["card"], cursor="hand2")
        log_head.pack(fill="x")
        self._log_toggle = tk.Label(log_head, text="▸  " + self.t("log.title").strip("▸ "),
                                    bg=c["card"], fg=c["accent_hover"],
                                    font=(self.ui_font, 9, "bold"), cursor="hand2")
        self._log_toggle.pack(side="left")
        self.log_last_var = tk.StringVar(value="")
        log_last = tk.Label(log_head, textvariable=self.log_last_var, bg=c["card"], fg=c["muted"],
                            font=(self.ui_font, 8), anchor="w", cursor="hand2")
        log_last.pack(side="left", fill="x", expand=True, padx=(12, 0))
        for widget in (log_head, self._log_toggle, log_last):
            widget.bind("<Button-1>", lambda _e: self._set_log_open(not self._log_open))
        self._log_open = False
        self.log = tk.Text(log_panel.inner, height=7, bg=c["log"], fg="#e5def8",
                           insertbackground=c["accent"], selectbackground="#514477",
                           selectforeground="#ffffff", relief="flat", bd=0,
                           highlightthickness=1, highlightbackground=c["border"],
                           highlightcolor=c["accent"],
                           font=("Consolas", 9), wrap="word", padx=10, pady=7,
                           spacing1=1, spacing3=1)
        self.write_log(self.t("log.line_ready"))
        self.write_log(self.t("log.line_offload"))
        self.write_log(self.t("log.line_build"))
        self.write_log(self.t("log.line_opencode"))
        self.write_log(self.t("log.line_context"))

    @staticmethod
    def _autowrap(label, padding=6):
        """Перенос текста по фактической ширине подписи (без фиксированного wraplength)."""
        def fit(event):
            width = max(120, event.width - padding)
            try:
                if int(float(str(label.cget("wraplength")) or 0)) != width:
                    label.configure(wraplength=width)
            except tk.TclError:
                pass
        label.bind("<Configure>", fit, add="+")
        return label

    def _set_log_open(self, open_):
        """Показывает или прячет полный журнал; в свёрнутом виде видна последняя строка."""
        self._log_open = bool(open_)
        if open_:
            self.log.pack(fill="both", expand=True, pady=(8, 0))
        else:
            self.log.pack_forget()
        self._log_toggle.configure(
            text=self.t("log.title_open" if open_ else "log.title"))
        try:
            self.after(60, lambda: self.scroll_canvas.yview_moveto(1.0) if open_ else None)
        except tk.TclError:
            pass

    def _set_engine_section(self, open_):
        """Раскрывает или сворачивает блок «Движки и загрузка»."""
        self._engine_section_open = bool(open_)
        if open_:
            self.engine_box.pack(fill="x", pady=(2, 0))
        else:
            self.engine_box.pack_forget()
        self._engine_toggle.configure(
            text=self.t("lib.engine_toggle_open" if open_ else "lib.engine_toggle"))

    def _create_scroll_area(self):
        """Прокручиваемая область окна: Canvas + необязательный скроллбар + внутренний фрейм."""
        c = self.colors
        outer = tk.Frame(self, bg=c["page"])
        outer.pack(fill="both", expand=True)
        # yscrollincrement=1: одна "unit" прокрутки = один пиксель, так шаг колеса
        # задаётся в пикселях и не зависит от высоты окна.
        self.scroll_canvas = tk.Canvas(outer, bg=c["page"], bd=0,
                                       highlightthickness=0, takefocus=0,
                                       yscrollincrement=1)
        self.scroll_bar = ttk.Scrollbar(outer, orient="vertical", command=self.scroll_canvas.yview)
        self.scroll_canvas.configure(yscrollcommand=self._on_scroll_range)
        self.scroll_bar.pack(side="right", fill="y")
        self.scroll_canvas.pack(side="left", fill="both", expand=True)

        root = ttk.Frame(self.scroll_canvas, padding=(12, 6, 12, 6))
        self._scroll_window = self.scroll_canvas.create_window((0, 0), window=root, anchor="nw")
        self._scroll_needed = False
        root.bind("<Configure>", self._on_scroll_content)
        self.scroll_canvas.bind("<Configure>", self._on_scroll_canvas)
        # Колесо должно работать над любым дочерним виджетом, а не только над пустым местом.
        self.bind_all("<MouseWheel>", self._on_mousewheel, add="+")
        return root

    def _on_scroll_range(self, first, last):
        first, last = float(first), float(last)
        self._scroll_needed = not (first <= 0.0 and last >= 1.0)
        if self._scroll_needed:
            if not self.scroll_bar.winfo_manager():
                self.scroll_bar.pack(side="right", fill="y")
        elif self.scroll_bar.winfo_manager():
            self.scroll_bar.pack_forget()
        self.scroll_bar.set(first, last)

    def _on_scroll_content(self, _event=None):
        self.scroll_canvas.configure(scrollregion=self.scroll_canvas.bbox("all"))

    def _on_scroll_canvas(self, event):
        # Внутренний фрейм всегда по ширине окна, иначе он «сжимается» до содержимого.
        self.scroll_canvas.itemconfigure(self._scroll_window, width=event.width)

    def _on_mousewheel(self, event):
        widget = event.widget
        # Событие из дочернего окна (чат, агенты, журнал) не должно крутить страницу.
        if isinstance(widget, str) or widget.winfo_toplevel() is not self:
            return None
        # Журнал и выпадающие списки прокручивают себя сами.
        if widget is getattr(self, "log", None) or isinstance(widget, tk.Listbox):
            return None
        if not getattr(self, "_scroll_needed", False):
            return "break"
        units = wheel_scroll_units(getattr(event, "delta", 0))
        if units:
            self._smooth_scroll_by(units)
        return "break"

    def _scroll_metrics(self):
        bbox = self.scroll_canvas.bbox("all")
        top = bbox[1] if bbox else 0
        total = (bbox[3] - bbox[1]) if bbox else 0
        view = max(1, self.scroll_canvas.winfo_height())
        return top, max(1, total), max(0, total - view)

    def _smooth_scroll_by(self, pixels):
        """Инерционная прокрутка: цель накапливается, вид догоняет её с замедлением."""
        top, _total, max_y = self._scroll_metrics()
        current = self.scroll_canvas.canvasy(0) - top
        job = getattr(self, "_scroll_job", None)
        base = self._scroll_target if job is not None else current
        self._scroll_target = min(max_y, max(0.0, base + pixels))
        if job is None:
            self._scroll_job = self.after(0, self._scroll_step)

    def _scroll_step(self):
        self._scroll_job = None
        try:
            top, total, max_y = self._scroll_metrics()
            current = self.scroll_canvas.canvasy(0) - top
            target = min(max_y, max(0.0, self._scroll_target))
            diff = target - current
            if abs(diff) < 0.75:
                self.scroll_canvas.yview_moveto((target + top) / total if total else 0)
                return
            move = diff * 0.24
            if abs(move) < 1:
                move = 1 if diff > 0 else -1
            self.scroll_canvas.yview_moveto((current + move) / total)
            self._scroll_job = self.after(16, self._scroll_step)
        except tk.TclError:
            pass

    LOG_MAX_LINES = 5000

    def write_log(self, text):
        text = str(text).rstrip()
        self.log.insert("end", text + "\n")
        try:
            lines = int(self.log.index("end-1c").split(".")[0])
            if lines > self.LOG_MAX_LINES + 200:
                self.log.delete("1.0", f"{lines - self.LOG_MAX_LINES}.0")
        except (tk.TclError, ValueError):
            pass
        self.log.see("end")
        last = getattr(self, "log_last_var", None)
        if last is not None and text:
            last.set(text.splitlines()[-1][:160])
        # Ошибки не должны оставаться незамеченными в свёрнутом журнале.
        if text.startswith("ОШИБКА") and not getattr(self, "_log_open", True):
            self._set_log_open(True)

    @staticmethod
    def model_display_name(path):
        item = Path(path)
        return item.name + (" [папка]" if item.is_dir() else "")

    def model_display_names(self):
        """Подписи в том же порядке, что и self.models; одинаковые имена дополняются папкой."""
        names = [self.model_display_name(p) for p in self.models]
        duplicates = {name for name in names if names.count(name) > 1}
        return [f"{name} — {Path(path).parent}" if name in duplicates else name
                for name, path in zip(names, self.models)]

    def refresh_models(self):
        self.models = self.config_data.get("models", [])
        self.apply_model_filter(preferred=self.config_data.get("selected", ""))

    def apply_model_filter(self, preferred=None):
        """Обновляет список моделей библиотеки и сохраняет выбранную."""
        # В библиотеке показываем полный список без дополнительного поиска.
        visible = list(self.models)
        self.filtered_models = visible
        names = self.model_display_names()
        self.model_combo["values"] = names
        wanted = self.model_var.get() if preferred is None else preferred
        if wanted in names:
            self.model_var.set(wanted)
        elif names:
            self.model_var.set(names[0])
        else:
            self.model_var.set("")
        self.on_model_change()

    def selected_path(self):
        combo = getattr(self, "model_combo", None)
        if combo is None:
            return None
        try:
            index = list(combo["values"]).index(self.model_var.get())
        except ValueError:
            return None
        return self.models[index] if index < len(self.models) else None

    def add_model(self):
        supported = "*.gguf *.safetensors *.onnx *.pt *.pth *.bin *.ckpt *.ggml"
        paths = filedialog.askopenfilenames(
            title="Выбери файл локальной модели",
            filetypes=[
                ("Файлы моделей", supported),
                ("GGUF", "*.gguf *.ggml"),
                ("Safetensors", "*.safetensors"),
                ("ONNX", "*.onnx"),
                ("PyTorch", "*.pt *.pth *.bin *.ckpt"),
                ("Все файлы", "*.*")
            ]
        )
        added = 0
        skipped_mmproj = 0
        for raw in paths:
            p = str(Path(raw).resolve())
            if is_mmproj_gguf(p):
                skipped_mmproj += 1
                continue
            if Path(p).is_file() and p not in self.models:
                self.models.append(p)
                added += 1
        if added:
            self.write_log(f"Добавлено файлов моделей: {added}")
            self.apply_model_filter(preferred=self.model_display_name(paths[-1]))
            self.save_config()
        if skipped_mmproj:
            messagebox.showwarning(
                "Не модель для запуска",
                "mmproj — это CLIP-проектор для мультимодальной модели. "
                "Выбери основную GGUF-модель; проектор передаётся llama.cpp только через --mmproj."
            )

    def add_model_folder(self):
        folder = filedialog.askdirectory(title="Выбери папку модели (например, Hugging Face)")
        if not folder:
            return
        p = str(Path(folder).resolve())
        if p not in self.models:
            self.models.append(p)
            self.apply_model_filter(preferred=self.model_display_name(p))
            self.save_config()
            self.write_log(f"Добавлена папка модели: {p}")

    def hf_download_dir(self):
        """Папка для скачанных моделей: из конфига или MODELS_DIR внутри данных."""
        configured = str(self.config_data.get("hf_dir") or "").strip()
        return Path(configured) if configured else self.paths.models

    def open_hf_dialog(self):
        dialog = getattr(self, "_hf_dialog", None)
        if dialog is not None and dialog.winfo_exists():
            dialog.lift()
            dialog.focus_force()
            return
        self._hf_dialog = HuggingFaceDialog(self)
        fade_in(self._hf_dialog)

    def register_repo_download(self, repo_id, downloaded):
        """Регистрирует скачанный репозиторий: пакет целиком или отдельные файлы."""
        owner, _, repo = (repo_id or "").partition("/")
        folder = self.paths.models / owner / repo
        records = []
        try:
            if folder.is_dir():
                records = [r for r in discover_models(folder) if r.is_runnable]
        except OSError:
            records = []
        if records:
            for record in sorted(records, key=lambda r: r.size_bytes):
                self.register_downloaded_model(record.path, notify=False)
            best = max(records, key=lambda r: r.size_bytes)
            self.model_var.set(self.model_display_name(best.path))
            self.apply_model_filter(preferred=self.model_display_name(best.path))
            self.save_config()
            self.catalog.add(self._catalog_record(best), persist=True)
            self.show_notification(self.t("lib.downloaded"), Path(best.path).name, "success")
            return
        if downloaded:
            for path in downloaded:
                self.register_downloaded_model(path)

    def _catalog_record(self, record):
        from lamcore.storage import ModelRecord
        return ModelRecord(path=record.path, kind=record.kind, size_bytes=record.size_bytes,
                           files=tuple(record.files), repo=record.repo, source="download")

    def register_downloaded_model(self, path, notify=True):
        """Добавляет скачанную модель в библиотеку. Вспомогательные файлы — нет."""
        p = str(Path(path).resolve())
        info = inspect_model(p)
        if info.is_supporting and Path(p).is_file():
            self.write_log(f"Не добавлен {Path(p).name}: вспомогательный файл, а не модель.")
            if notify:
                self.show_notification(self.t("lib.downloaded"), self.t("errors.not_runnable"),
                                       "warning")
            return
        if Path(p).is_dir() and not info.runnable:
            self.write_log(f"Папка {Path(p).name} не добавлена: неполный набор файлов.")
            if notify:
                self.show_notification(self.t("lib.downloaded"), self.t("lib.duplicate_shard"),
                                       "warning")
            return
        if p not in self.models:
            self.models.append(p)
        from lamcore.storage import ModelRecord
        self.catalog.add(ModelRecord(path=p, kind=info.kind or ModelKind.SINGLE_FILE,
                                     size_bytes=info.size_bytes, files=(Path(p).name,),
                                     repo="", source="download"), persist=True)
        self.apply_model_filter(preferred=self.model_display_name(p))
        self.save_config()
        self.write_log(self.t("lib.downloaded_registered", path=p))
        if notify:
            self.show_notification(self.t("lib.downloaded"), Path(p).name, "success")

    def assess_requirements_for_size(self, model_gb):
        """Оценка требований по размеру файла — работает и до скачивания модели."""
        info = self.system_hardware or {}
        total = info.get("ram_total")
        free = info.get("ram_free")
        if total is None or free is None:
            free, total = available_ram_gb()
        try:
            ctx = int(self.ctx_var.get())
        except (TypeError, ValueError):
            ctx = 4096
        max_vram = max((g.get("vram") or 0 for g in info.get("gpus", [])), default=0)
        try:
            gpu_layers = int(self.gpu_var.get())
        except (TypeError, ValueError):
            gpu_layers = 0
        return assess_model_requirements(model_gb, ctx, total, free, max_vram,
                                         self.gpu_offload_var.get(), gpu_layers)

    def opencode_export_state(self, path):
        """Состояние записи модели в OpenCode: none / exported / stale."""
        if not path:
            return "none"
        record = (self.config_data.get("opencode_exports") or {}).get(self.model_alias())
        if not record:
            return "none"
        try:
            ctx = int(self.ctx_var.get())
            port = int(self.port_var.get())
        except (TypeError, ValueError):
            return "exported"
        if record.get("ctx") != ctx or record.get("port") != port:
            return "stale"
        return "exported"

    def remember_opencode_export(self, alias, exported=True):
        records = self.config_data.setdefault("opencode_exports", {})
        if not exported:
            records.pop(alias, None)
            return
        try:
            records[alias] = {"ctx": int(self.ctx_var.get()),
                              "port": int(self.port_var.get())}
        except (TypeError, ValueError):
            records[alias] = {}

    def _next_action_state(self):
        """Собирает состояние приложения для выбора контекстного действия."""
        path = self.selected_path()
        has_model = bool(path and Path(path).exists())
        is_gguf = bool(has_model and Path(path).is_file()
                       and Path(path).suffix.lower() in (".gguf", ".ggml"))
        server = (self.server_var.get() or "").strip()
        server_ready = bool(server) and Path(server).is_file()
        running = bool(self.process and self.process.poll() is None)
        memory_issue = False
        if is_gguf:
            try:
                model_gb = Path(path).stat().st_size / (1024 ** 3)
                verdict = self.assess_requirements_for_size(model_gb)["verdict"]
                memory_issue = verdict in ("limited", "fail")
            except Exception:
                memory_issue = False
        return {
            "has_model": has_model,
            "is_gguf": is_gguf,
            "server_ready": server_ready,
            "server_running": running,
            "memory_issue": memory_issue,
            "export_state": self.opencode_export_state(path) if is_gguf else "none",
        }

    def _next_action_handlers(self):
        return {
            "add_model": self.add_model,
            "hf_dialog": self.open_hf_dialog,
            "choose_server": self.choose_server,
            "download_llama": self.download_selected_engine,
            "start": self.start_model,
            "stop": self.stop_model,
            "gpu_check": self.check_gpu_support,
            "chat": self.open_chat,
            "web_ui": self.open_chat,
            "api_check": self.check_api,
            "recommendations": self.show_recommendations,
            "smaller_ctx": self.reduce_context,
            "gpu_setup": self.focus_gpu_settings,
            "export": self.export_to_opencode,
            "re_export": self.export_to_opencode,
            "unexport": self.remove_from_opencode,
        }

    def refresh_next_actions(self):
        """Перерисовывает блок «Следующее действие» под текущее состояние."""
        row = getattr(self, "next_actions_row", None)
        if row is None:
            return
        plan = choose_next_actions(self._next_action_state())
        for widget in row.winfo_children():
            widget.destroy()
        self.next_reason_var.set(self.t(plan.get("reason_id") or plan["reason"]))
        handlers = self._next_action_handlers()
        primary = plan["primary"]
        self.rbutton(row, self.t(primary.get("key") or primary["label"]),
                     handlers.get(primary["id"], lambda: None),
                     variant="accent", pad=(16, 7)).pack(side="left")
        for item in plan["secondary"][:2]:
            self.rbutton(row, self.t(item.get("key") or item["label"]),
                         handlers.get(item["id"], lambda: None), pad=(12, 7)).pack(
                side="left", padx=(6, 0))

    def check_api(self):
        """Быстрая проверка HTTP API запущенного сервера."""
        try:
            port = int(self.port_var.get())
        except (TypeError, ValueError):
            messagebox.showerror("Проверка API", "Проверь номер порта.")
            return
        url = f"http://127.0.0.1:{port}/v1/models"
        # Зависший сервер держит ответ до тайм-аута: окно не должно ждать его.
        threading.Thread(target=self._check_api_worker, args=(url,), daemon=True).start()

    def _check_api_worker(self, url):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "Local-AI-Manager"})
            with urllib.request.urlopen(request, timeout=4) as response:
                payload = json.loads(response.read().decode("utf-8", "replace"))
            models = [item.get("id") for item in payload.get("data", []) if isinstance(item, dict)]
            text = "Модели: " + (", ".join(str(m) for m in models) if models else "список пуст")
            self._ui_event("call", lambda: self._show_api_result(url, text, None))
        except Exception as exc:
            # str(exc) нужно вычислить сразу: в Python 3 имя exc удаляется при
            # выходе из except, и лямбда падала с NameError вместо текста ошибки.
            error = str(exc)
            self._ui_event("call", lambda: self._show_api_result(url, None, error))

    def _show_api_result(self, url, text, error):
        if error:
            self.write_log(f"Проверка API {url}: ошибка — {error}")
            self.show_notification("API не отвечает", error, "error")
        else:
            self.write_log(f"Проверка API {url}: OK. {text}")
            self.show_notification("API отвечает", text, "success")

    def reduce_context(self):
        """Уменьшает контекст на один шаг ползунка."""
        index = self.ctx_scale.get()
        if index > 0:
            self.ctx_scale.set(index - 1)
            self.write_log(f"Контекст уменьшен до {self.ctx_var.get()} токенов.")
            self.show_notification("Контекст уменьшен",
                                   f"Теперь {self.ctx_var.get()} токенов — KV-кэш займёт меньше памяти.",
                                   "info")
        else:
            self.show_notification("Меньше некуда",
                                   "Контекст уже минимальный: 2048 токенов.", "info")

    def focus_gpu_settings(self):
        """Переводит фокус на настройки GPU offload и включает его."""
        try:
            self.scroll_canvas.yview_moveto(0.0)
        except Exception:
            pass
        if not self.gpu_offload_var.get():
            self.toggle_gpu_offload()
        self.write_log("GPU offload включён: проверь число слоёв (-ngl) и объём VRAM.")

    def show_recommendations(self):
        """Спокойные рекомендации, если по памяти модель не помещается."""
        path = self.selected_path()
        assessment = None
        if path and Path(path).is_file():
            size_gb = Path(path).stat().st_size / (1024 ** 3)
            assessment = self.assess_requirements_for_size(size_gb)
        info = self.system_hardware or {}
        free, total = info.get("ram_free"), info.get("ram_total")
        vram = max((gpu.get("vram") or 0 for gpu in info.get("gpus", [])), default=0)
        lines = ["Что можно сделать, если памяти не хватает:", "",
                 "1. Уменьшить контекст: KV-кэш растёт вместе с ним.",
                 "2. Взять меньший квант (Q4_K_M вместо Q8_0) — файл меньше в 1.5–2 раза.",
                 "3. Включить GPU offload и поднять число слоёв, если есть запас VRAM.",
                 "4. Закрыть другие программы, которые держат оперативную память."]
        if assessment:
            lines += ["", f"Расчёт: нужно примерно {assessment['required_gb']:.1f} GB RAM."]
            if free is not None:
                lines.append(f"Свободно RAM: {free:.1f} GB из {total:.1f} GB.")
            lines.append(f"Обнаружено VRAM: {vram:.1f} GB.")
            lines += ["", assessment["message"]]
        lines += ["", "Это ориентир по размещению в памяти, а не прогноз скорости генерации."]
        self.write_log("Открыты рекомендации по памяти.")
        messagebox.showinfo("Рекомендации", "\n".join(lines))

    def show_styled_advice(self, title, lines, accent="info"):
        """Тёмное скруглённое окно рекомендаций вместо стандартного Windows messagebox."""
        win = tk.Toplevel(self)
        win.title(title)
        win.configure(bg=self.colors["page"])
        win.transient(self)
        win.resizable(True, True)
        win.minsize(470, 330)
        win.geometry("590x470")
        win.grab_set()
        c = self.colors
        wrap = tk.Frame(win, bg=c["page"], padx=18, pady=16)
        wrap.pack(fill="both", expand=True)
        panel = RoundedPanel(wrap, page=c["page"], fill=c["card"],
                             shadow=c["shadow"], radius=22, padding=16,
                             lateral=7, stretch=True)
        panel.pack(fill="both", expand=True)
        head = tk.Frame(panel.inner, bg=c["card"])
        head.pack(fill="x", pady=(0, 12))
        dot_color = c.get("success", "#62d69a") if accent == "success" else c["accent"]
        tk.Label(head, text="✦", bg=c["card"], fg=dot_color,
                 font=(self.ui_font, 16, "bold")).pack(side="left", padx=(0, 9))
        tk.Label(head, text=title, bg=c["card"], fg=c["text"],
                 font=(self.ui_font, 12, "bold")).pack(side="left", anchor="center")
        body = tk.Text(panel.inner, bg=c["field"], fg=c["text"], relief="flat",
                       bd=0, wrap="word", font=(self.ui_font, 10, "bold"),
                       padx=14, pady=13, spacing1=3, spacing3=5,
                       highlightthickness=1, highlightbackground=c["border"])
        body.pack(fill="both", expand=True)
        body.insert("1.0", "\n".join(lines))
        body.configure(state="disabled")
        footer = tk.Frame(panel.inner, bg=c["card"])
        footer.pack(fill="x", pady=(12, 0))
        RoundedButton(footer, "ПОНЯТНО", win.destroy, c,
                      width=118, pad=(14, 7)).pack(side="right")
        win.bind("<Escape>", lambda _e: win.destroy())
        win.bind("<Return>", lambda _e: win.destroy())
        win.update_idletasks()
        x = self.winfo_rootx() + max(0, (self.winfo_width()-win.winfo_width())//2)
        y = self.winfo_rooty() + max(0, (self.winfo_height()-win.winfo_height())//2)
        win.geometry(f"+{x}+{y}")
        return win

    def check_requirements(self):
        """Явная проверка системных требований выбранной модели."""
        path = self.selected_path()
        if not path or not Path(path).is_file() or Path(path).suffix.lower() not in (".gguf", ".ggml"):
            messagebox.showinfo("Требования модели",
                                "Сначала выбери GGUF-модель в библиотеке.")
            return
        self.refresh_ram()
        self.update_system_score()
        size_gb = Path(path).stat().st_size / (1024 ** 3)
        assessment = self.assess_requirements_for_size(size_gb)
        info = self.system_hardware or {}
        free, total = info.get("ram_free"), info.get("ram_total")
        ram_line = ("Windows не вернула данные о свободной RAM."
                    if free is None else f"Свободно RAM: {free:.1f} GB из {total:.1f} GB.")
        lines = [
            Path(path).name,
            f"Размер файла: {size_gb:.2f} GB, контекст: {self.ctx_var.get()} токенов.",
            "",
            f"Нужно примерно {assessment['required_gb']:.1f} GB RAM:",
            f"  модель {size_gb:.1f} GB + KV-кэш ~{assessment['kv_gb']:.1f} GB "
            f"+ запас {assessment['overhead_gb']:.1f} GB.",
        ]
        if assessment["gpu_weight_gb"]:
            lines.append(f"  на GPU учтено до {assessment['gpu_weight_gb']:.1f} GB весов.")
        lines += ["", ram_line, "", assessment["message"], "",
                  "Это оценка размещения в памяти, не прогноз скорости генерации."]
        self.write_log("Проверка требований: " + assessment["message"])
        messagebox.showinfo("Требования модели", "\n".join(lines))

    def analyze_model(self, raw_path):
        """Совместимость со старым API: описание модели из новых метаданных.

        Раньше формат угадывался по расширению и структуре папки; теперь вывод
        строится из ModelInfo и проверки совместимости адаптером, чтобы UI и
        старые вызовы не расходились.
        """
        info = inspect_model(raw_path)
        if info.is_supporting:
            return {"format": info.summary(), "engine": self.t("lib.engine_unknown"),
                    "urls": [], "supported": False, "note": self.t("lib.note_supporting")}
        if info.completeness == "incomplete":
            files = ", ".join(info.missing_files[:4]) or self.t("common.unknown")
            return {"format": info.summary(), "engine": "llama.cpp", "urls": [],
                    "supported": False,
                    "note": self.t("lib.note_incomplete", files=files)}
        if info.format in ("gguf", "ggml"):
            adapter = self.engine_manager.adapter("llama.cpp", self.server_var.get().strip())
            verdict = adapter.can_run(info)
            key = f"compat.{verdict.code}"
            reason = self.t(key, detail=verdict.detail)
            return {"format": info.summary(), "engine": "llama.cpp",
                    "urls": ["https://github.com/ggml-org/llama.cpp/releases/latest"],
                    "supported": verdict.status == "compatible",
                    "note": verdict.detail if reason == key else reason}
        engine = {"safetensors": "Transformers + PyTorch",
                  "onnx": "ONNX Runtime"}.get(info.format, self.t("lib.engine_unknown"))
        return {"format": info.summary(), "engine": engine, "urls": [], "supported": False,
                "note": self.t("lib.note_non_gguf", format=info.format)}

    def on_engine_choice(self, event=None):
        engine = self.engine_choice_var.get()
        descriptions = {
            "llama.cpp": "lib.engine_desc_llama",
            "Ollama": "lib.engine_desc_ollama",
            "Transformers + PyTorch": "lib.engine_desc_transformers",
            "ONNX Runtime": "lib.engine_desc_onnx",
        }
        self.engine_status_var.set(self.t(descriptions.get(engine, "lib.engine_chosen")))
        self.engine_button.configure(state="normal")
        self._apply_engine_visibility()

    def download_selected_engine(self):
        engine = self.engine_choice_var.get()
        if not engine:
            return
        if not messagebox.askyesno(
            "Подтвердить загрузку",
            f"Загрузить и подготовить движок «{engine}»?\n\n"
            "Файлы будут сохранены в папке LocalAIManager внутри AppData. "
            "Для Ollama после загрузки откроется штатный установщик."
        ):
            return
        self._set_engine_section(True)
        self.download_cancel_event = threading.Event()
        self.download_process = None
        self.engine_button.configure(state="disabled")
        self.cancel_download_button.configure(state="normal")
        self.engine_choice.configure(state="disabled")
        self.download_progress.stop()
        self.download_progress.configure(mode="determinate", value=0)
        self.download_status_var.set("Подготовка загрузки…")
        self.set_status("busy", "status.downloading", name=engine)
        self.download_thread = threading.Thread(
            target=self._engine_download_worker, args=(engine,), daemon=True
        )
        self.download_thread.start()

    def cancel_engine_download(self):
        if not self.download_cancel_event or self.download_cancel_event.is_set():
            return
        self.download_cancel_event.set()
        proc = self.download_process
        if proc and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass
        self.cancel_download_button.configure(state="disabled")
        self.download_status_var.set("Остановка загрузки…")
        self.set_status("busy", "status.cancelled")

    def _check_download_cancelled(self, cancel_event=None):
        event = cancel_event if cancel_event is not None else self.download_cancel_event
        if event and event.is_set():
            raise DownloadCancelled("Загрузка остановлена пользователем.")

    def _ui_event(self, kind, payload):
        self.log_queue.put(("__ui__", kind, payload))

    def _download_url(self, url, destination, label, progress=None, cancel_event=None):
        emit = progress if callable(progress) else (lambda payload: self._ui_event("progress", payload))
        self._check_download_cancelled(cancel_event)
        def report(done, total):
            percent = min(100, done * 100 / total) if total else 0
            label_text = (f"{label}: {done/1024**2:.1f} / {total/1024**2:.1f} MB"
                          if total else f"{label}: скачано {done/1024**2:.1f} MB")
            emit((percent, label_text))
        return stream_download_resumable(
            url, destination, timeout=60,
            cancel_event=cancel_event if cancel_event is not None else self.download_cancel_event,
            progress=report
        )

    def _download_llama_cpp(self):
        self._check_download_cancelled()
        headers = {"User-Agent": APP_USER_AGENT, "Accept": "application/vnd.github+json"}
        gpu_text = " ".join(g.get("name", "") for g in (self.system_hardware or {}).get("gpus", [])).lower()
        release = asset = None
        # /releases/latest может указывать на релиз без Windows-сборок (например, тег v0.8.0),
        # поэтому смотрим несколько последних и берём первый с подходящим архивом.
        req = urllib.request.Request(
            "https://api.github.com/repos/ggml-org/llama.cpp/releases?per_page=5", headers=headers)
        with urllib.request.urlopen(req, timeout=15, context=create_ssl_context()) as response:
            for item in json.loads(response.read().decode("utf-8")):
                asset = pick_llama_cpp_asset(item.get("assets"), gpu_name=gpu_text)
                if asset:
                    release = item
                    break
        if not asset:
            raise RuntimeError("GitHub не вернул Windows x64 ZIP-архив llama.cpp ни в одном из последних релизов.")
        self._check_download_cancelled()
        engine_dir = APP_DIR / "engines" / "llama.cpp"
        engine_dir.mkdir(parents=True, exist_ok=True)
        archive = APP_DIR / "downloads" / asset["name"]
        self._download_url(asset["browser_download_url"], archive, "llama.cpp")
        expected = (asset.get("digest") or "").strip().lower()
        if expected.startswith("sha256:"):
            digest = hashlib.sha256()
            with open(archive, "rb") as downloaded:
                for block in iter(lambda: downloaded.read(1024 * 1024), b""):
                    digest.update(block)
            if digest.hexdigest() != expected.split(":", 1)[1]:
                archive.unlink(missing_ok=True)
                raise RuntimeError("SHA256 архива llama.cpp не совпал с digest из GitHub — файл удалён.")
        self._ui_event("status", "Распаковка llama.cpp…")
        with zipfile.ZipFile(archive) as zf:
            root = engine_dir.resolve()
            for member in zf.infolist():
                self._check_download_cancelled()
                target = (engine_dir / member.filename).resolve()
                if root not in target.parents and target != root:
                    raise RuntimeError("Архив содержит небезопасный путь, распаковка отменена.")
            zf.extractall(engine_dir)
        servers = list(engine_dir.rglob("llama-server.exe"))
        if not servers:
            raise RuntimeError("Архив распакован, но llama-server.exe внутри не найден.")
        servers.sort(key=lambda x: (0 if x.parent.name.lower() == "bin" else 1, len(str(x))))
        chosen = servers[0]
        self._ui_event("llama_ready", str(chosen))
        return f"llama.cpp установлен ({release.get('tag_name', '')}, {asset['name']}): {chosen}"

    def _download_ollama(self):
        installer = APP_DIR / "engines" / "OllamaSetup.exe"
        # Имя файла постоянное: остатки прошлой версии нельзя докачивать поверх новой.
        for stale in (installer, Path(str(installer) + ".part")):
            try:
                stale.unlink(missing_ok=True)
            except OSError:
                pass
        self._download_url("https://ollama.com/download/OllamaSetup.exe", installer, "Ollama")
        self._check_download_cancelled()
        self._ui_event("status", "Загрузка завершена. Запуск официального установщика Ollama…")
        subprocess.Popen([str(installer)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         close_fds=True, **hidden_process_kwargs())
        return f"Установщик Ollama скачан и запущен: {installer}"

    def _run_pip_install(self, env_dir, packages, label, extra_args=None):
        self._check_download_cancelled()
        env_dir = Path(env_dir)
        python_exe = env_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        if not python_exe.exists():
            if getattr(sys, "frozen", False):
                # В EXE sys.executable — это само приложение: venv перезапустил бы GUI.
                raise RuntimeError(
                    "Сборка EXE не содержит Python: отдельное окружение создать нельзя. "
                    "Установи Python 3.11+ и запусти программу из исходников (local_ai_manager.py).")
            self._ui_event("status", f"Создание изолированного окружения {label}…")
            subprocess.run([sys.executable, "-m", "venv", str(env_dir)], check=True,
                           capture_output=True, text=True, **hidden_process_kwargs())
        self._check_download_cancelled()
        cmd = [str(python_exe), "-m", "pip", "install", "--upgrade"]
        if extra_args:
            cmd.extend(extra_args)
        cmd.extend(packages)
        self._ui_event("indeterminate", f"Установка {label}. Пакеты скачиваются, это может занять время…")
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8", errors="replace", bufsize=1,
                                **hidden_process_kwargs())
        self.download_process = proc
        try:
            for line in proc.stdout:
                self._check_download_cancelled()
                line = line.strip()
                if line:
                    self.log_queue.put(f"[{label}] {line}")
                    if "Downloading" in line or "Installing collected packages" in line:
                        self._ui_event("status", f"{label}: {line[:100]}")
            code = proc.wait()
            self._check_download_cancelled()
            if code:
                raise RuntimeError(f"pip завершился с кодом {code}. Подробности смотри в журнале.")
        finally:
            if proc.poll() is None:
                try:
                    proc.terminate()
                    proc.wait(timeout=5)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
            self.download_process = None
        return f"{label} установлен в отдельное окружение: {env_dir}"

    def _engine_download_worker(self, engine):
        try:
            self._check_download_cancelled()
            self._ui_event("status", f"Загрузка {engine}…")
            if engine == "llama.cpp":
                result = self._download_llama_cpp()
            elif engine == "Ollama":
                result = self._download_ollama()
            elif engine == "ONNX Runtime":
                result = self._run_pip_install(
                    APP_DIR / "engines" / "venvs" / "onnx",
                    ["onnxruntime"], "ONNX Runtime"
                )
            elif engine == "Transformers + PyTorch":
                env_dir = APP_DIR / "engines" / "venvs" / "transformers"
                self._run_pip_install(
                    env_dir, ["torch"], "PyTorch CPU",
                    extra_args=["--index-url", "https://download.pytorch.org/whl/cpu"]
                )
                result = self._run_pip_install(
                    env_dir, ["transformers", "accelerate", "safetensors"],
                    "Transformers"
                )
            else:
                raise RuntimeError("Неизвестный движок.")
            self._check_download_cancelled()
            self._ui_event("complete", result)
        except DownloadCancelled as exc:
            self._ui_event("cancelled", str(exc))
        except Exception as exc:
            if self.download_cancel_event and self.download_cancel_event.is_set():
                self._ui_event("cancelled", "Загрузка остановлена пользователем.")
            else:
                self._ui_event("failed", str(exc))
        finally:
            self.download_process = None

    def check_engine_updates(self):
        if self.download_thread and self.download_thread.is_alive():
            messagebox.showwarning("Идёт операция", "Сначала дождись завершения или останови текущую загрузку.")
            return
        self._set_engine_section(True)
        self.update_check_button.configure(state="disabled")
        self.download_status_var.set("Проверка обновлений движков…")
        self.set_status("busy", "status.check_updates")
        threading.Thread(target=self._check_engine_updates_worker, daemon=True).start()

    @staticmethod
    def _version_tuple(value):
        numbers = re.findall(r"\d+", str(value or ""))
        return tuple(int(x) for x in numbers[:5]) if numbers else ()

    def _fetch_json(self, url, github=False):
        headers = {"User-Agent": APP_USER_AGENT, "Accept": "application/json"}
        if github:
            headers["Accept"] = "application/vnd.github+json"
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15, context=create_ssl_context()) as response:
            return json.loads(response.read().decode("utf-8"))

    def _local_package_version(self, env_dir, package):
        env_dir = Path(env_dir)
        python_exe = env_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        if not python_exe.exists():
            return None
        try:
            result = subprocess.run(
                [str(python_exe), "-m", "pip", "show", package],
                capture_output=True, text=True, timeout=12, **hidden_process_kwargs()
            )
            for line in result.stdout.splitlines():
                if line.lower().startswith("version:"):
                    return line.split(":", 1)[1].strip()
        except Exception:
            pass
        return None

    def _check_engine_updates_worker(self):
        report = []
        errors = []
        try:
            # llama.cpp: local binary version versus latest GitHub release tag
            llama_path = Path(self.server_var.get().strip()) if self.server_var.get().strip() else None
            if llama_path and llama_path.exists():
                try:
                    local = subprocess.run(
                        [str(llama_path), "--version"], capture_output=True,
                        text=True, timeout=10, **hidden_process_kwargs()
                    )
                    local_text = (local.stdout or local.stderr).strip().splitlines()
                    installed = local_text[0][:100] if local_text else "версия не определена"
                except Exception:
                    installed = "не удалось определить"
                try:
                    latest_data = self._fetch_json(
                        "https://api.github.com/repos/ggml-org/llama.cpp/releases/latest", github=True
                    )
                    latest = latest_data.get("tag_name", "неизвестно")
                    iv = self._version_tuple(installed)
                    lv = self._version_tuple(latest)
                    status = "доступно обновление" if iv and lv and lv > iv else "проверить вручную"
                    report.append(f"llama.cpp: установлено {installed}; последнее {latest} ({status})")
                except Exception as exc:
                    errors.append(f"llama.cpp: не удалось проверить GitHub Releases: {exc}")
            else:
                report.append("llama.cpp: не установлен или путь к llama-server.exe не указан")

            # Ollama: installed CLI version and latest GitHub release
            ollama_cmd = shutil.which("ollama")
            if ollama_cmd:
                try:
                    local = subprocess.run([ollama_cmd, "--version"], capture_output=True,
                                           text=True, timeout=10, **hidden_process_kwargs())
                    installed = ((local.stdout or local.stderr).strip().splitlines() or ["неизвестно"])[0]
                    latest_data = self._fetch_json(
                        "https://api.github.com/repos/ollama/ollama/releases/latest", github=True
                    )
                    latest = latest_data.get("tag_name", "неизвестно")
                    iv, lv = self._version_tuple(installed), self._version_tuple(latest)
                    status = "доступно обновление" if iv and lv and lv > iv else "актуально / сравнение не подтверждено"
                    report.append(f"Ollama: установлено {installed}; последнее {latest} ({status})")
                except Exception as exc:
                    errors.append(f"Ollama: {exc}")
            else:
                report.append("Ollama: команда не найдена в PATH")

            # Python runtime packages installed by this app
            package_sets = [
                ("ONNX Runtime", APP_DIR / "engines" / "venvs" / "onnx", ["onnxruntime"]),
                ("Transformers + PyTorch", APP_DIR / "engines" / "venvs" / "transformers",
                 ["torch", "transformers", "accelerate", "safetensors"]),
            ]
            for label, env_dir, packages in package_sets:
                python_exe = Path(env_dir) / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
                if not python_exe.exists():
                    report.append(f"{label}: окружение не установлено")
                    continue
                for package in packages:
                    try:
                        installed = self._local_package_version(env_dir, package)
                        latest_data = self._fetch_json(f"https://pypi.org/pypi/{package}/json")
                        latest = latest_data.get("info", {}).get("version", "неизвестно")
                        iv, lv = self._version_tuple(installed), self._version_tuple(latest)
                        if not installed:
                            state = "пакет не найден"
                        elif iv and lv and lv > iv:
                            state = "доступно обновление"
                        else:
                            state = "актуально / сравнение не подтверждено"
                        report.append(f"{label} / {package}: установлено {installed or 'нет'}; последнее {latest} ({state})")
                    except Exception as exc:
                        errors.append(f"{label} / {package}: {exc}")

            self._ui_event("updates_complete", (report, errors))
        except Exception as exc:
            self._ui_event("updates_failed", str(exc))

    def remove_model(self):
        p = self.selected_path()
        if not p:
            return
        if self.process and self.process.poll() is None:
            messagebox.showwarning("Модель запущена", "Сначала останови сервер.")
            return
        self.models.remove(p)
        self.apply_model_filter()
        self.save_config()
        self.write_log(f"Удалена из библиотеки: {Path(p).name}. Сам файл не удалён.")

    def on_model_change(self, event=None):
        p = self.selected_path()
        if p and Path(p).exists():
            path = Path(p)
            info = inspect_model(p)
            self.current_model_info = info
            if path.is_file():
                size = path.stat().st_size / (1024**3)
                size_text = self.t("lib.size_file", gb=size)
            else:
                # Пересчёт размера папки — в фоне: rglob по checkpoint'у идёт долго.
                size_text = self.t("lib.folder_size")
                threading.Thread(target=self._folder_size_worker, args=(p,), daemon=True).start()
            self.model_info.set(f"{self.model_display_name(p)}\n{size_text}\n{p}")
            self.format_var.set(self.t("lib.format", value=info.summary()))
            verdict = self.engine_manager.best_engine(info,
                                                      server_path=self.server_var.get().strip())
            engine_name = self._engine_label(verdict.engine)
            self.engine_var.set(self.t("lib.engine", value=engine_name))
            if verdict.status == "compatible":
                self.engine_status_var.set(self.t("verdict.compatible", engine=engine_name))
            elif verdict.status == "unverified":
                # UNVERIFIED: предупреждения нет, иначе пользователь решит, что запуск невозможен.
                self.engine_status_var.set(self.t("verdict.unverified"))
            else:
                key = f"compat.{verdict.code}"
                reason = self.t(key, detail=verdict.detail)
                self.engine_status_var.set(verdict.detail if reason == key else reason)
            recommended = self._recommended_engine(info)
            if recommended in ("llama.cpp", "ONNX Runtime", "Transformers + PyTorch"):
                self.engine_choice_var.set(recommended)
            self.engine_button.configure(state="normal" if recommended else "disabled")
            self.engine_button.configure(text=self.t("lib.engine_download"))
        elif p:
            self.current_model_info = ModelInfo(path=str(p))
            self.model_info.set(self.t("lib.model_missing"))
            self.format_var.set(self.t("lib.format", value=self.t("lib.format_unknown")))
            self.engine_var.set(self.t("lib.engine", value=self.t("lib.engine_none")))
            self.engine_status_var.set(self.t("lib.model_missing"))
            self.engine_button.configure(state="disabled")
        else:
            self.current_model_info = None
            self.model_info.set(self.t("lib.hint"))
            self.format_var.set(self.t("lib.format", value=self.t("lib.format_unknown")))
            self.engine_var.set(self.t("lib.engine", value=self.t("lib.engine_none")))
            self.engine_status_var.set(self.t("lib.select_model"))
            self.engine_button.configure(state="disabled")
        self._update_launch_visibility()
        self._apply_engine_visibility()
        self.update_estimate()
        self.refresh_next_actions()
        self.save_config_safely()

    def _engine_label(self, engine_id):
        labels = {"llama.cpp": "llama.cpp", "onnx": "ONNX Runtime",
                  "transformers": "Transformers + PyTorch", "none": self.t("lib.engine_none")}
        return labels.get(engine_id, engine_id or self.t("lib.engine_none"))

    def _recommended_engine(self, info):
        fmt = getattr(info, "format", "")
        if fmt in ("ggml", "gguf"):
            return "llama.cpp"
        return ""

    def _hardware_info(self):
        return HardwareInfo.from_mapping(getattr(self, "system_hardware", None) or {})

    def _engine_max_ctx(self):
        engine = self.engine_choice_var.get() if hasattr(self, "engine_choice_var") else "llama.cpp"
        return 1_048_576 if engine == "llama.cpp" else None

    def _update_launch_visibility(self):
        """Раздел параметров появляется только при наличии модели (phase F)."""
        shell = getattr(self, "launch_shell", None)
        if shell is None:
            return
        visible = uirules.launch_params_visible(len(self.models))
        mapped = bool(shell.winfo_manager())
        if visible and not mapped:
            shell.pack(side="left", fill="both", expand=True, padx=(5, 0))
        elif not visible and mapped:
            shell.pack_forget()

    def _apply_engine_visibility(self):
        """Скрывает параметры, которые выбранный движок не принимает."""
        if not hasattr(self, "param_widgets") or not hasattr(self, "engine_choice_var"):
            return
        info = getattr(self, "current_model_info", None)
        fmt = getattr(info, "format", "")
        engine = self.engine_choice_var.get()
        aliases = {"gpu_offload": "gpu_layers", "gpu_layers_cell": "gpu_layers"}
        for name, holder in self.param_widgets.items():
            shown = uirules.param_visible(aliases.get(name, name), engine, fmt)
            try:
                if holder.winfo_manager() == "grid":
                    if shown:
                        holder.grid()
                    else:
                        holder.grid_remove()
                elif shown and not holder.winfo_manager():
                    holder.pack(fill="x", pady=(0, 6))
                elif not shown and holder.winfo_manager():
                    holder.pack_forget()
            except tk.TclError:
                continue

    def save_config_safely(self):
        try:
            self.save_config()
        except Exception:
            pass

    def _folder_size_worker(self, path):
        try:
            total = sum(x.stat().st_size for x in Path(path).rglob("*") if x.is_file()) / (1024**3)
            text = f"Размер папки: около {total:.2f} GB"
        except OSError:
            text = "Размер папки: не удалось вычислить"
        self._ui_event("call", lambda: self._apply_folder_size(path, text))

    def _apply_folder_size(self, path, text):
        if self.selected_path() != path:
            return  # модель уже сменили, старый результат не нужен
        self.model_info.set(f"{self.model_display_name(path)}\n{text}\n{path}")

    def toggle_gpu_offload(self):
        if self.gpu_offload_var.get():
            restore = self._gpu_last_layers if self._gpu_last_layers not in ("", "0") else "99"
            self.gpu_var.set(restore)
            self.write_log(f"GPU offload включён: -ngl {restore}")
        else:
            current = self.gpu_var.get().strip()
            if current and current != "0":
                self._gpu_last_layers = current
            self.gpu_var.set("0")
            self.write_log("GPU offload выключен: -ngl 0, вычисления остаются на CPU")
        self.update_estimate()

    def on_ctx_scale(self, value):
        """Ползунок контекста: индекс в available_contexts -> ctx_var."""
        choices = getattr(self, "available_contexts", None) or DEFAULT_CONTEXTS
        try:
            index = max(0, min(len(choices) - 1, int(round(float(value)))))
        except (TypeError, ValueError):
            index = 0
        self.ctx_var.set(str(choices[index]))
        if hasattr(self, "estimate_var"):
            self.update_estimate()

    def apply_manual_context(self, _event=None):
        """Ручной ввод контекста ограничивается пределом модели и движка."""
        info = getattr(self, "current_model_info", None)
        if info is None:
            return
        try:
            value, _reason, changed = manual_context(self.ctx_var.get(), info,
                                                     engine_max=self._engine_max_ctx())
        except ValueError:
            recommendation = getattr(self, "recommendation", None)
            self.ctx_var.set(str(recommendation.context if recommendation else 4096))
            return
        if changed:
            self.ctx_var.set(str(value))
        self._sync_ctx_slider()
        self.update_estimate()

    def _sync_ctx_slider(self):
        choices = getattr(self, "available_contexts", None) or []
        if not choices or not hasattr(self, "ctx_scale"):
            return
        try:
            wanted = int(self.ctx_var.get())
        except (TypeError, ValueError):
            return
        nearest = min(range(len(choices)), key=lambda index: abs(int(choices[index]) - wanted))
        self.ctx_scale.set_range(len(choices) - 1, nearest)

    def _set_advanced(self, open_):
        """Дополнительные параметры свёрнуты, чтобы не перегружать главный вид."""
        self._advanced_open = bool(open_)
        prefix = "▾  " if open_ else "▸  "
        try:
            self.advanced_toggle.configure(text=prefix + self.t("launch.advanced"))
        except tk.TclError:
            pass
        try:
            if open_:
                self.advanced_inner.pack(fill="x", pady=(4, 0))
            else:
                self.advanced_inner.pack_forget()
        except tk.TclError:
            pass

    def update_estimate(self):
        """Оценка памяти по метаданным модели и пересчёт допустимого контекста."""
        self.refresh_context_choices()
        p = self.selected_path()
        info = getattr(self, "current_model_info", None)
        if not p or not Path(p).exists():
            self.estimate_var.set(self.t("mem.no_model"))
            self.speed_hint_var.set(self.t("launch.speed_default"))
            self.update_system_score()
            return
        if Path(p).is_dir() or getattr(info, "format", "") not in ("gguf", "ggml"):
            self.estimate_var.set(self.t("launch.no_gguf_estimate"))
            self.speed_hint_var.set(self.t("launch.speed_unknown"))
            self.update_system_score()
            return
        try:
            ctx = int(self.ctx_var.get())
            model_gb = Path(p).stat().st_size / (1024 ** 3)
            recommendation = getattr(self, "recommendation", None)
            if recommendation is not None and recommendation.kv_gb_per_1024:
                kv_gb = recommendation.kv_gb_per_1024 * ctx / 1024
            else:
                kv_gb = max(0.35, ctx / 4096 * 0.7)   # запасной вариант без метаданных
            overhead = 1.2
            self.estimate_var.set(self.t("launch.estimate", required=model_gb + kv_gb + overhead,
                                         model=model_gb, kv=kv_gb, overhead=overhead))
        except Exception:
            self.estimate_var.set(self.t("mem.calc_failed"))
        self.update_system_score()
        try:
            if getattr(self, "system_hardware", None):
                model_gb = Path(p).stat().st_size / (1024 ** 3)
                low, high = self.estimate_generation_speed(
                    model_gb, int(self.ctx_var.get()), self.system_hardware)
                mode = self.t("launch.speed_gpu" if self.gpu_offload_var.get() else "launch.speed_cpu")
                self.speed_hint_var.set(self.t("launch.speed_value", low=f"{low:.1f}",
                                               high=f"{high:.1f}", mode=mode))
            else:
                self.speed_hint_var.set(self.t("launch.speed_default"))
        except Exception:
            self.speed_hint_var.set(self.t("launch.speed_unknown"))

    def refresh_context_choices(self):
        """Предел контекста: минимум из предела модели, движка и памяти (phase F)."""
        if not hasattr(self, "ctx_scale"):
            return
        info = getattr(self, "current_model_info", None)
        if info is None:
            info = ModelInfo(path=str(self.selected_path() or ""))
        recommendation = recommend_context(
            info, self._hardware_info(),
            gpu_offload=self.gpu_offload_var.get(),
            engine_max=self._engine_max_ctx())
        self.recommendation = recommendation
        choices = [value for value in CONTEXT_STOPS
                   if value <= max(recommendation.context, CONTEXT_STOPS[0])]
        if not choices:
            choices = [CONTEXT_STOPS[0]]
        self.available_contexts = [str(value) for value in choices]
        current = self.ctx_var.get()
        if current not in self.available_contexts:
            current = str(choices[-1])
            self.ctx_var.set(current)
        self.ctx_scale.stops = list(self.available_contexts)
        try:
            self.ctx_scale.set_range(len(choices) - 1, self.available_contexts.index(current))
        except (ValueError, tk.TclError):
            pass
        if hasattr(self, "context_hint_var"):
            by = self.t("limit." + recommendation.limited_by)
            if recommendation.limited_by == "unknown" or not recommendation.model_limit:
                if recommendation.limited_by == "unknown":
                    self.context_hint_var.set(self.t("launch.ctx_hint_default"))
                else:
                    self.context_hint_var.set(self.t("launch.ctx_hint_memory",
                                                     ctx=recommendation.context, by=by))
            else:
                self.context_hint_var.set(self.t("launch.ctx_hint_model",
                                                 model=recommendation.model_limit, by=by))
        if hasattr(self, "max_tokens_var"):
            try:
                requested = int(self.max_tokens_var.get())
            except (TypeError, ValueError):
                requested = 0
            cap = max_output_tokens(self.ctx_var.get(), requested or 1024, engine_max=8192)
            if requested and cap < requested:
                self.max_tokens_var.set(str(cap))

    def estimate_generation_speed(self, model_gb, context, hardware):
        """Very rough decode-speed band; never presented as a benchmark."""
        cpu = (hardware.get("cpu") or "").lower()
        cores = max(1, int(hardware.get("cores") or 1))
        gpus = hardware.get("gpus") or []
        vram = max((g.get("vram") or 0 for g in gpus), default=0)
        gpu_name = " ".join(g.get("name", "") for g in gpus).lower()
        # Baseline CPU estimate for quantized GGUF; model file size is only a rough proxy.
        cpu_factor = min(1.45, max(0.55, (cores / 8) ** 0.45))
        cpu_factor *= 1.12 if any(x in cpu for x in ("ryzen", "core i5", "core i7", "core i9")) else 1.0
        cpu_base = max(0.35, 8.0 / max(1.0, model_gb ** 0.72) * cpu_factor)
        use_gpu = bool(self.gpu_offload_var.get())
        if use_gpu and int(self.gpu_var.get() or 0) > 0 and vram > 0:
            # Broad GPU tiers only; unknown cards get a cautious uplift.
            if any(x in gpu_name for x in ("rtx 40", "rtx 50")): gpu_factor = 5.0
            elif any(x in gpu_name for x in ("rtx 30", "rtx 20", "rx 6", "rx 7")): gpu_factor = 3.5
            elif any(x in gpu_name for x in ("gtx 10", "gtx 16", "rx 5", "radeon rx")): gpu_factor = 2.0
            else: gpu_factor = 1.5
            fit_fraction = min(1.0, max(0.15, (vram - 1.0) / max(model_gb, 1.0)))
            base = cpu_base * (1.0 + (gpu_factor - 1.0) * fit_fraction)
        else:
            base = cpu_base
        # Decode throughput usually changes less than prompt-prefill speed with context.
        context_factor = max(0.78, 1.0 - max(0, context-8192) / 131072 * 0.18)
        low = max(0.2, base * context_factor * 0.65)
        high = max(low + 0.2, base * context_factor * 1.35)
        return low, high

    def analyze_system(self):
        """Detect host hardware and estimate model resource suitability, not speed."""
        self.hardware_var.set("Считываю характеристики Windows…")
        self.system_score_var.set("Оценка запуска: анализ…")
        # WMI/PowerShell отвечают секундами: в потоке Tk окно зависает.
        threading.Thread(target=self._detect_hardware_worker, daemon=True).start()

    def _detect_hardware_worker(self):
        try:
            info = detect_system_hardware()
        except Exception as exc:
            self._ui_event("call", lambda: self.hardware_var.set(
                f"Не удалось прочитать характеристики: {exc}"))
            return
        self._ui_event("call", lambda: self.apply_system_hardware(info))

    def apply_system_hardware(self, info):
        self.system_hardware = info
        gpu_text = []
        for gpu in info["gpus"]:
            if gpu["vram"]:
                gpu_text.append(self.t("mem.hw_gpu_item", name=gpu["name"], vram=gpu["vram"]))
            else:
                gpu_text.append(self.t("mem.hw_gpu_item_unknown", name=gpu["name"]))
        if not gpu_text:
            gpu_text = [self.t("mem.hw_gpu_none")]
        if info["ram_total"] is not None and info["ram_free"] is not None:
            ram_text = self.t("mem.hw_ram", total=info["ram_total"], free=info["ram_free"])
        else:
            ram_text = self.t("mem.hw_ram_unknown")
        self.hardware_var.set(
            self.t("mem.hw_cpu", cpu=info["cpu"], cores=info["cores"]) + "\n"
            + self.t("mem.hw_gpu", gpus="; ".join(gpu_text)) + "\n" + ram_text
        )
        self.update_estimate()
        self.refresh_next_actions()

    def update_system_score(self):
        info = getattr(self, "system_hardware", None)
        if not info:
            return
        p = self.selected_path()
        if not p or not Path(p).is_file() or Path(p).suffix.lower() not in (".gguf", ".ggml"):
            self.system_score_var.set(self.t("mem.score", value=self.t("mem.score_need_model")))
            self.system_score_bar.configure(value=0)
            self.compatibility_var.set("•  " + self.t("mem.compat_model"))
            self.compatibility_label.configure(fg=self.colors["muted"])
            if hasattr(self, "compatibility_note_var"):
                self.compatibility_note_var.set("")
            return
        try:
            model_gb = Path(p).stat().st_size / (1024 ** 3)
            ctx = int(self.ctx_var.get())
            total = info.get("ram_total")
            free = info.get("ram_free")
            max_vram = max((g.get("vram") or 0 for g in info.get("gpus", [])), default=0)
            try:
                gpu_layers = int(self.gpu_var.get())
            except ValueError:
                gpu_layers = 0
            assessment = assess_model_requirements(
                model_gb, ctx, total, free, max_vram,
                self.gpu_offload_var.get(), gpu_layers
            )
            # Phase F: KV-кэш считается из слоёв/голов модели, а не из размера файла.
            model_info = getattr(self, "current_model_info", None)
            meta_kv = kv_cache_bytes(model_info, ctx) if model_info is not None else None
            if meta_kv:
                assessment["kv_gb"] = meta_kv / (1024 ** 3)
                assessment["required_gb"] = (model_gb + assessment["kv_gb"]
                                             + assessment["overhead_gb"])
            required_gb = assessment["required_gb"]
            kv_gb = assessment["kv_gb"]
            overhead_gb = assessment["overhead_gb"]
            if not total or free is None:
                score = 35
                explanation = self.t("mem.explain_no_ram")
            else:
                # Оценка — размещение в памяти, а не токены в секунду.
                usable_total = max(0.0, total - 2.5)
                if required_gb <= usable_total:
                    capacity_score = 100
                else:
                    capacity_score = max(5, min(100, 100 * usable_total / required_gb))
                free_ratio = min(1.0, max(0.0, (free - 1.0) / max(required_gb, 1.0)))
                score = round(capacity_score * 0.72 + (35 + 65 * free_ratio) * 0.28)
                explanation = self.t("mem.explain_need", required=required_gb)
            if assessment["gpu_weight_gb"]:
                explanation += self.t("mem.explain_gpu",
                                      weights=assessment["gpu_weight_gb"], vram=max_vram)
            elif self.gpu_offload_var.get() and gpu_layers > 0:
                explanation += self.t("mem.explain_no_vram")
            score = max(1, min(100, int(score)))
            self.system_score_var.set(self.t("mem.score_value", score=score))
            self.system_score_bar.configure(value=score)
            verdict_style = {
                "ok": ("✓", self.colors["success"]),
                "limited": ("▲", self.colors["warning"]),
                "fail": ("✕", self.colors["danger"]),
            }
            verdict = assessment["verdict"]
            verdict_key = verdict if verdict in verdict_style else "unknown"
            icon, color = verdict_style.get(verdict, ("?", self.colors["muted"]))
            self.compatibility_var.set(f"{icon}  {self.t('verdict.' + verdict_key)}")
            self.compatibility_label.configure(fg=color)
            try:
                self.system_score_bar.configure(style={
                    "ok": "Ok.Horizontal.TProgressbar", "limited": "Warn.Horizontal.TProgressbar",
                    "fail": "Bad.Horizontal.TProgressbar"}.get(verdict, "Horizontal.TProgressbar"))
            except tk.TclError:
                pass
            self.estimate_var.set(
                self.t("launch.estimate", required=required_gb, model=model_gb,
                       kv=kv_gb, overhead=overhead_gb) + " " + explanation + "."
            )
            if total is not None and free is not None:
                self.compatibility_note_var.set(
                    self.t("mem.note", total=total, free=free, vram=max_vram, ctx=ctx))
            else:
                self.compatibility_note_var.set(self.t("mem.note_no_ram"))
        except Exception as exc:
            self.system_score_var.set(self.t("mem.score", value=self.t("mem.score_failed")))
            self.compatibility_var.set(self.t("mem.requirements_failed"))
            self.compatibility_label.configure(fg=self.colors["danger"])
            self.write_log(f"Ошибка анализа совместимости: {exc}")

    def refresh_ram(self):
        free, total = available_ram_gb()
        if free is None:
            self.ram_var.set(self.t("mem.ram_failed"))
        else:
            self.ram_var.set(self.t("mem.ram", free=free, total=total))
        self.update_estimate()

    def choose_server(self):
        p = filedialog.askopenfilename(title="Выбери llama-server.exe", filetypes=[("llama.cpp server", "llama-server.exe"), ("Executable", "*.exe"), ("All files", "*.*")])
        if p:
            self.server_var.set(p)
            self.server_path_preview.set(p)
            self.save_config()
            self.write_log(f"Выбран движок: {p}")

    def auto_find_server(self):
        folder = filedialog.askdirectory(title="Выбери папку llama.cpp (поиск будет и в подпапках)")
        if not folder:
            return
        root = Path(folder)
        self.write_log(f"Поиск llama-server.exe в папке: {root}")
        # Обход большой папки занимает секунды: окно не должно ждать его.
        threading.Thread(target=self._find_server_worker, args=(root,), daemon=True).start()

    def _find_server_worker(self, root):
        matches = []
        # Walk directory names only; model files are not opened or loaded.
        try:
            for current, dirs, files in os.walk(root):
                dirs[:] = [d for d in dirs if d.lower() not in {".git", "__pycache__", "models", "model", "gguf"}]
                for filename in files:
                    if filename.lower() == "llama-server.exe":
                        matches.append(Path(current) / filename)
        except OSError as exc:
            # Как и в проверке API: текст ошибки фиксируем до выхода из except.
            error = str(exc)
            self._ui_event("call", lambda: messagebox.showerror("Автопоиск", error))
            return
        self._ui_event("call", lambda: self._apply_found_server(matches))

    def _apply_found_server(self, matches):
        if not matches:
            messagebox.showwarning(
                "Движок не найден",
                "В выбранной папке и подпапках не найден llama-server.exe.\n"
                "Убедись, что скачана или собрана Windows-версия llama.cpp, содержащая llama-server.exe."
            )
            self.write_log("Автопоиск: llama-server.exe не найден.")
            return
        # Prefer conventional build output locations (build/bin, bin), then shortest path.
        matches.sort(key=lambda x: (0 if any(part.lower() == "bin" for part in x.parts) else 1,
                                    0 if "build" in [part.lower() for part in x.parts] else 1,
                                    len(str(x))))
        chosen = matches[0]
        self.server_var.set(str(chosen))
        self.server_path_preview.set(str(chosen))
        self.save_config()
        self.write_log(f"Найден движок: {chosen}")
        if len(matches) > 1:
            messagebox.showinfo("Найдено несколько копий", f"Найдено файлов: {len(matches)}\nВыбран: {chosen}")
        else:
            messagebox.showinfo("Движок найден", f"Найден llama-server.exe:\n{chosen}")

    def model_alias(self):
        p = self.selected_path()
        if not p:
            return ""
        stem = Path(p).stem
        alias = re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip("-._")
        suffix = hashlib.sha1(stem.encode("utf-8")).hexdigest()[:8]
        # Одно имя файла в разных папках даёт один ID: экспорт второй модели молча
        # перезаписал бы первую в конфиге OpenCode.
        if sum(1 for other in (getattr(self, "models", None) or [])
               if Path(other).stem == stem) > 1:
            return (alias or "local-model")[:71] + "-" + hashlib.sha1(
                str(p).encode("utf-8")).hexdigest()[:8]
        if not alias or not stem.isascii():
            # Кириллица схлопывается в дефисы («Модель-Q4» → «Q4») и совпадает с чужой моделью;
            # хвост хеша держит ID стабильным и уникальным.
            alias = (alias or "local-model")[:71] + "-" + suffix
        elif len(alias) > 80:
            alias = alias[:71] + "-" + suffix
        return alias

    @staticmethod
    def parse_jsonc(content):
        # Remove // and /* */ comments without touching quoted strings, then trailing commas.
        out = []
        i = 0
        in_string = False
        escaped = False
        while i < len(content):
            ch = content[i]
            nxt = content[i + 1] if i + 1 < len(content) else ""
            if in_string:
                out.append(ch)
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_string = False
                i += 1
            elif ch == '"':
                in_string = True
                out.append(ch)
                i += 1
            elif ch == "/" and nxt == "/":
                i += 2
                while i < len(content) and content[i] not in "\r\n":
                    i += 1
            elif ch == "/" and nxt == "*":
                i += 2
                while i + 1 < len(content) and content[i:i+2] != "*/":
                    i += 1
                i = min(i + 2, len(content))
            else:
                out.append(ch)
                i += 1
        cleaned = "".join(out)
        # Lexically remove trailing commas without touching quoted string values.
        result = []
        in_string = False
        escaped = False
        i = 0
        while i < len(cleaned):
            ch = cleaned[i]
            if in_string:
                result.append(ch)
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_string = False
                i += 1
                continue
            if ch == '"':
                in_string = True
                result.append(ch)
                i += 1
                continue
            if ch == ",":
                j = i + 1
                while j < len(cleaned) and cleaned[j].isspace():
                    j += 1
                if j < len(cleaned) and cleaned[j] in "}]":
                    i += 1
                    continue
            result.append(ch)
            i += 1
        return "".join(result)

    def opencode_config_path(self):
        config_dir = Path.home() / ".config" / "opencode"
        json_path = config_dir / "opencode.json"
        jsonc_path = config_dir / "opencode.jsonc"
        return json_path if json_path.exists() else (jsonc_path if jsonc_path.exists() else json_path)

    def read_opencode_config(self, create_dir=False):
        target = self.opencode_config_path()
        if create_dir:
            target.parent.mkdir(parents=True, exist_ok=True)
        config = {}
        if target.exists():
            raw = target.read_text(encoding="utf-8-sig")
            config = json.loads(self.parse_jsonc(raw))
            if not isinstance(config, dict):
                raise ValueError("Корень конфигурации OpenCode должен быть JSON-объектом.")
        return target, config

    def write_opencode_config(self, target, config):
        target = Path(target)
        if target.exists():
            backup = target.with_name(target.name + ".bak-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f"))
            shutil.copy2(target, backup)
            backups = sorted(target.parent.glob(target.name + ".bak-*"),
                             key=lambda item: item.stat().st_mtime, reverse=True)
            for old_backup in backups[5:]:
                try:
                    old_backup.unlink()
                except OSError:
                    pass
            self.write_log(f"Резервная копия OpenCode config: {backup}")
        temp = target.with_name(target.name + ".tmp")
        payload = json.dumps(config, ensure_ascii=False, indent=2) + "\n"
        with open(temp, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, target)

    @staticmethod
    def opencode_cli_path():
        """Найти CLI OpenCode, чтобы попросить его перечитать конфиг."""
        found = shutil.which("opencode")
        if found:
            return found
        roots = [
            Path(os.getenv("APPDATA", "")) / "ai.opencode.desktop" / "cli",
            Path(os.getenv("APPDATA", "")) / "opencode" / "cli",
            Path(os.getenv("LOCALAPPDATA", "")) / "opencode" / "cli",
            Path(os.getenv("LOCALAPPDATA", "")) / "Programs" / "ai.opencode.desktop",
        ]
        candidates = []
        for root in roots:
            if root.is_dir():
                candidates.extend(root.glob("*/opencode-cli.exe"))
                candidates.extend(root.glob("opencode-cli.exe"))
        if not candidates:
            return None
        def version_key(p):
            return [int(x) if x.isdigit() else 0 for x in re.split(r"[.\-]", p.parent.name)]
        return str(sorted(candidates, key=version_key)[-1])

    @staticmethod
    def opencode_is_running():
        """Запущен ли OpenCode, чтобы не поднимать его сервис без спроса."""
        try:
            out = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq OpenCode.exe", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=15, **hidden_process_kwargs(),
            ).stdout
            if "OpenCode.exe" in out:
                return True
            out = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq opencode-cli.exe", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=15, **hidden_process_kwargs(),
            ).stdout
            return "opencode-cli.exe" in out
        except Exception:
            return False

    def reload_opencode(self):
        """Применить новый конфиг в работающем OpenCode без ручного /reload (в фоне)."""
        exe = self.opencode_cli_path()
        if not exe:
            self.write_log("CLI OpenCode не найден — перезагрузку пропускаю (нужен /reload вручную).")
            return False
        if not self.opencode_is_running():
            self.write_log("OpenCode не запущен — конфиг прочитается при следующем старте.")
            return False
        threading.Thread(target=self._reload_opencode_worker, args=(exe,), daemon=True).start()
        return True

    def _reload_opencode_worker(self, exe):
        try:
            result = subprocess.run(
                [exe, "api", "post", "/api/location/reload"],
                capture_output=True, text=True, timeout=60, **hidden_process_kwargs(),
            )
            output = ((result.stdout or "") + " " + (result.stderr or "")).strip()
            if result.returncode == 0:
                self.log_queue.put("OpenCode перечитал конфиг: модели обновились без /reload.")
            else:
                self.log_queue.put(f"OpenCode не перечитал конфиг (код {result.returncode}): "
                                   f"{output[:300] or 'без вывода'}. Нажми /reload вручную.")
        except Exception as e:
            self.log_queue.put(f"Не удалось перезагрузить OpenCode ({e}). Нажми /reload вручную.")

    def export_to_opencode(self):
        model = self.selected_path()
        if not model or not Path(model).is_file() or Path(model).suffix.lower() != ".gguf":
            messagebox.showerror("Формат пока не поддерживается запуском", "Сейчас встроенный запуск и интеграция OpenCode работают для GGUF через llama.cpp. Для выбранного формата программа определила рекомендуемый движок, но запуск через него ещё не подключён.")
            return
        try:
            port = int(self.port_var.get())
            ctx = int(self.ctx_var.get())
            if not (1 <= port <= 65535) or ctx < 512:
                raise ValueError()
            target, config = self.read_opencode_config(create_dir=True)
        except Exception as e:
            messagebox.showerror("Ошибка конфигурации OpenCode", str(e))
            return

        alias = self.model_alias()
        config.setdefault("$schema", "https://opencode.ai/config.json")
        try:
            schema_label = upsert_opencode_local_provider(
                config, port, alias, Path(model).stem, ctx,
                output_limit=self.config_data.get("opencode_output_limit", 8192)
            )
        except Exception as exc:
            messagebox.showerror("Конфликт конфигурации", str(exc))
            return

        try:
            self.write_opencode_config(target, config)
        except Exception as e:
            messagebox.showerror("Ошибка экспорта", str(e))
            return

        self.write_log(f"Экспортировано в OpenCode {schema_label}: {target}")
        self.write_log(f"ID модели: local/{alias}; API: http://127.0.0.1:{port}/v1")
        self.remember_opencode_export(alias)
        self.save_config()
        self.refresh_next_actions()
        self.reload_opencode()
        running = bool(self.process and self.process.poll() is None)
        if running:
            restart = messagebox.askyesno(
                "Сервер уже запущен",
                "Чтобы OpenCode использовал именно выбранную сейчас модель, перезапустить llama-server?"
            )
            if restart:
                self._stop_process_sync()
                self.write_log("Повторный запуск выбранной модели для OpenCode…")
                self.start_model()
                extra = "\nСервер перезапущен с выбранной моделью."
            else:
                extra = "\nСервер оставлен без перезапуска. Убедись, что в нём уже загружена нужная модель."
        else:
            self.write_log("Запуск локального сервера для OpenCode…")
            self.start_model()
            extra = "\nЛокальный сервер запущен или отправлена команда его запуска."
        messagebox.showinfo(
            "Экспорт завершён",
            f"Конфигурация OpenCode ({schema_label}) обновлена:\n{target}\n\n"
            f"Модель: local/{alias}\n"
            f"API: http://127.0.0.1:{port}/v1\n"
            f"Контекст: {ctx}; output: {self.config_data.get('opencode_output_limit', 8192)}; tools: true\n\n"
            "Модель уже в списке OpenCode (/models → local), /reload не нужен."
            + extra
        )

    def remove_from_opencode(self):
        model = self.selected_path()
        if not model:
            messagebox.showerror("Модель не выбрана", "Выбери модель в библиотеке Local AI Manager.")
            return
        if Path(model).suffix.lower() != ".gguf":
            messagebox.showerror("Формат пока не поддерживается", "Удаление записи из OpenCode доступно для GGUF-моделей, добавленных этой программой.")
            return
        alias = self.model_alias()
        try:
            target, config = self.read_opencode_config(create_dir=False)
        except Exception as e:
            messagebox.showerror("Ошибка конфигурации OpenCode", str(e))
            return

        root_key = opencode_root_key(config)
        providers = config.get(root_key, {})
        if not isinstance(providers, dict):
            messagebox.showerror("Неверный конфиг", f'В конфигурации отсутствует корректный объект "{root_key}".')
            return
        provider = providers.get("local")
        if not isinstance(provider, dict):
            messagebox.showinfo("Нечего удалять", 'Провайдер "local" в OpenCode не найден.')
            return
        models = provider.get("models", {})
        if not isinstance(models, dict) or alias not in models:
            messagebox.showinfo("Модель не найдена", f"Модель local/{alias} не найдена в OpenCode.")
            return

        if not messagebox.askyesno(
            "Удалить модель из OpenCode?",
            f"Удалить запись local/{alias} из конфигурации OpenCode?\n\n"
            "Файл GGUF и модель в библиотеке Local AI Manager останутся нетронутыми."
        ):
            return

        del models[alias]
        try:
            self.write_opencode_config(target, config)
        except Exception as e:
            messagebox.showerror("Ошибка удаления", str(e))
            return

        self.write_log(f"Удалена из OpenCode модель local/{alias}. Файл GGUF не тронут.")
        self.remember_opencode_export(alias, exported=False)
        self.save_config()
        self.refresh_next_actions()
        self.reload_opencode()
        messagebox.showinfo(
            "Модель удалена",
            f"Запись local/{alias} удалена из OpenCode:\n{target}\n\n"
            "Провайдер local и остальные его модели сохранены. Список моделей обновлён автоматически."
        )

    def check_gpu_support(self):
        server = self.server_var.get().strip()
        if not server or not Path(server).is_file():
            messagebox.showwarning("Сначала выбери движок", "Укажи llama-server.exe или выполни автопоиск.")
            return
        self.write_log("Проверка GPU-устройств через llama-server.exe --list-devices …")
        threading.Thread(target=self._check_gpu_worker, args=(server,), daemon=True).start()

    def _check_gpu_worker(self, server):
        try:
            result = subprocess.run(
                [server, "--list-devices"],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20,
                **hidden_process_kwargs()
            )
            output = ((result.stdout or "") + "\n" + (result.stderr or "")).strip()
            ok = any(token in output.lower() for token in ("cuda", "vulkan", "metal", "opencl", "kompute", "sycl"))
            self._ui_event("call", lambda: self._show_gpu_result(ok, output, result.returncode))
        except subprocess.TimeoutExpired:
            self._ui_event("call", lambda: messagebox.showerror("Тайм-аут", "Проверка llama.cpp заняла больше 20 секунд."))
        except Exception as e:
            self._ui_event("call", lambda: messagebox.showerror("Ошибка проверки GPU", str(e)))

    def _show_gpu_result(self, ok, output, returncode):
        if output:
            self.write_log(output)
        if ok:
            messagebox.showinfo("GPU обнаружен", "В выводе llama.cpp найден GPU-backend/устройство.\n\n" + output[:1800])
        else:
            messagebox.showwarning(
                "GPU не подтверждён",
                "llama.cpp не показал явного GPU-устройства. Возможно, это CPU-сборка или backend/драйвер не настроен.\n\n"
                + (output[:1400] or f"Код завершения: {returncode}")
            )

    def start_model(self):
        """Запуск только через адаптер и только после проверки совместимости."""
        if self._stopping:
            self.message_info(self.t("errors.stopping_title"), self.t("errors.stopping"))
            return
        if self.process and self.process.poll() is None:
            self.message_info(self.t("errors.already_running_title"), self.t("errors.already_running"))
            return
        model = self.selected_path()
        server = self.server_var.get().strip()
        if not model or not Path(model).exists():
            self.message_error(self.t("errors.no_model_title"), self.t("errors.no_model"))
            return
        info = self.current_model_info or inspect_model(model)
        self.current_model_info = info
        engine_id = self.engine_choice_var.get() or "llama.cpp"
        try:
            # Проверка до plan() и до subprocess: несовместимую модель запустить нельзя.
            adapter = self.engine_manager.require_launchable(
                info, engine_id=engine_id, server_path=server)
        except IncompatibleModelError as exc:
            self.show_incompatibility(exc.compatibility, info)
            return
        try:
            params = LaunchParams(
                model_path=model,
                ctx=int(self.ctx_var.get()),
                gpu_layers=int(self.gpu_var.get()),
                threads=int(self.threads_var.get()),
                port=int(self.port_var.get()),
                alias=self.model_alias(),
            ).validated(engine_max_ctx=self._engine_max_ctx())
            plan = adapter.plan(params)
        except (TypeError, ValueError, LaunchError):
            self.message_error(self.t("errors.bad_params_title"), self.t("errors.bad_params"))
            return
        if not server or not Path(server).is_file():
            self.message_error(self.t("errors.llama_missing_title"), self.t("errors.llama_missing"))
            return
        self.save_config()
        self.write_log(f"Движок: {engine_id}; ID модели API: {self.model_alias()} "
                       f"(провайдер OpenCode: local/{self.model_alias()})")
        self.write_log(f"Запрос GPU offload: {params.gpu_layers} слоёв (-ngl). "
                       "Значение 99 просит llama.cpp выгрузить максимум слоёв на GPU.")
        self.write_log("Запуск: " + plan.command_line())
        try:
            self.process = adapter.launch(params, info, spawn=self._spawn_server)
            self.process_started_at = time.time()
        except Exception as exc:
            self.write_log(self.t("errors.launch_failed", error=str(exc)))
            self.message_error(self.t("errors.launch"), str(exc))
            return
        if not self._take_process_under_supervision(self.process):
            return
        self.set_status("busy", "status.starting")
        threading.Thread(target=self.read_output, daemon=True).start()
        if not self._process_monitor_started:
            self._process_monitor_started = True
            self.after(900, self.check_process)
        self.refresh_next_actions()

    def _spawn_server(self, plan):
        """Spaw-функция для адаптера: процесс стартует приостановленным (Job Object)."""
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        if IS_WINDOWS:
            flags |= CREATE_SUSPENDED
        kwargs = {key: value for key, value in hidden_process_kwargs().items()
                  if key != "creationflags"}
        return subprocess.Popen(
            list(plan.argv), cwd=plan.cwd or None,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            creationflags=flags, **kwargs)

    def message_info(self, title, text):
        messagebox.showinfo(title, text)

    def message_error(self, title, text):
        messagebox.showerror(title, text)

    def show_incompatibility(self, verdict, info):
        """Объясняет, почему модель не запустится, и предлагает действия."""
        installed = [name for name, status in self.engine_manager.detect_installed().items()
                     if status.installed]
        compatible = []
        for engine_id in installed:
            adapter = self.engine_manager.adapter(engine_id, self.server_var.get().strip())
            if adapter.can_run(info).status == CompatibilityStatus.COMPATIBLE:
                compatible.append(engine_id)
        dialog = IncompatibilityDialog(self, verdict, info, compatible)
        self.wait_window(dialog)
        if dialog.result == "switch" and dialog.chosen_engine:
            self.engine_choice_var.set(dialog.chosen_engine)
            self.on_engine_choice()
            return
        if dialog.result == "download":
            self._set_engine_section(True)
            self.download_selected_engine()

    def _take_process_under_supervision(self, proc):
        """Создаёт Job Object и снимает CREATE_SUSPENDED.

        Возвращает False, если запуск нужно отменить (процесс уже остановлен).
        """
        self.job = create_process_job()
        if self.job:
            if assign_process_to_job(self.job, proc.pid):
                self.write_log("Сервер под надзором Job Object: при закрытии или падении "
                               "менеджера llama-server завершится автоматически.")
            else:
                error = _last_error()
                close_handle(self.job)
                self.job = None
                self.write_log(f"Не удалось назначить Job Object (код {error}). "
                               "Сервер работает без страховки от осиротевших процессов.")
        else:
            self.write_log("Job Object недоступен: сервер работает без страховки "
                           "от осиротевших процессов.")

        if not resume_suspended_process(proc.pid):
            self.write_log("ОШИБКА: не удалось возобновить приостановленный процесс сервера.")
            stop_process_gracefully(proc, self.job, timeout=0.5, log=self.write_log)
            self.process = None
            self.job = None
            self.set_status("idle")
            messagebox.showerror("Ошибка запуска",
                                 "Не удалось возобновить llama-server после создания процесса.")
            return False
        return True

    def read_output(self):
        proc = self.process
        if not proc or not proc.stdout:
            return
        for line in proc.stdout:
            self.log_queue.put(line.rstrip())

    def poll_log_queue(self):
        try:
            while True:
                item = self.log_queue.get_nowait()
                if isinstance(item, tuple) and item and item[0] == "__ui__":
                    _, kind, payload = item
                    if kind == "stop_done":
                        self._finish_stop()
                    elif kind == "updates_complete":
                        report, errors = payload
                        self.update_check_button.configure(state="normal")
                        self.download_status_var.set("Проверка обновлений завершена")
                        self.set_status("idle", "status.updates_done")
                        lines = ["Результаты проверки обновлений:", ""] + report
                        if errors:
                            lines += ["", "Ошибки проверки:"] + errors
                        summary = "\n".join(lines)
                        self.write_log(summary)
                        update_count = sum("доступно обновление" in line for line in report)
                        note = (f"Найдено обновлений: {update_count}. Подробности записаны в журнал."
                                if update_count else "Проверка завершена. Подробности записаны в журнал.")
                        self.show_notification("Проверка обновлений", note,
                                               "success" if update_count == 0 else "info", duration=10000)
                    elif kind == "updates_failed":
                        self.update_check_button.configure(state="normal")
                        self.download_status_var.set("Не удалось проверить обновления")
                        self.set_status("error")
                        self.write_log("Ошибка проверки обновлений: " + payload)
                        self.show_notification("Ошибка проверки обновлений", payload, "error", duration=10000)
                    elif kind == "call":
                        # Единственная точка, где фоновый поток попадает в Tk-поток.
                        try:
                            payload()
                        except Exception as exc:
                            self.write_log(f"ОШИБКА фоновой операции: {exc}")
                    elif kind == "progress":
                        percent, label = payload
                        self.download_progress.configure(mode="determinate", value=percent)
                        self.download_status_var.set(label)
                    elif kind == "indeterminate":
                        self.download_progress.configure(mode="indeterminate")
                        self.download_progress.start(10)
                        self.download_status_var.set(payload)
                    elif kind == "status":
                        self.download_status_var.set(payload)
                    elif kind == "llama_ready":
                        self.server_var.set(payload)
                        self.server_path_preview.set(payload)
                        self.save_config_safely()
                    elif kind == "complete":
                        self.download_progress.stop()
                        self.download_progress.configure(mode="determinate", value=100)
                        self.download_status_var.set("Готово: " + payload)
                        self.set_status("running", "status.engine_ready")
                        self.engine_status_var.set(payload)
                        self.engine_button.configure(state="normal")
                        self.cancel_download_button.configure(state="disabled")
                        self.engine_choice.configure(state="readonly")
                        self.download_cancel_event = None
                        self.write_log(payload)
                        self.show_notification("Движок подготовлен", payload, "success")
                    elif kind == "cancelled":
                        self.download_progress.stop()
                        self.download_progress.configure(mode="determinate", value=0)
                        self.download_status_var.set("Загрузка остановлена")
                        self.set_status("idle", "status.cancelled")
                        self.engine_button.configure(state="normal")
                        self.cancel_download_button.configure(state="disabled")
                        self.engine_choice.configure(state="readonly")
                        self.download_cancel_event = None
                        self.write_log("Загрузка движка отменена пользователем.")
                    elif kind == "failed":
                        self.download_progress.stop()
                        self.download_progress.configure(mode="determinate", value=0)
                        self.download_status_var.set("Ошибка загрузки/установки: " + payload)
                        self.set_status("error", "status.engine_error")
                        self.engine_button.configure(state="normal")
                        self.cancel_download_button.configure(state="disabled")
                        self.engine_choice.configure(state="readonly")
                        self.download_cancel_event = None
                        self.write_log("ОШИБКА загрузки движка: " + payload)
                        self.show_notification("Ошибка загрузки движка", payload, "error", duration=10000)
                else:
                    self.write_log(str(item))
        except queue.Empty:
            pass
        self.after(150, self.poll_log_queue)

    def check_process(self):
        """Периодически проверяет процесс и обновляет статус после аварийного завершения."""
        alive = bool(self.process and self.process.poll() is None)
        if alive:
            if not self._stopping:
                self.set_status("running")
        elif not self._stopping:
            if self.process:
                self.write_log(f"Процесс завершился (код {self.process.returncode}).")
                self.process = None
                self.process_started_at = None
            if self.job:
                close_handle(self.job)
                self.job = None
            self.set_status("idle")
        # Дорогое обновление подсказок — только при смене состояния.
        if alive != getattr(self, "_last_process_alive", None):
            self._last_process_alive = alive
            self.refresh_next_actions()
        try:
            if self.winfo_exists():
                self._process_check_job = self.after(lam_timers.next_delay("process_state"),
                                                 self.check_process)
        except Exception:
            pass

    def _stop_process_sync(self, timeout=8.0):
        """Останавливает сервер синхронно (для закрытия окна и перезапуска из экспорта)."""
        proc, job = self.process, self.job
        self.process = None
        self.job = None
        self._stopping = False
        if not proc:
            if job:
                close_handle(job)
            return None
        return stop_process_gracefully(proc, job, timeout=timeout, log=self.write_log)

    def stop_model(self):
        if self._stopping:
            self.write_log("Остановка уже выполняется…")
            return
        if not self.process or self.process.poll() is not None:
            self.set_status("idle")
            self.write_log("Активного процесса нет.")
            return
        self._stopping = True
        self.set_status("busy", "status.stopping")
        self.write_log("Остановка сервера…")
        threading.Thread(target=self._stop_worker, args=(self.process, self.job),
                         daemon=True).start()

    def _stop_worker(self, proc, job):
        """Остановка в фоновом потоке: UI не замирает на время тайм-аутов."""
        def safe_log(text):
            self.log_queue.put(text)
        try:
            result = stop_process_gracefully(proc, job, timeout=8.0, log=safe_log)
            if result is None:
                return
            if result["graceful"]:
                hint = (" — остановка по CTRL_BREAK" if result["exit_code"] == CTRL_BREAK_EXIT_CODE
                        else "")
                safe_log(f"Сервер завершился штатно, код {result['exit_code']}{hint}.")
            elif result["forced"]:
                safe_log(f"Сервер остановлен принудительно (код {result['exit_code']}).")
            else:
                safe_log("Процесс сервера уже был завершён.")
        except Exception as exc:
            safe_log("Ошибка остановки сервера: " + str(exc))
        finally:
            # UI-часть выполняет главный поток в poll_log_queue: из рабочего
            # потока напрямую трогать Tkinter нельзя.
            self.log_queue.put(("__ui__", "stop_done", None))

    def _finish_stop(self):
        self.process = None
        self.job = None
        self._stopping = False
        self.set_status("idle")
        self.refresh_next_actions()
        if getattr(self, "_close_after_stop", False):
            self._close_after_stop = False
            self.save_config_safely()
            self.destroy()

    def open_agent_skill_manager(self):
        existing = getattr(self, "agent_skill_window", None)
        if existing is not None and existing.winfo_exists():
            existing.lift()
            existing.focus_force()
            return
        self.agent_skill_window = AgentSkillManager(self)
        fade_in(self.agent_skill_window)

    def open_chat(self):
        if not self.selected_path():
            messagebox.showwarning("Нет модели", "Сначала выбери модель в библиотеке.")
            return
        existing = getattr(self, "chat_window", None)
        if existing is not None and existing.winfo_exists():
            existing.lift()
            existing.focus_force()
            return
        self.chat_window = LocalChatWindow(self)
        fade_in(self.chat_window)

    def open_dashboard(self):
        existing = getattr(self, "dashboard_window", None)
        if existing is not None and existing.winfo_exists():
            existing.lift()
            existing.focus_force()
            return
        self.dashboard_window = RuntimeDashboard(self)
        fade_in(self.dashboard_window)

    def build_agent_system_prompt(self, agent_id):
        agents = self.config_data.setdefault("agents", [])
        skills = self.config_data.setdefault("skills", [])
        agent = next((a for a in agents if a.get("id") == agent_id), None)
        if not agent:
            agent = {"name": "Универсальный помощник", "system_prompt": "Ты полезный ассистент."}
        sections = [agent.get("system_prompt", "Ты полезный ассистент.").strip()]
        selected_ids = set(agent.get("skill_ids", []))
        active_skills = [sk for sk in skills if sk.get("id") in selected_ids and sk.get("enabled", True)]
        if active_skills:
            sections.append("АКТИВНЫЕ НАВЫКИ. Следуй этим инструкциям при выполнении подходящих задач:")
            for skill in active_skills:
                sections.append(f"\n### {skill.get('name', 'Навык')}\n{skill.get('content', '').strip()}")
        sections.append("Работай только в пределах реально доступных возможностей локального runtime. Не утверждай, что вызываешь инструменты или изменяешь файлы, если соответствующая функция не предоставлена.")
        return "\n\n".join(part for part in sections if part)

    def get_optimization_tips(self):
        tips = []
        path = self.selected_path()
        if not path or not Path(path).is_file():
            return [self.t("tips.need_model")]
        try:
            model_gb = Path(path).stat().st_size / (1024 ** 3)
            ctx = int(self.ctx_var.get())
            free, total = available_ram_gb()
            hardware = self.system_hardware or {}
            vram = max((g.get("vram") or 0 for g in hardware.get("gpus", [])), default=0)
            if ctx > 16384:
                tips.append(self.t("tips.ctx_high"))
            if free is not None and free < model_gb + 2.5:
                tips.append(self.t("tips.low_ram"))
            if self.gpu_offload_var.get() and vram and vram < model_gb + 1.0:
                tips.append(self.t("tips.low_vram"))
            if not self.gpu_offload_var.get():
                tips.append(self.t("tips.no_offload"))
            if int(self.threads_var.get() or 1) > (os.cpu_count() or 4):
                tips.append(self.t("tips.many_threads"))
            if not tips:
                tips.append(self.t("tips.ok"))
        except Exception as exc:
            tips.append(self.t("tips.calc_failed", error=exc))
        tips.append(self.t("tips.note"))
        return tips

    def open_ui(self):
        # Backward-compatible alias: the browser UI was replaced by the native chat window.
        self.open_chat()

    def on_close(self):
        if self.process and self.process.poll() is None:
            if messagebox.askyesno("Выход", "Сервер ещё работает. Остановить его перед выходом?"):
                self._close_after_stop = True
                self.write_log("Останавливаю сервер в фоне перед закрытием…")
                self.stop_model()
                return
            detached = detach_job(self.job)
            self.job = None
            self.process = None
            self.write_log("Сервер оставлен работать без надзора менеджера."
                           if detached else
                           "Сервер оставлен работать: Job Object недоступен, "
                           "надзор снят без изменений.")
        elif self.job:
            close_handle(self.job)
            self.job = None
        self.save_config_safely()
        self.destroy()


class MainMonitorPanel(tk.Frame):
    """Компактный живой мониторинг без виджета CPU (снят вместе с его таймерами)."""

    def __init__(self, parent, app):
        super().__init__(parent, bg=app.colors["page"], bd=0, highlightthickness=0)
        self.app = app
        self.t = app.t
        self.c = app.colors
        self._alive = True
        self._pulse = 0
        self._gpu_mb = None
        self._gpu_stats = None
        self._gpu_pending = False
        self._gpu_last = 0.0
        self._api_state = None
        self._api_pending = False
        self._api_last = 0.0
        self._worker_queue = queue.Queue()
        self._bindings = []
        self._build()
        self._refresh_job = self.after(500, self._refresh)

    # --- локализация -------------------------------------------------------
    def _i18n_bind(self, widget, key):
        self._bindings.append((widget, key))
        try:
            widget.configure(text=self.t(key))
        except tk.TclError:
            pass
        return widget

    def retranslate(self):
        for widget, key in self._bindings:
            try:
                widget.configure(text=self.t(key))
            except tk.TclError:
                continue
        for key, var in (("monitor.note_model", self.metric_notes.get("model")),
                         ("monitor.note_ram", self.metric_notes.get("ram")),
                         ("monitor.note_vram", self.metric_notes.get("vram"))):
            if var is not None and not var.get():
                var.set(self.t(key))

    # --- интерфейс ---------------------------------------------------------
    def _build(self):
        c = self.c
        shell = RoundedPanel(self, page=c["page"], fill=c["card"],
                             shadow=c["shadow"], radius=16, padding=7, lateral=8)
        shell.pack(fill="x")
        body = shell.inner
        top = tk.Frame(body, bg=c["card"])
        top.pack(fill="x", pady=(0, 6))
        self.pulse = tk.Canvas(top, width=22, height=22, bg=c["card"],
                               bd=0, highlightthickness=0)
        self.pulse.pack(side="left", padx=(0, 6))
        self._i18n_bind(tk.Label(top, text=self.t("monitor.title"), bg=c["card"], fg=c["text"],
                            font=(self.app.ui_font, 9, "bold")), "monitor.title").pack(side="left")
        self.status_var = tk.StringVar(value=self.t("monitor.waiting"))
        tk.Label(top, textvariable=self.status_var, bg=c["card"], fg=c["muted"],
                 font=(self.app.ui_font, 8)).pack(side="left", padx=(10, 0))
        self.detail_btn = self.app.rbutton(top, self.t("monitor.details"),
                                           self.app.open_dashboard, bg=c["card"], pad=(11, 4))
        self.detail_btn.pack(side="right")
        self._bindings.append((self.detail_btn, "monitor.details"))

        metrics = tk.Frame(body, bg=c["card"])
        metrics.pack(fill="x")
        self.metric_values = {}
        self.metric_notes = {}
        specs = [
            ("model", "monitor.model", "—", "monitor.note_model"),
            ("ram", "monitor.ram", "—", "monitor.note_ram"),
            ("vram", "monitor.vram", "—", "monitor.note_vram"),
        ]
        for index, (key, label_key, value, note_key) in enumerate(specs):
            tile = tk.Frame(metrics, bg=c["field"], padx=9, pady=5,
                            highlightthickness=1, highlightbackground=c["border"])
            tile.pack(side="left", fill="both", expand=True, padx=(0 if index == 0 else 5, 0))
            self._i18n_bind(tk.Label(tile, text=self.t(label_key), bg=c["field"], fg=c["muted"],
                                font=(self.app.ui_font, 7, "bold")), label_key).pack(
                side="left", anchor="s", pady=(0, 2))
            var = tk.StringVar(value=value)
            self.metric_values[key] = var
            tk.Label(tile, textvariable=var, bg=c["field"], fg=c["text"],
                     font=(self.app.ui_font, 11, "bold"), anchor="w").pack(side="left", padx=(8, 0))
            note_var = tk.StringVar(value=self.t(note_key))
            self.metric_notes[key] = note_var
            tk.Label(tile, textvariable=note_var, bg=c["field"], fg=c["muted"],
                     font=(self.app.ui_font, 7), anchor="w").pack(side="left", padx=(7, 0),
                                                                 anchor="s", pady=(0, 2))

        bars = tk.Frame(body, bg=c["card"])
        bars.pack(fill="x", pady=(5, 0))
        self.ram_bar = self._make_bar(bars, "monitor.system_ram", "—")
        self.ram_bar.frame.pack(side="left", fill="x", expand=True, padx=(0, 12))
        self.ctx_bar = self._make_bar(bars, "monitor.context", "—")
        self.ctx_bar.frame.pack(side="left", fill="x", expand=True)

    def _make_bar(self, parent, label_key, value):
        c = self.c
        wrap = tk.Frame(parent, bg=c["card"])
        head = tk.Frame(wrap, bg=c["card"])
        head.pack(fill="x")
        title = tk.Label(head, text=self.t(label_key), bg=c["card"], fg=c["muted"],
                         font=(self.app.ui_font, 7, "bold"))
        title.pack(side="left")
        self._bindings.append((title, label_key))
        val = tk.StringVar(value=value)
        tk.Label(head, textvariable=val, bg=c["card"], fg=c["text"],
                 font=(self.app.ui_font, 7, "bold")).pack(side="right")
        canvas = tk.Canvas(wrap, height=6, bg=c["field"], bd=0, highlightthickness=0)
        canvas.pack(fill="x", pady=(4, 0))
        return type("MonitorBar", (), {"frame": wrap, "value": val, "canvas": canvas})()

    def _set_bar(self, bar, percent, label, warn=False):
        """Скруглённая полоса; при warn цвет меняется: акцент → жёлтый → красный."""
        bar.value.set(label)
        canvas = bar.canvas
        width = max(12, canvas.winfo_width())
        percent = max(0.0, min(100.0, float(percent)))
        canvas.delete("all")
        canvas.create_line(3, 3, width - 3, 3, width=6, capstyle="round", fill=self.c["field"])
        fill = percent / 100 * (width - 6)
        if fill > 0:
            color = self.c["accent"]
            if warn:
                color = (self.c["danger"] if percent >= 90 else
                         self.c["warning"] if percent >= 75 else self.c["accent"])
            canvas.create_line(3, 3, 3 + max(0.5, fill), 3, width=6, capstyle="round", fill=color)

    def _draw_pulse(self, running, api_ready):
        c = self.c
        self.pulse.delete("all")
        color = c["success"] if running and api_ready else c["warning"] if running else c["muted"]
        self._pulse = (self._pulse + 1) % 4
        for index in range(3 - self._pulse % 3):
            pad = 3 + index * 3
            self.pulse.create_oval(pad, pad, 28 - pad, 28 - pad, outline=color, width=1)
        self.pulse.create_oval(10, 10, 18, 18, fill=color, outline="")

    def _query_gpu(self, pid):
        value = gpu_process_vram_mb(pid)
        stats = gpu_system_stats()
        self._worker_queue.put(("gpu", (value, stats)))

    def _gpu_result(self, value):
        self._gpu_mb, self._gpu_stats = value
        self._gpu_pending = False

    def _query_api(self, port):
        try:
            request = urllib.request.Request(f"http://127.0.0.1:{port}/v1/models",
                                             headers={"User-Agent": "LocalAIManager"})
            with urllib.request.urlopen(request, timeout=1.2) as response:
                result = (200 <= response.status < 300, f"HTTP {response.status}")
        except Exception as exc:
            result = (False, str(exc)[:70])
        self._worker_queue.put(("api", result))

    def _api_result(self, result):
        self._api_state = result
        self._api_pending = False

    def _refresh(self):
        if not self._alive or not self.winfo_exists():
            return
        try:
            # Свёрнутое/скрытое окно не опрашивает железо: раньше опрос шёл всегда.
            if lam_timers.should_pause(self.winfo_toplevel()):
                self._refresh_job = self.after(
                    lam_timers.next_delay("monitor", hidden=True), self._refresh)
                return
            self._refresh_body()
        finally:
            try:
                if self._alive and self.winfo_exists():
                    self._refresh_job = self.after(lam_timers.next_delay("monitor"), self._refresh)
            except tk.TclError:
                pass

    def _refresh_body(self):
        if not self.winfo_exists():
            return
        # Результаты фоновых опросов (GPU / API) приходят через очередь.
        try:
            while True:
                kind, payload = self._worker_queue.get_nowait()
                if kind == "gpu":
                    self._gpu_result(payload)
                elif kind == "api":
                    self._api_result(payload)
        except queue.Empty:
            pass

        app = self.app
        t = self.t
        proc = app.process
        running = bool(proc and proc.poll() is None)
        now = time.time()
        api_ready = bool(running and self._api_state and self._api_state[0])

        # Системная RAM
        free, total = available_ram_gb()
        if free is not None and total:
            used = max(0.0, total - free)
            self._set_bar(self.ram_bar, used / total * 100, f"{used:.1f} / {total:.1f} GB", warn=True)
        else:
            self._set_bar(self.ram_bar, 0, "—")

        # Контекст модели
        try:
            ctx = int(app.ctx_var.get())
        except (TypeError, ValueError):
            ctx = 0
        info = getattr(app, "current_model_info", None)
        ctx_max = getattr(info, "max_context", None) or 0
        if ctx and ctx_max:
            self._set_bar(self.ctx_bar, min(100, ctx / ctx_max * 100), f"{ctx} / {ctx_max}")
        elif ctx:
            self._set_bar(self.ctx_bar, 0, t("monitor.tokens", count=ctx))
        else:
            self._set_bar(self.ctx_bar, 0, "—")

        if running:
            # Процесс / API
            self.metric_values["model"].set(t("monitor.running") if api_ready else "…")
            self.metric_notes["model"].set(t("monitor.api_ready") if api_ready
                                           else t("monitor.api_wait"))
            mem = process_memory_stats(proc.pid)
            if mem:
                self.metric_values["ram"].set(f"{mem['working_set'] / (1024 ** 3):.1f} GB")
            else:
                self.metric_values["ram"].set("—")
            self.metric_notes["ram"].set(t("monitor.note_ram"))
            if self._gpu_mb is not None:
                self.metric_values["vram"].set(f"{self._gpu_mb / 1024:.1f} GB")
            elif self._gpu_stats:
                try:
                    self.metric_values["vram"].set(f"{float(self._gpu_stats[0]['used']) / 1024:.1f} GB")
                except (KeyError, ValueError, TypeError, IndexError):
                    self.metric_values["vram"].set("—")
            else:
                self.metric_values["vram"].set("—")
            self.metric_notes["vram"].set(t("monitor.note_vram"))

            # Фоновые опросы: GPU раз в 5 с, API раз в 4 с.
            if now - self._gpu_last > 5 and not self._gpu_pending:
                self._gpu_pending = True
                self._gpu_last = now
                threading.Thread(target=self._query_gpu, args=(proc.pid,), daemon=True).start()
            try:
                port = int(app.port_var.get())
            except (TypeError, ValueError):
                port = 0
            if port and now - self._api_last > 4 and not self._api_pending:
                self._api_pending = True
                self._api_last = now
                threading.Thread(target=self._query_api, args=(port,), daemon=True).start()

            elapsed = int(now - app.process_started_at) if app.process_started_at else 0
            uptime = f"{elapsed // 3600:02d}:{(elapsed % 3600) // 60:02d}:{elapsed % 60:02d}"
            self.status_var.set(t("monitor.uptime", uptime=uptime,
                                  api=t("monitor.api_ready" if api_ready else "monitor.api_wait")))
        else:
            self._api_state = None
            self._gpu_mb = None
            self.metric_values["model"].set("—")
            self.metric_values["ram"].set("—")
            self.metric_values["vram"].set("—")
            self.metric_notes["model"].set(t("monitor.note_model"))
            self.metric_notes["ram"].set(t("monitor.note_ram"))
            self.metric_notes["vram"].set(t("monitor.note_vram"))
            self.status_var.set(t("monitor.stopped") if app.process_started_at is not None
                                or getattr(app, "_last_process_alive", None) is False
                                else t("monitor.waiting"))
        self._draw_pulse(running, api_ready)

    def destroy(self):
        self._alive = False
        try:
            self.after_cancel(self._refresh_job)
        except Exception:
            pass
        super().destroy()


class IncompatibilityDialog(tk.Toplevel):
    """Объясняет, почему модель не запустится, и предлагает действия."""

    def __init__(self, app, verdict, info, compatible):
        super().__init__(app)
        self.app = app
        self.t = app.t
        self.result = None
        self.compatible = list(compatible or [])
        self.chosen_engine = self.compatible[0] if self.compatible else None
        self.configure(bg=app.colors["page"])
        self.transient(app)
        _build_frameless_dialog_titlebar(self, app, self.t("errors.incompatible_title"))
        self.resizable(False, False)
        c = app.colors
        body = tk.Frame(self, bg=c["page"], padx=18, pady=14)
        body.pack(fill="both", expand=True)
        panel = RoundedPanel(body, page=c["page"], fill=c["card"], shadow=c["shadow"],
                             radius=18, padding=14, lateral=7)
        panel.pack(fill="both", expand=True)
        inner = tk.Frame(panel.inner, bg=c["card"])
        inner.pack(fill="both", expand=True)
        tk.Label(inner, text=info.name, bg=c["card"], fg=c["text"],
                 font=(app.ui_font, 11, "bold"), anchor="w").pack(fill="x")
        tk.Label(inner, text=info.summary(), bg=c["card"], fg=c["muted"],
                 font=(app.ui_font, 9), anchor="w").pack(fill="x", pady=(2, 8))
        key = f"compat.{verdict.code}"
        reason = self.t(key, detail=verdict.detail)
        if reason == key:
            reason = verdict.detail or self.t("errors.not_runnable")
        label = tk.Label(inner, text=reason, bg=c["card"], fg=c["warning"], justify="left",
                         anchor="w", wraplength=460, font=(app.ui_font, 9))
        label.pack(fill="x")
        label.bind("<Configure>", lambda e: label.configure(wraplength=max(200, e.width - 4)))
        engines = ", ".join(self.compatible) if self.compatible else self.t("errors.no_compatible")
        tk.Label(inner, text=self.t("errors.compatible_engines", engines=engines),
                 bg=c["card"], fg=c["muted"], justify="left", anchor="w",
                 wraplength=460, font=(app.ui_font, 8)).pack(fill="x", pady=(10, 0))
        actions = tk.Frame(inner, bg=c["card"])
        actions.pack(fill="x", pady=(14, 0))
        if self.compatible:
            app.rbutton(actions, f'{self.t("errors.choose_engine")}: {self.chosen_engine}',
                        lambda: self._finish("switch"), variant="accent",
                        bg=c["card"]).pack(side="left")
        app.rbutton(actions, self.t("errors.download_engine"),
                    lambda: self._finish("download"), bg=c["card"]).pack(side="left", padx=(7, 0))
        app.rbutton(actions, self.t("common.cancel"),
                    lambda: self._finish("cancel"), bg=c["card"]).pack(side="right")
        self.protocol("WM_DELETE_WINDOW", lambda: self._finish("cancel"))
        self.bind("<Escape>", lambda _e: self._finish("cancel"))
        self.update_idletasks()
        width, height = 540, max(300, self.winfo_reqheight())
        try:
            x = app.winfo_rootx() + (app.winfo_width() - width) // 2
            y = app.winfo_rooty() + (app.winfo_height() - height) // 2
        except tk.TclError:
            x = y = 120
        self.geometry(f"{width}x{height}+{max(0, x)}+{max(0, y)}")
        self.grab_set()
        self.lift()
        self.focus_force()

    def _finish(self, action):
        self.result = action
        try:
            self.grab_release()
        except tk.TclError:
            pass
        self.destroy()


def _build_frameless_dialog_titlebar(window, app, title):
    """Единая тёмная шапка для дочерних окон без системной белой рамки."""
    c = app.colors
    window.overrideredirect(True)
    border = _mix_color(c["accent"], c["page"], 0.62)
    window.configure(highlightthickness=1, highlightbackground=border, highlightcolor=border)
    GradientLine(window, c["accent"], c["page"]).pack(side="top", fill="x")
    window.after(120, lambda: fit_toplevel_to_content(window))

    # У безрамочных Toplevel на Windows transient иногда перестаёт надёжно
    # удерживать дочернее окно над родителем. Поднимаем окно при показе и
    # возвращении фокуса, а topmost используем только кратковременно.
    def raise_dialog(_event=None):
        try:
            if not window.winfo_exists() or window.state() == "iconic":
                return
            window.lift()
            window.attributes("-topmost", True)
            window.after(350, lambda: (
                window.attributes("-topmost", False)
                if window.winfo_exists() and window.state() != "iconic" else None
            ))
        except tk.TclError:
            pass

    window.bind("<Map>", raise_dialog, add="+")
    window.bind("<FocusIn>", raise_dialog, add="+")
    window.after_idle(raise_dialog)

    bar = tk.Frame(window, bg=c["page"], height=42, bd=0, highlightthickness=0)
    bar.pack(side="top", fill="x")
    bar.pack_propagate(False)
    mark = tk.Label(bar, text="◆", bg=c["page"], fg=c["accent"],
                    font=(app.ui_font, 10, "bold"))
    mark.pack(side="left", padx=(14, 9))
    caption = tk.Label(bar, text=title, bg=c["page"], fg=c["text"],
                       font=(app.ui_font, 9, "bold"))
    caption.pack(side="left", fill="y")
    controls = tk.Frame(bar, bg=c["page"])
    controls.pack(side="right", fill="y", padx=(0, 7))

    def minimize():
        try:
            window.overrideredirect(False)
            window.iconify()
            def restore_frameless():
                try:
                    if window.winfo_exists() and window.state() != "iconic":
                        window.overrideredirect(True)
                        raise_dialog()
                except tk.TclError:
                    pass
            window.after(300, restore_frameless)
        except tk.TclError:
            pass

    RoundIconButton(controls, "—", minimize, size=28, bg=c["page"], fg=c["muted"],
                    hover_bg=c["card_alt"], hover_fg=c["text"],
                    font=(app.ui_font, 13), glyph="minimize").pack(side="left", padx=2, pady=6)
    RoundIconButton(controls, "×", window.destroy, size=28, bg=c["page"], fg=c["muted"],
                    hover_bg=c["danger"], hover_fg="#ffffff",
                    font=(app.ui_font, 13), glyph="close").pack(side="left", padx=2, pady=6)

    def start_drag(event):
        window._dialog_drag_x = event.x_root - window.winfo_x()
        window._dialog_drag_y = event.y_root - window.winfo_y()

    def drag(event):
        window.geometry(f"+{event.x_root-window._dialog_drag_x}+{event.y_root-window._dialog_drag_y}")

    for widget in (bar, mark, caption):
        widget.bind("<ButtonPress-1>", start_drag)
        widget.bind("<B1-Motion>", drag)
        widget.bind("<Double-Button-1>", lambda _event: None)
    return bar


class AgentSkillManager(tk.Toplevel):
    """Редактор prompt-агентов и Markdown-навыков, хранящихся в config.json."""

    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.t = app.t
        self.title(self.t("agents.title"))
        self.geometry("920x720")
        self.minsize(760, 620)
        self.configure(bg=app.colors["page"])
        self.transient(app)
        _build_frameless_dialog_titlebar(self, app, self.t("agents.header"))
        self.grab_set()
        self._bindings = []
        self._build()
        self.refresh_lists()

    # --- локализация -------------------------------------------------------
    def _i18n_bind(self, widget, key, **params):
        self._bindings.append((widget, key, params))
        try:
            widget.configure(text=self.t(key, **params))
        except tk.TclError:
            pass
        return widget

    def retranslate(self):
        try:
            self.title(self.t("agents.title"))
            for widget, key, params in self._bindings:
                try:
                    widget.configure(text=self.t(key, **params))
                except tk.TclError:
                    continue
            self.tabs.tab(self.agent_tab, text="  " + self.t("agents.tab_agents") + "  ")
            self.tabs.tab(self.skill_tab, text="  " + self.t("agents.tab_skills") + "  ")
        except tk.TclError:
            pass

    def _build(self):
        c = self.app.colors
        header_panel = RoundedPanel(self, page=c["page"], fill=c["card"],
                                    shadow=c["shadow"], radius=23, padding=16, lateral=8)
        header_panel.pack(fill="x", padx=12, pady=(12, 8))
        header = tk.Frame(header_panel.inner, bg=c["card"])
        header.pack(fill="x")
        title_col = tk.Frame(header, bg=c["card"])
        title_col.pack(side="left", fill="x", expand=True)
        self._i18n_bind(tk.Label(title_col, text=self.t("agents.header"), bg=c["card"],
                                 fg=c["text"], font=(self.app.ui_font, 16, "bold")),
                        "agents.header").pack(anchor="w")
        self._i18n_bind(tk.Label(title_col, text=self.t("agents.subtitle"), bg=c["card"],
                                 fg=c["muted"], font=(self.app.ui_font, 9)),
                        "agents.subtitle").pack(anchor="w", pady=(4, 0))
        self.tabs = ttk.Notebook(self, style="App.TNotebook")
        self.tabs.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        self.agent_tab = tk.Frame(self.tabs, bg=c["page"], padx=4, pady=8)
        self.skill_tab = tk.Frame(self.tabs, bg=c["page"], padx=4, pady=8)
        self.tabs.add(self.agent_tab, text="  " + self.t("agents.tab_agents") + "  ")
        self.tabs.add(self.skill_tab, text="  " + self.t("agents.tab_skills") + "  ")
        self._build_agent_tab()
        self._build_skill_tab()

    def _label(self, parent, key):
        label = tk.Label(parent, text=self.t(key), bg=self.app.colors["card"],
                         fg=self.app.colors["muted"], font=(self.app.ui_font, 9, "bold"))
        label.pack(anchor="w", pady=(7, 3))
        self._i18n_bind(label, key)
        return label

    def _entry(self, parent, var):
        c = self.app.colors
        entry = tk.Entry(parent, textvariable=var, relief="flat", bd=0, bg=c["field"],
                         fg=c["text"], insertbackground=c["text"], selectbackground=c["accent"],
                         selectforeground="#171225", highlightthickness=1,
                         highlightbackground=c["border"], highlightcolor=c["accent"],
                         font=(self.app.ui_font, 10))
        entry.pack(fill="x", ipady=8)
        return entry

    def _text(self, parent, height=8):
        c = self.app.colors
        widget = tk.Text(parent, height=height, wrap="word", bg=c["field"],
                         fg=c["text"], insertbackground=c["text"], selectbackground=c["accent"],
                         selectforeground="#171225", relief="flat", bd=0, padx=12, pady=10,
                         font=(self.app.ui_font, 10), highlightthickness=1,
                         highlightbackground=c["border"], highlightcolor=c["accent"],
                         spacing1=2, spacing3=3)
        widget.pack(fill="both", expand=True)
        return widget

    def _editor_panel(self, parent, *, width=None):
        c = self.app.colors
        panel = RoundedPanel(parent, page=c["page"], fill=c["card"],
                             shadow=c["shadow"], radius=21, padding=15, lateral=8)
        if width:
            panel.configure(width=width)
            panel.pack_propagate(False)
        return panel

    def _build_agent_tab(self):
        c = self.app.colors
        left_panel = self._editor_panel(self.agent_tab, width=248)
        left_panel.pack(side="left", fill="y", padx=(0, 10))
        left = tk.Frame(left_panel.inner, bg=c["card"])
        left.pack(fill="both", expand=True)
        self._i18n_bind(tk.Label(left, text=self.t("agents.created"), bg=c["card"],
                                 fg=c["text"], font=(self.app.ui_font, 9, "bold")),
                        "agents.created").pack(anchor="w", pady=(0, 8))
        list_wrap = tk.Frame(left, bg=c["card"])
        list_wrap.pack(fill="both", expand=True)
        self.agent_list = tk.Listbox(list_wrap, bg=c["field"], fg=c["text"],
                                     selectbackground=c["accent"], selectforeground="#171225",
                                     relief="flat", bd=0, highlightthickness=1,
                                     highlightbackground=c["border"], highlightcolor=c["accent"],
                                     activestyle="none", exportselection=False,
                                     font=(self.app.ui_font, 10))
        self.agent_list.pack(fill="both", expand=True)
        self.agent_list.bind("<<ListboxSelect>>", self.load_agent)
        row = tk.Frame(left, bg=c["card"])
        row.pack(fill="x", pady=(10, 0))
        new_button = self.app.rbutton(row, self.t("agents.new"), self.new_agent, bg=c["card"])
        new_button.pack(side="left", fill="x", expand=True)
        self._i18n_bind(new_button, "agents.new")
        delete_button = self.app.rbutton(row, self.t("agents.delete"), self.delete_agent,
                                         variant="danger", bg=c["card"])
        delete_button.pack(side="left", padx=(6, 0))
        self._i18n_bind(delete_button, "agents.delete")

        right_panel = self._editor_panel(self.agent_tab)
        right_panel.pack(side="left", fill="both", expand=True)
        right = tk.Frame(right_panel.inner, bg=c["card"])
        right.pack(fill="both", expand=True)
        self.agent_id = None
        self.agent_name = tk.StringVar()
        self.agent_desc = tk.StringVar()
        self.agent_temp = tk.StringVar(value="0.7")
        self.agent_max = tk.StringVar(value="1024")
        self._label(right, "agents.name")
        self._entry(right, self.agent_name)
        self._label(right, "agents.description")
        self._entry(right, self.agent_desc)
        self._label(right, "agents.prompt")
        self.agent_prompt = self._text(right, 5)
        opts = tk.Frame(right, bg=c["card"])
        opts.pack(fill="x", pady=(10, 0))
        self._i18n_bind(tk.Label(opts, text=self.t("agents.temperature"), bg=c["card"],
                                 fg=c["muted"], font=(self.app.ui_font, 9, "bold")),
                        "agents.temperature").pack(side="left")
        temp_entry = tk.Entry(opts, textvariable=self.agent_temp, width=8, bg=c["field"],
                              fg=c["text"], insertbackground=c["text"], relief="flat",
                              highlightthickness=1, highlightbackground=c["border"],
                              highlightcolor=c["accent"], font=(self.app.ui_font, 9))
        temp_entry.pack(side="left", padx=(7, 18), ipady=6)
        self._i18n_bind(tk.Label(opts, text=self.t("agents.max_tokens"), bg=c["card"],
                                 fg=c["muted"], font=(self.app.ui_font, 9, "bold")),
                        "agents.max_tokens").pack(side="left")
        max_entry = tk.Entry(opts, textvariable=self.agent_max, width=10, bg=c["field"],
                             fg=c["text"], insertbackground=c["text"], relief="flat",
                             highlightthickness=1, highlightbackground=c["border"],
                             highlightcolor=c["accent"], font=(self.app.ui_font, 9))
        max_entry.pack(side="left", padx=7, ipady=6)
        self._label(right, "agents.agent_skills")
        self.agent_skill_list = tk.Listbox(right, height=4, selectmode="multiple",
                                           exportselection=False, bg=c["field"], fg=c["text"],
                                           selectbackground=c["accent"], selectforeground="#171225",
                                           relief="flat", bd=0, highlightthickness=1,
                                           highlightbackground=c["border"], font=(self.app.ui_font, 9))
        self.agent_skill_list.pack(fill="x")
        save_button = self.app.rbutton(right, self.t("agents.save_agent"), self.save_agent,
                                       variant="accent", bg=c["card"])
        save_button.pack(anchor="e", pady=(12, 2))
        self._i18n_bind(save_button, "agents.save_agent")

    def _build_skill_tab(self):
        c = self.app.colors
        left_panel = self._editor_panel(self.skill_tab, width=248)
        left_panel.pack(side="left", fill="y", padx=(0, 10))
        left = tk.Frame(left_panel.inner, bg=c["card"])
        left.pack(fill="both", expand=True)
        self._i18n_bind(tk.Label(left, text=self.t("agents.library"), bg=c["card"],
                                 fg=c["text"], font=(self.app.ui_font, 9, "bold")),
                        "agents.library").pack(anchor="w", pady=(0, 8))
        self.skill_list = tk.Listbox(left, bg=c["field"], fg=c["text"],
                                     selectbackground=c["accent"], selectforeground="#171225",
                                     relief="flat", bd=0, highlightthickness=1,
                                     highlightbackground=c["border"], highlightcolor=c["accent"],
                                     activestyle="none", exportselection=False,
                                     font=(self.app.ui_font, 10))
        self.skill_list.pack(fill="both", expand=True)
        self.skill_list.bind("<<ListboxSelect>>", self.load_skill)
        row = tk.Frame(left, bg=c["card"])
        row.pack(fill="x", pady=(10, 0))
        new_button = self.app.rbutton(row, self.t("agents.new"), self.new_skill, bg=c["card"])
        new_button.pack(side="left", fill="x", expand=True)
        self._i18n_bind(new_button, "agents.new")
        delete_button = self.app.rbutton(row, self.t("agents.delete"), self.delete_skill,
                                         variant="danger", bg=c["card"])
        delete_button.pack(side="left", padx=(6, 0))
        self._i18n_bind(delete_button, "agents.delete")

        right_panel = self._editor_panel(self.skill_tab)
        right_panel.pack(side="left", fill="both", expand=True)
        right = tk.Frame(right_panel.inner, bg=c["card"])
        right.pack(fill="both", expand=True)
        self.skill_id = None
        self.skill_name = tk.StringVar()
        self.skill_desc = tk.StringVar()
        self.skill_enabled = tk.BooleanVar(value=True)
        self._label(right, "agents.skill_name")
        self._entry(right, self.skill_name)
        self._label(right, "agents.skill_desc")
        self._entry(right, self.skill_desc)
        enabled_row = tk.Frame(right, bg=c["card"])
        enabled_row.pack(fill="x", pady=(8, 0))
        self._i18n_bind(tk.Label(enabled_row, text=self.t("agents.skill_active"), bg=c["card"],
                                 fg=c["muted"], font=(self.app.ui_font, 9, "bold")),
                        "agents.skill_active").pack(side="left")
        NeumorphicSwitch(enabled_row, self.skill_enabled, bg=c["card"]).pack(side="right")
        self._label(right, "agents.skill_content")
        self.skill_content = self._text(right, 9)
        row = tk.Frame(right, bg=c["card"])
        row.pack(fill="x", pady=(10, 2))
        save_button = self.app.rbutton(row, self.t("agents.save"), self.save_skill,
                                       variant="accent", bg=c["card"])
        save_button.pack(side="left")
        self._i18n_bind(save_button, "agents.save")
        import_button = self.app.rbutton(row, self.t("agents.import"), self.import_skill,
                                         bg=c["card"])
        import_button.pack(side="left", padx=6)
        self._i18n_bind(import_button, "agents.import")
        export_button = self.app.rbutton(row, self.t("agents.export"), self.export_skill,
                                         bg=c["card"])
        export_button.pack(side="left")
        self._i18n_bind(export_button, "agents.export")

    def refresh_lists(self, select_agent=None, select_skill=None):
        agents = self.app.config_data.setdefault("agents", [])
        skills = self.app.config_data.setdefault("skills", [])
        unnamed = self.t("agents.unnamed")
        self.agent_list.delete(0, "end")
        for agent in agents:
            self.agent_list.insert("end", agent.get("name", unnamed))
        self.skill_list.delete(0, "end")
        for skill in skills:
            marker = "● " if skill.get("enabled", True) else "○ "
            self.skill_list.insert("end", marker + skill.get("name", unnamed))
        self.agent_skill_list.delete(0, "end")
        for skill in skills:
            self.agent_skill_list.insert("end", skill.get("name", unnamed))
        if select_agent is not None:
            for index, agent in enumerate(agents):
                if agent.get("id") == select_agent:
                    self.agent_list.selection_set(index)
                    self.agent_list.see(index)
                    self.load_agent()
                    break
        elif agents and not self.agent_list.curselection():
            self.agent_list.selection_set(0)
            self.load_agent()
        if select_skill is not None:
            for index, skill in enumerate(skills):
                if skill.get("id") == select_skill:
                    self.skill_list.selection_set(index)
                    self.skill_list.see(index)
                    self.load_skill()
                    break

    def _selected(self, widget, records):
        selection = widget.curselection()
        return records[selection[0]] if selection and selection[0] < len(records) else None

    def load_agent(self, _event=None):
        agent = self._selected(self.agent_list, self.app.config_data.get("agents", []))
        if not agent:
            return
        self.agent_id = agent.get("id")
        self.agent_name.set(agent.get("name", ""))
        self.agent_desc.set(agent.get("description", ""))
        self.agent_temp.set(str(agent.get("temperature", 0.7)))
        self.agent_max.set(str(agent.get("max_tokens", 1024)))
        self.agent_prompt.delete("1.0", "end")
        self.agent_prompt.insert("1.0", agent.get("system_prompt", ""))
        self.agent_skill_list.selection_clear(0, "end")
        ids = set(agent.get("skill_ids", []))
        for index, skill in enumerate(self.app.config_data.get("skills", [])):
            if skill.get("id") in ids:
                self.agent_skill_list.selection_set(index)

    def new_agent(self):
        self.agent_id = None
        self.agent_list.selection_clear(0, "end")
        self.agent_name.set(self.t("agents.new_agent"))
        self.agent_desc.set("")
        self.agent_temp.set("0.7")
        self.agent_max.set("1024")
        self.agent_prompt.delete("1.0", "end")
        self.agent_prompt.insert("1.0", self.t("agents.default_prompt"))
        self.agent_skill_list.selection_clear(0, "end")

    def save_agent(self):
        name = self.agent_name.get().strip()
        if not name:
            messagebox.showwarning(self.t("agents.warn_agent"), self.t("agents.warn_name"),
                                   parent=self)
            return
        try:
            temperature = float(self.agent_temp.get())
            max_tokens = int(self.agent_max.get())
            if not 0 <= temperature <= 2 or not 1 <= max_tokens <= 32768:
                raise ValueError
        except ValueError:
            messagebox.showwarning(self.t("agents.warn_params"), self.t("agents.warn_params_text"),
                                   parent=self)
            return
        skills = self.app.config_data.setdefault("skills", [])
        selected = self.agent_skill_list.curselection()
        record = {"id": self.agent_id or uuid.uuid4().hex[:12], "name": name,
                  "description": self.agent_desc.get().strip(),
                  "system_prompt": self.agent_prompt.get("1.0", "end-1c").strip(),
                  "temperature": temperature, "max_tokens": max_tokens,
                  "skill_ids": [skills[i]["id"] for i in selected if i < len(skills)]}
        agents = self.app.config_data.setdefault("agents", [])
        old = next((i for i, a in enumerate(agents) if a.get("id") == self.agent_id), None)
        if old is None:
            agents.append(record)
        else:
            agents[old] = record
        self.agent_id = record["id"]
        self.app.config_data["active_agent"] = self.agent_id
        self.app.save_config_safely()
        self.refresh_lists(select_agent=self.agent_id)
        self.app.write_log(self.t("agents.saved_agent", name=name))

    def delete_agent(self):
        agent = self._selected(self.agent_list, self.app.config_data.get("agents", []))
        if not agent:
            return
        if not messagebox.askyesno(self.t("agents.confirm_agent"),
                                   self.t("agents.confirm_agent_text",
                                          name=agent.get("name")), parent=self):
            return
        self.app.config_data["agents"] = [x for x in self.app.config_data.get("agents", [])
                                          if x.get("id") != agent.get("id")]
        if self.app.config_data.get("active_agent") == agent.get("id"):
            self.app.config_data["active_agent"] = ""
        self.app.save_config_safely()
        self.refresh_lists()
        self.new_agent()

    def load_skill(self, _event=None):
        skill = self._selected(self.skill_list, self.app.config_data.get("skills", []))
        if not skill:
            return
        self.skill_id = skill.get("id")
        self.skill_name.set(skill.get("name", ""))
        self.skill_desc.set(skill.get("description", ""))
        self.skill_enabled.set(skill.get("enabled", True))
        self.skill_content.delete("1.0", "end")
        self.skill_content.insert("1.0", skill.get("content", ""))

    def new_skill(self):
        self.skill_id = None
        self.skill_list.selection_clear(0, "end")
        self.skill_name.set(self.t("agents.new_skill"))
        self.skill_desc.set("")
        self.skill_enabled.set(True)
        self.skill_content.delete("1.0", "end")

    def save_skill(self):
        name = self.skill_name.get().strip()
        content = self.skill_content.get("1.0", "end-1c").strip()
        if not name or not content:
            messagebox.showwarning(self.t("agents.warn_skill"),
                                   self.t("agents.warn_skill_text"), parent=self)
            return
        record = {"id": self.skill_id or uuid.uuid4().hex[:12], "name": name,
                  "description": self.skill_desc.get().strip(),
                  "enabled": bool(self.skill_enabled.get()), "content": content}
        skills = self.app.config_data.setdefault("skills", [])
        old = next((i for i, x in enumerate(skills) if x.get("id") == self.skill_id), None)
        if old is None:
            skills.append(record)
        else:
            skills[old] = record
        self.skill_id = record["id"]
        self.app.save_config_safely()
        self.refresh_lists(select_skill=self.skill_id)
        self.app.write_log(self.t("agents.saved_skill", name=name))

    def delete_skill(self):
        skill = self._selected(self.skill_list, self.app.config_data.get("skills", []))
        if not skill:
            return
        if not messagebox.askyesno(self.t("agents.confirm_skill"),
                                   self.t("agents.confirm_skill_text",
                                          name=skill.get("name")), parent=self):
            return
        skill_id = skill.get("id")
        self.app.config_data["skills"] = [x for x in self.app.config_data.get("skills", [])
                                          if x.get("id") != skill_id]
        for agent in self.app.config_data.get("agents", []):
            agent["skill_ids"] = [x for x in agent.get("skill_ids", []) if x != skill_id]
        self.app.save_config_safely()
        self.refresh_lists()
        self.new_skill()

    def import_skill(self):
        path = filedialog.askopenfilename(
            parent=self, title=self.t("agents.import_title"),
            filetypes=[("Markdown", "*.md"), (self.t("agents.all_files"), "*.*")])
        if not path:
            return
        try:
            content = Path(path).read_text(encoding="utf-8-sig")
            self.skill_id = None
            self.skill_name.set(Path(path).stem)
            self.skill_desc.set(self.t("agents.imported_from"))
            self.skill_enabled.set(True)
            self.skill_content.delete("1.0", "end")
            self.skill_content.insert("1.0", content)
            self.tabs.select(self.skill_tab)
        except Exception as exc:
            messagebox.showerror(self.t("agents.import"), str(exc), parent=self)

    def export_skill(self):
        skill = self._selected(self.skill_list, self.app.config_data.get("skills", []))
        if not skill:
            messagebox.showinfo(self.t("agents.export"), self.t("agents.pick_skill"), parent=self)
            return
        path = filedialog.asksaveasfilename(
            parent=self, title=self.t("agents.export_title"), defaultextension=".md",
            initialfile=skill.get("name", "skill") + ".md",
            filetypes=[("Markdown", "*.md")])
        if not path:
            return
        try:
            Path(path).write_text(skill.get("content", ""), encoding="utf-8")
            self.app.write_log(self.t("agents.exported", path=path))
        except Exception as exc:
            messagebox.showerror(self.t("agents.export"), str(exc), parent=self)


class LocalChatWindow(tk.Toplevel):
    """Нативный чат к OpenAI-compatible API локального llama-server.

    Состояние потока живёт в ChatTranscript: экран показывает ровно те куски,
    что пришли от API, каждый один раз. Поэтому «hhello» невозможно как класс
    ошибки: нет второго пути вставки первого куска и нет постобработки текста.
    """

    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.t = app.t
        self.title(self.t("chat.title"))
        self.geometry("900x720")
        self.minsize(680, 520)
        self.configure(bg=app.colors["page"])
        self.transient(app)
        _build_frameless_dialog_titlebar(self, app, self.t("chat.title"))
        self.chat = ChatTranscript()
        self.events = queue.Queue()
        self.busy = False
        self._stream_started = False
        self._binds = EventSubscription()
        self._bindings = []
        self._tooltips = []
        agents = app.config_data.setdefault("agents", [])
        if not agents:
            default = {"id": uuid.uuid4().hex[:12], "name": "Универсальный помощник",
                       "description": "Базовый ассистент",
                       "system_prompt": "Ты полезный локальный ИИ-ассистент. Отвечай ясно, "
                                        "проверяй факты и не выдумывай возможности.",
                       "temperature": 0.7, "max_tokens": 1024, "skill_ids": []}
            agents.append(default)
            app.config_data["active_agent"] = default["id"]
            app.save_config_safely()
        names = [a.get("name", "Агент") for a in agents]
        active = next((a for a in agents if a.get("id") == app.config_data.get("active_agent")),
                      agents[0])
        self.agent_var = tk.StringVar(value=active.get("name", names[0]))
        self.busy_var = tk.StringVar(value="")
        self._build()
        self._binds.bind(self, "<Control-Return>", self._send_event, key="send")
        self._binds.bind(self, "<Destroy>", self._on_destroy, key="destroy")
        self._poll_job = self.after(100, self._poll_events)
        self.lift()
        self.focus_force()

    # --- интерфейс ---------------------------------------------------------
    def _i18n_bind(self, widget, key, **params):
        self._bindings.append((widget, key, params))
        try:
            widget.configure(text=self.t(key, **params))
        except tk.TclError:
            pass
        return widget

    def retranslate(self):
        """Перерисовывает подписи и весь транскрипт на выбранном языке."""
        try:
            self.title(self.t("chat.title"))
            for widget, key, params in self._bindings:
                try:
                    widget.configure(text=self.t(key, **params))
                except tk.TclError:
                    continue
            for tip, key in self._tooltips:
                tip.set_text(self.t(key))
            self._render_all()
        except tk.TclError:
            pass

    def _build(self):
        c = self.app.colors
        shell = tk.Frame(self, bg=c["page"], padx=12, pady=12)
        shell.pack(fill="both", expand=True)
        header_panel = RoundedPanel(shell, page=c["page"], fill=c["card"],
                                    shadow=c["shadow"], radius=22, padding=14, lateral=8)
        header_panel.pack(fill="x", pady=(0, 10))
        head = tk.Frame(header_panel.inner, bg=c["card"])
        head.pack(fill="x")
        title_col = tk.Frame(head, bg=c["card"])
        title_col.pack(side="left", fill="x", expand=True)
        self._i18n_bind(tk.Label(title_col, text=self.t("chat.title"), bg=c["card"], fg=c["text"],
                            font=(self.app.ui_font, 15, "bold")), "chat.title").pack(anchor="w")
        self._i18n_bind(tk.Label(title_col, text=self.t("chat.subtitle"), bg=c["card"],
                            fg=c["muted"], font=(self.app.ui_font, 8)),
                   "chat.subtitle").pack(anchor="w", pady=(2, 0))
        controls = tk.Frame(head, bg=c["card"])
        controls.pack(side="right")
        self._i18n_bind(tk.Label(controls, text=self.t("chat.agent"), bg=c["card"], fg=c["muted"],
                            font=(self.app.ui_font, 8, "bold")), "chat.agent").pack(
            side="left", padx=(0, 7))
        self.agent_combo = ttk.Combobox(
            controls, textvariable=self.agent_var, state="readonly",
            values=[a.get("name", "Агент") for a in self.app.config_data.get("agents", [])],
            width=23)
        self.agent_combo.pack(side="left")
        self.agent_combo.bind("<<ComboboxSelected>>", self._agent_changed)
        configure = self.app.rbutton(controls, self.t("chat.configure"),
                                     self._manage_agents, bg=c["card"])
        configure.pack(side="left", padx=(7, 0))
        self._i18n_bind(configure, "chat.configure")

        transcript_panel = RoundedPanel(shell, page=c["page"], fill=c["card"],
                                        shadow=c["shadow"], radius=22, padding=10,
                                        lateral=8, stretch=True)
        transcript_panel.pack(fill="both", expand=True, pady=(0, 8))
        self.transcript = tk.Text(
            transcript_panel.inner, bg=c["log"], fg=c["text"], insertbackground=c["accent"],
            relief="flat", bd=0, wrap="word", state="disabled", height=6,
            font=(self.app.ui_font, 11, "normal"), padx=20, pady=18,
            highlightthickness=1, highlightbackground=c["border"],
            selectbackground=c["accent"], selectforeground="#ffffff",
            spacing1=2, spacing3=4)
        transcript_scroll = ttk.Scrollbar(transcript_panel.inner, orient="vertical",
                                          command=self.transcript.yview)
        self.transcript.configure(yscrollcommand=transcript_scroll.set)
        self.transcript.pack(side="left", fill="both", expand=True)
        transcript_scroll.pack(side="right", fill="y", padx=(7, 0))
        self._empty_hint = tk.Label(self.transcript, bg=c["log"], fg=c["muted"],
                                    font=(self.app.ui_font, 10), justify="center")
        self._i18n_bind(self._empty_hint, "chat.empty")
        self._empty_hint.place(relx=0.5, rely=0.45, anchor="center")
        for role_name, color in (("user", "#a98af0"), ("assistant", c["muted"]), ("error", "#e59ab4")):
            self.transcript.tag_configure("label_" + role_name, foreground=color, spacing1=14,
                                          spacing3=3, font=(self.app.ui_font, 8, "bold"))
        self.transcript.tag_configure("user", foreground="#e4d8ff", spacing1=2,
                                      spacing3=8, font=(self.app.ui_font, 11, "bold"))
        self.transcript.tag_configure("assistant", foreground=c["text"], spacing1=2,
                                      spacing3=16, font=(self.app.ui_font, 11))
        self.transcript.tag_configure("error", foreground="#e59ab4", spacing1=2, spacing3=10)
        self._i18n_bind(tk.Label(shell, textvariable=self.busy_var, bg=c["page"], fg=c["accent_hover"],
                            font=(self.app.ui_font, 8, "bold")), "chat.generating")

        input_panel = RoundedPanel(shell, page=c["page"], fill=c["card"],
                                   shadow=c["shadow"], radius=22, padding=11, lateral=8)
        input_panel.pack(fill="x")
        bottom = tk.Frame(input_panel.inner, bg=c["card"])
        bottom.pack(fill="x")
        bottom.columnconfigure(0, weight=1)
        self.input = tk.Text(bottom, height=4, wrap="word", bg=c["field"], fg=c["text"],
                             insertbackground=c["text"], relief="flat", bd=0,
                             font=(self.app.ui_font, 11), padx=16, pady=13,
                             highlightthickness=1, highlightbackground=c["border"],
                             highlightcolor=c["accent"], selectbackground=c["accent"],
                             selectforeground="#ffffff")
        self.input.grid(row=0, column=0, sticky="nsew")
        self.send_btn = self.app.rbutton(bottom, self.t("chat.send"), self.send_message,
                                         variant="accent", pad=(20, 12), bg=c["card"])
        self.send_btn.grid(row=0, column=1, sticky="se", padx=(10, 0))
        self._i18n_bind(self.send_btn, "chat.send")
        self._i18n_bind(tk.Label(shell, bg=c["page"], fg=c["muted"], text=self.t("chat.hint"),
                            font=(self.app.ui_font, 8, "bold")), "chat.hint").pack(
            anchor="w", padx=6, pady=(7, 0))
        self.input.focus_set()

    # --- отрисовка ---------------------------------------------------------
    def _label_for(self, role):
        return {"user": self.t("chat.you"), "assistant": self.t("chat.model"),
                "error": self.t("chat.error")}.get(role, str(role).upper())

    def _hide_empty_hint(self, show=False):
        hint = getattr(self, "_empty_hint", None)
        if hint is None:
            return
        try:
            if show:
                hint.place(relx=0.5, rely=0.45, anchor="center")
            else:
                hint.place_forget()
        except tk.TclError:
            pass

    def _append(self, role, text):
        self._hide_empty_hint()
        self.transcript.configure(state="normal")
        self.transcript.insert("end", self._label_for(role) + "\n", "label_" + str(role))
        self.transcript.insert("end", text + "\n\n", role)
        self.transcript.configure(state="disabled")
        self.transcript.see("end")

    def _render_stream_chunk(self, text):
        """Первый кусок печатает подпись, дальше — только текст (ровно один раз)."""
        self._hide_empty_hint()
        if not self._stream_started:
            self.transcript.configure(state="normal")
            self.transcript.insert("end", self._label_for("assistant") + "\n", "label_assistant")
            self.transcript.configure(state="disabled")
            self._stream_started = True
        self.transcript.configure(state="normal")
        self.transcript.insert("end", text, "assistant")
        self.transcript.configure(state="disabled")
        self.transcript.see("end")

    def _render_all(self):
        """Полная перерисовка из состояния (нужна после смены языка)."""
        self.transcript.configure(state="normal")
        self.transcript.delete("1.0", "end")
        self.transcript.configure(state="disabled")
        self._stream_started = False
        blocks = self.chat.blocks()
        self._hide_empty_hint(show=not blocks)
        for index, (role, text) in enumerate(blocks):
            last = index == len(blocks) - 1
            if last and self.chat.streaming and text:
                # Текущий поток: подпись + уже полученный текст, без лишней пустой строки.
                self.transcript.configure(state="normal")
                self.transcript.insert("end", self._label_for("assistant") + "\n", "label_assistant")
                self.transcript.insert("end", text, "assistant")
                self.transcript.configure(state="disabled")
                self._stream_started = True
            else:
                self._append(role, text)
        self.transcript.see("end")

    # --- агенты ------------------------------------------------------------
    def _active_agent(self):
        return next((a for a in self.app.config_data.get("agents", [])
                     if a.get("name") == self.agent_var.get()), None)

    def _agent_changed(self, _event=None):
        agent = self._active_agent()
        if agent:
            self.app.config_data["active_agent"] = agent.get("id", "")
            self.app.save_config_safely()
            self.chat = ChatTranscript()
            self._render_all()
            self._append("assistant", self.t("chat.agent_changed",
                                             name=agent.get("name", "")))

    @staticmethod
    def _trim_history(messages, max_messages=24):
        """Совместимость: та же обрезка истории, что делает ChatTranscript."""
        trimmed = list(messages[-max_messages:])
        while trimmed and trimmed[0].get("role") != "user":
            trimmed.pop(0)
        return trimmed

    def _manage_agents(self):
        self.app.open_agent_skill_manager()
        manager = getattr(self.app, "agent_skill_window", None)
        if manager is not None:
            # Одна подписка на окно: раньше обработчик добавлялся при каждом клике.
            self._agents_manager = manager
            manager.bind("<Destroy>", self._on_agents_closed, add="+")

    def _on_agents_closed(self, event):
        manager = getattr(self, "_agents_manager", None)
        if manager is not None and event.widget is manager:
            self._agents_manager = None
            self._refresh_agents()

    def _refresh_agents(self):
        if not self.winfo_exists():
            return
        values = [a.get("name", "Агент") for a in self.app.config_data.get("agents", [])]
        self.agent_combo.configure(values=values)
        if values and self.agent_var.get() not in values:
            self.agent_var.set(values[0])
            self._agent_changed()

    # --- отправка ----------------------------------------------------------
    def _send_event(self, _event=None):
        self.send_message()
        return "break"

    def send_message(self):
        if self.busy:
            return
        text = self.input.get("1.0", "end-1c").strip()
        if not text:
            return
        try:
            port = int(self.app.port_var.get())
            if not 1 <= port <= 65535:
                raise ValueError
        except (TypeError, ValueError):
            self.chat.add_error(self.t("chat.bad_port"))
            self._render_all()
            return
        agent = self._active_agent()
        if not agent:
            self.chat.add_error(self.t("chat.no_agent"))
            self._render_all()
            return
        self.input.delete("1.0", "end")
        self.chat.add_user(text)
        self._append("user", text)
        self.chat.begin_stream()
        self._stream_started = False
        try:
            temperature = max(0.0, min(2.0, float(agent.get("temperature", 0.7))))
        except (TypeError, ValueError):
            temperature = 0.7
        try:
            agent_tokens = max(1, min(32768, int(agent.get("max_tokens", 1024))))
        except (TypeError, ValueError):
            agent_tokens = 1024
        cap = max_output_tokens(self.app.ctx_var.get(), agent_tokens, engine_max=8192)
        try:
            launch_cap = int(self.app.max_tokens_var.get())
        except (AttributeError, TypeError, ValueError):
            launch_cap = cap
        max_tokens = max(1, min(cap, launch_cap))
        payload = {
            "model": self.app.model_alias() or "local-model",
            "messages": [{"role": "system",
                          "content": self.app.build_agent_system_prompt(agent.get("id"))}]
                        + self.chat.history_for_api(max_messages=24),
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }
        url = f"http://127.0.0.1:{port}/v1/chat/completions"
        self.busy = True
        self.send_btn.configure(state="disabled")
        self.busy_var.set(self.t("chat.generating"))
        threading.Thread(target=self._request_worker, args=(url, payload), daemon=True).start()

    def _request_worker(self, url, payload):
        try:
            stream_chat_completion(
                url, payload, timeout=600,
                on_chunk=lambda text: self.events.put(("chunk", text)))
            self.events.put(("done", None))
        except Exception as exc:
            self.events.put(("error", str(exc)))

    def _finish_busy(self):
        self.busy = False
        try:
            self.send_btn.configure(state="normal")
            self.busy_var.set("")
        except tk.TclError:
            pass

    def _poll_events(self):
        try:
            while True:
                kind, text = self.events.get_nowait()
                if kind == "chunk":
                    if not self.chat.streaming:
                        continue
                    self.chat.feed(text)
                    self._render_stream_chunk(text)
                elif kind == "done":
                    if self.chat.streaming:
                        self.chat.finish()
                    if self._stream_started:
                        self.transcript.configure(state="normal")
                        self.transcript.insert("end", "\n\n")
                        self.transcript.configure(state="disabled")
                        self.transcript.see("end")
                    self._stream_started = False
                    self._finish_busy()
                else:
                    if self.chat.streaming:
                        self.chat.cancel(self.t("chat.stream_stopped"))
                    self.chat.add_error(text)
                    self._render_all()
                    self._finish_busy()
        except queue.Empty:
            pass
        except tk.TclError:
            return
        try:
            if self.winfo_exists():
                self._poll_job = self.after(100, self._poll_events)
        except tk.TclError:
            pass

    def _on_destroy(self, event):
        if event.widget is self:
            try:
                self.after_cancel(self._poll_job)
            except Exception:
                pass


class RuntimeDashboard(tk.Toplevel):
    """Информационная панель локального процесса и рекомендаций по настройке."""
    def __init__(self,app):
        super().__init__(app); self.app=app; self.title(app.t("dash.title")); self.geometry("760x680"); self.minsize(620,540)
        self.configure(bg=app.colors["page"]); self.transient(app)
        # Без белой системной шапки, как у чата и агентов.
        _build_frameless_dialog_titlebar(self, app, app.t("dash.header"))
        self._gpu_queue=queue.Queue(); self._gpu_query_pending=False; self._last_gpu_query=0.0; self._gpu_mb=None
        self._api_queue=queue.Queue(); self._api_query_pending=False; self._last_api_query=0.0; self._api_state=None
        self._build(); self._refresh()
        self.lift(); self.focus_force()

    def _build(self):
        c = self.app.colors
        root = tk.Frame(self, bg=c["page"], padx=14, pady=14)
        root.pack(fill="both", expand=True)
        header_panel = RoundedPanel(root, page=c["page"], fill=c["card"],
                                    shadow=c["shadow"], radius=22, padding=15, lateral=8)
        header_panel.pack(fill="x", pady=(0, 10))
        head = tk.Frame(header_panel.inner, bg=c["card"])
        head.pack(fill="x")
        self.header_label = tk.Label(head, text=self.app.t("dash.header"), bg=c["card"],
                                     fg=c["text"], font=(self.app.ui_font, 15, "bold"))
        self.header_label.pack(anchor="w")
        self.status = tk.StringVar(value=self.app.t("dash.waiting"))
        tk.Label(head, textvariable=self.status, bg=c["card"], fg=c["success"],
                 font=(self.app.ui_font, 9, "bold")).pack(anchor="w", pady=(4, 0))

        data_panel = RoundedPanel(root, page=c["page"], fill=c["card"],
                                  shadow=c["shadow"], radius=22, padding=11,
                                  lateral=8, stretch=True)
        data_panel.pack(fill="both", expand=True, pady=(0, 10))
        self.metrics = tk.Text(data_panel.inner, bg=c["log"], fg=c["text"],
                               relief="flat", bd=0, state="disabled", wrap="word",
                               font=(self.app.ui_font, 9), padx=14, pady=12,
                               highlightthickness=1, highlightbackground=c["border"],
                               selectbackground=c["accent"], selectforeground="#ffffff",
                               spacing1=2, spacing3=3)
        self.metrics.pack(fill="both", expand=True)
        self.metrics.tag_configure("section", foreground=c["accent_hover"],
                                   font=(self.app.ui_font, 9, "bold"), spacing1=9, spacing3=4)
        self.metrics.tag_configure("value", foreground=c["text"], font=(self.app.ui_font, 9))

        tips_panel = RoundedPanel(root, page=c["page"], fill=c["card"],
                                  shadow=c["shadow"], radius=22, padding=13, lateral=8)
        tips_panel.pack(fill="x")
        tips_head = tk.Frame(tips_panel.inner, bg=c["card"])
        tips_head.pack(fill="x", pady=(0, 7))
        self.tips_head = tk.Label(tips_head, text=self.app.t("dash.tips"), bg=c["card"],
                                  fg=c["accent_hover"], font=(self.app.ui_font, 9, "bold"))
        self.tips_head.pack(anchor="w")
        self.tips = tk.Text(tips_panel.inner, height=6, bg=c["field"], fg=c["muted"],
                            relief="flat", bd=0, state="disabled", wrap="word",
                            font=(self.app.ui_font, 9), padx=12, pady=9,
                            highlightthickness=1, highlightbackground=c["border"],
                            selectbackground=c["accent"], selectforeground="#ffffff")
        self.tips.pack(fill="x")

    def retranslate(self):
        try:
            self.title(self.app.t("dash.title"))
            self.header_label.configure(text=self.app.t("dash.header"))
            self.tips_head.configure(text=self.app.t("dash.tips"))
            self._refresh_body()
        except tk.TclError:
            pass

    def _query_gpu_memory(self, pid):
        value = gpu_process_vram_mb(pid)
        self._gpu_queue.put(value)

    def _query_api(self, port):
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{port}/v1/models", headers={"User-Agent": "LocalAIManager"})
            with urllib.request.urlopen(req, timeout=1.5) as response:
                self._api_queue.put((200 <= response.status < 300, f"HTTP {response.status}"))
        except Exception as exc:
            self._api_queue.put((False, str(exc)[:120]))

    def _refresh(self):
        if not self.winfo_exists():
            return
        try:
            # Скрытое окно не опрашивает железо.
            if lam_timers.should_pause(self):
                self._refresh_job = self.after(
                    lam_timers.next_delay("dashboard", hidden=True), self._refresh)
                return
            self._refresh_body()
        except Exception as exc:
            # Ошибка в телеметрии не должна навсегда останавливать панель.
            try:
                self.status.set(f"{self.app.t('dash.waiting')} {exc}"[:120])
            except tk.TclError:
                pass
        finally:
            try:
                if self.winfo_exists():
                    self._refresh_job = self.after(lam_timers.next_delay("dashboard"),
                                                   self._refresh)
            except tk.TclError:
                pass

    def _refresh_body(self):
        if not self.winfo_exists():
            return
        try:
            while True:
                self._gpu_mb = self._gpu_queue.get_nowait()
                self._gpu_query_pending = False
        except queue.Empty:
            pass
        try:
            while True:
                self._api_state = self._api_queue.get_nowait()
                self._api_query_pending = False
        except queue.Empty:
            pass
        app = self.app
        t = app.t
        none = t("common.none")
        proc = app.process
        running = bool(proc and proc.poll() is None)
        selected = app.selected_path()
        lines = [
            t("dash.state_line",
              state=t("dash.state_running" if running else "dash.state_stopped")),
            t("dash.model", name=Path(selected).name if selected else t("dash.model_none")),
            t("dash.file", path=selected or none),
            t("dash.size", gb=Path(selected).stat().st_size / (1024 ** 3))
            if selected and Path(selected).is_file() else t("dash.file", path=none),
            t("dash.engine", engine=app.server_var.get() or none),
            t("dash.pid", pid=proc.pid if running else none),
            t("dash.api", url=f"http://127.0.0.1:{app.port_var.get()}/v1"),
            t("dash.ctx", ctx=app.ctx_var.get()),
            t("dash.gpu_layers", layers=app.gpu_var.get(), threads=app.threads_var.get()),
        ]
        if running:
            mem = process_memory_stats(proc.pid)
            if mem:
                lines += [t("dash.ws", gb=mem["working_set"] / (1024 ** 3)),
                          t("dash.private", gb=mem["private"] / (1024 ** 3)),
                          t("dash.peak", gb=mem["peak_working_set"] / (1024 ** 3))]
            else:
                lines.append(t("dash.no_mem"))
            if self._gpu_mb is not None:
                lines.append(t("dash.vram_proc", mb=self._gpu_mb))
            else:
                lines.append(t("dash.no_vram_proc"))
            if time.time() - self._last_gpu_query > 5 and not self._gpu_query_pending:
                self._gpu_query_pending = True
                self._last_gpu_query = time.time()
                threading.Thread(target=self._query_gpu_memory, args=(proc.pid,),
                                 daemon=True).start()
            try:
                import psutil
                lines.append(t("dash.cpu_proc",
                               percent=psutil.Process(proc.pid).cpu_percent(interval=None)))
            except Exception:
                lines.append(t("dash.no_cpu"))
            started = app.process_started_at
            if started:
                elapsed = int(time.time() - started)
                lines.append(t("dash.uptime", value=f"{elapsed // 3600:02d}:"
                                                   f"{(elapsed % 3600) // 60:02d}:{elapsed % 60:02d}"))
        free, total = available_ram_gb()
        if free is not None:
            lines.append(t("dash.sys_ram", free=free, total=total))
        else:
            lines.append(t("dash.sys_ram_none"))
        hardware = app.system_hardware or {}
        gpus = hardware.get("gpus", [])
        if gpus:
            items = "; ".join(f"{g.get('name', 'GPU')} · VRAM {g.get('vram', 0):.1f} GB"
                              for g in gpus)
            lines.append(t("dash.gpus", items=items))
        else:
            lines.append(t("dash.gpus_none"))
        lines.append(t("dash.vram_note"))
        try:
            port = int(app.port_var.get())
        except (TypeError, ValueError):
            port = 0
        if running and port and time.time() - self._last_api_query > 4 and not self._api_query_pending:
            self._api_query_pending = True
            self._last_api_query = time.time()
            threading.Thread(target=self._query_api, args=(port,), daemon=True).start()
        if self._api_state is not None:
            key = "dash.api_ready" if self._api_state[0] else "dash.api_not"
            api_text = t(key, detail=self._api_state[1])
        elif running:
            api_text = t("dash.api_checking")
        else:
            api_text = t("dash.api_stopped")
        lines.append(api_text)
        if running and self._api_state is not None and self._api_state[0]:
            self.status.set(t("dash.running"))
        elif running:
            self.status.set(t("dash.started"))
        else:
            self.status.set(t("dash.stopped"))
        self.metrics.configure(state="normal")
        self.metrics.delete("1.0", "end")
        self.metrics.insert("1.0", "\n".join(lines))
        self.metrics.configure(state="disabled")
        tip_lines = ["• " + item for item in app.get_optimization_tips()]
        self.tips.configure(state="normal")
        self.tips.delete("1.0", "end")
        self.tips.insert("1.0", "\n\n".join(tip_lines))
        self.tips.configure(state="disabled")



if __name__ == "__main__":
    _app = LocalAIManager()
    try:
        import ai_bridge

        ai_bridge.start(_app)
    except Exception as _bridge_error:  # мост не должен мешать обычному запуску
        try:
            sys.stderr.write("ai_bridge: %s\n" % _bridge_error)
        except Exception:
            pass
    _app.mainloop()
