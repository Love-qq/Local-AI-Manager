"""Универсальный загрузчик репозиториев Hugging Face (любые форматы).

Модуль не знает про Tkinter. Загрузка идёт потоком (1 МиБ), с докачкой через
HTTP Range, проверкой размера и SHA256 (если метаданные его дают), атомарным
переименованием .part -> файл и запретом молчаливой перезаписи.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from . import __version__
from .paths import UnsafePathError, safe_join
from .tls import https_handler
from .storage import (SUPPORTING_EXTENSIONS, WEIGHT_EXTENSIONS, extension_of,
                      is_mmproj_name, unique_path)

USER_AGENT = f"Local-AI-Manager/{__version__}"
API_ROOT = "https://huggingface.co/api/models"
WEB_ROOT = "https://huggingface.co"
CHUNK = 1024 * 1024

_OWNER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

ERROR_MESSAGES = {
    "invalid_url": "Некорректная ссылка на репозиторий Hugging Face: {detail}",
    "not_found": "Репозиторий или файл не найден (HTTP 404). Проверь имя репозитория и ревизию.",
    "auth": "Репозиторий закрытый или требует принятия условий (HTTP {code}). "
            "Укажи токен Hugging Face с доступом к нему.",
    "rate_limit": "Hugging Face ограничил частоту запросов (HTTP 429). "
                  "Повтори через {retry_after} с.",
    "network": "Сетевая ошибка: {detail}. Проверь подключение и повтори загрузку — "
               "она продолжится с места обрыва.",
    "interrupted": "Загрузка прервана: получено {received} из {total} байт. "
                   "Частичный файл сохранён, повтори загрузку.",
    "disk": "Недостаточно места на диске: нужно ещё примерно {needed} GB.",
    "integrity": "Файл повреждён: {detail}. Скачай его заново.",
    "http": "Сервер Hugging Face вернул HTTP {code}.",
    "cancelled": "Загрузка остановлена. Частичный файл сохранён для продолжения.",
}


class HFError(Exception):
    """Ошибка загрузки с машинным кодом и готовым текстом."""

    def __init__(self, kind, *, detail="", code=None, retry_after=None, **extra):
        self.kind = kind
        self.detail = detail
        self.code = code
        self.retry_after = retry_after
        self.extra = extra
        super().__init__(self.message(it=True))

    def message(self, *, retry_after=None, **extra) -> str:
        values = {"detail": self.detail, "code": self.code or "",
                  "retry_after": retry_after if retry_after is not None else (self.retry_after or 5)}
        values.update(self.extra)
        values.update(extra)
        template = ERROR_MESSAGES.get(self.kind, ERROR_MESSAGES["http"])
        try:
            return template.format(**values)
        except (KeyError, IndexError):
            return template


class DownloadCancelled(HFError):
    def __init__(self, detail=""):
        super().__init__("cancelled", detail=detail)


class InvalidRepoURL(HFError):
    def __init__(self, detail):
        super().__init__("invalid_url", detail=detail)


@dataclass(frozen=True)
class RepoRef:
    repo_id: str
    revision: str = "main"
    file_path: str = ""

    @property
    def owner(self) -> str:
        return self.repo_id.split("/", 1)[0]

    @property
    def repo(self) -> str:
        return self.repo_id.split("/", 1)[1]

    def api_url(self) -> str:
        return f"{API_ROOT}/{self.repo_id}"

    def resolve_url(self, filename=None, revision=None) -> str:
        name = filename if filename is not None else self.file_path
        rev = revision or self.revision
        quoted = urllib.parse.quote(str(name).lstrip("/"), safe="/")
        return f"{WEB_ROOT}/{self.repo_id}/resolve/{urllib.parse.quote(rev, safe='')}/{quoted}"


def _clean_parts(text):
    parts = []
    for part in str(text).split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise InvalidRepoURL("переход вверх по пути репозитория запрещён")
        parts.append(part)
    return parts


def parse_repo_url(text) -> RepoRef:
    """Разбирает ссылку или owner/repo (+/tree/<rev>, /blob/<rev>/<file>).

    Принимаются https://huggingface.co/owner/repo, .../tree/main,
    .../blob/main/file.gguf, hf.co/... и просто owner/repo.
    """
    raw = (text or "").strip()
    if not raw:
        raise InvalidRepoURL("ссылка пустая")
    if re.search(r"\s", raw):
        raise InvalidRepoURL("ссылка содержит пробелы")
    if "://" in raw:
        split = urllib.parse.urlsplit(raw)
        if split.scheme not in ("http", "https"):
            raise InvalidRepoURL(f"схема {split.scheme!r} не поддерживается")
        host = (split.hostname or "").lower()
        if host not in ("huggingface.co", "www.huggingface.co", "hf.co"):
            raise InvalidRepoURL(f"домен {host!r} не является huggingface.co")
        path = split.path
    else:
        path = raw
        if path.startswith("/"):
            raise InvalidRepoURL("ссылка не должна начинаться со слэша")
    parts = _clean_parts(path)
    if len(parts) < 2:
        raise InvalidRepoURL("нужно указать owner/repo")
    owner, repo = parts[0], parts[1]
    if not _OWNER_RE.match(owner) or not _OWNER_RE.match(repo):
        raise InvalidRepoURL("недопустимые символы в имени owner/repo")
    revision = "main"
    file_path = ""
    tail = parts[2:]
    if tail:
        marker = tail[0].lower()
        if marker == "tree":
            if len(tail) < 2:
                raise InvalidRepoURL("после /tree нужна ревизия")
            revision = tail[1]
            if len(tail) > 2:
                raise InvalidRepoURL("после /tree/<ревизия> не должно быть пути")
        elif marker == "blob":
            if len(tail) < 3:
                raise InvalidRepoURL("формат: /blob/<ревизия>/<файл>")
            revision = tail[1]
            file_path = "/".join(tail[2:])
        else:
            raise InvalidRepoURL(f"непонятный фрагмент пути: {'/'.join(tail)}")
    if not revision or revision in (".", "..") or "/" in revision or "\\" in revision:
        raise InvalidRepoURL("недопустимая ревизия")
    return RepoRef(repo_id=f"{owner}/{repo}", revision=revision, file_path=file_path)


def category_of(name) -> str:
    text = str(name)
    ext = extension_of(text)
    if ext in WEIGHT_EXTENSIONS and not is_mmproj_name(text):
        return "weights"
    if is_mmproj_name(text):
        return "mmproj"
    if ext in SUPPORTING_EXTENSIONS:
        return "supporting"
    return "other"


@dataclass(frozen=True)
class FileEntry:
    name: str
    size: int = 0
    sha256: str = ""
    category: str = "other"

    @property
    def extension(self) -> str:
        return extension_of(self.name)

    @property
    def is_weight(self) -> bool:
        return self.category == "weights"


def build_file_list(payload) -> list:
    """Все файлы репозитория: .safetensors, .json, .onnx, .gguf и прочие."""
    siblings = (payload or {}).get("siblings") or []
    entries = []
    for item in siblings:
        if not isinstance(item, dict):
            continue
        name = item.get("rfilename") or item.get("path")
        if not name:
            continue
        lfs = item.get("lfs") if isinstance(item.get("lfs"), dict) else {}
        size = item.get("size") or lfs.get("size") or 0
        digest = lfs.get("oid") or item.get("oid") or ""
        if isinstance(digest, str) and digest.lower().startswith("sha256:"):
            digest = digest.split(":", 1)[1]
        if not re.fullmatch(r"[0-9a-fA-F]{64}", str(digest or "")):
            digest = ""
        entries.append(FileEntry(name=str(name), size=int(size or 0),
                                 sha256=str(digest).lower(), category=category_of(name)))
    entries.sort(key=lambda entry: entry.name.lower())
    return entries


def filter_files(entries, query="", extension="", category="") -> list:
    text = (query or "").strip().lower()
    ext = (extension or "").strip().lower()
    if ext and not ext.startswith("."):
        ext = "." + ext
    result = []
    for entry in entries or []:
        if text and text not in entry.name.lower():
            continue
        if ext and entry.extension != ext:
            continue
        if category and entry.category != category:
            continue
        result.append(entry)
    return result


def extensions_available(entries) -> list:
    return sorted({entry.extension for entry in entries or [] if entry.extension})


def local_path_for(models_root, repo_id, name) -> Path:
    """Сохраняет структуру репозитория и не выпускает путь за models_root."""
    owner, _, repo = (repo_id or "").partition("/")
    if not owner or not repo:
        raise InvalidRepoURL(f"некорректный repo id: {repo_id!r}")
    try:
        base = safe_join(models_root, owner)
        base = safe_join(base, repo)
        return safe_join(base, name)
    except UnsafePathError as exc:
        raise InvalidRepoURL(str(exc)) from exc


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    """Не отдаёт токен другому хосту при редиректе на CDN."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is None:
            return None
        old_host = urllib.parse.urlsplit(req.full_url).netloc.lower()
        new_host = urllib.parse.urlsplit(newurl).netloc.lower()
        if old_host != new_host:
            for header in ("Authorization", "authorization"):
                redirected.remove_header(header)
                redirected.unredirected_hdrs.pop(header, None)
        return redirected


def build_opener():
    return urllib.request.build_opener(_SafeRedirect(), https_handler())


def fetch_json(url, token="", timeout=30):
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if token and token.strip():
        headers["Authorization"] = "Bearer " + token.strip()
    request = urllib.request.Request(url, headers=headers)
    try:
        with build_opener().open(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise error_from_http(exc) from exc
    except urllib.error.URLError as exc:
        raise HFError("network", detail=str(exc.reason or exc)) from exc
    except (TimeoutError, OSError) as exc:
        raise HFError("network", detail=str(exc)) from exc


def error_from_http(exc: urllib.error.HTTPError) -> HFError:
    code = int(getattr(exc, "code", 0) or 0)
    retry = None
    if code == 429:
        try:
            retry = int(exc.headers.get("Retry-After") or 0) or None
        except (TypeError, ValueError, AttributeError):
            retry = None
    if code == 404:
        return HFError("not_found", code=code)
    if code in (401, 403):
        return HFError("auth", code=code)
    if code == 429:
        return HFError("rate_limit", code=code, retry_after=retry)
    return HFError("http", code=code, detail=str(getattr(exc, "reason", "") or ""))


def sha256_file(path, chunk=CHUNK) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest()


def download_file(url, destination, *, expected_size=0, expected_sha256="", headers=None,
                  token="", timeout=60, cancel_event=None, progress=None,
                  overwrite=False, part_dir=None) -> str:
    """Скачивает url в destination. Возвращает путь готового файла.

    * существующий файл не перезаписывается (overwrite=False -> FileExistsError
      с уже занятым именем; вызывающий код берёт unique_path());
    * .part лежит в part_dir (по умолчанию рядом с файлом) и переживает обрыв;
    * после докачки проверяются размер и SHA256, затем атомарное переименование.
    """
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and not overwrite:
        raise FileExistsError(str(destination))
    part_folder = Path(part_dir) if part_dir else destination.parent
    part_folder.mkdir(parents=True, exist_ok=True)
    temp = part_folder / (destination.name + ".part")
    existing = temp.stat().st_size if temp.exists() else 0
    if existing and expected_size and existing == expected_size:
        # Прошлая загрузка дошла до конца, но не успела переименоваться.
        if not expected_sha256 or sha256_file(temp) == expected_sha256.lower():
            _move_into_place(temp, destination)
            return str(destination)
    request_headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    if token and token.strip():
        request_headers["Authorization"] = "Bearer " + token.strip()
    request_headers.update(headers or {})
    if existing:
        request_headers["Range"] = f"bytes={existing}-"
    request = urllib.request.Request(url, headers=request_headers)
    try:
        response_cm = build_opener().open(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        if exc.code == 416 and existing:
            temp.unlink(missing_ok=True)
            return download_file(url, destination, expected_size=expected_size,
                                 expected_sha256=expected_sha256, headers=headers, token=token,
                                 timeout=timeout, cancel_event=cancel_event, progress=progress,
                                 overwrite=overwrite, part_dir=part_dir)
        raise error_from_http(exc) from exc
    except urllib.error.URLError as exc:
        raise HFError("network", detail=str(exc.reason or exc)) from exc
    except OSError as exc:
        raise HFError("network", detail=str(exc)) from exc

    done = existing
    total = 0
    with response_cm as response:
        status = getattr(response, "status", response.getcode())
        content_length = int(response.headers.get("Content-Length") or 0)
        content_range = response.headers.get("Content-Range", "")
        append = bool(existing and status == 206)
        if append:
            match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", content_range or "")
            if not match or int(match.group(1)) != existing:
                raise HFError("integrity", detail="сервер вернул неверный Content-Range")
            total = int(match.group(3))
        else:
            total = content_length or (expected_size or 0)
            if existing:
                existing = 0
                done = 0
        if total:
            try:
                free = shutil.disk_usage(destination.parent).free
            except OSError:
                free = None
            remaining = max(0, total - done)
            if free is not None and free < remaining + 64 * 1024 * 1024:
                raise HFError("disk", needed=f"{remaining / 1024 ** 3:.2f}")
        mode = "ab" if append else "wb"
        digest = hashlib.sha256()
        if append and expected_sha256:
            # Хеш уже скачанной части нужен, чтобы сверить итог.
            digest = hashlib.sha256()
            with open(temp, "rb") as handle:
                for block in iter(lambda: handle.read(CHUNK), b""):
                    digest.update(block)
        with open(temp, mode) as output:
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    raise DownloadCancelled()
                chunk = response.read(CHUNK)
                if not chunk:
                    break
                output.write(chunk)
                digest.update(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)
            output.flush()
            os.fsync(output.fileno())
    if total and done < total:
        raise HFError("interrupted", received=done, total=total,
                      detail=f"{done}/{total}")
    if expected_size and done != int(expected_size):
        raise HFError("integrity", detail=f"размер {done} вместо {expected_size}")
    if expected_sha256:
        actual = digest.hexdigest()
        if actual != str(expected_sha256).lower():
            raise HFError("integrity", detail=f"SHA256 {actual[:12]}… не совпал")
    _move_into_place(temp, destination)
    return str(destination)


def _move_into_place(temp, destination):
    """Атомарно рядом; при другом томе (TMP на ином диске) — копированием."""
    try:
        Path(temp).replace(destination)
    except OSError:
        shutil.move(str(temp), str(destination))


def plan_download(models_root, repo: RepoRef, names, *, suffix_existing=True):
    """Возвращает [(entry_name, target_path)] без перезаписи существующих файлов."""
    plan = []
    for name in names:
        target = local_path_for(models_root, repo.repo_id, name)
        if suffix_existing and target.exists():
            target = unique_path(target)
        plan.append((name, target))
    return plan


def default_part_dir(paths, repo_id):
    """Временные .part — в TMP_DIR, чтобы каталог моделей оставался чистым."""
    owner, _, repo = (repo_id or "").partition("/")
    folder = paths.tmp / "downloads" / owner / repo
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def newest_stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def format_size(num_bytes) -> str:
    """Человеческий размер для списка файлов (0/None -> «—»)."""
    value = float(num_bytes or 0)
    if value <= 0:
        return "—"
    if value < 1024:
        return f"{int(value)} B"
    value /= 1024
    if value < 1024:
        return f"{value:.0f} KB"
    value /= 1024
    if value < 1024:
        return f"{value:.1f} MB"
    value /= 1024
    if value < 1024:
        return f"{value:.2f} GB"
    return f"{value / 1024:.2f} TB"


def row_label(entry, checked=False) -> str:
    """Строка списка файлов: отметка, имя, расширение, размер."""
    mark = "☑" if checked else "☐"
    extension = entry.extension or "—"
    return f"{mark}  {entry.name}   ·   {extension}   ·   {format_size(entry.size)}"


class FilePicker:
    """Выбор файлов репозитория: поиск, фильтр по расширению, отметки.

    Логика отделена от Tk, чтобы выбор нескольких файлов проверялся тестом.
    """

    def __init__(self, entries=()):
        self.entries = list(entries)
        self.selected = set()      # имена файлов
        self.current = ""
        self.query = ""
        self.extension = ""

    def set_entries(self, entries):
        self.entries = list(entries)
        names = {entry.name for entry in self.entries}
        self.selected &= names
        if self.current not in names:
            self.current = ""

    def visible(self):
        return filter_files(self.entries, self.query, self.extension)

    def toggle(self, name):
        if name in self.selected:
            self.selected.discard(name)
            checked = False
        else:
            self.selected.add(name)
            checked = True
        self.current = name
        return checked

    def check(self, name, value=True):
        if value:
            self.selected.add(name)
        else:
            self.selected.discard(name)
        self.current = name
        return value in self.selected

    def by_name(self, name):
        for entry in self.entries:
            if entry.name == name:
                return entry
        return None

    def selected_entries(self):
        return [entry for entry in self.entries if entry.name in self.selected]

    def targets(self):
        """Отмеченные файлы; если ничего не отмечено — текущий (одиночная загрузка)."""
        chosen = self.selected_entries()
        if chosen:
            return chosen
        current = self.by_name(self.current)
        return [current] if current is not None else []

    def clear(self):
        self.entries = []
        self.selected.clear()
        self.current = ""
