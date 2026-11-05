"""Память и рекомендации по параметрам запуска.

KV-кэш считается из метаданных модели (слои, KV-головы, размер головы), а не
из размера файла. Оценка всегда помечается как оценка: точное потребление
зависит от сборки llama.cpp, типа кэша и батча.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .modelinfo import ModelInfo

CONTEXT_STOPS = (2048, 4096, 8192, 16384, 32768, 65536, 131072, 262144)

GiB = 1024 ** 3


@dataclass(frozen=True)
class GpuInfo:
    name: str = ""
    vram_gb: float = 0.0
    driver: str = ""


@dataclass(frozen=True)
class HardwareInfo:
    ram_total_gb: float | None = None
    ram_free_gb: float | None = None
    gpus: tuple = ()
    cpu: str = ""
    cores: int = 0
    source: str = ""

    @property
    def max_vram_gb(self) -> float:
        return max((gpu.vram_gb or 0.0 for gpu in self.gpus), default=0.0)

    @classmethod
    def from_mapping(cls, payload):
        payload = payload or {}
        gpus = []
        for gpu in payload.get("gpus") or []:
            if isinstance(gpu, dict):
                gpus.append(GpuInfo(name=str(gpu.get("name") or ""),
                                    vram_gb=float(gpu.get("vram") or 0),
                                    driver=str(gpu.get("driver") or "")))
        def number(key):
            value = payload.get(key)
            return float(value) if isinstance(value, (int, float)) else None
        return cls(ram_total_gb=number("ram_total"), ram_free_gb=number("ram_free"),
                   gpus=tuple(gpus), cpu=str(payload.get("cpu") or ""),
                   cores=int(payload.get("cores") or 0), source="probe")


@dataclass(frozen=True)
class Recommendation:
    context: int
    limited_by: str                 # model | vram | ram | engine | unknown | none
    model_limit: int | None = None
    hardware_limit: int | None = None
    kv_gb_per_1024: float = 0.0
    notes: tuple = ()
    exact: bool = False             # True только если пределы известны точно

    def explain(self) -> str:
        return {"model": "ограничение модели",
                "vram": "ограничение видеопамяти",
                "ram": "ограничение оперативной памяти",
                "engine": "ограничение движка",
                "unknown": "недостаточно данных",
                "none": "нет ограничений"}.get(self.limited_by, self.limited_by)


def _head_dim(info: ModelInfo) -> int | None:
    if info.head_dim:
        return int(info.head_dim)
    if info.embedding and info.heads:
        return max(1, int(info.embedding) // int(info.heads))
    return None


def kv_cache_bytes(info: ModelInfo, context: int, *, bytes_per_element=2, batch=1) -> int | None:
    """K и V для всех слоёв. None, если метаданных не хватает."""
    layers = info.layers
    heads = info.kv_heads or info.heads
    head_dim = _head_dim(info)
    if not layers or not heads or not head_dim:
        return None
    return int(2 * int(layers) * int(context) * int(heads) * int(head_dim)
               * int(bytes_per_element) * max(1, int(batch)))


def weights_gb(info: ModelInfo) -> float:
    return (info.size_bytes or 0) / GiB


def recommend_context(info: ModelInfo, hardware: HardwareInfo, *, gpu_offload=True,
                      engine_max=None, reserve_gb=1.5, safety=0.85,
                      stops=CONTEXT_STOPS, cache_bytes_per_element=2) -> Recommendation:
    """Максимальный контекст, который проходит и по модели, и по памяти."""
    notes = []
    model_limit = int(info.max_context) if info.max_context else None
    engine_limit = int(engine_max) if engine_max else None
    candidate_limits = [value for value in (model_limit, engine_limit) if value]
    hard_limit = min(candidate_limits) if candidate_limits else None

    weights = weights_gb(info)
    kv_per_ctx = kv_cache_bytes(info, 1, bytes_per_element=cache_bytes_per_element)
    if kv_per_ctx is None:
        # Без метаданных KV оценить нельзя: не выдумываем число.
        fallback = hard_limit or 4096
        return Recommendation(context=int(fallback), limited_by="unknown",
                              model_limit=model_limit, hardware_limit=None,
                              notes=tuple(notes) + (
                                  "в метаданных нет слоёв/голов — KV-кэш не рассчитан, "
                                  "показан предел модели",), exact=False)

    vram = hardware.max_vram_gb
    ram_free = hardware.ram_free_gb
    if gpu_offload and vram:
        budget = vram * 0.90 - weights * 0.90 - reserve_gb * 0.5
        by = "vram"
    elif ram_free is not None:
        budget = ram_free * safety - weights - reserve_gb
        by = "ram"
    else:
        budget = None
        by = "unknown"

    if budget is not None and budget <= 0:
        notes.append("даже без KV-кэша памяти впритык: выбери меньший квант")
        hardware_limit = 0
    elif budget is not None:
        hardware_limit = int(max(0, budget) * GiB / kv_per_ctx)
    else:
        hardware_limit = None

    limit = hard_limit
    if hardware_limit is not None:
        limit = hardware_limit if limit is None else min(limit, hardware_limit)

    if limit is None:
        chosen = 4096
        limited_by = "unknown"
        notes.append("нет ни предела модели, ни данных о памяти — показано 4096 по умолчанию")
    else:
        fitting = [stop for stop in stops if stop <= limit]
        chosen = fitting[-1] if fitting else stops[0]
        if not fitting:
            notes.append("даже минимальный контекст не укладывается в память по оценке")
        if gpu_offload and vram:
            limited_by = "vram" if hardware_limit is not None and hardware_limit < (hard_limit or 10 ** 9) else "model"
        else:
            limited_by = "ram" if hardware_limit is not None and hardware_limit < (hard_limit or 10 ** 9) else "model"
        if hard_limit is not None and hard_limit <= chosen:
            limited_by = "engine" if (engine_limit or 10 ** 9) <= hard_limit else "model"
        if hardware_limit is not None and hardware_limit < chosen:
            limited_by = by
    if hardware_limit is not None and hardware_limit < 2048:
        notes.append("памяти меньше, чем нужно даже для 2048 токенов контекста")

    per_1024 = kv_per_ctx * 1024 / GiB
    return Recommendation(context=int(chosen), limited_by=limited_by, model_limit=model_limit,
                          hardware_limit=hardware_limit, kv_gb_per_1024=per_1024,
                          notes=tuple(notes), exact=bool(model_limit and hardware_limit))


def manual_context(value, info: ModelInfo, *, engine_max=None, hardware=None,
                   gpu_offload=True, minimum=512) -> tuple:
    """Проверяет ручной ввод. Возвращает (значение, объяснение, изменено ли)."""
    try:
        wanted = int(value)
    except (TypeError, ValueError):
        raise ValueError("контекст должен быть целым числом")
    limit = None
    for candidate in (info.max_context, engine_max):
        if candidate:
            limit = int(candidate) if limit is None else min(limit, int(candidate))
    if wanted < minimum:
        return minimum, f"минимум {minimum} токенов", True
    if limit is not None and wanted > limit:
        return limit, f"предел {limit} токенов (модель или движок)", True
    return wanted, "", False


def max_output_tokens(context, requested=1024, *, prompt_reserve=512, engine_max=8192) -> int:
    """Максимум генерации ограничен остатком контекста и пределом движка."""
    try:
        context = int(context)
    except (TypeError, ValueError):
        context = 4096
    try:
        requested = int(requested)
    except (TypeError, ValueError):
        requested = 1024
    ceiling = min(int(engine_max or 8192), max(1, context - int(prompt_reserve)))
    return max(1, min(requested, ceiling))


def fits_in_ram(required_gb, hardware: HardwareInfo, *, safety=0.9):
    if hardware.ram_free_gb is None:
        return None
    return required_gb <= hardware.ram_free_gb * safety
