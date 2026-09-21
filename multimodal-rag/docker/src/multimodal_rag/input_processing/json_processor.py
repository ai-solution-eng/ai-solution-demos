import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from multimodal_rag.utils.logging_utils import logging

logger = logging.getLogger(__name__)


def _flatten_json(
    obj: Any,
    prefix: str = "",
    separator: str = ".",
) -> dict[str, Any]:
    """Recursively flatten a nested JSON structure.

    Arrays are indexed numerically (``items.0.name``).  The result is a
    single-level dict of ``dot.path → value``.
    """
    items: dict[str, Any] = {}

    if isinstance(obj, dict):
        for k, v in obj.items():
            path = f"{prefix}{separator}{k}" if prefix else k
            if isinstance(v, (dict, list)):
                nested = _flatten_json(v, path, separator)
                items.update(nested)
            else:
                items[path] = v

    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            path = f"{prefix}{separator}{i}" if prefix else str(i)
            if isinstance(v, (dict, list)):
                nested = _flatten_json(v, path, separator)
                items.update(nested)
            else:
                items[path] = v

    else:
        items[prefix] = obj

    return items


def _flatten_to_text(obj: Any, separator: str = ".") -> str:
    """Flatten *obj* and render as ``key: value`` lines."""
    flat = _flatten_json(obj, separator=separator)
    lines: list[str] = []
    for key in sorted(flat):
        val = flat[key]
        if val is None:
            continue
        lines.append(f"{key}: {val}")
    return "\n".join(lines)


def _json_path_for_keys(keys: list[str], parent_path: str = "$", separator: str = ".") -> str:
    """Build a JSONPath-like string from a list of flattened keys."""
    if not keys:
        return parent_path
    path = parent_path
    for k in keys:
        if isinstance(k, int):
            path += f"[{k}]"
        else:
            path += f"{separator}{k}"
    return path


@dataclass
class JSONProcessor:
    """Flatten, stringify, and optionally chunk JSON data for RAG ingestion.

    Each top-level object becomes one or more document dicts with a
    flattened key-value text representation and a ``json_path`` metadata
    field pointing to its location in the source.

    When the root value is an array of records, consecutive records are
    merged into context-aware chunks that fill up to ``merge_token_budget``
    tokens (default 2048) instead of emitting one entry per record.  A
    record that alone exceeds the budget is still split on its own.  Each
    merged chunk carries a range ``json_path`` (e.g. ``$.0..5``) and a
    ``json_element_count`` field so provenance stays traceable.

    Parameters
    ----------
    chunk_size:
        Maximum characters per chunk.  Documents larger than this are
        split at top-level key boundaries.
    chunk_overlap:
        Overlap characters between consecutive chunks (applies when a
        single object is large enough to require splitting).
    flatten:
        Whether to flatten nested structures into ``key: value`` lines.
        When ``False`` the JSON is simply pretty-printed.
    separator:
        Key path separator for flattened output (default ``"."``).
    merge_records:
        When the root is an array, merge consecutive records into chunks
        that fill up to *merge_token_budget* (instead of one doc each).
    merge_token_budget:
        Target token budget for a merged array chunk (default 2048).
        When *text_splitter* is ``None`` this is treated as a character
        budget.
    """

    chunk_size: int = 8192
    chunk_overlap: int = 512
    flatten: bool = True
    separator: str = "."
    text_splitter: Any | None = None
    merge_records: bool = True
    merge_token_budget: int = 2048

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def process(self, json_path: str) -> list[dict[str, Any]]:
        """Read a JSON file and return chunked document dicts.

        Returns
        -------
        list[dict]
            ``[{"text": …, "source": …, "json_path": "$.path"}, …]``
        """
        content = Path(json_path).read_text(encoding="utf-8")
        try:
            data = json.loads(content)
        except json.JSONDecodeError as e:
            logger.warning("Invalid JSON in %s: %s", json_path, e)
            return []

        source = str(json_path)
        return self.process_data(data, source)

    def process_data(self, data: Any, source: str = "") -> list[dict[str, Any]]:
        """Process an already-parsed JSON value into document dicts."""
        if isinstance(data, list):
            if self.merge_records:
                return self._process_records(data, source)
            docs: list[dict[str, Any]] = []
            for i, item in enumerate(data):
                item_docs = self._process_single(item, source)
                for d in item_docs:
                    existing_path = d.get("json_path", "$")
                    d["json_path"] = f"$[{i}]{existing_path[1:]}" if existing_path != "$" else f"$[{i}]"
                docs.extend(item_docs)
            return docs
        else:
            return self._process_single(data, source)

    # ------------------------------------------------------------------
    # Record merging (array root)
    # ------------------------------------------------------------------

    def _process_records(self, records: list[Any], source: str) -> list[dict[str, Any]]:
        """Merge consecutive array records into token-budgeted chunks.

        Records are flattened to text and greedily packed into groups that
        fit within *merge_token_budget*.  A record that alone exceeds the
        budget is processed/split on its own via :meth:`_process_single`.
        Each merged group becomes one document with a range ``json_path``
        (e.g. ``$.0..5``) and a ``json_element_count`` field.
        """
        if not records:
            return []

        docs: list[dict[str, Any]] = []
        group: list[tuple[int, Any, str]] = []  # (index, record, text)
        group_tokens = 0

        def _flush() -> None:
            nonlocal group, group_tokens
            if not group:
                return
            indexes = [idx for idx, _, _ in group]
            texts = [t for _, _, t in group]
            text = self._join_records(indexes, texts)
            doc: dict[str, Any] = {
                "text": text,
                "source": source,
                "json_path": self._path_for_indexes(indexes),
                "json_element_count": len(indexes),
            }
            docs.append(doc)
            group = []
            group_tokens = 0

        for idx, record in enumerate(records):
            if self.flatten:
                text = _flatten_to_text(record, self.separator)
            else:
                text = json.dumps(record, indent=2, ensure_ascii=False)

            # A record that alone exceeds the chunk budget is split on its
            # own (an oversized single object), never merged with siblings.
            if self._exceeds_budget(text):
                _flush()
                item_docs = self._process_single(record, source)
                for d in item_docs:
                    existing = d.get("json_path", "$")
                    d["json_path"] = f"$[{idx}]{existing[1:]}" if existing != "$" else f"$[{idx}]"
                docs.extend(item_docs)
                continue

            tokens = self.count(text)
            if group and group_tokens + tokens > self.merge_token_budget:
                _flush()
            group.append((idx, record, text))
            group_tokens += tokens

        _flush()
        return docs

    @staticmethod
    def _join_records(indexes: list[int], texts: list[str]) -> str:
        """Join flattened record texts with a readable record header."""
        parts: list[str] = []
        for idx, text in zip(indexes, texts):
            parts.append(f"[record {idx}]")
            parts.append(text)
        return "\n".join(parts)

    @staticmethod
    def _path_for_indexes(indexes: list[int]) -> str:
        """Render a compact range json_path for a set of element indexes."""
        if not indexes:
            return "$"
        if len(indexes) == 1:
            return f"$[{indexes[0]}]"
        # contiguous run -> range, else comma list
        if indexes == list(range(indexes[0], indexes[0] + len(indexes))):
            return f"$[{indexes[0]}..{indexes[-1]}]"
        inner = ",".join(str(i) for i in indexes)
        return f"$[{inner}]"

    def count(self, text: str) -> int:
        """Return token count when a tokenizer is available, else chars."""
        if self.text_splitter is not None:
            return self.text_splitter.count_tokens(text)
        return len(text)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _process_single(self, obj: Any, source: str) -> list[dict[str, Any]]:
        """Convert one JSON value into one-or-more document dicts."""
        if self.flatten:
            text = _flatten_to_text(obj, self.separator)
        else:
            text = json.dumps(obj, indent=2, ensure_ascii=False)

        if not self._exceeds_budget(text):
            return [{"text": text, "source": source, "json_path": "$"}]

        return self._chunk_large_object(obj, source)

    # ------------------------------------------------------------------
    # Budget helpers
    # ------------------------------------------------------------------

    def _exceeds_budget(self, text: str) -> bool:
        if self.text_splitter is not None:
            return self.text_splitter.count_tokens(text) > self.chunk_size
        return len(text) > self.chunk_size

    def _exceeds_combined_budget(self, a: str, b: str) -> bool:
        if self.text_splitter is not None:
            combined = a + "\n\n" + b if a and b else a or b
            return self.text_splitter.count_tokens(combined) > self.chunk_size
        return len(a) + len(b) + 1 > self.chunk_size

    def _exceeds_combined_buffer(self, buffer: dict[str, Any], entry_text: str) -> bool:
        """Check if adding *entry_text* to *buffer* exceeds the token/char budget."""
        if not buffer:
            return False
        if self.text_splitter is not None:
            import json

            combined = json.dumps(buffer, indent=2, ensure_ascii=False) + "\n" + entry_text
            return self.text_splitter.count_tokens(combined) > self.chunk_size
        return sum(len(str(v)) for v in buffer.values()) + len(entry_text) + 1 > self.chunk_size

    def _chunk_large_object(self, obj: Any, source: str) -> list[dict[str, Any]]:
        """Split a large JSON object by top-level keys into chunks."""
        if not isinstance(obj, dict):
            text = (
                json.dumps(obj, indent=2, ensure_ascii=False)
                if not self.flatten
                else _flatten_to_text(obj, self.separator)
            )
            return self._split_text(text, source, "$")

        docs: list[dict[str, Any]] = []
        buffer: dict[str, Any] = {}
        buffer_size = 0

        for k, v in obj.items():
            if self.flatten:
                entry_text = _flatten_to_text({k: v}, self.separator)
            else:
                entry_text = json.dumps({k: v}, indent=2, ensure_ascii=False)

            entry_size = len(entry_text)

            if self._exceeds_budget(entry_text):
                if buffer:
                    docs.append(self._make_doc(buffer, source))
                    buffer = {}
                    buffer_size = 0
                sub_docs = self._split_text(entry_text, source, f"$.{k}")
                docs.extend(sub_docs)
                continue

            if self._exceeds_combined_buffer(buffer, entry_text):
                docs.append(self._make_doc(buffer, source))
                buffer = {}
                buffer_size = 0

            buffer[k] = v
            buffer_size += entry_size

        if buffer:
            docs.append(self._make_doc(buffer, source))

        return docs

    def _split_text(self, text: str, source: str, json_path: str) -> list[dict[str, Any]]:
        """Split a text that exceeds chunk_size into smaller pieces."""
        text_processor = TextProcessor(
            chunk_size=self.chunk_size,
            chunk_overlap=self.chunk_overlap,
        )
        chunks = text_processor.process_text(text, source)
        for ch in chunks:
            ch["json_path"] = json_path
        return chunks

    @staticmethod
    def _make_doc(data: dict[str, Any], source: str) -> dict[str, Any]:
        text = _flatten_to_text(data)
        return {"text": text, "source": source, "json_path": "$"}


# Late import to avoid circular issues
from multimodal_rag.input_processing.text_processor import (  # noqa: E402
    TextProcessor,
)
