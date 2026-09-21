import csv
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from multimodal_rag.utils.logging_utils import logging

logger = logging.getLogger(__name__)

_SUPPORTED_TABLE_EXTS = frozenset({".csv", ".tsv", ".xlsx", ".xls", ".ods"})


def _rows_to_json_text(rows: list[dict[str, Any]], row_index: int) -> str:
    """Serialise a single table row as a compact JSON string."""
    return json.dumps(rows[row_index], ensure_ascii=False, default=str)


@dataclass
class TableProcessor:
    """Parse tabular data (CSV, Excel, ODS) into per-row document dicts.

    Each row is serialised as a JSON string so it pairs naturally with
    :class:`JSONProcessor` for downstream flattening if desired.

    Parameters
    ----------
    chunk_size:
        Maximum characters per row-group chunk.  Rows are grouped into
        chunks that stay under this limit so that very large tables
        produce a manageable number of documents.
    chunk_overlap:
        Number of overlap rows between consecutive row-group chunks.
    rows_per_doc:
        Number of rows to bundle into a single document.  Overrides
        *chunk_size* when set to a positive value.  ``0`` means use
        *chunk_size* for the character budget.
    """

    chunk_size: int = 8192
    chunk_overlap: int = 0
    text_splitter: Any | None = None
    rows_per_doc: int = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def process(self, table_path: str) -> list[dict[str, Any]]:
        """Read a CSV / Excel / ODS file and return per-row document dicts.

        Returns
        -------
        list[dict]
            ``[{"text": "<row as JSON>", "source": …, "row_index": N}, …]``
        """
        ext = Path(table_path).suffix.lower()

        if ext == ".csv":
            rows = self._read_csv(table_path)
        elif ext == ".tsv":
            rows = self._read_csv(table_path, delimiter="\t")
        elif ext in (".xlsx", ".xls"):
            rows = self._read_excel(table_path)
        elif ext == ".ods":
            rows = self._read_ods(table_path)
        else:
            raise ValueError(f"Unsupported table format: {ext}")

        if not rows:
            logger.warning("No rows found in %s", table_path)
            return []

        source = str(table_path)
        return self._build_docs(rows, source)

    def process_data(
        self,
        rows: list[dict[str, Any]],
        source: str = "",
    ) -> list[dict[str, Any]]:
        """Process an already-parsed list of row dicts into documents."""
        return self._build_docs(rows, source)

    # ------------------------------------------------------------------
    # Readers
    # ------------------------------------------------------------------

    @staticmethod
    def _read_csv(path: str, delimiter: str = ",") -> list[dict[str, Any]]:
        with open(path, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f, delimiter=delimiter)
            rows: list[dict[str, Any]] = []
            for row in reader:
                cleaned = {k.strip(): v.strip() if isinstance(v, str) else v for k, v in row.items()}
                rows.append(cleaned)
        logger.info("CSV %s: %d rows, %d columns", path, len(rows), len(rows[0]) if rows else 0)
        return rows

    def _read_excel(self, path: str) -> list[dict[str, Any]]:
        try:
            import pandas as pd
        except ImportError:
            raise ImportError("pandas is required for Excel support. Install it with: pip install pandas openpyxl")

        try:
            df = pd.read_excel(path, engine="openpyxl", dtype=str)
        except ImportError:
            raise ImportError("openpyxl is required for .xlsx files. Install it with: pip install openpyxl")

        df = df.fillna("").map(self._clean_val)
        rows = df.to_dict(orient="records")
        logger.info(
            "Excel %s: %d rows, %d columns",
            path,
            len(rows),
            len(rows[0]) if rows else 0,
        )
        return rows

    def _read_ods(self, path: str) -> list[dict[str, Any]]:
        try:
            import pandas as pd
        except ImportError:
            raise ImportError("pandas is required for ODS support. Install it with: pip install pandas odfpy")

        try:
            df = pd.read_excel(path, engine="odf", dtype=str)
        except ImportError:
            raise ImportError("odfpy is required for .ods files. Install it with: pip install odfpy")

        df = df.fillna("").map(self._clean_val)
        rows = df.to_dict(orient="records")
        logger.info("ODS %s: %d rows, %d columns", path, len(rows), len(rows[0]) if rows else 0)
        return rows

    @staticmethod
    def _clean_val(v: Any) -> str:
        if isinstance(v, str):
            return v.strip()
        if v is None:
            return ""
        return str(v)

    # ------------------------------------------------------------------
    # Document building
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

    def _build_docs(self, rows: list[dict[str, Any]], source: str) -> list[dict[str, Any]]:
        docs: list[dict[str, Any]] = []

        if self.rows_per_doc > 0:
            # Fixed-size row groups
            for start in range(0, len(rows), self.rows_per_doc):
                end = min(start + self.rows_per_doc, len(rows))
                text = "\n\n".join(_rows_to_json_text(rows, i) for i in range(start, end))
                docs.append(
                    {
                        "text": text,
                        "source": source,
                        "row_index": start,
                    }
                )
        else:
            # Character-budget row grouping
            current_row_dicts: list[dict[str, Any]] = []
            current_size = 0

            for i, row in enumerate(rows):
                row_text = _rows_to_json_text(rows, i)
                row_size = len(row_text) + 1

                if not current_row_dicts:
                    current_row_dicts.append(row)
                    current_size = row_size
                elif self._exceeds_budget("\n".join(r.get("text", "") for r in current_row_dicts) + "\n" + row_text):
                    docs.append(self._make_doc(current_row_dicts, source))
                    # Overlap: carry last N rows
                    if self.chunk_overlap > 0 and current_row_dicts:
                        overlap_count = min(self.chunk_overlap, len(current_row_dicts))
                        current_row_dicts = current_row_dicts[-overlap_count:]
                        current_size = self._json_rows_size(current_row_dicts)
                    else:
                        current_row_dicts = []
                        current_size = 0
                    current_row_dicts.append(row)
                    current_size += row_size
                else:
                    current_row_dicts.append(row)
                    current_size += row_size

            if current_row_dicts:
                docs.append(self._make_doc(current_row_dicts, source))

        logger.info("Built %d document(s) from %d rows", len(docs), len(rows))
        return docs

    @staticmethod
    def _json_rows_size(rows: list[dict[str, Any]]) -> int:
        return sum(len(json.dumps(r, ensure_ascii=False, default=str)) + 1 for r in rows)

    @staticmethod
    def _make_doc(rows: list[dict[str, Any]], source: str) -> dict[str, Any]:
        text = "\n\n".join(json.dumps(r, ensure_ascii=False, default=str) for r in rows)
        return {"text": text, "source": source}

    @staticmethod
    def supported_extensions() -> frozenset[str]:
        return _SUPPORTED_TABLE_EXTS
