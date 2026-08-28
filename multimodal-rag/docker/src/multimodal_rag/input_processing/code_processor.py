import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from multimodal_rag.utils.logging_utils import logging

logger = logging.getLogger(__name__)

# Language → (extensions, patterns for top-level definitions)
_LANGUAGE_PATTERNS: dict[str, dict[str, Any]] = {
    "python": {
        "extensions": {".py", ".pyw"},
        "patterns": [
            re.compile(r"^(async\s+)?def\s+\w+\s*\("),
            re.compile(r"^class\s+\w+"),
            re.compile(r"^@\w+"),  # decorator
        ],
    },
    "javascript": {
        "extensions": {".js", ".jsx", ".mjs", ".cjs"},
        "patterns": [
            re.compile(r"^(async\s+)?function\s+\w+\s*\("),
            re.compile(r"^class\s+\w+"),
            re.compile(r"^(export\s+)?(const|let|var)\s+\w+\s*=\s*(async\s+)?\(.*\)\s*=>"),
            re.compile(r"^(export\s+)?(const|let|var)\s+\w+\s*=\s*(async\s+)?function"),
        ],
    },
    "typescript": {
        "extensions": {".ts", ".tsx"},
        "patterns": [
            re.compile(r"^(async\s+)?function\s+\w+\s*\("),
            re.compile(r"^class\s+\w+"),
            re.compile(r"^(export\s+)?(const|let|var)\s+\w+\s*=\s*(async\s+)?\(.*\)\s*=>"),
            re.compile(r"^(export\s+)?interface\s+\w+"),
            re.compile(r"^(export\s+)?type\s+\w+\s*="),
        ],
    },
    "java": {
        "extensions": {".java"},
        "patterns": [
            re.compile(r"^\s*(public|private|protected|static)?\s*(class|interface|enum|record)\s+\w+"),
            re.compile(r"^\s*(public|private|protected|static)?\s+\w[\w<>[\],\s]*\s+\w+\s*\("),
        ],
    },
    "cpp": {
        "extensions": {".cpp", ".cxx", ".cc", ".h", ".hpp", ".hxx"},
        "patterns": [
            re.compile(r"^\s*(class|struct|namespace|union)\s+\w+"),
            re.compile(
                r"^\s*(virtual\s+)?(void|int|bool|char|float|double|long|short|unsigned|signed|std::\w+)\s+\w+\s*\("
            ),
            re.compile(r"^\s*template\s*<"),
        ],
    },
    "c": {
        "extensions": {".c", ".h"},
        "patterns": [
            re.compile(r"^\s*(void|int|bool|char|float|double|long|short|unsigned|signed|static|struct)\s+\w+\s*\("),
        ],
    },
    "csharp": {
        "extensions": {".cs"},
        "patterns": [
            re.compile(r"^\s*(public|private|protected|internal)?\s*(class|struct|interface|enum|record)\s+\w+"),
            re.compile(r"^\s*(public|private|protected|internal)?\s+\w[\w<>,\[\]]*\s+\w+\s*\("),
        ],
    },
    "go": {
        "extensions": {".go"},
        "patterns": [
            re.compile(r"^func\s+\w+\s*\("),
            re.compile(r"^type\s+\w+\s+(struct|interface)\s*"),
        ],
    },
    "rust": {
        "extensions": {".rs"},
        "patterns": [
            re.compile(r"^fn\s+\w+\s*\("),
            re.compile(r"^(pub\s+)?(struct|enum|trait|impl|mod|union)\s+\w+"),
        ],
    },
    "ruby": {
        "extensions": {".rb"},
        "patterns": [
            re.compile(r"^(def\s+\w+)"),
            re.compile(r"^class\s+\w+"),
            re.compile(r"^module\s+\w+"),
        ],
    },
    "swift": {
        "extensions": {".swift"},
        "patterns": [
            re.compile(r"^func\s+\w+\s*\("),
            re.compile(r"^class\s+\w+"),
            re.compile(r"^(public|private|internal)?\s*(struct|enum|protocol|extension)\s+\w+"),
        ],
    },
    "php": {
        "extensions": {".php"},
        "patterns": [
            re.compile(r"^function\s+\w+\s*\("),
            re.compile(r"^class\s+\w+"),
            re.compile(r"^(public|private|protected)?\s+function\s+\w+\s*\("),
        ],
    },
    "kotlin": {
        "extensions": {".kt", ".kts"},
        "patterns": [
            re.compile(r"^fun\s+\w+\s*\("),
            re.compile(r"^class\s+\w+"),
            re.compile(r"^(data|sealed|open|abstract)?\s*class\s+\w+"),
            re.compile(r"^interface\s+\w+"),
        ],
    },
    "scala": {
        "extensions": {".scala"},
        "patterns": [
            re.compile(r"^def\s+\w+\s*\("),
            re.compile(r"^class\s+\w+"),
            re.compile(r"^(object|trait|case class|enum)\s+\w+"),
        ],
    },
    "shell": {
        "extensions": {".sh", ".bash", ".zsh", ".fish"},
        "patterns": [
            re.compile(r"^function\s+\w+\s*\(\)"),
            re.compile(r"^\w+\s*\(\s*\)\s*\{"),
        ],
    },
    "r": {
        "extensions": {".r", ".R"},
        "patterns": [
            re.compile(r"^\w+\s*<-\s*function\s*\("),
        ],
    },
}

# Fallback for other languages — just detect any line that looks like a definition
_FALLBACK_PATTERNS = [
    re.compile(r"^(function|def|fun|func|fn|sub)\s+\w+"),
    re.compile(r"^(class|struct|trait|interface|module|namespace)\s+\w+"),
]


def _detect_language(path: str) -> str | None:
    ext = Path(path).suffix.lower()
    for lang, info in _LANGUAGE_PATTERNS.items():
        if ext in info["extensions"]:
            return lang
    return None


@dataclass
class CodeProcessor:
    """Split source code into chunks respecting function/class boundaries.

    Detects the programming language by file extension and uses
    language-specific patterns to find top-level definitions so that
    chunks never split mid-function or mid-class.

    Parameters
    ----------
    chunk_size:
        Target number of characters per chunk.
    chunk_overlap:
        Number of overlap characters between consecutive chunks.
    add_language_annotation:
        Whether to prepend ``[Language: python]`` to each chunk.
    """

    chunk_size: int = 8192
    chunk_overlap: int = 512
    add_language_annotation: bool = True
    text_splitter: Any | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def process(self, code_path: str) -> list[dict[str, Any]]:
        content = Path(code_path).read_text(encoding="utf-8")
        source = str(code_path)
        return self.process_code(content, source)

    def process_code(self, content: str, source: str = "") -> list[dict[str, Any]]:
        lang = _detect_language(source) or "unknown"
        patterns = self._get_patterns(lang)
        sections = self._split_by_definitions(content, patterns)
        return self._build_chunks(sections, source, lang)

    # ------------------------------------------------------------------
    # Language detection
    # ------------------------------------------------------------------

    @staticmethod
    def _get_patterns(lang: str) -> list[re.Pattern]:
        if lang in _LANGUAGE_PATTERNS:
            return _LANGUAGE_PATTERNS[lang]["patterns"]
        return _FALLBACK_PATTERNS

    # ------------------------------------------------------------------
    # Section splitting
    # ------------------------------------------------------------------

    def _split_by_definitions(self, content: str, patterns: list[re.Pattern]) -> list[dict[str, Any]]:
        """Split code into sections at top-level definition boundaries."""
        lines = content.split("\n")
        sections: list[dict[str, Any]] = []
        current: list[str] = []
        current_def = ""

        for line in lines:
            stripped = line.strip()

            # Check if this line is a top-level definition
            is_definition = False
            if stripped and not stripped.startswith(("#", "//", "/*", "*", "<!--")):
                for pat in patterns:
                    if pat.match(stripped):
                        is_definition = True
                        break

            if is_definition:
                if current:
                    sections.append({"text": "\n".join(current), "definition": current_def})
                current = [line]
                current_def = stripped
            else:
                current.append(line)

        if current:
            sections.append({"text": "\n".join(current), "definition": current_def})

        return sections

    # ------------------------------------------------------------------
    # Budget helpers
    # ------------------------------------------------------------------

    def _exceeds_budget(self, text: str) -> bool:
        if self.text_splitter is not None:
            return self.text_splitter.count_tokens(text) > self.chunk_size
        return len(text) > self.chunk_size

    def _exceeds_combined_budget(self, a: str, b: str) -> bool:
        if self.text_splitter is not None:
            combined = a + "\n" + b if a and b else a or b
            return self.text_splitter.count_tokens(combined) > self.chunk_size
        return len(a) + len(b) + 1 > self.chunk_size

    def _overlap_text(self, text: str) -> str:
        if self.text_splitter is not None:
            return self.text_splitter.overlap_text(text)
        lines = text.split("\n")
        num_lines = max(1, self.chunk_overlap // 40)
        return "\n".join(lines[-num_lines:]) if lines else ""

    # ------------------------------------------------------------------
    # Chunking
    # ------------------------------------------------------------------

    def _build_chunks(self, sections: list[dict[str, Any]], source: str, lang: str) -> list[dict[str, Any]]:
        if not sections:
            return []

        chunks: list[dict[str, Any]] = []
        buffer = ""
        buffer_defs: list[str] = []

        def flush():
            nonlocal buffer, buffer_defs
            if buffer.strip():
                text = buffer.strip()
                if self.add_language_annotation:
                    text = f"[Language: {lang}]\n{text}"
                chunks.append({"text": text, "source": source})
            buffer = ""
            buffer_defs = []

        for sec in sections:
            sec_text = sec["text"]
            sec_def = sec.get("definition", "")

            if self._exceeds_budget(sec_text):
                flush()
                sub_chunks = self._split_oversized(sec_text)
                for sc in sub_chunks:
                    if sc.strip():
                        text = sc.strip()
                        if self.add_language_annotation:
                            text = f"[Language: {lang}]\n{text}"
                        chunks.append({"text": text, "source": source})
                continue

            if not buffer:
                buffer = sec_text
                buffer_defs = [sec_def] if sec_def else []
            elif self._exceeds_combined_budget(buffer, sec_text):
                flush()
                buffer = sec_text
                buffer_defs = [sec_def] if sec_def else []
            else:
                buffer += "\n" + sec_text
                if sec_def:
                    buffer_defs.append(sec_def)

        flush()
        return chunks

    def _split_oversized(self, text: str) -> list[str]:
        if self.text_splitter is not None:
            return self.text_splitter.split_text(text)
        lines = text.split("\n")
        chunks: list[str] = []
        current = ""
        for line in lines:
            if not current:
                current = line
            elif len(current) + len(line) + 1 > self.chunk_size:
                chunks.append(current.strip())
                # Carry forward trailing lines from the current chunk
                # that fit within chunk_overlap characters as overlap.
                current_lines = current.split("\n")
                overlap: list[str] = []
                overlap_len = 0
                for ol in reversed(current_lines):
                    if overlap_len + len(ol) + 1 > self.chunk_overlap:
                        break
                    overlap.insert(0, ol)
                    overlap_len += len(ol) + 1
                overlap.append(line)
                current = "\n".join(overlap)
            else:
                current += "\n" + line
        if current.strip():
            chunks.append(current.strip())
        return chunks
