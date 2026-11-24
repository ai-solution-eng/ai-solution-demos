# Release version — kept in sync with the helm charts by bump_version.sh
# (automation.sh bumps BEFORE the docker build, so the running image can
# self-identify: `python3 -c "import multimodal_rag; print(multimodal_rag.__version__)"`).
__version__ = "6.3.2"

import os as _os


def _normalize_no_proxy_env() -> None:
    """Rewrite bracketed-IPv6 NO_PROXY entries into the bare form httpx accepts.

    NO_PROXY is a HOST LIST, not a URL list, so ``[::1]`` is a legal and common
    way to write a loopback IPv6 literal (curl, urllib and the Go toolchain all
    accept it).  httpx2 parses each entry as a URL pattern and raises
    ``InvalidURL: Invalid port: ':1]'`` on the brackets — and it does so in the
    ``Client.__init__`` of EVERY client, including ones built at module scope.
    A single bracketed entry therefore made the whole package unimportable
    (2026-11-10 audit: reproduced on a desktop whose NO_PROXY carried
    ``[::1]``; 65 pytest collection errors, app would not start).

    Normalizing once at package import fixes all 17 client-construction sites
    (api_server, dataset_manager, mcp_server, rag_system, model_adapters,
    pcai_model_classes) without touching any of them.  Only the brackets are
    stripped: ``[::1]`` -> ``::1``, which httpx2 accepts; ports and every other
    entry are left byte-identical.
    """
    for var in ("NO_PROXY", "no_proxy"):
        raw = _os.environ.get(var)
        if not raw or "[" not in raw:
            continue
        entries: list[str] = []
        for entry in raw.split(","):
            item = entry.strip()
            if item.startswith("[") and "]" in item:
                item = item[1 : item.index("]")]
            if item and item not in entries:  # de-dup after the rewrite
                entries.append(item)
        _os.environ[var] = ",".join(entries)


_normalize_no_proxy_env()

# Imported AFTER _normalize_no_proxy_env(): every httpx client constructed during
# these imports would otherwise fail on a bracketed-IPv6 NO_PROXY entry.
from .dataset_manager import DatasetManager  # noqa: E402
from .rag_system import MultimodalRAG, MultiModalRAGSystem, Postprocessor, Preprocessor  # noqa: E402

__all__ = [
    "DatasetManager",
    "MultiModalRAGSystem",
    "MultimodalRAG",
    "Postprocessor",
    "Preprocessor",
]
