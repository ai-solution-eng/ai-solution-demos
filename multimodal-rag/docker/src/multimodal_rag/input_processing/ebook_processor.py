import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from multimodal_rag.utils.logging_utils import logging

logger = logging.getLogger(__name__)

# EPUB decompression bounds.  An EPUB is a ZIP, and ``zf.read()`` decompresses
# a whole entry into RAM before any size check could run — a crafted EPUB
# (ZIP bombs reach ~1000:1) could balloon a small upload into gigabytes.
# Both caps are checked against the entry's DECLARED uncompressed size before
# reading.  ``0`` disables a cap.
_MAX_EPUB_MEMBER_BYTES = max(0, int(os.environ.get("MAX_EPUB_MEMBER_BYTES", str(256 * 1024 * 1024))))
_MAX_EPUB_TOTAL_BYTES = max(0, int(os.environ.get("MAX_EPUB_TOTAL_BYTES", str(1024 * 1024 * 1024))))


def _extract_text_from_html(html: str) -> str:
    """Strip HTML tags and return clean text."""
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _read_member(zf: Any, name: str, budget: dict[str, int]) -> bytes:
    """Read one EPUB member with declared-size bounds; raise ValueError on breach."""
    info = zf.getinfo(name)
    if _MAX_EPUB_MEMBER_BYTES and info.file_size > _MAX_EPUB_MEMBER_BYTES:
        raise ValueError(
            f"EPUB member '{name}' declares {info.file_size} bytes — exceeds "
            f"MAX_EPUB_MEMBER_BYTES ({_MAX_EPUB_MEMBER_BYTES})"
        )
    if _MAX_EPUB_TOTAL_BYTES:
        budget["total"] += info.file_size
        if budget["total"] > _MAX_EPUB_TOTAL_BYTES:
            raise ValueError(
                f"EPUB members total {budget['total']} bytes — exceeds MAX_EPUB_TOTAL_BYTES ({_MAX_EPUB_TOTAL_BYTES})"
            )
    return zf.read(name)


@dataclass
class EbookProcessor:
    """Extract text and images from EPUB e-books.

    Reads the OPF manifest to determine the spine (reading order),
    then extracts text (and optionally images) from each chapter.

    Parameters
    ----------
    chunk_size:
        Target characters per chunk.  Chapters are merged up to this limit.
    chunk_overlap:
        Overlap characters between merged chunks.
    extract_images:
        Whether to extract inline images from chapters.
    """

    chunk_size: int = 8192
    chunk_overlap: int = 512
    text_splitter: Any | None = None
    extract_images: bool = True

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def process(self, epub_path: str) -> list[dict[str, Any]]:
        source = str(epub_path)

        try:
            import zipfile
        except ImportError:
            raise ImportError("zipfile (stdlib) is required for EPUB processing")

        try:
            zf = zipfile.ZipFile(epub_path, "r")
        except Exception as e:
            logger.warning("Failed to open EPUB %s: %s", epub_path, e)
            return []

        try:
            chapters = self._parse_opf(zf, budget={"total": 0})
            if not chapters:
                logger.warning("No chapters found in %s", epub_path)
                return []
            docs = self._build_chunks(chapters, source)
            return docs
        finally:
            zf.close()

    # ------------------------------------------------------------------
    # OPF parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_opf(zf: Any, budget: dict[str, int]) -> list[dict[str, Any]]:
        """Find and parse the OPF manifest, return chapters in spine order.

        ``budget`` is a ``{"total": int}`` accumulator shared across all
        member reads so the per-file decompression cap is global, not
        per-entry.
        """
        # Locate the OPF file from META-INF/container.xml
        try:
            container_xml = _read_member(zf, "META-INF/container.xml", budget).decode("utf-8")
        except KeyError:
            logger.warning("No META-INF/container.xml in EPUB")
            return []

        opf_path = ""
        m = re.search(
            r'<rootfile\s[^>]*full-path\s*=\s*"([^"]+)"',
            container_xml,
            re.IGNORECASE,
        )
        if m:
            opf_path = m.group(1)
        else:
            logger.warning("Could not find OPF path in container.xml")
            return []

        try:
            opf_data = _read_member(zf, opf_path, budget).decode("utf-8")
        except KeyError:
            logger.warning("OPF file %s not found in EPUB", opf_path)
            return []

        opf_dir = str(Path(opf_path).parent)
        if opf_dir == ".":
            opf_dir = ""

        # Parse the spine (reading order)
        spine_ids: list[str] = []
        for m in re.finditer(r'<itemref\s[^>]*idref\s*=\s*"([^"]+)"', opf_data, re.IGNORECASE):
            spine_ids.append(m.group(1))

        # Build id → href map from the manifest
        id_to_href: dict[str, str] = {}
        for m in re.finditer(
            r'<item\s[^>]*id\s*=\s*"([^"]+)"[^>]*href\s*=\s*"([^"]+)"',
            opf_data,
            re.IGNORECASE,
        ):
            id_to_href[m.group(1)] = m.group(2)

        # Read each spine item
        chapters: list[dict[str, Any]] = []
        for item_id in spine_ids:
            href = id_to_href.get(item_id)
            if not href:
                continue
            chapter_path = f"{opf_dir}/{href}" if opf_dir else href
            chapter_path = chapter_path.replace("\\", "/")
            # Normalize: remove leading ./
            while chapter_path.startswith("./"):
                chapter_path = chapter_path[2:]

            try:
                html_data = _read_member(zf, chapter_path, budget).decode("utf-8")
            except KeyError:
                # Try alternate path forms
                alt = str(Path(opf_dir) / href)
                alt = alt.replace("\\", "/")
                while alt.startswith("./"):
                    alt = alt[2:]
                try:
                    html_data = _read_member(zf, alt, budget).decode("utf-8")
                except KeyError:
                    logger.debug("Could not read chapter %s (%s)", item_id, chapter_path)
                    continue

            # Extract images from this chapter
            chapter_images: list[str] = []
            if hasattr(zf, "namelist"):
                for img_src in re.findall(r'<img[^>]+src\s*=\s*"([^"]+)"', html_data, re.IGNORECASE):
                    img_path = str(Path(opf_dir) / img_src)
                    img_path = img_path.replace("\\", "/")
                    while img_path.startswith("./"):
                        img_path = img_path[2:]
                    try:
                        # Bound violations (ValueError) deliberately propagate:
                        # a member over the declared-size cap fails the whole
                        # file rather than being silently skipped.
                        img_data = _read_member(zf, img_path, budget)
                    except KeyError:
                        continue
                    ext = Path(img_path).suffix.lower()
                    mime = {
                        ".png": "image/png",
                        ".jpg": "image/jpeg",
                        ".jpeg": "image/jpeg",
                        ".gif": "image/gif",
                        ".webp": "image/webp",
                        ".svg": "image/svg+xml",
                    }.get(ext, "image/png")
                    import base64

                    b64 = base64.b64encode(img_data).decode("utf-8")
                    chapter_images.append(f"data:{mime};base64,{b64}")

            text = _extract_text_from_html(html_data)
            chapter: dict[str, Any] = {"text": text}
            if chapter_images:
                chapter["image"] = chapter_images
            chapters.append(chapter)

        return chapters

    # ------------------------------------------------------------------
    # Chunking
    # ------------------------------------------------------------------

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

    def _build_chunks(self, chapters: list[dict[str, Any]], source: str) -> list[dict[str, Any]]:
        chunks: list[dict[str, Any]] = []
        buffer_text = ""
        buffer_images: list[str] = []

        def flush() -> None:
            nonlocal buffer_text, buffer_images
            if buffer_text.strip():
                chunk: dict[str, Any] = {
                    "text": buffer_text.strip(),
                    "source": source,
                }
                if buffer_images:
                    seen: list[str] = []
                    for img in buffer_images:
                        if img not in seen:
                            seen.append(img)
                    chunk["image"] = seen
                chunks.append(chunk)
            buffer_text = ""
            buffer_images = []

        for ch in chapters:
            chap_text = ch.get("text", "")
            chap_images = ch.get("image", []) if isinstance(ch.get("image"), list) else []

            if not chap_text and not chap_images:
                continue

            if not buffer_text:
                buffer_text = chap_text
                buffer_images = list(chap_images)
            elif self._exceeds_combined_budget(buffer_text, chap_text):
                flush()
                buffer_text = chap_text
                buffer_images = list(chap_images)
            else:
                if buffer_text:
                    buffer_text += "\n\n"
                buffer_text += chap_text
                buffer_images.extend(chap_images)

        flush()

        for i, ch in enumerate(chunks):
            ch["chunk_index"] = i

        return chunks
