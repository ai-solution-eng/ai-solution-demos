import importlib
import os
from pathlib import Path
from typing import Optional

from .logging_utils import logging

logger = logging.getLogger(__name__)

__all__ = ["TokenTextSplitter"]


class TokenTextSplitter:
    """Token-count-aware text splitter using the standalone HuggingFace
    ``tokenizers`` library (Rust-based, CPU-only — no PyTorch needed).

    Designed to be embedded in the Docker image so no runtime download is
    required.  When the tokenizer file is not found, ``from_bundled()``
    returns ``None`` and callers fall back to character-based chunking.

    Parameters
    ----------
    tokenizer_path:
        Path to a ``tokenizer.json`` file on disk.
    chunk_size:
        Target number of tokens per chunk.
    chunk_overlap:
        Number of overlap tokens between consecutive chunks.
    """

    def __init__(self, tokenizer_path: str, chunk_size: int, chunk_overlap: int):
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self._tok = _load_tokenizer(tokenizer_path)
        self._tokenizer_path = tokenizer_path

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def count_tokens(self, text: str) -> int:
        """Return the number of tokens in *text*."""
        return len(self._tok.encode(text).ids)

    def split_text(self, text: str) -> list[str]:
        """Split *text* into chunks of at most *chunk_size* tokens.

        Overlap is applied at token boundaries.  Two tail rules prevent tiny
        final chunks:

        * **Merge:** if the final chunk would contain fewer than 10 %
          *chunk_size* **net-new** tokens (tokens not already present in the
          previous chunk via overlap), those tokens are folded into the
          previous chunk instead.
        * **Backfill:** otherwise, if the final chunk is still shorter than
          ``chunk_size // 4`` tokens (512 for a 2048-token *chunk_size*, 2048
          for an 8192-token one), its start is extended backward so the chunk
          reaches that minimum.  The extended region duplicates tokens already
          present in the previous chunk (extra leading overlap), which is
          consistent with the overlap-based design.
        """
        if not text:
            return []
        ids = self._tok.encode(text).ids
        if len(ids) <= self.chunk_size:
            return [text]

        min_new = max(self.chunk_size // 10, 1)
        min_tail = max(self.chunk_size // 4, 1)

        chunks: list[str] = []
        start = 0
        prev_start = 0
        prev_end = 0
        while start < len(ids):
            end = min(start + self.chunk_size, len(ids))

            if chunks and end >= len(ids):
                new_content = len(ids) - prev_end
                if new_content < min_new:
                    # Tail is tiny: fold it into the previous chunk.
                    chunks[-1] = self._tok.decode(ids[prev_start:end])
                    break
                # Tail stands on its own; backfill to the minimum length by
                # extending its start backward (extra leading overlap).
                if end - start < min_tail:
                    start = max(0, end - min_tail)

            chunks.append(self._tok.decode(ids[start:end]))
            if end >= len(ids):
                break
            prev_start = start
            prev_end = end
            start = end - self.chunk_overlap
            start = max(start, 0)
        return chunks

    def merge_until_budget(self, texts: list[str]) -> list[list[str]]:
        """Merge text fragments into groups that fit within *chunk_size* tokens.

        This is the core method that processors call instead of the old
        character-count merge pattern::

            if len(current) + len(next) > chunk_size:   # old
            if splitter.count_tokens(current) + splitter.count_tokens(next) > chunk_size:  # new

        Returns a list of groups (each group is a list of text fragments)
        so that callers can attach per-fragment metadata (images, sources,
        page numbers) to the merged result.
        """
        groups: list[list[str]] = []
        current_group: list[str] = []
        current_tokens = 0

        for text in texts:
            n = self.count_tokens(text)

            if current_tokens + n > self.chunk_size and current_group:
                groups.append(current_group)
                current_group = []
                current_tokens = 0

                # Carry overlap from the last fragment of the previous group
                if self.chunk_overlap > 0 and groups:
                    prev_text = groups[-1][-1]
                    carry_ids = self._tok.encode(prev_text).ids[-self.chunk_overlap :]
                    carry = self._tok.decode(carry_ids)
                    if carry.strip():
                        current_group.append(carry)
                        current_tokens = self.count_tokens(carry)

            current_group.append(text)
            current_tokens += n

        if current_group:
            groups.append(current_group)

        return groups

    def overlap_text(self, text: str) -> str:
        """Return the last *chunk_overlap* tokens of *text* decoded back to text.

        Call this when a single oversized fragment needs to carry overlap
        into the next chunk.
        """
        if self.chunk_overlap <= 0 or not text:
            return ""
        ids = self._tok.encode(text).ids
        if len(ids) <= self.chunk_overlap:
            return text
        carry_ids = ids[-self.chunk_overlap :]
        return self._tok.decode(carry_ids).strip()

    # ------------------------------------------------------------------
    # Classmethod helper for bundled tokenizer
    # ------------------------------------------------------------------

    @classmethod
    def from_bundled(
        cls,
        chunk_size: int,
        chunk_overlap: int,
        tokenizer_rel: str = "tokenizer.json",
    ) -> Optional["TokenTextSplitter"]:
        """Load the tokenizer bundled with the application.

        Searches upward from the ``utils/`` directory for *tokenizer_rel*.
        Returns ``None`` if the file is not found (callers fall back to
        character-based chunking).
        """
        path = _find_bundled_tokenizer(tokenizer_rel)
        if path is None:
            return None
        return cls(str(path), chunk_size, chunk_overlap)


# -----------------------------------------------------------------------
# Internal helpers
# -----------------------------------------------------------------------


def _load_tokenizer(path: str):
    """Import ``tokenizers`` and load the tokenizer file."""
    try:
        mod = importlib.import_module("tokenizers")
        return mod.Tokenizer.from_file(path)
    except Exception as exc:
        logger.warning("Failed to load tokenizer from %s: %s", path, exc)
        raise


_TOKENIZER_CACHE: dict[str, Path | None] = {}


def _find_bundled_tokenizer(rel: str) -> Path | None:
    """Locate the bundled tokenizer file.

    Resolution order:

    1. ``RAG_TOKENIZER_PATH`` env var (explicit override; takes precedence).
    2. An upward search from this file's directory: ``utils/`` -> package ->
       ``src/`` root -> application root.  The final level (application root)
       covers the production Docker layout, where the image bundles the
       tokenizer at ``/app/tokenizer.json`` while the package lives under
       ``/app/src/multimodal_rag/``.

    Returns ``None`` (and logs a warning) when the file cannot be found, so
    callers fall back to character-based chunking visibly rather than silently.
    """
    if rel in _TOKENIZER_CACHE:
        return _TOKENIZER_CACHE[rel]

    env_path = os.environ.get("RAG_TOKENIZER_PATH")
    if env_path:
        candidate = Path(env_path)
        if candidate.exists():
            _TOKENIZER_CACHE[rel] = candidate
            logger.info("Using tokenizer from RAG_TOKENIZER_PATH: %s", candidate)
            return candidate
        logger.warning("RAG_TOKENIZER_PATH=%s does not exist; ignoring", env_path)

    start = Path(__file__).resolve().parent  # multimodal_rag/utils/
    for parent in (
        start,
        start.parent,
        start.parent.parent,
        start.parent.parent.parent,
    ):
        candidate = parent / rel
        if candidate.exists():
            _TOKENIZER_CACHE[rel] = candidate
            logger.info("Using bundled tokenizer: %s", candidate)
            return candidate

    logger.warning(
        "Bundled tokenizer %r not found near %s; falling back to character-based chunking",
        rel,
        start,
    )
    _TOKENIZER_CACHE[rel] = None
    return None
