import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from multimodal_rag.utils.logging_utils import logging

logger = logging.getLogger(__name__)

_HEADING_TAGS = re.compile(r"^</?h([1-6])", re.IGNORECASE)


@dataclass
class HTMLProcessor:
    """Parse HTML into clean text chunks with structural awareness.

    Extracts page title, headings, paragraphs, lists, tables, image
    descriptions (alt text), and hyperlink context.  Chunks respect
    heading boundaries so that sections stay intact whenever possible.

    Parameters
    ----------
    chunk_size:
        Target number of characters per chunk.
    chunk_overlap:
        Number of overlap characters between consecutive chunks.
    include_links:
        Whether to append ``[text](url)`` for hyperlinks.
    include_images:
        Whether to include ``[alt text](src)`` for images.
    """

    chunk_size: int = 8192
    chunk_overlap: int = 512
    text_splitter: Any | None = None
    include_links: bool = True
    include_images: bool = True

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def process(self, html_path: str) -> list[dict[str, Any]]:
        content = Path(html_path).read_text(encoding="utf-8")
        source = str(html_path)
        return self.process_html(content, source)

    def process_html(self, content: str, source: str = "") -> list[dict[str, Any]]:
        soup = self._parse(content)
        if soup is None:
            return []

        sections = self._extract_sections(soup)
        if not sections:
            return []

        return self._build_chunks(sections, source)

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _parse(html: str) -> Any:
        try:
            from bs4 import BeautifulSoup
        except ImportError:
            raise ImportError("BeautifulSoup is required for HTML processing. Install with: pip install beautifulsoup4")

        try:
            return BeautifulSoup(html, "html.parser")
        except Exception as e:
            logger.warning("Failed to parse HTML: %s", e)
            return None

    # ------------------------------------------------------------------
    # Section extraction
    # ------------------------------------------------------------------

    def _extract_sections(self, soup: Any) -> list[dict[str, Any]]:
        """Split HTML into sections by heading tags."""
        sections: list[dict[str, Any]] = []
        current: list[str] = []
        current_heading = ""
        current_level = 0

        # Page title
        title_tag = soup.find("title")
        title = title_tag.get_text(strip=True) if title_tag else ""

        def flush():
            text = self._clean_html("\n".join(current))
            if text:
                sections.append({"text": text, "heading": current_heading, "level": current_level})

        body = soup.find("body") or soup
        for el in body.descendants:
            if el.name is None:
                continue
            if el.name in ("h1", "h2", "h3", "h4", "h5", "h6"):
                flush()
                current = []
                current_level = int(el.name[1])
                current_heading = el.get_text(strip=True)
                current.append(el.get_text(strip=True))
            else:
                chunk = self._element_text(el)
                if chunk:
                    current.append(chunk)

        flush()

        # Prepend title to first section if no title section exists
        if title and sections and sections[0]["level"] > 0:
            sections.insert(0, {"text": f"Title: {title}", "heading": "", "level": 0})

        return sections

    def _element_text(self, el: Any) -> str:
        """Extract clean text from a single HTML element."""
        if el.name in ("script", "style", "nav", "footer", "header", "noscript"):
            return ""

        text = ""

        if el.name == "p":
            text = el.get_text(" ", strip=True)
        elif el.name in ("ul", "ol"):
            items = [li.get_text(" ", strip=True) for li in el.find_all("li", recursive=False)]
            text = "\n".join(f"- {item}" for item in items if item)
        elif el.name == "blockquote":
            inner = el.get_text(" ", strip=True)
            text = f"> {inner}" if inner else ""
        elif el.name == "pre":
            text = el.get_text("\n", strip=True)
        elif el.name == "table":
            text = self._table_text(el)
        elif el.name == "img":
            if self.include_images:
                alt = el.get("alt", "").strip()
                src = el.get("src", "").strip()
                if alt and src:
                    text = f"[Image: {alt}]({src})"
                elif alt:
                    text = f"[Image: {alt}]"
                elif src:
                    text = f"[Image: {src}]"
        elif el.name == "a":
            if self.include_links:
                href = el.get("href", "").strip()
                link_text = el.get_text(" ", strip=True)
                if link_text and href and not href.startswith("#"):
                    text = f"{link_text} ({href})"
                elif link_text:
                    text = link_text
        elif el.name == "br":
            text = "\n"
        elif el.name == "hr":
            text = "\n---\n"

        return text.strip()

    @staticmethod
    def _table_text(table: Any) -> str:
        """Convert an HTML table to a compact text representation."""
        rows: list[str] = []
        for tr in table.find_all("tr"):
            cells: list[str] = []
            for cell in tr.find_all(["td", "th"]):
                cells.append(cell.get_text(" ", strip=True))
            if cells:
                rows.append(" | ".join(cells))
        return "\n".join(rows)

    # ------------------------------------------------------------------
    # Cleaning
    # ------------------------------------------------------------------

    @staticmethod
    def _clean_html(text: str) -> str:
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r" *\n *", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    # ------------------------------------------------------------------
    # Chunking
    # ------------------------------------------------------------------

    def _build_chunks(self, sections: list[dict[str, Any]], source: str) -> list[dict[str, Any]]:
        from multimodal_rag.input_processing.text_processor import TextProcessor

        tp = TextProcessor(
            chunk_size=self.chunk_size,
            chunk_overlap=self.chunk_overlap,
            text_splitter=self.text_splitter,
        )
        full_text = "\n\n".join(sec["text"] for sec in sections)
        return tp.process_text(full_text, source)
