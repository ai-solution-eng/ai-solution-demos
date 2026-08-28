import importlib
from typing import Any

__pcai_models: Any = None


def __getattr__(name: str) -> Any:
    global __pcai_models
    if __pcai_models is None:
        __pcai_models = importlib.import_module(".pcai_models", __package__)
    return getattr(__pcai_models, name)


__all__ = [
    "cohere_transcribe_3_2b",
    "deepseek_v4_flash_280B",
    "gemma4_31B",
    "qwen3_vl_8B",
    "qwen3_vl_reranker_8B",
]


def __dir__() -> list[str]:
    return sorted(__all__)
