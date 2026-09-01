import base64
import os
import re
import shutil
import subprocess
from collections.abc import Generator
from typing import Any

from multimodal_rag.utils.logging_utils import logging

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# OCR fallback for scanned PDFs (roadmap feature 3)
# ---------------------------------------------------------------------------
# Pages that carry images but no extractable text layer (scans) are
# rasterized and OCR'd through the tesseract CLI — no python binding, the
# repo already drives ffmpeg/unrar via subprocess.  Gated per dataset via the
# ``ocr`` flag (default off); when the binary is missing the fallback is
# skipped silently and scanned pages keep their image-only behaviour (plus
# VLM caption twins when captioning is enabled).

_OCR_LANG = os.environ.get("OCR_LANG", "eng")
_OCR_DPI = int(os.environ.get("OCR_DPI", "150"))
_OCR_TIMEOUT_S = float(os.environ.get("OCR_TIMEOUT_S", "120"))


def _tesseract_available() -> bool:
    """True when the tesseract CLI is on PATH."""
    return shutil.which("tesseract") is not None


def _ocr_png(png_bytes: bytes) -> str | None:
    """OCR a PNG raster via the tesseract CLI; ``None`` on any failure."""
    try:
        proc = subprocess.run(
            ["tesseract", "stdin", "stdout", "-l", _OCR_LANG],
            input=png_bytes,
            capture_output=True,
            timeout=_OCR_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        logger.debug("tesseract exited %d: %s", proc.returncode, (proc.stderr or b"")[:200])
        return None
    text = " ".join(proc.stdout.decode("utf-8", "replace").split())
    return text.strip() or None


def _collapse_whitespace(text: str) -> str:
    """Collapse consecutive whitespace into single spaces (preserving paragraph breaks)."""
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _strip_chart_noise(text: str) -> str:
    """Remove numeric axis-label and chart-value noise from extracted text.

    PDF text extraction from plot-heavy pages produces runs of bare numbers
    (axis ticks like ``0 100 200 300 400 500``, chart values like
    ``35 90.0 70 87.5``) that dilute the semantic content of the chunk and
    hurt embedding quality.  This function detects runs of 3+ consecutive
    numeric tokens and removes them, preserving word tokens (including
    mixed alphanumeric like ``GPQA``, ``AIME25``, ``2025a``) that may be
    interspersed within the noise.

    A token is considered "numeric" if it matches any of:

    - Bare integers / decimals: ``42``, ``3.14``, ``0.5``
    - Negative numbers: ``-10``, ``-0.5``
    - Percentages: ``85%``, ``-2.3%``
    - Numbers with thousands separators: ``1,000``, ``-2,500.50``
    - Scientific notation: ``1e-5``, ``3.14E+10``

    Tokens with **alphabetic** suffixes (``100ms``, ``16K``, ``2025a``) are
    preserved — they carry semantic meaning that aids retrieval.
    """
    tokens = text.split()
    if len(tokens) < 8:
        return text

    # Numeric token: optional sign, digits with optional comma separators,
    # optional decimal part, optional exponent, optional percent sign.
    _NUM_RE = re.compile(r"-?\d{1,3}(?:,\d{3})*(?:\.\d+)?(?:[eE][+-]?\d+)?%?$")

    result: list[str] = []
    num_run: list[str] = []

    for token in tokens:
        if _NUM_RE.match(token):
            num_run.append(token)
        else:
            if len(num_run) <= 2:
                result.extend(num_run)
            num_run.clear()
            result.append(token)
    if len(num_run) <= 2:
        result.extend(num_run)

    return " ".join(result)


def _strip_pdf_artifacts(text: str) -> str:
    """Remove common PDF extraction artifacts from text.

    Strips table-of-contents dotted leaders (both spaced ``. . . .`` and
    contiguous ``.......`` styles) along with their trailing page-number
    references (e.g. ``ii-2``, ``7``, ``A-3``).

    Requires 4+ contiguous dots or 2+ spaced dot-pairs to avoid matching
    ellipses (``...``) or decimal points in prose.

    Also removes numeric chart-axis noise (runs of 3+ consecutive
    purely-numeric tokens) from plot-heavy pages.
    """
    # Spaced dotted leaders with page reference: ". . . . . ii-2"
    text = re.sub(
        r"(?:\.\s){2,}\.?\s*[A-Za-z]{0,4}-?\d{1,4}",
        " ",
        text,
    )
    # Remaining spaced dotted leaders without page reference: ". . . . ."
    text = re.sub(r"(?:\.\s){2,}\.?", " ", text)
    # Contiguous dotted leaders with page reference: "........ ii-5"
    text = re.sub(
        r"\.{4,}\s*[A-Za-z]{0,4}-?\d{1,4}",
        " ",
        text,
    )
    # Remaining contiguous dotted leaders: "........"
    text = re.sub(r"\.{4,}", " ", text)
    # Remove chart axis-label / value noise
    text = _strip_chart_noise(text)
    return _collapse_whitespace(text)


_HORIZONTAL_PROXIMITY_PX = 60
_VERTICAL_PROXIMITY_PX = 120

# Tolerance (in PDF points) for matching image-block bboxes to image-region
# bboxes.  PyMuPDF may produce slightly different float coordinates for the
# same image depending on the extraction method used.
_BBOX_TOLERANCE = 1.0


def _bbox_close(a: Any, b: Any, tol: float = _BBOX_TOLERANCE) -> bool:
    """Return True if two 4-tuple bboxes are within *tol* on every component."""
    return len(a) == 4 and len(b) == 4 and all(abs(a[i] - b[i]) < tol for i in range(4))


# ---------------------------------------------------------------------------
# Heuristic filters for scientific PDF noise (references, author lists, etc.)
# ---------------------------------------------------------------------------

_REFERENCE_SECTION_HEADERS = re.compile(
    r"^\s*(?:References|Bibliography|Works?\s+Cited|Further\s+Reading)\s*:?\s*$",
    re.IGNORECASE,
)

_LINE_REF_PATTERNS = [
    # [1], [2,3], [4-6] — most common for numbered references
    re.compile(r"\[\d+(?:[,\s-]+\d+)*\]"),
    # arXiv:XXXX.XXXXX or arXiv.XXXX.XXXXX  (inline or at line start)
    # (PDF extraction often replaces the colon with a period)
    re.compile(r"arXiv[:\.,]\S+"),
    # DOI:10.xxxx/...  (inline or at line start)
    re.compile(r"DOI\s*:\s*10\.\S+", re.IGNORECASE),
    # "1.  Author, ..."  — numbered list style
    re.compile(r"^\s*\d+\.\s+[A-Z\"'`(]"),
    # Year in parentheses at end of line:  ... (2021). or ... (2023b).
    re.compile(r"\(\d{4}[a-z]?\)[\.)\s]*$"),
    # URL to known preprint servers
    re.compile(r"https?://(?:www\.)?(?:arxiv|doi|openreview)\.[a-z]+/\S+", re.IGNORECASE),
    # Initial-dot pattern common in inline references: "A. B. C. ..."
    re.compile(r"(?:[A-Z]\.\s+){3,}"),
    # Bare year at end of line (no parens):  "... 2023."  or  "... 2023b."
    re.compile(r"\b(?:19|20)\d{2}[a-z]?[\.)\s]*$"),
    # Lines starting with initials like "A. B. Some Title..."
    re.compile(r"^\s*(?:[A-Z]\.\s+){2,}[A-Z][a-z]"),
    # Common reference keywords appearing inline
    re.compile(r"\b(?:pp\.|Vol\.|No\.|arXiv|doi)\b", re.IGNORECASE),
]

# Minimum non-empty lines before the line-density heuristic is applied.
# Avoids false-positives on single-line snippets containing a URL.
_REFERENCE_MIN_LINES = 3

# Heuristic threshold: if this fraction of non-empty lines match reference
# patterns, the chunk is considered noise.
_REFERENCE_LINE_THRESHOLD = 0.60

# ---------------------------------------------------------------------------
# Table of Contents detection
# ---------------------------------------------------------------------------

_TOC_HEADER = re.compile(
    r"^\s*(?:Contents|Table\s+of\s+Contents)\s*:?\s*$",
    re.IGNORECASE,
)

# Content-based ToC/Index detection: PyMuPDF frequently flattens a whole
# ToC/Index page into one or two run-on lines, which defeats line-oriented
# heuristics (observed on real papers: "Contents 1 Introduction 4 2
# Architecture 6 2.1 Designs . . . . 7 ...").  These patterns work on the
# flattened text itself.
_TOC_HEADER_PREFIX = re.compile(r"^\s*(?:contents|table\s+of\s+contents|index)\b", re.IGNORECASE)
# Dotted leaders, with or without spaces between the dots
_TOC_DOT_RUN = re.compile(r"(?:\.\s*){3,}")
# Section references like 2.3.1 / 3.4.2.1
_TOC_SECTION_REF = re.compile(r"\b\d{1,2}(?:\.\d{1,2}){1,3}\b")
# Standalone small page numbers (ToC/Index entries end with them)
_TOC_PAGE_NUM = re.compile(r"(?:^|\s)\d{1,3}(?:\s|$)")
# Index-style entries: term, 42
_TOC_INDEX_ENTRY = re.compile(r"\b\w{3,},\s*\d{1,3}\b")
_TOC_MIN_DOT_RUNS = 4
_TOC_MIN_SECTION_REFS = 6
_TOC_MIN_PAGE_NUMS = 10
_TOC_MIN_INDEX_ENTRIES = 8

_LINE_TOC_PATTERNS = [
    # Dotted leaders followed by a page number (very distinctive of ToCs)
    # e.g., "2.1 Designs Inherited . . . . . . . . . . . . . . 7"
    re.compile(r"\.\s*\.\s*\.+\s*\d+\s*$"),
    # Section number at line start + title text + small page number at end
    # e.g., "1 Introduction 4", "5 Post-Training 28"
    re.compile(r"^\s*\d+(?:\.\d+)*\s+[A-Z].*\d{1,3}\s*$"),
]

_TOC_MIN_LINES = 3
_TOC_LINE_THRESHOLD = 0.50


def _is_reference_chunk(text: str) -> bool:
    """Return True if *text* looks like a reference list or bibliography.

    Heuristics (designed for scientific PDFs):
      1. Chunk starts with a reference section header.
      2. >60% of non-empty lines match common reference-line patterns
         (numbered citations, arXiv IDs, DOIs, trailing years in parens).
    """
    if not text:
        return False

    # Heuristic 1: the chunk itself or its first line is a section header
    first_line = text.strip().split("\n")[0].strip()
    if _REFERENCE_SECTION_HEADERS.match(first_line):
        return True

    # Heuristic 2: reference-line density
    lines = [line.strip() for line in text.split("\n") if line.strip()]

    match_count = 0
    for line in lines:
        for pat in _LINE_REF_PATTERNS:
            if pat.search(line):
                match_count += 1
                break

    if len(lines) >= _REFERENCE_MIN_LINES:
        return (match_count / len(lines)) >= _REFERENCE_LINE_THRESHOLD

    # Short chunk (fewer than _REFERENCE_MIN_LINES lines): at least 2
    # distinct pattern matches across the entire text, or every line matches.
    return match_count >= 2 or (len(lines) > 0 and match_count == len(lines))


def _is_author_list_chunk(text: str) -> bool:
    """Return True if *text* looks like a list of authors/affiliations.

    Heuristics:
      1. The chunk is short (<5 lines or <200 chars).
      2. Most lines contain comma+space separated capitalized words (names).
      3. At least one line has a superscript-style digit or asterisk
         for affiliation markers (e.g. ``John Smith¹``, ``Jane Doe²*``).
    """
    if not text:
        return False

    text = text.strip()
    # Short threshold: author lists are typically compact
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    if len(lines) > 8 or len(text) > 600:
        return False

    # Every line should be relatively short (author lists aren't paragraphs)
    if any(len(line) > 120 for line in lines):
        return False

    # Check for affiliation markers (digits, asterisks, daggers as superscript)
    has_affiliation_marker = bool(re.search(r"[¹²³⁴⁵⁶⁷⁸⁹⁰*†‡]\s*,?\s*$", text))

    # Check that most lines have comma-separated capitalized tokens (names)
    # e.g. "John Smith, Jane Doe, and Bob Johnson"
    name_like = 0
    for line in lines:
        # Must be non-empty and start with a capital letter
        if not line or not line[0].isupper():
            continue
        # Must contain commas with spaces separating capitalized words
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 2:
            # At least some parts should be capitalized name fragments
            capped = sum(1 for p in parts if p and p[0].isupper())
            if capped >= len(parts) * 0.5:
                name_like += 1
                continue
        # Also catch lines like "John Smith1, Jane Doe2, Bob Johnson3"
        # where numbers are tacked onto names (common in affiliations)
        words = re.split(r"[,\s]+", line)
        capped_words = sum(1 for w in words if w and w[0].isupper())
        if capped_words >= len([w for w in words if w]) * 0.6 and len(words) >= 3:
            name_like += 1

    return (name_like / len(lines)) >= 0.6 or (has_affiliation_marker and name_like >= 1)


# ---------------------------------------------------------------------------
# Dense author-list detection (space-separated names, e.g. large ML papers)
# ---------------------------------------------------------------------------

# Minimum token count before the density heuristic is applied.  Real
# author blocks list dozens of names; ordinary sentences are far shorter.
_DENSE_AUTHOR_MIN_TOKENS = 15

# When a chunk has no affiliation markers (*, †, ‡, superscripts), require
# at least this many tokens before accepting it as a large-collaboration
# author list.
_DENSE_AUTHOR_MIN_TOKENS_NO_MARKERS = 40

# Affiliation markers commonly attached to author names.
_AFFILIATION_MARKER_RE = re.compile(r"[¹²³⁴⁵⁶⁷⁸⁹⁰*†‡]")

# Lowercase function words that signal real prose rather than a name list.
_PROSE_STOPWORDS = re.compile(
    r"\b(?:the|and|of|in|for|with|a|an|to|is|are|was|were|on|at|by|from|"
    r"that|this|which|we|our|their|his|her|its|as|or|but|not|be|been|"
    r"has|have|had|do|does|did|will|would|can|could|shall|should)\b",
    re.IGNORECASE,
)


def _is_dense_author_list(text: str) -> bool:
    """Return True if *text* is a dense, space-separated list of author names.

    Catches paper front-matter where many authors are printed across the top
    of the first page (common in large ML papers such as DeepSeek, Llama).
    PyMuPDF extracts such blocks as one long string of names separated by
    spaces — no commas and no sentence structure — which the comma-oriented
    :func:`_is_author_list_chunk` heuristic deliberately misses (it rejects
    chunks over ~600 chars / 120 chars per line).

    Distinguishing features:

      * a high ratio of capitalized name-like tokens,
      * affiliation markers (``*``, ``†``, ``‡``, superscript digits),
      * an optional trailing bare page number (footer artifact), and
      * almost no prose stop-words.
    """
    if not text:
        return False

    text = text.strip()
    tokens = text.split()
    if len(tokens) < _DENSE_AUTHOR_MIN_TOKENS:
        return False

    # A trailing bare page number is a footer artifact ("... Xin Liu 45").
    if re.fullmatch(r"\d{1,4}", tokens[-1]):
        tokens = tokens[:-1]
    if len(tokens) < _DENSE_AUTHOR_MIN_TOKENS:
        return False

    name_like = 0
    markers = 0
    for tok in tokens:
        if _AFFILIATION_MARKER_RE.search(tok):
            markers += 1
        clean = _AFFILIATION_MARKER_RE.sub("", tok)
        # Name tokens start with an uppercase letter — covers "Hanwei",
        # "Xu", "Bao", initials "A.", and all-caps acronyms alike.
        if clean[:1].isupper():
            name_like += 1

    if name_like / len(tokens) < 0.85:
        return False

    # Require affiliation markers for medium-length lists; very long
    # lists (large collaborations) are accepted without them.
    if markers < 2 and len(tokens) < _DENSE_AUTHOR_MIN_TOKENS_NO_MARKERS:
        return False

    # Reject prose: a name list has almost no function words.
    stopword_hits = len(_PROSE_STOPWORDS.findall(text))
    return not stopword_hits > max(2, len(tokens) * 0.05)


def _is_toc_chunk(text: str) -> bool:
    """Return True if *text* looks like a table of contents."""
    if not text:
        return False

    # Heuristic 1: chunk starts with a ToC header
    first_line = text.strip().split("\n")[0].strip()
    if _TOC_HEADER.match(first_line):
        return True

    # Heuristic 2: line-density of ToC-like patterns
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    # NOTE: no early return on few lines - flattened ToC/Index extractions
    # collapse onto one or two run-on lines and must still reach the
    # content-based heuristics below.
    if len(lines) >= _TOC_MIN_LINES:
        match_count = 0
        for line in lines:
            for pat in _LINE_TOC_PATTERNS:
                if pat.search(line):
                    match_count += 1
                    break
        if (match_count / len(lines)) >= _TOC_LINE_THRESHOLD:
            return True

    # Content-based heuristics for flattened extractions (whole ToC/Index
    # collapsed into one run-on line).
    head = text.lstrip()[:64]
    if _TOC_HEADER_PREFIX.match(head):
        return True
    if len(_TOC_DOT_RUN.findall(text)) >= _TOC_MIN_DOT_RUNS:
        return True
    if (
        len(_TOC_SECTION_REF.findall(text)) >= _TOC_MIN_SECTION_REFS
        and len(_TOC_PAGE_NUM.findall(text)) >= _TOC_MIN_PAGE_NUMS
    ):
        return True
    return len(_TOC_INDEX_ENTRY.findall(text)) >= _TOC_MIN_INDEX_ENTRIES


class PDFProcessor:
    """Extract text and images from PDFs, returning images as inline base64 data URLs."""

    # Minimum dimensions for an image to be considered meaningful.
    # Filters out decorative elements (horizontal rules, dots, 1px strips)
    # that PyMuPDF extracts as images but add no semantic value.
    _MIN_IMG_WIDTH = 10
    _MIN_IMG_HEIGHT = 10
    _MIN_IMG_PIXELS = 1000  # ~32×32

    # Maximum number of images attached to a single chunk.  Prevents
    # embedding API 400 errors when a page has many small figures.
    _MAX_IMAGES_PER_CHUNK = 20

    # Maximum number of chunks on a page that an image can be attached to.
    # Spatial proximity matching can associate a large figure with every text
    # block on the page; this cap prevents embedding the same image many times.
    _MAX_IMAGE_ASSOCIATIONS = 2

    @staticmethod
    def _is_meaningful_image(width: int, height: int) -> bool:
        """Return True if an image is large enough to be a real figure/chart."""
        if width < PDFProcessor._MIN_IMG_WIDTH or height < PDFProcessor._MIN_IMG_HEIGHT:
            return False
        return not width * height < PDFProcessor._MIN_IMG_PIXELS

    @staticmethod
    def _img_to_data_url(img_bytes: bytes, ext: str) -> str:
        mime = {
            "png": "image/png",
            "jpg": "image/jpeg",
            "jpeg": "image/jpeg",
            "gif": "image/gif",
            "webp": "image/webp",
            "bmp": "image/bmp",
            "tiff": "image/tiff",
        }.get(ext, f"image/{ext}")
        b64 = base64.b64encode(img_bytes).decode("ascii")
        return f"data:{mime};base64,{b64}"

    @staticmethod
    def _ensure_valid_image(
        doc: Any,
        xref: int,
        img_bytes: bytes,
        ext: str,
        page: Any = None,
        img_ref: Any = None,
    ) -> tuple[bytes, str]:
        """Return image bytes guaranteed to be PIL-readable and non-degenerate.

        Tries PIL first.  If PIL fails or the image is degenerate (all-black
        or all-white, indicating the real content lives in an SMask / soft
        mask that raw extraction cannot composite), falls back to
        ``pymupdf.Pixmap(doc, xref)``.  If that also fails or is degenerate,
        renders the image's page region via ``page.get_pixmap(clip=...)``
        which composites image + SMask correctly.

        Parameters
        ----------
        page, img_ref:
            Required for the page-render fallback.  *img_ref* is the full
            tuple returned by ``page.get_images(full=True)`` whose ``[1]``
            element is the SMask xref and whose bbox is obtained via
            ``page.get_image_bbox(img_ref)``.
        """
        import io as _io

        from PIL import Image as _PIL

        def _is_degenerate(data: bytes) -> bool:
            """True if the image is all-black or all-white (no visible content)."""
            try:
                pil = _PIL.open(_io.BytesIO(data)).convert("RGB")
                extrema = pil.getextrema()
                # getextrema() returns [(min,max), ...] for multi-channel,
                # but (min, max) for single-channel.  Normalise to a list.
                channels: list[tuple[Any, Any]] = [extrema] if isinstance(extrema[0], (int, float)) else list(extrema)
                return all(mx == 0 for _, mx in channels) or all(mn == 255 for mn, _ in channels)
            except Exception:
                return False

        # 1. Raw extracted bytes (may be degenerate if content is in SMask)
        try:
            _PIL.open(_io.BytesIO(img_bytes))  # PIL-readable?
            if not _is_degenerate(img_bytes):
                return img_bytes, ext
            logger.debug("Image xref %d is degenerate (all-black/white), trying fallbacks", xref)
        except Exception:
            logger.debug("Suppressed exception", exc_info=True)

        # 2. pymupdf.Pixmap (handles CMYK etc. but not SMask compositing)
        try:
            import pymupdf

            pix = pymupdf.Pixmap(doc, xref)
            # CMYK (n==4) or 5+ component (CMYK+alpha) → RGB
            if pix.n >= 4 or pix.n == 2:
                pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
            png_bytes = pix.tobytes("png")
            if not _is_degenerate(png_bytes):
                return png_bytes, "png"
            logger.debug("Pixmap for xref %d also degenerate, trying page render", xref)
        except Exception as conv_exc:
            logger.warning(
                "Pixmap conversion failed for xref %d: %s — trying page render",
                xref,
                conv_exc,
            )

        # 3. Page render (composites image + SMask + color spaces correctly)
        if page is not None and img_ref is not None:
            try:
                import pymupdf

                bbox = page.get_image_bbox(img_ref)
                if bbox:
                    pix = page.get_pixmap(matrix=pymupdf.Matrix(2, 2), clip=bbox)
                    png_bytes = pix.tobytes("png")
                    if not _is_degenerate(png_bytes):
                        return png_bytes, "png"
                    logger.warning(
                        "Page render for xref %d also degenerate — using raw bytes",
                        xref,
                    )
            except Exception as render_exc:
                logger.warning("Page render failed for xref %d: %s", xref, render_exc)

        return img_bytes, ext

    @staticmethod
    def _overlap_text(text: str, num_chars: int, text_splitter=None) -> str:
        """Return overlap text (token-aware or character-based)."""
        if text_splitter is not None:
            return text_splitter.overlap_text(text)
        if num_chars <= 0 or not text:
            return ""
        if len(text) <= num_chars:
            return text
        truncated = text[-num_chars:]
        first_space = truncated.find(" ")
        if first_space != -1:
            truncated = truncated[first_space + 1 :]
        return truncated.strip()

    @staticmethod
    def _split_oversized(text: str, chunk_size: int, chunk_overlap: int, text_splitter=None) -> list[str]:
        """Split *text* into budget-sized sub-chunks (token-aware or character-based).

        When *text_splitter* is provided the token-aware ``TokenTextSplitter.split_text``
        is used (which itself avoids tiny tails via a 10 % net-new merge and a
        ``chunk_size // 4`` minimum-tail backfill).  The character-based fallback
        applies the same two rules.

        *chunk_overlap* is capped at 80 % of *chunk_size* to prevent infinite
        loops where the overlap consumes the entire chunk (start never advances).
        """
        if not text:
            return []
        if text_splitter is not None:
            return text_splitter.split_text(text)

        max_overlap = int(chunk_size * 0.8)
        if chunk_overlap > max_overlap:
            logger.warning(
                "chunk_overlap (%d) exceeds 80%% of chunk_size (%d) — capping to %d",
                chunk_overlap,
                chunk_size,
                max_overlap,
            )
            chunk_overlap = max_overlap

        min_new = max(chunk_size // 10, 1)
        min_tail = max(chunk_size // 4, 1)
        chunks: list[str] = []
        start = 0
        prev_start = 0
        prev_end = 0
        while start < len(text):
            end = min(start + chunk_size, len(text))
            if end < len(text):
                next_space = text.find(" ", end)
                if next_space != -1 and next_space - end < chunk_size // 2:
                    end = next_space

            if chunks and end >= len(text):
                new_content = len(text) - prev_end
                if new_content < min_new:
                    chunks[-1] = text[prev_start:end].strip()
                    break
                if end - start < min_tail:
                    start = max(0, end - min_tail)

            chunks.append(text[start:end].strip())
            if end >= len(text):
                break
            prev_start = start
            prev_end = end
            start = end - chunk_overlap
            start = max(start, 0)
        return [c for c in chunks if c]

    # ------------------------------------------------------------------
    # Public extraction methods
    # ------------------------------------------------------------------

    def extract_pages(self, pdf_path: str) -> list[dict[str, Any]]:
        """
        Page-level extraction.

        Returns one dict per page:
          {page_num, text, images: [{data_url, index}]}
        """
        try:
            import pymupdf
        except ImportError:
            raise ImportError("PyMuPDF is required. pip install PyMuPDF")

        doc = pymupdf.open(pdf_path)
        pages = []

        for page_num in range(len(doc)):
            page = doc[page_num]
            text = page.get_text()

            images = []
            seen_xrefs: set[int] = set()
            for img_idx, img_info in enumerate(page.get_images(full=True)):
                xref = img_info[0]
                if xref in seen_xrefs:
                    continue
                seen_xrefs.add(xref)
                try:
                    extracted = doc.extract_image(xref)
                    if not self._is_meaningful_image(extracted.get("width", 0), extracted.get("height", 0)):
                        continue
                    img_bytes, img_ext = self._ensure_valid_image(
                        doc, xref, extracted["image"], extracted["ext"], page=page, img_ref=img_info
                    )
                    images.append(
                        {
                            "data_url": self._img_to_data_url(img_bytes, img_ext),
                            "index": img_idx,
                        }
                    )
                except Exception as e:
                    logger.warning(
                        "Failed to extract image %d from page %d: %s",
                        img_idx,
                        page_num + 1,
                        e,
                    )

            pages.append(
                {
                    "page_num": page_num + 1,
                    "text": _strip_pdf_artifacts(text),
                    "images": images,
                }
            )

        doc.close()
        return pages

    def _extract_page_blocks(self, doc: Any, page: Any, page_num: int) -> list[dict[str, Any]]:
        """Extract text and image blocks for a single PDF page.

        Returns a list of block dicts with ``page_num``, ``block_num``,
        ``text``, ``block_type``, ``bbox``, and image info.
        """
        page_blocks = page.get_text("dict")["blocks"]

        # Extract all images on this page first
        image_regions = []
        for img_idx, img_ref in enumerate(page.get_images(full=True)):
            xref = img_ref[0]
            try:
                extracted = doc.extract_image(xref)
                if not self._is_meaningful_image(extracted.get("width", 0), extracted.get("height", 0)):
                    continue
                bbox = page.get_image_bbox(img_ref)
                img_bytes, img_ext = self._ensure_valid_image(
                    doc, xref, extracted["image"], extracted["ext"], page=page, img_ref=img_ref
                )
                image_regions.append(
                    {
                        "bbox": (bbox.x0, bbox.y0, bbox.x1, bbox.y1) if bbox else None,
                        "data_url": self._img_to_data_url(img_bytes, img_ext),
                    }
                )
            except Exception as e:
                logger.warning(
                    "Failed to extract image %d from page %d: %s",
                    img_idx,
                    page_num,
                    e,
                )

        blocks = []
        for b_idx, b in enumerate(page_blocks):
            bbox = b.get("bbox", (0, 0, 0, 0))

            if b.get("type") == 0:  # text block
                block_text = ""
                for line in b.get("lines", []):
                    for span in line.get("spans", []):
                        block_text += span.get("text", "") + " "
                block_text = _strip_pdf_artifacts(block_text)
                if not block_text:
                    continue

                nearby = []
                for ir in image_regions:
                    if ir["bbox"]:
                        ir_bbox = ir["bbox"]
                        # vertical: overlap OR close proximity (captions below figures)
                        vertical_near = (
                            max(bbox[1], ir_bbox[1]) < min(bbox[3], ir_bbox[3])
                            or abs(bbox[3] - ir_bbox[1]) < _VERTICAL_PROXIMITY_PX
                            or abs(ir_bbox[3] - bbox[1]) < _VERTICAL_PROXIMITY_PX
                        )
                        # horizontal: overlap OR close proximity
                        horizontal_near = (
                            max(bbox[0], ir_bbox[0]) < min(bbox[2], ir_bbox[2])
                            or abs(bbox[0] - ir_bbox[2]) < _HORIZONTAL_PROXIMITY_PX
                            or abs(ir_bbox[0] - bbox[2]) < _HORIZONTAL_PROXIMITY_PX
                        )
                        if vertical_near and horizontal_near:
                            nearby.append(ir["data_url"])

                blocks.append(
                    {
                        "page_num": page_num,
                        "block_num": b_idx,
                        "text": block_text,
                        "block_type": "text",
                        "bbox": bbox,
                        "nearby_images": nearby,
                    }
                )

            elif b.get("type") == 1:  # image block
                for ir in image_regions:
                    if ir["bbox"] and _bbox_close(ir["bbox"], bbox):
                        blocks.append(
                            {
                                "page_num": page_num,
                                "block_num": b_idx,
                                "text": "",
                                "block_type": "image",
                                "bbox": bbox,
                                "image_data_url": ir["data_url"],
                            }
                        )

        # Emit standalone image entries for image_regions that didn't match
        # any dict-block (page.get_images() can report more images than
        # page.get_text("dict")["blocks"] — e.g. inline images, form XObjects).
        # Without this, such images are silently dropped if no text block is
        # nearby.
        matched_urls = {b["image_data_url"] for b in blocks if b["block_type"] == "image"}
        for ir in image_regions:
            if ir["data_url"] not in matched_urls:
                blocks.append(
                    {
                        "page_num": page_num,
                        "block_num": -1,
                        "text": "",
                        "block_type": "image",
                        "bbox": ir["bbox"],
                        "image_data_url": ir["data_url"],
                    }
                )

        return blocks

    def extract_text_blocks(self, pdf_path: str) -> list[dict[str, Any]]:
        """
        Block-level extraction with spatial proximity detection.

        Returns a list of blocks, each with:
          page_num, block_num, text, block_type ('text'|'image'),
          bbox, nearby_images (list of data URLs)
        """
        try:
            import pymupdf
        except ImportError:
            raise ImportError("PyMuPDF is required. pip install PyMuPDF")

        doc = pymupdf.open(pdf_path)
        blocks = []

        for page_num in range(len(doc)):
            page = doc[page_num]
            blocks.extend(self._extract_page_blocks(doc, page, page_num + 1))

        doc.close()
        return blocks

    def extract_structured_pages(self, pdf_path: str) -> list[dict[str, Any]]:
        """
        RAG-ready extraction (block level).

        Produces entries suitable for the multimodal embedding model:
          {'text': '...', 'image': 'data:image/png;base64,...', 'source': '...', 'page': N}

        Each text block becomes one entry, with its nearby images attached
        as ``image`` (a list of data URLs for multiple images).
        Standalone image blocks produce entries with ``[Image on page N]`` as text.
        """
        blocks = self.extract_text_blocks(pdf_path)
        pages: dict[int, list[dict[str, Any]]] = {}

        for block in blocks:
            pn = block["page_num"]
            if pn not in pages:
                pages[pn] = []

            if block["block_type"] == "image" and block.get("image_data_url"):
                pages[pn].append(
                    {
                        "text": f"[Image on page {pn}]",
                        "image": block["image_data_url"],
                        "source": pdf_path,
                        "page": pn,
                    }
                )

            elif block["block_type"] == "text" and block["text"]:
                entry: dict[str, Any] = {
                    "text": block["text"],
                    "source": pdf_path,
                    "page": pn,
                }
                nearby = block.get("nearby_images", [])
                if nearby:
                    entry["image"] = nearby
                pages[pn].append(entry)

        result = []
        for pn in sorted(pages):
            result.extend(pages[pn])
        return result

    def _ocr_page_raster(self, page: Any) -> str | None:
        """Rasterize one PDF page and OCR it; ``None`` on any failure."""
        try:
            pix = page.get_pixmap(dpi=_OCR_DPI)
            png = pix.tobytes("png")
        except Exception:
            logger.debug("page rasterization for OCR failed", exc_info=True)
            return None
        return _ocr_png(png)

    def extract_chunks(
        self,
        pdf_path: str,
        chunk_size: int = 8192,
        chunk_overlap: int = 512,
        text_splitter=None,
        ocr: bool | None = None,
    ) -> list[dict[str, Any]]:
        """
        Split a PDF into multimodal chunks of roughly *chunk_size* characters
        or tokens (when *text_splitter* is provided).

        Consecutive text blocks are merged until the budget is reached, then
        a new chunk begins.  The last ~*chunk_overlap* characters/tokens of
        the previous chunk carry over so that no context is lost at boundaries.

        Images spatially near each chunk's text are attached as a list under the
        ``image`` key so that charts, figures and diagrams
        stay paired with their surrounding text.

        This is a convenience wrapper around :meth:`extract_chunks_iter` that
        materialises the full list.  Use the iterator directly for large PDFs
        to start embedding before extraction finishes.

        Parameters
        ----------
        pdf_path:
            Path to the PDF file.
        chunk_size:
            Target character or token count per chunk.
        chunk_overlap:
            Number of overlap characters/tokens between consecutive chunks.
        text_splitter:
            Optional ``TokenTextSplitter``.  When set, all size comparisons
            use token counts instead of character counts.
        """
        return list(
            self.extract_chunks_iter(
                pdf_path,
                chunk_size=chunk_size,
                chunk_overlap=chunk_overlap,
                text_splitter=text_splitter,
                ocr=ocr,
            )
        )

    def extract_chunks_iter(
        self,
        pdf_path: str,
        chunk_size: int = 8192,
        chunk_overlap: int = 512,
        text_splitter=None,
        ocr: bool | None = None,
    ) -> Generator[dict[str, Any], None, None]:
        """
        Generator version of :meth:`extract_chunks`.

        Opens the PDF once and yields chunks page-by-page.  Overlap state
        (``current_text`` / ``current_images``) carries between pages so
        cross-page chunking works identically to the list version.

        This allows callers to start processing (e.g. embedding) the first
        chunks while later pages are still being extracted.

        *ocr*: OCR fallback for scanned pages (roadmap feature 3).  ``None``
        resolves to "on when the tesseract binary is present"; ``False``
        disables it; ``True`` forces the attempt.  A page qualifies when it
        carries images but its text layer is empty — the raster is OCR'd and
        the recognised text becomes the page's text blocks (tagged
        ``[OCR page N]``).
        """
        try:
            import pymupdf
        except ImportError:
            raise ImportError("PyMuPDF is required. pip install PyMuPDF")

        if ocr is None:
            ocr_enabled = _tesseract_available()
        else:
            ocr_enabled = bool(ocr) and _tesseract_available()

        def _exceeds_budget(text: str) -> bool:
            if text_splitter is not None:
                return text_splitter.count_tokens(text) > chunk_size
            return len(text) > chunk_size

        def _combined_budget(a: str, b: str) -> bool:
            if text_splitter is not None:
                combined = a + " " + b if a and b else a or b
                return text_splitter.count_tokens(combined) > chunk_size
            return len(a) + len(b) + 1 > chunk_size

        doc = pymupdf.open(pdf_path)

        # Overlap state carries across pages
        current_text = ""
        current_images: list[str] = []

        for page_num in range(len(doc)):
            page = doc[page_num]
            pn = page_num + 1
            page_blocks = self._extract_page_blocks(doc, page, pn)

            # Separate text & image blocks, sort top→bottom by bbox y1
            text_blocks = sorted(
                [b for b in page_blocks if b["block_type"] == "text" and b["text"]],
                key=lambda b: b["bbox"][1],
            )
            image_blocks = [b for b in page_blocks if b["block_type"] == "image" and b.get("image_data_url")]

            # OCR fallback: an image-only page (no text layer — typically a
            # scan) gets its raster OCR'd into a synthetic text block so the
            # normal chunking path picks the recognised text up.
            if ocr_enabled and not text_blocks and image_blocks:
                ocr_text = self._ocr_page_raster(page)
                if ocr_text:
                    logger.debug("OCR page %d of %s: %d chars", pn, pdf_path, len(ocr_text))
                    text_blocks = [
                        {
                            "block_num": 0,
                            "text": f"[OCR page {pn}] {ocr_text}",
                            "block_type": "text",
                            "bbox": (0.0, 0.0, float(page.rect.width), float(page.rect.height)),
                            "nearby_images": [],
                        }
                    ]

            if not text_blocks:
                # Flush overlap text from the previous page before yielding
                # image-only entries — otherwise cross-page carry is lost.
                if current_text.strip():
                    flushed_text = current_text.strip()
                    current_text = ""
                    flushed_images = list(current_images)
                    current_images = []
                    if not (
                        _is_reference_chunk(flushed_text)
                        or _is_author_list_chunk(flushed_text)
                        or _is_dense_author_list(flushed_text)
                        or _is_toc_chunk(flushed_text)
                    ):
                        seen_f: set[str] = set()
                        ordered_f: list[str] = []
                        for url in flushed_images:
                            if url not in seen_f:
                                seen_f.add(url)
                                ordered_f.append(url)
                        if len(ordered_f) > self._MAX_IMAGES_PER_CHUNK:
                            ordered_f = ordered_f[: self._MAX_IMAGES_PER_CHUNK]
                        entry_f: dict[str, Any] = {
                            "text": flushed_text,
                            "source": pdf_path,
                            "page": pn,
                        }
                        if ordered_f:
                            entry_f["image"] = ordered_f
                        yield entry_f

                for ib in image_blocks:
                    yield {
                        "text": f"[Image on page {pn}]",
                        "image": ib["image_data_url"],
                        "source": pdf_path,
                        "page": pn,
                    }
                continue

            # -- Build text chunks from consecutive text blocks ----------------
            raw_chunks: list[dict[str, Any]] = []

            for tb in text_blocks:
                tb_text = tb["text"]
                tb_images = tb.get("nearby_images", [])

                # Single block exceeds budget — split it into sub-chunks
                if _exceeds_budget(tb_text):
                    if current_text.strip():
                        raw_chunks.append({"text": current_text.strip(), "images": current_images})
                    carry_text = (
                        self._overlap_text(current_text, chunk_overlap, text_splitter=text_splitter)
                        if current_text
                        else ""
                    )
                    sub_chunks = self._split_oversized(tb_text, chunk_size, chunk_overlap, text_splitter=text_splitter)
                    for i, sub in enumerate(sub_chunks):
                        if i == 0 and carry_text:
                            sub = carry_text + " " + sub
                        raw_chunks.append({"text": sub.strip(), "images": list(tb_images)})
                    current_text = ""
                    current_images = []
                    continue

                if not current_text:
                    current_text = tb_text
                    current_images = list(tb_images)
                elif _combined_budget(current_text, tb_text):
                    raw_chunks.append({"text": current_text.strip(), "images": current_images})
                    carry = self._overlap_text(current_text, chunk_overlap, text_splitter=text_splitter)
                    current_text = (carry + " " + tb_text) if carry else tb_text
                    current_images = list(tb_images)
                else:
                    current_text += " " + tb_text
                    current_images.extend(tb_images)

            if current_text.strip():
                raw_chunks.append({"text": current_text.strip(), "images": current_images})
                # Carry overlap TEXT to the next page for context continuity,
                # but DROP accumulated images — they belong to the page just
                # emitted.  Without this, images accumulate across every page
                # the overlap text touches and get mislabelled with the wrong
                # page number.
                current_text = ""
                current_images = []

            # -- Emit each chunk with deduplicated images --------------------
            emitted_images: set[str] = set()
            # Track how many chunks each image has been attached to on this
            # page.  Cap at _MAX_IMAGE_ASSOCIATIONS to avoid embedding the
            # same image many times (proximity matching can associate a large
            # figure with every text block on the page).
            image_assoc_count: dict[str, int] = {}
            for ch in raw_chunks:
                # Skip noise chunks: reference lists, author lists, and tables of contents
                if (
                    _is_reference_chunk(ch["text"])
                    or _is_author_list_chunk(ch["text"])
                    or _is_dense_author_list(ch["text"])
                    or _is_toc_chunk(ch["text"])
                ):
                    continue

                seen: set[str] = set()
                ordered: list[str] = []
                for url in ch["images"]:
                    if url not in seen:
                        seen.add(url)
                        if image_assoc_count.get(url, 0) < self._MAX_IMAGE_ASSOCIATIONS:
                            ordered.append(url)
                            image_assoc_count[url] = image_assoc_count.get(url, 0) + 1

                # Cap images per chunk to avoid embedding API limits
                if len(ordered) > self._MAX_IMAGES_PER_CHUNK:
                    ordered = ordered[: self._MAX_IMAGES_PER_CHUNK]

                emitted_images.update(ordered)

                # Re-run chart-noise filter on the merged chunk text.
                # _strip_chart_noise runs per text-block during extraction,
                # but numeric axis-label noise can span multiple small blocks
                # that individually pass the <8-token guard.  When those
                # blocks are merged into a chunk, the noise is reassembled.
                chunk_text = _strip_chart_noise(ch["text"])

                entry: dict[str, Any] = {
                    "text": chunk_text,
                    "source": pdf_path,
                    "page": pn,
                }
                if ordered:
                    entry["image"] = ordered
                yield entry

            # -- Standalone images not already emitted in a chunk -------------
            for ib in image_blocks:
                if ib["image_data_url"] not in emitted_images:
                    yield {
                        "text": f"[Image on page {pn}]",
                        "image": ib["image_data_url"],
                        "source": pdf_path,
                        "page": pn,
                    }

        doc.close()
