"""Build model instances from environment variables.

Replaces the hardcoded model definitions in ``pcai_models.py`` for
production deployment.  URLs, API keys, and (optionally) model names
are injected at pod start via ConfigMap / Secret, keeping secrets out
of the Docker image; model names are auto-discovered from the endpoint
when not specified.

Environment variable convention (each model role has its own prefix)::

    MODEL_<ROLE>_NAME       — optional served model id, e.g.
                              "Qwen/Qwen3-VL-Embedding-8B".  When empty
                              the id is auto-discovered via GET /v1/models.
    MODEL_<ROLE>_URL        — full remote URL (e.g. "https://..."); required
                              to enable the role (empty disables it)
    MODEL_<ROLE>_API_KEY    — API key / service-account token
    MODEL_<ROLE>_CLASS      — Python class: "MultiModalEmbeddings" |
                              "MultiModalReranker"
    MODEL_<ROLE>_EXTRA      — JSON object of additional constructor kwargs
                              (e.g. ``{"chunk_size": 2048, "embedding_dim": 4096}``)

Roles:  EMBEDDER, RERANKER, VLM, ASR
"""

import hashlib
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

from multimodal_rag.utils.model_adapters import (
    MultiModalEmbeddings,
)
from multimodal_rag.utils.pcai_model_classes import (
    ChatModel,
    EmbeddingModel,
    RerankerModel,
    VoiceModel,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Defaults (sensible for Qwen3-VL models; override via EXTRA env var)
# ---------------------------------------------------------------------------

_DEFAULT_MM_KWARGS: dict[str, Any] = {
    "fps": 1.0,
    "max_frames": 64,
    "min_pixels": 4096,
    "max_pixels": 720 * 720,
    "total_pixels": 5 * 720 * 720,
}

_VLM_MODALITIES = ("text", "image", "video")

_EMBEDDER_INST_KWARGS: dict[str, Any] = {
    "tiktoken_enabled": False,
    "check_embedding_ctx_length": False,
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get(prefix: str, key: str, default: str = "") -> str:
    return os.environ.get(f"{prefix}_{key}", default)


def _load_extra(prefix: str) -> dict[str, Any]:
    """Read ``<prefix>_EXTRA`` as a JSON object and return the dict."""
    raw = _get(prefix, "EXTRA")
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        import logging

        logging.getLogger(__name__).warning(
            "Invalid JSON in %s_EXTRA: %s — ignoring",
            prefix,
            exc,
        )
        return {}


# ---------------------------------------------------------------------------
# Factory functions
# ---------------------------------------------------------------------------


def build_embedder(prefix: str = "MODEL_EMBEDDER") -> EmbeddingModel | None:
    # URL is required to enable a role; NAME is optional and, when empty,
    # auto-discovered from the endpoint's /v1/models listing.
    url = _get(prefix, "URL")
    if not url:
        return None
    name = _get(prefix, "NAME")
    api_key = _get(prefix, "API_KEY")

    extra = _load_extra(prefix)

    # Merge defaults with extra (extra wins)
    mm_kwargs = dict(_DEFAULT_MM_KWARGS)
    mm_kwargs.update(extra.pop("mm_processor_kwargs", {}))

    inst_kwargs = dict(_EMBEDDER_INST_KWARGS)
    inst_kwargs.update(extra.pop("model_instantiation_kwargs", {}))

    return EmbeddingModel(
        model_name=name,
        url_remote=url,
        api_key=api_key,
        model_instantiation_class=MultiModalEmbeddings,
        allowable_modalities=tuple(extra.pop("allowable_modalities", _VLM_MODALITIES)),
        mm_processor_kwargs=mm_kwargs,
        model_instantiation_kwargs=inst_kwargs,
        **extra,
    )


def build_reranker(prefix: str = "MODEL_RERANKER") -> RerankerModel | None:
    # URL is required to enable a role; NAME is optional and, when empty,
    # auto-discovered from the endpoint's /v1/models listing.
    url = _get(prefix, "URL")
    if not url:
        return None
    name = _get(prefix, "NAME")
    api_key = _get(prefix, "API_KEY")

    extra = _load_extra(prefix)
    mm_kwargs = dict(_DEFAULT_MM_KWARGS)
    mm_kwargs.update(extra.pop("mm_processor_kwargs", {}))

    return RerankerModel(
        model_name=name,
        url_remote=url,
        api_key=api_key,
        allowable_modalities=tuple(extra.pop("allowable_modalities", _VLM_MODALITIES)),
        mm_processor_kwargs=mm_kwargs,
        **extra,
    )


def build_vlm(prefix: str = "MODEL_VLM") -> ChatModel | None:
    # URL is required to enable a role; NAME is optional and, when empty,
    # auto-discovered from the endpoint's /v1/models listing.
    url = _get(prefix, "URL")
    if not url:
        return None
    name = _get(prefix, "NAME")
    api_key = _get(prefix, "API_KEY")
    extra = _load_extra(prefix)
    return ChatModel(
        model_name=name,
        url_remote=url,
        api_key=api_key,
        allowable_modalities=tuple(extra.pop("allowable_modalities", _VLM_MODALITIES)),
        **extra,
    )


def build_asr(prefix: str = "MODEL_ASR") -> VoiceModel | None:
    # URL is required to enable a role; NAME is optional and, when empty,
    # auto-discovered from the endpoint's /v1/models listing.
    url = _get(prefix, "URL")
    if not url:
        return None
    name = _get(prefix, "NAME")
    api_key = _get(prefix, "API_KEY")
    extra = _load_extra(prefix)
    return VoiceModel(
        model_name=name,
        url_remote=url,
        api_key=api_key,
        **extra,
    )


# ---------------------------------------------------------------------------
# Batch builder
# ---------------------------------------------------------------------------

ModelPack = tuple[
    EmbeddingModel | None,  # embedder
    RerankerModel | None,  # reranker
    ChatModel | None,  # vlm
    VoiceModel | None,  # asr
]


def build_all() -> ModelPack:
    """Build all four models from environment variables.

    Falls back to ``None`` for any model whose ``_URL`` variable is not
    set (``_NAME`` is optional and auto-discovered when omitted).
    """
    return (
        build_embedder("MODEL_EMBEDDER"),
        build_reranker("MODEL_RERANKER"),
        build_vlm("MODEL_VLM"),
        build_asr("MODEL_ASR"),
    )


# ---------------------------------------------------------------------------
# Hot config reload from mounted ConfigMap/Secret volumes
# ---------------------------------------------------------------------------
#
# Kubernetes env vars (``envFrom``) are snapshotted at container start, so a
# ConfigMap/Secret edit does NOT change them in a running pod.  To support
# no-rollout model swaps the charts additionally mount the ConfigMap + model
# Secret as *file volumes*: kubelet updates those files (config propagate ~1s
# after change), and this watcher detects the change and rebuilds the model
# objects live.


def _config_files(dirs: str) -> list[Path]:
    files: list[Path] = []
    for d in dirs.split(":"):
        p = Path(d)
        if not p.is_dir():
            continue
        try:
            for f in sorted(p.iterdir()):
                if f.is_file() and not f.name.startswith("."):
                    files.append(f)
        except OSError:
            continue
    return files


def apply_config_dirs(dirs: str) -> dict[str, str]:
    """Merge every file in ``:``-separated *dirs* into ``os.environ``.

    Mounted ConfigMaps/Secrets expose one file per key (file name = env var
    name, file content = value).  Returns the merged mapping that was applied.
    """
    merged: dict[str, str] = {}
    for f in _config_files(dirs):
        try:
            merged[f.name] = f.read_text(encoding="utf-8").rstrip("\r\n")
        except OSError:
            continue
    for key, value in merged.items():
        os.environ[key] = value
    return merged


def _config_snapshot(dirs: str) -> str:
    digest = hashlib.sha256()
    for f in _config_files(dirs):
        digest.update(f.name.encode("utf-8", "replace"))
        digest.update(b"=")
        try:
            digest.update(f.read_bytes())
        except OSError:
            continue
        digest.update(b"\n")
    return digest.hexdigest()


def start_config_watcher(dirs: str, reload_fn: Any, interval: float | None = None) -> None:
    """Poll *dirs* in a daemon thread and call ``reload_fn()`` when changed.

    ``reload_fn`` receives no arguments; it should swap in newly built model
    objects.  The watcher applies the file values to ``os.environ`` (via
    :func:`apply_config_dirs`) *before* invoking it, so ``build_all()`` sees
    the fresh values.  A failed ``reload_fn`` is logged and the previous
    configuration is kept; polling continues.  No-op when *dirs* is empty.
    The poll interval defaults to ``CONFIG_RELOAD_INTERVAL`` (15 s) unless an
    explicit *interval* is given.
    """
    if not dirs:
        return
    if interval is None:
        interval = float(os.environ.get("CONFIG_RELOAD_INTERVAL", "15.0"))
    last = {"snapshot": _config_snapshot(dirs)}

    def _loop() -> None:
        while True:
            time.sleep(interval)
            try:
                snapshot = _config_snapshot(dirs)
            except Exception:
                continue
            if snapshot == last["snapshot"]:
                continue
            last["snapshot"] = snapshot
            try:
                apply_config_dirs(dirs)
                reload_fn()
                logger.info("Hot-reloaded model configuration from %s", dirs)
            except Exception:
                logger.exception("Config reload failed — keeping previous configuration")

    thread = threading.Thread(target=_loop, daemon=True, name="config-watcher")
    thread.start()
    logger.info("Config watcher active on %s (poll %.0fs)", dirs, interval)
