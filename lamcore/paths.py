"""Каталоги приложения и безопасная сборка путей.

Все данные лежат внутри одного корня (по умолчанию %APPDATA%\\LocalAIManager),
поэтому пути не зависят от того, откуда запущено приложение:

    root/
      config.json
      model/<owner>/<repo>/...      скачанные модели
      engines/<engine>/...          движки, установленные приложением
      logs/                         журналы
      tmp/                          временные файлы и .part
      downloads/                    загруженные архивы (например, ZIP llama.cpp)
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

APP_FOLDER_NAME = "LocalAIManager"

_DRIVE_RE = re.compile(r"^[A-Za-z]:")


class UnsafePathError(ValueError):
    """Путь пытается выйти за пределы базового каталога или задан абсолютно."""


def default_app_dir(env=None) -> Path:
    """Корень данных приложения. env подменяется в тестах."""
    env = os.environ if env is None else env
    base = env.get("APPDATA") or env.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / APP_FOLDER_NAME


def safe_join(base, relative) -> Path:
    """Склеивает base и relative, отклоняя всё, что может выйти за пределы base.

    Отклоняются: пустая строка, абсолютные пути (/x, C:\\x, C:x), любые
    компоненты "..", NUL, двоеточия (буква диска или alternate data stream)
    и симлинки, чья цель оказывается вне base.
    """
    if relative is None:
        raise UnsafePathError("пустой путь")
    text = str(relative).strip()
    if not text:
        raise UnsafePathError("пустой путь")
    if "\x00" in text:
        raise UnsafePathError("NUL в пути")
    text = text.replace("\\", "/")
    if text.startswith("/"):
        raise UnsafePathError(f"абсолютный путь запрещён: {relative!r}")
    if _DRIVE_RE.match(text):
        raise UnsafePathError(f"путь с буквой диска запрещён: {relative!r}")
    if ":" in text:
        raise UnsafePathError(f"двоеточие в пути запрещено: {relative!r}")

    parts = []
    for part in text.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise UnsafePathError(f"переход вверх по каталогу запрещён: {relative!r}")
        parts.append(part)
    if not parts:
        raise UnsafePathError(f"пустой путь: {relative!r}")

    base_resolved = Path(base).resolve()
    target = base_resolved.joinpath(*parts)
    resolved = target.resolve()
    if resolved != base_resolved and base_resolved not in resolved.parents:
        raise UnsafePathError(f"путь выходит за пределы каталога: {relative!r}")
    return resolved


class AppPaths:
    """Набор каталогов приложения внутри одного корня."""

    def __init__(self, root):
        self.root = Path(root)

    # --- каталоги ---------------------------------------------------------
    @property
    def config_file(self) -> Path:
        return self.root / "config.json"

    @property
    def models(self) -> Path:
        return self.root / "model"

    @property
    def engines(self) -> Path:
        return self.root / "engines"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    @property
    def tmp(self) -> Path:
        return self.root / "tmp"

    @property
    def downloads(self) -> Path:
        return self.root / "downloads"

    def all_dirs(self):
        return (self.models, self.engines, self.logs, self.tmp, self.downloads)

    def ensure(self):
        for folder in self.all_dirs():
            folder.mkdir(parents=True, exist_ok=True)
        return self

    # --- операции ---------------------------------------------------------
    def model_target(self, repo_id: str, relative_name: str) -> Path:
        """Куда положить файл репозитория. Структура репозитория сохраняется."""
        owner, _, repo = (repo_id or "").partition("/")
        if not owner or not repo:
            raise UnsafePathError(f"некорректный repo id: {repo_id!r}")
        repo_dir = safe_join(self.models, owner)
        repo_dir = safe_join(repo_dir, repo)
        return safe_join(repo_dir, relative_name)

    def scratch_dir(self, prefix="lam-"):
        """Временный каталог внутри TMP_DIR (а не в системном /tmp)."""
        self.tmp.mkdir(parents=True, exist_ok=True)
        return Path(tempfile.mkdtemp(prefix=prefix, dir=str(self.tmp)))


def same_path(a, b) -> bool:
    try:
        return Path(a).resolve() == Path(b).resolve()
    except OSError:
        return False


def is_inside(path, folder) -> bool:
    try:
        path = Path(path).resolve()
        folder = Path(folder).resolve()
    except OSError:
        return False
    return path == folder or folder in path.parents
