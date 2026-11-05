"""Чтение метаданных моделей без движков и без загрузки весов в память.

* GGUF — заголовок и метаданные (архитектура, контекст, слои, головы,
  эмбеддинг, квантование) читаются из файла;
* Safetensors — config.json, model.safetensors.index.json, список шардов,
  dtype, файлы токенизатора; недостающие шарды дают "incomplete", не падение;
* ONNX — граф и внешние веса (*.onnx_data).
"""

from __future__ import annotations

import json
import re
import struct
from dataclasses import dataclass, field
from pathlib import Path

from .storage import (ModelKind, classify_directory, classify_file, extension_of,
                      gguf_part_info, is_mmproj_name)

GGUF_MAGIC = b"GGUF"
_MAX_METADATA_BYTES = 32 * 1024 * 1024

_GGUF_SCALARS = {0: "<B", 1: "<b", 2: "<H", 3: "<h", 4: "<I", 5: "<i", 6: "<f",
                 7: "<?", 10: "<Q", 11: "<q", 12: "<d"}

# llama_ftype из ggml.h: значение general.file_type -> имя кванта.
FILE_TYPES = {
    0: "F32", 1: "F16", 2: "Q4_0", 3: "Q4_1", 4: "Q4_2", 5: "Q4_3", 6: "Q5_0",
    7: "Q8_0", 8: "Q5_0", 9: "Q5_1", 10: "Q2_K", 11: "Q3_K_S", 12: "Q3_K_M",
    13: "Q3_K_L", 14: "Q4_K_S", 15: "Q4_K_M", 16: "Q5_K_S", 17: "Q5_K_M",
    18: "Q6_K", 19: "IQ2_XXS", 20: "IQ2_XS", 21: "Q2_K_S", 22: "IQ3_XS",
    23: "IQ3_XXS", 24: "IQ1_S", 25: "IQ4_NL", 26: "IQ3_S", 27: "IQ3_M",
    28: "IQ2_S", 29: "IQ2_M", 30: "IQ4_XS", 31: "IQ1_M", 32: "BF16",
    33: "Q4_0_4_4", 34: "Q4_0_4_8", 35: "Q4_0_8_8", 36: "TQ1_0", 37: "TQ2_0",
    38: "IQ4_NL_4_4", 39: "IQ4_NL_4_8", 40: "IQ4_NL_8_8",
}

TOKENIZER_FILES = ("tokenizer.json", "tokenizer.model", "tokenizer_config.json",
                   "vocab.json", "merges.txt", "special_tokens_map.json")


class ModelInfoError(Exception):
    """Файл не является корректной моделью ожидаемого формата."""


@dataclass(frozen=True)
class ModelInfo:
    path: str
    format: str = "unknown"
    architecture: str = "unknown"
    quantization: str = ""
    max_context: int | None = None
    size_bytes: int = 0
    completeness: str = "unknown"          # complete | incomplete | unknown
    missing_files: tuple = ()
    layers: int | None = None
    heads: int | None = None
    kv_heads: int | None = None
    head_dim: int | None = None
    embedding: int | None = None
    dtype: str = ""
    shards: tuple = ()
    tokenizer_files: tuple = ()
    kind: str = ModelKind.SINGLE_FILE
    is_supporting: bool = False
    notes: tuple = ()

    @property
    def name(self) -> str:
        return Path(self.path).name

    @property
    def runnable(self) -> bool:
        return not self.is_supporting and self.completeness != "incomplete"

    def with_notes(self, *notes):
        return ModelInfo(**{**self.__dict__, "notes": tuple(self.notes) + tuple(notes)})

    def summary(self) -> str:
        """Короткая техническая строка без языка: GGUF · qwen2 · Q4_K_M · 32768."""
        parts = [self.format.upper() or "?"]
        if self.architecture and self.architecture != "unknown":
            parts.append(self.architecture)
        if self.quantization:
            parts.append(self.quantization)
        if self.max_context:
            parts.append(str(self.max_context))
        return " · ".join(parts)


class _Reader:
    def __init__(self, handle):
        self.handle = handle
        self.consumed = 0

    def read(self, size):
        data = self.handle.read(size)
        if len(data) != size:
            raise ModelInfoError("файл обрывается внутри заголовка GGUF")
        self.consumed += size
        if self.consumed > _MAX_METADATA_BYTES:
            raise ModelInfoError("метаданные GGUF больше допустимого размера")
        return data

    def unpack(self, fmt):
        return struct.unpack(fmt, self.read(struct.calcsize(fmt)))[0]

    def string(self):
        length = self.unpack("<Q")
        if length > _MAX_METADATA_BYTES:
            raise ModelInfoError("строка GGUF неверной длины")
        return self.read(length).decode("utf-8", errors="replace")

    def value(self, vtype):
        if vtype in _GGUF_SCALARS:
            return self.unpack(_GGUF_SCALARS[vtype])
        if vtype == 8:
            return self.string()
        if vtype == 9:
            item_type = self.unpack("<I")
            count = self.unpack("<Q")
            if count > 10_000_000:
                raise ModelInfoError("массив GGUF неверной длины")
            return [self.value(item_type) for _ in range(count)]
        raise ModelInfoError(f"неизвестный тип значения GGUF: {vtype}")


def read_gguf_metadata(path) -> dict:
    """Читает KV-метаданные GGUF. Бросает ModelInfoError на битом файле."""
    with open(path, "rb") as handle:
        reader = _Reader(handle)
        if reader.read(4) != GGUF_MAGIC:
            raise ModelInfoError("это не GGUF: не совпала сигнатура")
        version = reader.unpack("<I")
        if version not in (1, 2, 3):
            raise ModelInfoError(f"неподдерживаемая версия GGUF: {version}")
        tensor_count = reader.unpack("<Q")
        kv_count = reader.unpack("<Q")
        if kv_count > 200_000:
            raise ModelInfoError("слишком много ключей GGUF")
        meta = {"__version__": version, "__tensors__": tensor_count}
        for _ in range(kv_count):
            key = reader.string()
            vtype = reader.unpack("<I")
            meta[key] = reader.value(vtype)
    return meta


def _int(value):
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _quant_from_name(name) -> str:
    text = Path(str(name)).name
    match = re.search(r"(?i)(iq\d[_a-z0-9]*|q\d[_a-z0-9]*|bf16|f16|f32)", text)
    return match.group(1).upper() if match else ""


def _gguf_info(path: Path, notes) -> ModelInfo:
    if is_mmproj_name(path.name):
        return ModelInfo(path=str(path), format="gguf", architecture="clip-projector",
                         size_bytes=path.stat().st_size, is_supporting=True,
                         kind=ModelKind.SUPPORTING_FILE,
                         notes=tuple(notes) + ("mmproj — проектор, не языковая модель",))
    try:
        meta = read_gguf_metadata(path)
    except (ModelInfoError, OSError) as exc:
        return ModelInfo(path=str(path), format="gguf", size_bytes=path.stat().st_size,
                         completeness="unknown", notes=tuple(notes) + (str(exc),))
    arch = str(meta.get("general.architecture") or "unknown")
    prefix = arch.lower()
    context = _int(meta.get(f"{prefix}.context_length"))
    if context is None:
        context = _int(meta.get("general.context_length"))
    layers = _int(meta.get(f"{prefix}.block_count"))
    heads = _int(meta.get(f"{prefix}.attention.head_count"))
    kv_heads = _int(meta.get(f"{prefix}.attention.head_count_kv"))
    embedding = _int(meta.get(f"{prefix}.embedding_length"))
    head_dim = _int(meta.get(f"{prefix}.attention.key_length"))
    if head_dim is None and embedding and heads:
        head_dim = embedding // heads
    file_type = _int(meta.get("general.file_type"))
    quant = FILE_TYPES.get(file_type, f"type {file_type}" if file_type is not None else "")
    if not quant:
        quant = _quant_from_name(path.name)
    if not context:
        notes.append("модель не сообщила context_length")
    return ModelInfo(
        path=str(path), format="gguf", architecture=arch, quantization=quant,
        max_context=context, size_bytes=path.stat().st_size, completeness="complete",
        layers=layers, heads=heads, kv_heads=kv_heads, head_dim=head_dim,
        embedding=embedding, dtype=quant, notes=tuple(notes),
    )


def _read_json(path: Path, notes, label):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        notes.append(f"{label} не читается: {exc}")
        return {}


def _safetensors_info(path: Path, notes) -> ModelInfo:
    folder = path if path.is_dir() else path.parent
    config = {}
    config_path = folder / "config.json"
    if config_path.is_file():
        config = _read_json(config_path, notes, "config.json")
        if not isinstance(config, dict):
            config = {}
    archs = config.get("architectures") or []
    arch = archs[0] if archs else str(config.get("model_type") or "unknown")
    context = _int(config.get("max_position_embeddings"))
    layers = _int(config.get("num_hidden_layers"))
    heads = _int(config.get("num_attention_heads"))
    kv_heads = _int(config.get("num_key_value_heads")) or heads
    embedding = _int(config.get("hidden_size"))
    head_dim = _int(config.get("head_dim"))
    if head_dim is None and embedding and heads:
        head_dim = embedding // heads
    dtype = str(config.get("torch_dtype") or config.get("dtype") or "")

    index_path = folder / "model.safetensors.index.json"
    if not index_path.is_file():
        index_path = folder / "pytorch_model.bin.index.json"
    shards, missing = [], []
    if index_path.is_file():
        index = _read_json(index_path, notes, index_path.name)
        weight_map = index.get("weight_map") if isinstance(index, dict) else {}
        names = sorted({str(v) for v in (weight_map or {}).values()})
        for name in names:
            shards.append(name)
            if not (folder / name).is_file():
                missing.append(name)
    else:
        weights = sorted(p.name for p in folder.glob("*.safetensors"))
        shards = weights

    tokenizers = [name for name in TOKENIZER_FILES if (folder / name).is_file()]
    size = 0
    for name in shards or ([path.name] if path.is_file() else []):
        candidate = folder / name
        if candidate.is_file():
            size += candidate.stat().st_size
    if path.is_file() and not shards:
        size = path.stat().st_size

    completeness = "complete"
    if missing:
        completeness = "incomplete"
    elif not config_path.is_file():
        completeness = "incomplete"
        notes.append("нет config.json — пакет Hugging Face неполный")
    elif index_path.is_file() and not shards:
        completeness = "incomplete"
        notes.append("индекс весов пуст")
    is_supporting = path.is_file() and not index_path.is_file() and not config_path.is_file()
    if is_supporting:
        notes.append("одиночный шард без config.json и индекса")
    return ModelInfo(
        path=str(path), format="safetensors", architecture=str(arch), quantization=dtype,
        max_context=context, size_bytes=size, completeness=completeness,
        missing_files=tuple(missing), layers=layers, heads=heads, kv_heads=kv_heads,
        head_dim=head_dim, embedding=embedding, dtype=dtype, shards=tuple(shards),
        tokenizer_files=tuple(tokenizers),
        kind=classify_directory(folder) if folder.is_dir() else ModelKind.SINGLE_FILE,
        is_supporting=is_supporting, notes=tuple(notes),
    )


def _onnx_info(path: Path, notes) -> ModelInfo:
    folder = path if path.is_dir() else path.parent
    graphs = sorted(p for p in folder.glob("*.onnx")) if folder.is_dir() else []
    graph = graphs[0] if graphs else (path if path.suffix.lower() == ".onnx" else None)
    referenced, missing = set(), []
    size = 0
    for item in graphs or ([path] if path.suffix.lower() == ".onnx" else []):
        if not item.is_file():
            continue
        size += item.stat().st_size
        try:
            with open(item, "rb") as handle:
                head = handle.read(2 * 1024 * 1024)
        except OSError:
            continue
        for match in re.finditer(rb"([A-Za-z0-9_.\-/]{1,120}\.onnx_data)", head):
            referenced.add(match.group(1).decode("latin-1"))
    for name in sorted(referenced):
        candidate = folder / Path(name).name
        if not candidate.is_file():
            missing.append(Path(name).name)
        else:
            size += candidate.stat().st_size
    completeness = "incomplete" if missing else ("complete" if graph else "unknown")
    if missing:
        notes.append("не хватает внешних файлов весов ONNX")
    return ModelInfo(
        path=str(path), format="onnx", architecture="onnx-graph",
        quantization=_quant_from_name(graph.name if graph else path.name),
        size_bytes=size, completeness=completeness, missing_files=tuple(missing),
        dtype="", kind=ModelKind.SINGLE_FILE, notes=tuple(notes),
    )


def inspect_path(path, *, kind=None, notes=None) -> ModelInfo:
    """Метаданные файла или каталога модели. Никогда не бросает на битом файле."""
    path = Path(path)
    notes = list(notes or [])
    if not path.exists():
        return ModelInfo(path=str(path), completeness="unknown",
                         is_supporting=True, notes=tuple(notes) + ("путь не существует",))
    try:
        if path.is_dir():
            kind = kind or classify_directory(path)
            if (path / "config.json").is_file() or list(path.glob("*.safetensors")):
                return _safetensors_info(path, notes)
            graphs = list(path.glob("*.onnx"))
            if graphs:
                return _onnx_info(path, notes)
            return ModelInfo(path=str(path), kind=kind, completeness="unknown",
                             size_bytes=0, notes=tuple(notes) + ("формат папки не распознан",))
        suffix = extension_of(path.name)
        part = gguf_part_info(path.name)
        if part and part[2] > 1:
            notes.append(f"это часть {part[1]} из {part[2]}")
        if suffix in (".gguf", ".ggml"):
            return _gguf_info(path, notes)
        if suffix == ".safetensors":
            return _safetensors_info(path, notes)
        if suffix == ".onnx":
            return _onnx_info(path, notes)
        file_kind = kind or classify_file(path)
        return ModelInfo(path=str(path), size_bytes=path.stat().st_size, kind=file_kind,
                         completeness="unknown", is_supporting=file_kind == ModelKind.SUPPORTING_FILE,
                         notes=tuple(notes) + ("формат не распознан",))
    except OSError as exc:
        return ModelInfo(path=str(path), completeness="unknown", is_supporting=True,
                         notes=tuple(notes) + (f"не удалось прочитать: {exc}",))


def looks_like_supporting(path) -> bool:
    path = Path(path)
    if path.is_file():
        return is_mmproj_name(path.name) or classify_file(path) == ModelKind.SUPPORTING_FILE
    return False
