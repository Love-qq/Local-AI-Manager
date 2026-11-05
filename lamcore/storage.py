"""Классификация моделей на диске, каталог загрузок и защита от перезаписи.

Правила:
* supporting_file (tokenizer.json, README.md, mmproj-*.gguf, *.onnx_data, .part)
  никогда не считается запускаемой моделью;
* multi-file репозиторий живёт в своей папке и сохраняет структуру;
* уже существующий файл не перезаписывается молча — вызывающий код обязан
  либо взять unique_path(), либо явно разрешить перезапись;
* пути, добавленные пользователем раньше, остаются на месте: миграция только
  дочитывает их в каталог, ничего не перемещая.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from .paths import safe_join

WEIGHT_EXTENSIONS = {".gguf", ".ggml", ".safetensors", ".onnx", ".bin", ".pt", ".pth", ".ckpt"}
SUPPORTING_EXTENSIONS = {
    ".json", ".md", ".txt", ".model", ".spm", ".vocab", ".tiktoken", ".py", ".yaml",
    ".yml", ".csv", ".jinja", ".lock", ".onnx_data", ".part", ".incomplete", ".safetensors.index",
    ".gitattributes", ".png", ".jpg", ".jpeg", ".svg", ".pdf",
}
# Файлы, которые лежат в папке модели, но моделью не являются.
SUPPORTING_NAMES = {"readme.md", "license", "license.md", ".gitattributes", "config.json"}

_PART_RE = re.compile(r"^(?P<stem>.+?)-(?P<index>\d{3,5})-of-(?P<total>\d{3,5})(?P<ext>\.gguf)$", re.I)
_MMPROJ_RE = re.compile(r"(?:^|[-_.])mmproj(?:[-_.]|$)", re.I)


class ModelKind:
    SINGLE_FILE = "single_file"
    MULTI_FILE_REPO = "multi_file_repo"
    COMPLETE_PACKAGE = "complete_package"
    SUPPORTING_FILE = "supporting_file"

    ALL = (SINGLE_FILE, MULTI_FILE_REPO, COMPLETE_PACKAGE, SUPPORTING_FILE)


def is_mmproj_name(name) -> bool:
    return bool(_MMPROJ_RE.search(Path(str(name)).name))


def is_partial_download(path) -> bool:
    name = Path(path).name.lower()
    return name.endswith(".part") or name.endswith(".incomplete")


def gguf_part_info(name):
    """('model', 1, 4) для model-00001-of-00004.gguf, иначе None."""
    match = _PART_RE.match(Path(str(name)).name)
    if not match:
        return None
    return match.group("stem"), int(match.group("index")), int(match.group("total"))


def extension_of(name) -> str:
    name = Path(str(name)).name.lower()
    if name.endswith(".safetensors.index.json"):
        return ".index.json"
    return Path(name).suffix


def is_weight_file(name) -> bool:
    text = str(name)
    if is_mmproj_name(text):
        return False
    return extension_of(text) in WEIGHT_EXTENSIONS


def classify_file(path) -> str:
    """Классифицирует один файл. Папки см. classify_directory()."""
    path = Path(path)
    name = path.name
    lower = name.lower()
    if lower in SUPPORTING_NAMES or is_mmproj_name(name) or is_partial_download(name):
        return ModelKind.SUPPORTING_FILE
    ext = extension_of(name)
    if ext not in WEIGHT_EXTENSIONS:
        return ModelKind.SUPPORTING_FILE
    return ModelKind.SINGLE_FILE


def _weights(directory: Path):
    try:
        return [p for p in directory.iterdir() if p.is_file() and is_weight_file(p.name)]
    except OSError:
        return []


def classify_directory(path) -> str:
    """Тип каталога: complete_package / multi_file_repo / supporting_file."""
    path = Path(path)
    if not path.is_dir():
        return ModelKind.SUPPORTING_FILE
    weights = _weights(path)
    has_config = (path / "config.json").is_file()
    has_index = (path / "model.safetensors.index.json").is_file()
    if has_config and (weights or has_index):
        return ModelKind.COMPLETE_PACKAGE
    if len(weights) >= 2 or has_index:
        return ModelKind.MULTI_FILE_REPO
    if weights:
        # Папка с единственным весом: это репозиторий из одного файла, но
        # запускать удобнее сам файл, поэтому папку считаем репозиторием.
        return ModelKind.MULTI_FILE_REPO
    return ModelKind.SUPPORTING_FILE


@dataclass
class ModelRecord:
    """Одна запускаемая единица: файл, набор шардов или пакет HF."""

    path: str
    kind: str
    size_bytes: int = 0
    files: tuple = ()
    repo: str = ""
    source: str = ""          # "disk" | "user" | "download"
    partials: tuple = ()
    note: str = ""

    @property
    def name(self) -> str:
        return Path(self.path).name

    @property
    def is_runnable(self) -> bool:
        return (self.kind in (ModelKind.SINGLE_FILE, ModelKind.MULTI_FILE_REPO,
                              ModelKind.COMPLETE_PACKAGE)
                and not is_mmproj_name(self.path)
                and not is_partial_download(self.path))

    def as_dict(self):
        return {
            "path": self.path, "kind": self.kind, "size_bytes": self.size_bytes,
            "files": list(self.files), "repo": self.repo, "source": self.source,
            "partials": list(self.partials), "note": self.note,
        }

    @classmethod
    def from_dict(cls, payload):
        payload = dict(payload or {})
        known = {k: payload[k] for k in payload if k in cls.__dataclass_fields__}
        if "files" in known:
            known["files"] = tuple(known["files"] or ())
        if "partials" in known:
            known["partials"] = tuple(known["partials"] or ())
        return cls(**known)


def _dir_size(path: Path, files=None) -> int:
    total = 0
    if files is not None:
        for item in files:
            try:
                total += (path / item).stat().st_size
            except OSError:
                continue
        return total
    for item in path.rglob("*"):
        try:
            if item.is_file():
                total += item.stat().st_size
        except OSError:
            continue
    return total


def _repo_of(path: Path, root: Path) -> str:
    """owner/repo для файла или папки внутри каталога моделей."""
    try:
        rel = path.resolve().relative_to(root.resolve())
    except (ValueError, OSError):
        return ""
    parts = rel.parts
    needed = 2 if path.is_dir() else 3
    if len(parts) >= needed:
        return f"{parts[0]}/{parts[1]}"
    return ""


def discover_models(root) -> list:
    """Все модели, лежащие в каталоге моделей. Ничего не пишет на диск.

    Поддерживает частично скачанные наборы: они попадают в partials и не
    считаются готовыми.
    """
    root = Path(root)
    found = []
    if not root.is_dir():
        return found

    package_dirs = set()
    directories = [root] + sorted([p for p in root.rglob("*") if p.is_dir()])
    for directory in directories:
        kind = classify_directory(directory)
        if kind == ModelKind.COMPLETE_PACKAGE:
            files = tuple(sorted(p.name for p in directory.iterdir()
                                 if p.is_file() and extension_of(p.name) != ".part"))
            found.append(ModelRecord(
                path=str(directory), kind=kind, size_bytes=_dir_size(directory),
                files=files, repo=_repo_of(directory, root), source="disk"))
            package_dirs.add(directory)

    for directory in directories:
        if any(directory == p or p in directory.parents for p in package_dirs):
            continue
        try:
            entries = sorted(directory.iterdir())
        except OSError:
            continue
        weights = [p for p in entries if p.is_file() and is_weight_file(p) and not is_partial_download(p)]
        partials = [p for p in entries if p.is_file() and is_partial_download(p)]
        if not weights and not partials:
            continue
        if len(weights) > 1:
            found.append(ModelRecord(
                path=str(directory), kind=ModelKind.MULTI_FILE_REPO,
                size_bytes=_dir_size(directory), files=tuple(p.name for p in weights),
                partials=tuple(p.name for p in partials),
                repo=_repo_of(directory, root), source="disk"))
            continue
        for weight in weights:
            part = gguf_part_info(weight.name)
            if part:
                # Одиночный шард без соседей — неполный набор, не модель.
                found.append(ModelRecord(
                    path=str(weight), kind=ModelKind.SUPPORTING_FILE, size_bytes=weight.stat().st_size,
                    files=(weight.name,), repo=_repo_of(weight, root), source="disk",
                    note="incomplete_shard_set"))
                continue
            found.append(ModelRecord(
                path=str(weight), kind=ModelKind.SINGLE_FILE,
                size_bytes=weight.stat().st_size, files=(weight.name,),
                repo=_repo_of(weight, root), source="disk"))
        for part_file in partials:
            found.append(ModelRecord(
                path=str(part_file), kind=ModelKind.SUPPORTING_FILE,
                size_bytes=part_file.stat().st_size, files=(part_file.name,),
                repo=_repo_of(part_file, root), source="disk", note="incomplete_download"))

    found.sort(key=lambda record: record.path.lower())
    return found


class ModelCatalog:
    """Журнал моделей (только пути и метаданные; сами файлы не трогаются)."""

    FILENAME = "model_catalog.json"

    def __init__(self, path):
        self.path = Path(path)
        self.records = []          # list[ModelRecord]
        self.load()

    def load(self):
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.records = []
            return self
        items = payload.get("models") if isinstance(payload, dict) else payload
        self.records = [ModelRecord.from_dict(item) for item in (items or []) if isinstance(item, dict)]
        return self

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".json.tmp")
        temp.write_text(json.dumps({"version": 1, "models": [r.as_dict() for r in self.records]},
                                   ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(self.path)
        return self

    def get(self, path):
        text = str(path)
        for record in self.records:
            if record.path == text:
                return record
        return None

    def add(self, record: ModelRecord, *, persist=True):
        existing = self.get(record.path)
        if existing is not None:
            existing.kind = record.kind
            existing.size_bytes = record.size_bytes or existing.size_bytes
            existing.files = record.files or existing.files
            existing.repo = record.repo or existing.repo
            existing.partials = record.partials
            existing.note = record.note
            if persist:
                self.save()
            return existing
        self.records.append(record)
        if persist:
            self.save()
        return record

    def sync(self, root, *, persist=True):
        """Дочитывает то, что лежит в каталоге моделей, не теряя старых записей."""
        known = {record.path for record in self.records}
        added = []
        for record in discover_models(root):
            if record.path in known:
                # Обновляем размер/состояние, но не источник записи пользователя.
                current = self.get(record.path)
                current.size_bytes = record.size_bytes
                current.files = record.files or current.files
                current.partials = record.partials
                continue
            self.records.append(record)
            added.append(record)
        if (added or persist) and persist:
            self.save()
        return added

    def remove(self, path, *, persist=True):
        self.records = [r for r in self.records if r.path != str(path)]
        if persist:
            self.save()
        return self

    def runnable(self):
        return [r for r in self.records if r.is_runnable]

    def missing(self):
        return [r for r in self.records if not Path(r.path).exists()]


def unique_path(path) -> Path:
    """Свободное имя рядом: model.gguf -> model (1).gguf. Ничего не перезаписывает."""
    path = Path(path)
    if not path.exists():
        return path
    for index in range(1, 10000):
        candidate = path.with_name(f"{path.stem} ({index}){path.suffix}")
        if not candidate.exists():
            return candidate
    raise FileExistsError(f"не удалось подобрать свободное имя для {path}")


def ensure_new_file(path) -> Path:
    """Возвращает путь или падает, если файл уже существует."""
    path = Path(path)
    if path.exists():
        raise FileExistsError(f"файл уже существует: {path}")
    return path


def write_json_atomic(path, payload) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)
    return path


def merge_user_models(existing, discovered, root):
    """Объединяет старые пути пользователя и найденные модели без перемещения файлов.

    existing — список путей из config.json, discovered — list[ModelRecord].
    Возвращает (все_пути, найденные_без_дублей).
    """
    root = str(root).lower()
    result = [str(item) for item in existing or []]
    seen = {os.path.normcase(os.path.abspath(p)) for p in result}
    added = []
    for record in discovered:
        if not record.is_runnable:
            continue
        key = os.path.normcase(os.path.abspath(record.path))
        if key in seen:
            continue
        seen.add(key)
        result.append(record.path)
        added.append(record)
    return result, added


def touch_stamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")
