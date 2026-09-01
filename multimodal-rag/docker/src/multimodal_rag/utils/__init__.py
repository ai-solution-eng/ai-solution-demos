import importlib
from typing import Any

# The model catalog (pcai_models.py) is intentionally EXCLUDED from the
# Docker image (.dockerignore: it hardcodes PCAI-internal credentials), so
# this lazy loader must tolerate its absence.  It is name-aware for the same
# reason: "from multimodal_rag.utils import <submodule>" consults
# __getattr__ whenever the submodule attribute is not yet set (PEP 562),
# and a name-blind loader there would try to import the catalog for ANY
# attribute miss - crashing every container in the image at startup
# (regression found in the v3.3.0 G2 rollout).

__pcai_models: Any = None
__pcai_models_missing = False

_CATALOG_NAMES = frozenset(
    {
        "cohere_transcribe_3_2b",
        "deepseek_v4_flash_280B",
        "gemma4_31B",
        "qwen3_vl_8B",
        "qwen3_vl_reranker_8B",
    }
)


def _missing_catalog_msg() -> str:
    return (
        "the model catalog is unavailable in this image: pcai_models.py is "
        "docker-ignored (it hardcodes PCAI-internal credentials). Models are "
        "configured at runtime via the MODEL_* environment variables."
    )


def __getattr__(name: str) -> Any:
    global __pcai_models, __pcai_models_missing

    if name not in _CATALOG_NAMES:
        # Not a catalog slug - behave like a plain module (this is what
        # lets "from multimodal_rag.utils import bm25" fall through to the
        # normal submodule import machinery).
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    if __pcai_models_missing:
        raise AttributeError(_missing_catalog_msg())
    if __pcai_models is None:
        try:
            __pcai_models = importlib.import_module(".pcai_models", __package__)
        except ImportError:
            __pcai_models_missing = True
            raise AttributeError(_missing_catalog_msg())
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
