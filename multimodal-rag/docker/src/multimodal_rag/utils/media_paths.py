"""Local media-path allowlist + document media-ref validation.

Canonical home of the ``file://`` / local-path allowlist previously defined
in ``mcp_server`` (re-exported there so existing call sites keep working).
``rag_system`` uses it to enforce the same policy on media refs carried
inside user-supplied documents (REST ``POST /documents``, MCP ``add_memory``)
— those refs are read by the server at embed time and again at query time,
so an unvalidated ``file:///etc/passwd`` or bare ``/etc/passwd`` in a
document dict was an arbitrary-file-read channel.

Configuration (read once at import):

* ``MEDIA_ALLOW_PATH_PREFIXES`` — os.pathsep-separated prefixes, e.g.
  ``/data/datasets:/data/staging``.  Default: ``DATA_PATH/datasets`` +
  ``DATA_PATH/staging``.  An explicitly empty value allows nothing
  (fail-closed).  The special value ``*`` allows any local path — a
  dev/test escape hatch; never use it in production.
"""

import os

from multimodal_rag.utils.logging_utils import logging

logger = logging.getLogger(__name__)


class MediaRefError(ValueError):
    """A document media ref violates the local-path allowlist or URL policy."""


_DEFAULT_DATA_PATH = os.environ.get("DATA_PATH", "/data")
_MEDIA_ALLOW_DEFAULT = os.pathsep.join(
    (
        os.path.join(_DEFAULT_DATA_PATH, "datasets"),
        os.path.join(_DEFAULT_DATA_PATH, "staging"),
    )
)
_MEDIA_ALLOW_PATH_PREFIXES: tuple[str, ...] = tuple(
    os.path.normpath(p).rstrip(os.sep)
    for p in os.environ.get("MEDIA_ALLOW_PATH_PREFIXES", _MEDIA_ALLOW_DEFAULT).split(os.pathsep)
    if p.strip()
)
# Explicit "*" (alone or among prefixes) disables the local-path restriction.
_MEDIA_ALLOW_ANY = "*" in _MEDIA_ALLOW_PATH_PREFIXES


def _media_path_allowed(raw: str) -> bool:
    """True if *raw* (a file:// or local path) is inside an allowed prefix.

    With no configured prefixes nothing is allowed (fail-closed).  The env
    default is ``DATA_PATH``/datasets + ``DATA_PATH``/staging.  The special
    prefix value ``*`` allows any path (dev/test escape hatch).
    """
    if _MEDIA_ALLOW_ANY:
        return True
    if not _MEDIA_ALLOW_PATH_PREFIXES:
        return False
    p = raw.removeprefix("file://")
    try:
        resolved = os.path.realpath(p)
    except Exception:
        return False
    for prefix in _MEDIA_ALLOW_PATH_PREFIXES:
        if resolved == prefix or resolved.startswith(prefix + os.sep):
            return True
    return False


def _validate_media_ref(ref: str, key: str = "media") -> None:
    """Raise :class:`MediaRefError` if a document media *ref* is not allowed.

    Allowed refs:

    * ``data:`` URLs — inert, size-bounded by the request body cap;
    * ``http(s)://`` URLs — passed through the query-time URL policy
      (``_check_media_url_policy``); they are fetched by the server at embed
      and query time, so they get the same SSRF treatment;
    * ``file://`` URLs and bare paths — only when inside the
      ``MEDIA_ALLOW_PATH_PREFIXES`` allowlist (realpath-resolved).

    Everything else — including ``s3://`` (the server never fetches S3 refs
    from document payloads; ingest S3 URLs through ``/batch-urls`` instead)
    — is rejected.
    """
    if not isinstance(ref, str) or not ref:
        return
    if ref.startswith("data:"):
        return
    if ref.startswith(("http://", "https://")):
        # Deferred import: url_policy imports nothing from this package's
        # managers, but keep the coupling lazy anyway.
        from multimodal_rag.utils.url_policy import _check_media_url_policy

        try:
            _check_media_url_policy(ref)
        except ValueError as exc:
            # Re-raise as MediaRefError so the REST layer maps it to 400
            # (ingest-time validation), with the offending field named.
            raise MediaRefError(f"Document field '{key}': {exc}") from exc
        return
    if ref.startswith("s3://"):
        raise MediaRefError(
            f"Document field '{key}': s3:// refs are not supported — ingest S3 objects via "
            f"POST /api/datasets/{{name}}/batch-urls instead"
        )
    if not _media_path_allowed(ref):
        prefixes = (
            os.environ.get("MEDIA_ALLOW_PATH_PREFIXES") or f"{_DEFAULT_DATA_PATH}/datasets:{_DEFAULT_DATA_PATH}/staging"
        )
        raise MediaRefError(
            f"Document field '{key}': local media path '{ref}' is outside the allowed prefixes "
            f"(MEDIA_ALLOW_PATH_PREFIXES={prefixes})"
        )


def _validate_document_media_refs(documents: list) -> None:
    """Validate every media ref in user-supplied *documents* (in place, raising).

    Checks the media keys the pipeline reads server-side: ``image`` /
    ``video`` / ``audio`` (tier-3, fetched at embed + query time) and the
    ``preprocessed_*`` tier-2 refs (resolved for display and for the
    query-time VLM/ASR paths).  String documents are skipped.
    """
    media_keys = (
        "image",
        "video",
        "audio",
        "preprocessed_image",
        "preprocessed_video",
        "preprocessed_audio",
    )
    for doc in documents:
        if not isinstance(doc, dict):
            continue
        for key in media_keys:
            val = doc.get(key)
            if not val:
                continue
            refs = val if isinstance(val, list) else [val]
            for ref in refs:
                _validate_media_ref(ref, key)
