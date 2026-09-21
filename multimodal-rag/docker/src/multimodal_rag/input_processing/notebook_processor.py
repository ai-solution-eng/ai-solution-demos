import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from multimodal_rag.utils.logging_utils import logging

logger = logging.getLogger(__name__)


def _collapse(text: str) -> str:
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


@dataclass
class NotebookProcessor:
    """Parse Jupyter notebooks (``.ipynb``) into cell-level document chunks.

    Each cell (markdown, code, or raw) becomes one or more document dicts.
    Code cell outputs are captured as text, and inline images (``image/png``,
    ``image/jpeg``) are extracted as data URLs.

    Parameters
    ----------
    chunk_size:
        Target characters per chunk.  Consecutive small cells are merged.
    chunk_overlap:
        Overlap characters between merged chunks.
    strip_markdown:
        Whether to remove Markdown formatting from markdown cells.
    include_code:
        Whether to include code cell source in the output.
    include_outputs:
        Whether to include code cell outputs (text and images).
    """

    chunk_size: int = 8192
    chunk_overlap: int = 512
    strip_markdown: bool = False
    include_code: bool = True
    include_outputs: bool = True
    text_splitter: Any | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def process(self, nb_path: str) -> list[dict[str, Any]]:
        source = str(nb_path)
        raw = Path(nb_path).read_text(encoding="utf-8")
        try:
            nb = json.loads(raw)
        except json.JSONDecodeError as e:
            logger.warning("Invalid notebook %s: %s", nb_path, e)
            return []
        return self._process_notebook(nb, source)

    def process_data(self, nb: dict[str, Any], source: str = "") -> list[dict[str, Any]]:
        return self._process_notebook(nb, source)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _process_notebook(self, nb: dict[str, Any], source: str) -> list[dict[str, Any]]:
        cells = nb.get("cells", [])
        lang = nb.get("metadata", {}).get("kernelspec", {}).get("language", "python")

        cell_docs: list[dict[str, Any]] = []
        for i, cell in enumerate(cells):
            ctype = cell.get("cell_type", "code")
            doc = self._process_cell(cell, i, ctype, lang)
            if doc:
                cell_docs.append(doc)

        return self._merge_cells(cell_docs, source)

    def _process_cell(self, cell: dict[str, Any], idx: int, ctype: str, lang: str) -> dict[str, Any] | None:
        source_lines = cell.get("source", [])
        src_text = "".join(source_lines).strip()
        if not src_text and ctype != "code":
            return None

        result: dict[str, Any] = {
            "cell_index": idx,
            "cell_type": ctype,
        }

        if ctype == "markdown":
            text = src_text
            if self.strip_markdown:
                text = self._strip_md(text)
            result["text"] = text

        elif ctype == "code":
            texts: list[str] = []
            images: list[str] = []

            if self.include_code and src_text:
                texts.append(f"```{lang}\n{src_text}\n```")

            if self.include_outputs:
                for output in cell.get("outputs", []):
                    otype = output.get("output_type", "")
                    if otype == "stream":
                        otext = "".join(output.get("text", []))
                        if otext.strip():
                            texts.append(otext.strip())
                    elif otype in ("execute_result", "display_data"):
                        data = output.get("data", {})
                        # Text
                        for mime in ("text/plain", "text/html", "text/markdown"):
                            if mime in data:
                                val = data[mime]
                                if isinstance(val, list):
                                    texts.append("".join(val).strip())
                                elif isinstance(val, str):
                                    texts.append(val.strip())
                        # Images
                        for mime in ("image/png", "image/jpeg", "image/gif"):
                            if mime in data:
                                b64 = data[mime]
                                if isinstance(b64, list):
                                    b64 = "".join(b64)
                                images.append(f"data:{mime};base64,{b64}")
                    elif otype == "error":
                        ename = output.get("ename", "")
                        evalue = output.get("evalue", "")
                        if ename or evalue:
                            texts.append(f"[Error: {ename}: {evalue}]")

            if not texts and not images:
                return None

            result["text"] = "\n\n".join(texts) if texts else ""
            if images:
                result["image"] = images

        elif ctype == "raw":
            result["text"] = src_text

        else:
            return None

        return result

    # ------------------------------------------------------------------
    # Budget helpers
    # ------------------------------------------------------------------

    def _exceeds_budget(self, text: str) -> bool:
        if self.text_splitter is not None:
            return self.text_splitter.count_tokens(text) > self.chunk_size
        return len(text) > self.chunk_size

    def _exceeds_combined_budget(self, buffer: list[dict[str, Any]], next_text: str) -> bool:
        """Check if adding *next_text* to *buffer* exceeds the budget."""
        total = sum(len(d.get("text", "")) for d in buffer)
        if self.text_splitter is not None:
            all_text = "\n\n".join(d.get("text", "") for d in buffer)
            combined = all_text + "\n\n" + next_text if all_text else next_text
            return self.text_splitter.count_tokens(combined) > self.chunk_size
        return total + len(next_text) + 1 > self.chunk_size

    # ------------------------------------------------------------------
    # Chunking
    # ------------------------------------------------------------------

    def _merge_cells(self, cell_docs: list[dict[str, Any]], source: str) -> list[dict[str, Any]]:
        if not cell_docs:
            return []

        chunks: list[dict[str, Any]] = []
        buffer: list[dict[str, Any]] = []
        buffer_len = 0

        def flush() -> None:
            nonlocal buffer, buffer_len
            if not buffer:
                return
            if len(buffer) == 1:
                doc = dict(buffer[0])
                doc["source"] = source
                chunks.append(doc)
            else:
                texts: list[str] = []
                images: list[str] = []
                for d in buffer:
                    if d.get("text"):
                        texts.append(d["text"])
                    for k in ("image", "images"):
                        val = d.get(k)
                        if val:
                            if isinstance(val, list):
                                images.extend(val)
                            else:
                                images.append(val)
                merged: dict[str, Any] = {
                    "text": "\n\n".join(texts),
                    "source": source,
                }
                if images:
                    merged["image"] = images
                chunks.append(merged)
            buffer = []
            buffer_len = 0

        for doc in cell_docs:
            doc_text = doc.get("text", "")
            doc_len = len(doc_text)

            if not buffer:
                buffer.append(doc)
                buffer_len = doc_len
            elif self._exceeds_combined_budget(buffer, doc_text):
                flush()
                buffer.append(doc)
                buffer_len = doc_len
            else:
                buffer.append(doc)
                buffer_len += doc_len

        flush()

        # Assign chunk_index to each
        for i, ch in enumerate(chunks):
            ch["chunk_index"] = i

        return chunks

    @staticmethod
    def _strip_md(text: str) -> str:
        text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
        text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
        text = re.sub(r"__(.+?)__", r"\1", text)
        text = re.sub(r"\*(.+?)\*", r"\1", text)
        text = re.sub(r"_(.+?)_", r"\1", text)
        text = re.sub(r"`(.+?)`", r"\1", text)
        text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
        text = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"\1", text)
        return _collapse(text)
