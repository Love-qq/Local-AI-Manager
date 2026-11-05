"""Правила «что показывать», отделённые от Tkinter (проверяются тестами)."""

from __future__ import annotations

# Параметры, которые имеют смысл для конкретного движка/формата.
LLAMA_CPP_PARAMS = ("ctx", "max_tokens", "gpu_layers", "threads", "port")
GENERIC_PARAMS = ("ctx", "max_tokens", "port")


def launch_params_visible(model_count) -> bool:
    """Раздел параметров скрыт, пока в библиотеке нет ни одной модели."""
    try:
        return int(model_count) > 0
    except (TypeError, ValueError):
        return False


def relevant_params(engine_id, model_format) -> tuple:
    """Какие поля показывать для выбранного движка и формата модели."""
    if engine_id == "llama.cpp" and model_format in ("gguf", "ggml"):
        return LLAMA_CPP_PARAMS
    if engine_id in ("ONNX Runtime", "Transformers + PyTorch", "Ollama"):
        # Эти движки не получают контекст/слои от приложения.
        return ("port",)
    return GENERIC_PARAMS


def param_visible(name, engine_id, model_format) -> bool:
    return name in relevant_params(engine_id, model_format)


def allowed_engines(model_format) -> tuple:
    """Для какого формата какой движок имеет смысл показывать в списке."""
    if model_format in ("gguf", "ggml"):
        return ("llama.cpp",)
    if model_format == "onnx":
        return ("ONNX Runtime",)
    if model_format in ("safetensors", "pytorch"):
        return ("Transformers + PyTorch",)
    return ()
