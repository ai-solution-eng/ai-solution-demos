import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from multimodal_rag.utils.logging_utils import logging

logger = logging.getLogger(__name__)

# Regex for common structured log prefixes
_SYSLOG_RE = re.compile(r"^(\w{3}\s+\d+\s+\d{2}:\d{2}:\d{2})\s+(\S+)\s+(\S+)\s*(\S*)?\s*:?\s*(.*)")
_TIMESTAMP_RE = re.compile(r"^\d{4}[-/]\d{2}[-/]\d{2}[T ]\d{2}:\d{2}:\d{2}")
_SEVERITY_RE = re.compile(r"\b(TRACE|DEBUG|INFO|WARN(?:ING)?|ERROR|FATAL|CRITICAL)\b", re.IGNORECASE)
_JSON_LINE_RE = re.compile(r"^\s*\{")


@dataclass
class LogProcessor:
    """Parse log files into timestamp/severity-tagged chunks.

    Handles common log formats including syslog, ISO-8601 timestamped
    logs, severity-prefixed entries, and JSON-line logs.

    Parameters
    ----------
    chunk_size:
        Target characters per chunk.  Log entries are grouped into
        chunks that stay under this limit.
    chunk_overlap:
        Number of overlap *entries* between consecutive chunks.
    max_entries_per_chunk:
        Maximum number of log entries per chunk.  ``0`` means no limit
        (use *chunk_size* character budget instead).
    """

    chunk_size: int = 8192
    chunk_overlap: int = 0
    text_splitter: Any | None = None
    max_entries_per_chunk: int = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def process(self, log_path: str) -> list[dict[str, Any]]:
        content = Path(log_path).read_text(encoding="utf-8", errors="replace")
        source = str(log_path)
        return self.process_text(content, source)

    def process_text(self, content: str, source: str = "") -> list[dict[str, Any]]:
        entries = self._parse_entries(content)
        if not entries:
            return []
        return self._build_chunks(entries, source)

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    def _parse_entries(self, content: str) -> list[dict[str, Any]]:
        lines = content.split("\n")

        # Heuristic: if most lines start with JSON, parse as JSON-lines
        json_count = sum(1 for line in lines[:50] if _JSON_LINE_RE.match(line))
        if json_count > len(lines[:50]) * 0.5 and len(lines) > 5:
            return self._parse_json_lines(lines)

        entries: list[dict[str, Any]] = []
        current: list[str] = []

        for line in lines:
            if self._is_new_entry(line):
                if current:
                    entry = self._build_entry("\n".join(current))
                    if entry:
                        entries.append(entry)
                current = [line]
            else:
                current.append(line)

        if current:
            entry = self._build_entry("\n".join(current))
            if entry:
                entries.append(entry)

        return entries

    @staticmethod
    def _is_new_entry(line: str) -> bool:
        if not line.strip():
            return False
        # Check for common log entry start patterns
        if _SYSLOG_RE.match(line):
            return True
        if _TIMESTAMP_RE.match(line):
            return True
        return bool(_SEVERITY_RE.match(line))

    @staticmethod
    def _build_entry(text: str) -> dict[str, Any]:
        entry: dict[str, Any] = {"text": text.strip()}

        # Extract timestamp (best effort)
        ts_match = _TIMESTAMP_RE.search(text)
        if ts_match:
            entry["timestamp"] = ts_match.group(0)
        else:
            syslog_match = _SYSLOG_RE.match(text.strip())
            if syslog_match:
                entry["timestamp"] = syslog_match.group(1)

        # Extract severity
        sev_match = _SEVERITY_RE.search(text)
        if sev_match:
            sev = sev_match.group(1).upper()
            # Normalise
            sev_normalized = {
                "TRACE": "TRACE",
                "DEBUG": "DEBUG",
                "INFO": "INFO",
                "WARN": "WARN",
                "WARNING": "WARN",
                "ERROR": "ERROR",
                "FATAL": "FATAL",
                "CRITICAL": "FATAL",
            }.get(sev, sev)
            entry["severity"] = sev_normalized

        return entry

    @staticmethod
    def _parse_json_lines(lines: list[str]) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        import json

        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue

            # JSON arrays or scalars — stringify directly (no .items())
            if not isinstance(obj, dict):
                entries.append({"text": json.dumps(obj, ensure_ascii=False)})
                continue

            # Convert JSON object to flattened text
            text_parts: list[str] = []
            for k, v in obj.items():
                if k.lower() in ("message", "msg", "event"):
                    text_parts.append(str(v))
                elif k.lower() in ("timestamp", "time", "ts", "@timestamp"):
                    pass  # handled as metadata
                else:
                    text_parts.append(f"{k}: {v}")

            entry: dict[str, Any] = {
                "text": " | ".join(text_parts) if text_parts else json.dumps(obj, ensure_ascii=False),
            }

            # Extract timestamp
            for ts_key in ("timestamp", "time", "ts", "@timestamp"):
                if ts_key in obj:
                    entry["timestamp"] = str(obj[ts_key])
                    break

            # Extract severity
            for sev_key in ("severity", "level", "loglevel", "log_level", "log.level"):
                if sev_key in obj:
                    entry["severity"] = str(obj[sev_key]).upper()
                    break

            entries.append(entry)

        return entries

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
            combined = a + "\n" + b if a and b else a or b
            return self.text_splitter.count_tokens(combined) > self.chunk_size
        return len(a) + len(b) + 1 > self.chunk_size

    def _build_chunks(self, entries: list[dict[str, Any]], source: str) -> list[dict[str, Any]]:
        docs: list[dict[str, Any]] = []

        if self.max_entries_per_chunk > 0:
            for start in range(0, len(entries), self.max_entries_per_chunk):
                end = min(start + self.max_entries_per_chunk, len(entries))
                docs.append(self._merge_entries(entries[start:end], source))
        else:
            buffer: list[dict[str, Any]] = []
            buffer_size = 0

            for entry in entries:
                entry_size = len(entry.get("text", ""))

                if not buffer:
                    buffer.append(entry)
                    buffer_size = entry_size
                elif self._exceeds_budget("\n".join(e.get("text", "") for e in buffer) + "\n" + entry.get("text", "")):
                    docs.append(self._merge_entries(buffer, source))
                    if self.chunk_overlap > 0:
                        overlap_idx = max(0, len(buffer) - self.chunk_overlap)
                        buffer = buffer[overlap_idx:]
                        buffer_size = sum(len(e.get("text", "")) for e in buffer)
                    else:
                        buffer = []
                        buffer_size = 0
                    buffer.append(entry)
                    buffer_size += entry_size
                else:
                    buffer.append(entry)
                    buffer_size += entry_size

            if buffer:
                docs.append(self._merge_entries(buffer, source))

        for i, doc in enumerate(docs):
            doc["chunk_index"] = i

        logger.info("Built %d chunk(s) from %d log entries", len(docs), len(entries))
        return docs

    @staticmethod
    def _merge_entries(entries: list[dict[str, Any]], source: str) -> dict[str, Any]:
        texts: list[str] = []
        timestamps: list[str] = []
        severities: set[str] = set()

        for e in entries:
            texts.append(e.get("text", ""))
            if "timestamp" in e:
                timestamps.append(e["timestamp"])
            if "severity" in e:
                severities.add(e["severity"])

        result: dict[str, Any] = {
            "text": "\n".join(texts),
            "source": source,
        }

        if timestamps:
            result["timestamp_start"] = timestamps[0]
            result["timestamp_end"] = timestamps[-1]
        if severities:
            result["severities"] = sorted(severities)

        return result
