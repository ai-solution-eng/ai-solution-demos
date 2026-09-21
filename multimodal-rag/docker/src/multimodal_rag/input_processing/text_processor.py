import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from multimodal_rag.utils.logging_utils import logging

logger = logging.getLogger(__name__)

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)
_PARAGRAPH_RE = re.compile(r"\n\s*\n")


def _collapse_whitespace(text: str) -> str:
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


@dataclass
class TextProcessor:
    """Split plain text and Markdown into chunks.

    For Markdown files the splitter respects heading boundaries so that
    sections stay intact whenever possible.  Plain text is split by
    paragraph boundaries, falling back to token-level or character-level
    splitting for long runs.

    When *text_splitter* is provided (a ``TokenTextSplitter``), all size
    comparisons use **token counts** instead of character counts.
    """

    chunk_size: int = 8192
    chunk_overlap: int = 512
    strip_markdown: bool = False
    text_splitter: Any | None = None  # TokenTextSplitter

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def process(self, text_path: str) -> list[dict[str, Any]]:
        """Read a text/Markdown file and return chunked document dicts."""
        content = Path(text_path).read_text(encoding="utf-8")
        source = str(text_path)
        return self.process_text(content, source)

    def process_text(self, content: str, source: str = "") -> list[dict[str, Any]]:
        """Chunk a text string and return document dicts."""
        if self.strip_markdown:
            content = self._strip_md_formatting(content)

        is_markdown = bool(_HEADING_RE.search(content))
        if is_markdown:
            sections = self._split_by_headings(content)
        else:
            sections = [{"text": content, "level": 0, "heading": ""}]

        chunks = self._build_chunks(sections)
        return [{"text": ch["text"], "source": source, "chunk_index": i} for i, ch in enumerate(chunks)]

    # ------------------------------------------------------------------
    # Markdown helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _strip_md_formatting(text: str) -> str:
        text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
        text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
        text = re.sub(r"__(.+?)__", r"\1", text)
        text = re.sub(r"\*(.+?)\*", r"\1", text)
        text = re.sub(r"_(.+?)_", r"\1", text)
        text = re.sub(r"~~(.+?)~~", r"\1", text)
        text = re.sub(r"`(.+?)`", r"\1", text)
        text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
        text = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"\1", text)
        text = re.sub(r"^>\s+", "", text, flags=re.MULTILINE)
        text = re.sub(r"^[-*+]\s+", "", text, flags=re.MULTILINE)
        text = re.sub(r"^\d+\.\s+", "", text, flags=re.MULTILINE)
        text = re.sub(r"^---+$", "", text, flags=re.MULTILINE)
        text = re.sub(r"^===+$", "", text, flags=re.MULTILINE)
        text = re.sub(r"\|", " ", text)
        return _collapse_whitespace(text)

    @staticmethod
    def _split_by_headings(content: str) -> list[dict[str, Any]]:
        lines = content.split("\n")
        sections: list[dict[str, Any]] = []
        current_buf: list[str] = []
        current_heading = ""
        current_level = 0

        def flush():
            text = _collapse_whitespace("\n".join(current_buf))
            if text:
                sections.append({"text": text, "heading": current_heading, "level": current_level})

        for line in lines:
            m = _HEADING_RE.match(line)
            if m:
                flush()
                current_buf = []
                current_level = len(m.group(1))
                current_heading = m.group(2).strip()
                current_buf.append(line)
            else:
                current_buf.append(line)

        flush()
        return sections

    # ------------------------------------------------------------------
    # Chunking
    # ------------------------------------------------------------------

    def _exceeds_budget(self, text: str) -> bool:
        """Return True if *text* exceeds the chunk budget."""
        if self.text_splitter is not None:
            return self.text_splitter.count_tokens(text) > self.chunk_size
        return len(text) > self.chunk_size

    def _combined_budget(self, a: str, b: str) -> bool:
        """Return True if a + separator + b exceeds the chunk budget."""
        budget = self.chunk_size
        if self.text_splitter is not None:
            combined = a + " " + b if a and b else a or b
            return self.text_splitter.count_tokens(combined) > budget
        return len(a) + len(b) + 1 > budget

    def _build_chunks(self, sections: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Merge sections into chunks respecting *chunk_size*."""
        chunks: list[dict[str, Any]] = []
        buffer = ""
        buffer_sections: list[str] = []

        def flush_buffer():
            nonlocal buffer, buffer_sections
            if buffer.strip():
                chunks.append({"text": buffer.strip()})
            buffer = ""
            buffer_sections = []

        for sec in sections:
            sec_text = sec["text"]

            if self._exceeds_budget(sec_text):
                flush_buffer()
                sub_chunks = self._split_oversized(sec_text)
                for sc in sub_chunks:
                    if sc.strip():
                        chunks.append({"text": sc.strip()})
                continue

            if not buffer:
                buffer = sec_text
                buffer_sections = [sec_text]
            elif self._combined_budget(buffer, sec_text):
                carry = self._overlap_text(buffer)
                chunks.append({"text": buffer.strip()})
                buffer = (carry + " " + sec_text) if carry else sec_text
                buffer_sections = [sec_text]
            else:
                buffer += "\n\n" + sec_text
                buffer_sections.append(sec_text)

        flush_buffer()
        return chunks

    def _split_oversized(self, text: str) -> list[str]:
        """Split a single oversized text into chunks."""
        paragraphs = _PARAGRAPH_RE.split(text)
        paragraphs = [p.strip() for p in paragraphs if p.strip()]

        if not paragraphs:
            return self._split_fallback(text)

        if len(paragraphs) == 1 and self._exceeds_budget(paragraphs[0]):
            return self._split_fallback(paragraphs[0])

        chunks: list[str] = []
        current = ""

        for para in paragraphs:
            if not current:
                current = para
            elif self._combined_budget(current, para):
                chunks.append(current.strip())
                carry = self._overlap_text(current)
                current = (carry + " " + para) if carry else para
            else:
                current += "\n\n" + para

        if current.strip():
            chunks.append(current.strip())
        return chunks

    def _split_fallback(self, text: str) -> list[str]:
        """Split text by token budget (when splitter available) or characters."""
        if self.text_splitter is not None:
            return self.text_splitter.split_text(text)
        return self._split_by_chars(text)

    def _split_by_chars(self, text: str) -> list[str]:
        """Split text into chunks of *chunk_size* characters with overlap."""
        if not text:
            return []
        chunks: list[str] = []
        start = 0
        while start < len(text):
            end = min(start + self.chunk_size, len(text))
            if end < len(text):
                next_space = text.find(" ", end)
                if next_space != -1 and next_space - end < self.chunk_size // 2:
                    end = next_space
            chunks.append(text[start:end].strip())
            start = end - self.chunk_overlap if end < len(text) else len(text)
            start = max(start, 0)
        return [c for c in chunks if c]

    def _overlap_text(self, text: str) -> str:
        """Return overlap text (token-aware or character-based)."""
        if self.text_splitter is not None:
            return self.text_splitter.overlap_text(text)
        return self._overlap_by_chars(text, self.chunk_overlap)

    @staticmethod
    def _overlap_by_chars(text: str, num_chars: int) -> str:
        if num_chars <= 0 or not text:
            return ""
        if len(text) <= num_chars:
            return text
        truncated = text[-num_chars:]
        first_space = truncated.find(" ")
        if first_space != -1:
            truncated = truncated[first_space + 1 :]
        return truncated.strip()
