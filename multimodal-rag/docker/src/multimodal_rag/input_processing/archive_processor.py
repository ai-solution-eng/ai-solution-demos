import os
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from multimodal_rag.utils.logging_utils import logging

logger = logging.getLogger(__name__)

_SUPPORTED_ARCHIVE_EXTS = frozenset({".zip", ".tar", ".gz", ".bz2", ".xz", ".tgz", ".tbz2", ".txz", ".rar"})
_MAX_DEPTH = 3

# ---------------------------------------------------------------------------
# Archive-bomb guards: total uncompressed budget, per-member cap, entry cap.
# The declared (uncompressed) member sizes are inspected BEFORE extracting, so
# a crafted archive cannot fill the disk / memory in the extraction step.
# ---------------------------------------------------------------------------
_ARCHIVE_MAX_TOTAL_BYTES = max(0, int(os.environ.get("ARCHIVE_MAX_TOTAL_BYTES", str(2 * 1024 * 1024 * 1024))))
_ARCHIVE_MAX_MEMBER_BYTES = max(0, int(os.environ.get("ARCHIVE_MAX_MEMBER_BYTES", str(1024 * 1024 * 1024))))
_ARCHIVE_MAX_ENTRIES = max(0, int(os.environ.get("ARCHIVE_MAX_ENTRIES", "10000")))


def _is_archive(path: str) -> bool:
    return Path(path).suffix.lower() in _SUPPORTED_ARCHIVE_EXTS


def _tar_extract(path: str, mode: str, dest_dir: str) -> None:
    import tarfile

    with tarfile.open(path, mode) as tf:  # type: ignore[call-overload]
        tf.extractall(dest_dir, filter="data")


def _is_safe_member(dest_dir: str, member_path: str) -> bool:
    """Return True if *member_path* stays within *dest_dir* after extraction.

    Prevents Zip Slip / path traversal via crafted archive member names
    (e.g. ``../../etc/passwd``).  Mirrors the lexical check performed
    by tarfile's ``filter="data"``.
    """
    dest = os.path.abspath(dest_dir)
    target = os.path.abspath(os.path.join(dest_dir, member_path))
    try:
        return os.path.commonpath([dest, target]) == dest
    except ValueError:
        # Different drives (Windows only) — treat as unsafe.
        return False


@dataclass
class ArchiveProcessor:
    """Extract archives and hand every contained file to the caller's handler.

    This class owns only *extraction*: archive-bomb bounds checks, safe
    extraction (Zip Slip protection), and recursion through nested
    directories and nested archives up to ``max_depth``.  Each extracted file
    is passed to ``process_member(member_path, member_name)``, which is
    responsible for processing it **exactly like a standalone upload** — the
    caller supplies the same handler used for individually uploaded files, so
    files inside an archive behave identically to the same file uploaded
    directly (same preprocessing, segmentation, model params, metadata and
    per-type chunk counts).

    ``process_member`` returns the stored vector ids it produced for that
    member; ``process`` aggregates them so the archive reports one total.
    """

    max_depth: int = _MAX_DEPTH
    process_member: Callable[[str, str], list[str]] | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def process(self, archive_path: str) -> list[str]:
        """Extract *archive_path* and process every member.

        Returns the aggregated list of stored vector ids for all members.
        """
        self._check_bounds(archive_path)
        extract_dir = tempfile.mkdtemp(prefix="mmrag_archive_")
        stored_ids: list[str] = []
        try:
            self._extract(archive_path, extract_dir)
            self._process_dir(extract_dir, depth=0, stored_ids=stored_ids)
            return stored_ids
        except Exception as e:
            logger.error("Failed to process archive %s: %s", archive_path, e)
            return []
        finally:
            shutil.rmtree(extract_dir, ignore_errors=True)

    # ------------------------------------------------------------------
    # Bounds checking (archive-bomb guard)
    # ------------------------------------------------------------------

    @staticmethod
    def _check_bounds(path: str) -> None:
        """Raise ``ValueError`` if the archive's declared sizes exceed the caps.

        The uncompressed sizes in the member headers are audited BEFORE any
        extraction so a crafted zip/tar/rar cannot fill the PVC.  Bounds are
        configured via ``ARCHIVE_MAX_TOTAL_BYTES`` / ``ARCHIVE_MAX_MEMBER_BYTES``
        / ``ARCHIVE_MAX_ENTRIES`` (0 disables a check).
        """
        if _ARCHIVE_MAX_TOTAL_BYTES <= 0 and _ARCHIVE_MAX_MEMBER_BYTES <= 0 and _ARCHIVE_MAX_ENTRIES <= 0:
            return
        ext = Path(path).suffix.lower()
        total = 0
        count = 0

        def _account(size: int) -> None:
            nonlocal total, count
            count += 1
            if _ARCHIVE_MAX_ENTRIES > 0 and count > _ARCHIVE_MAX_ENTRIES:
                raise ValueError(
                    f"Archive {path} contains more than ARCHIVE_MAX_ENTRIES ({_ARCHIVE_MAX_ENTRIES}) entries"
                )
            if _ARCHIVE_MAX_MEMBER_BYTES > 0 and size > _ARCHIVE_MAX_MEMBER_BYTES:
                raise ValueError(f"Archive member exceeds ARCHIVE_MAX_MEMBER_BYTES ({size} bytes)")
            total += size
            if _ARCHIVE_MAX_TOTAL_BYTES > 0 and total > _ARCHIVE_MAX_TOTAL_BYTES:
                raise ValueError(f"Archive total exceeds ARCHIVE_MAX_TOTAL_BYTES ({_ARCHIVE_MAX_TOTAL_BYTES} bytes)")

        if ext == ".zip":
            import zipfile

            with zipfile.ZipFile(path) as zf:
                for info in zf.infolist():
                    _account(info.file_size)
        elif ext == ".rar":
            try:
                import rarfile

                with rarfile.RarFile(path) as rf:
                    for member in rf.infolist():
                        _account(getattr(member, "file_size", 0) or 0)
            except Exception:
                logger.warning("rar bounds check unavailable (%s) — extracting without size guard", path)
        else:
            # tar / tar.gz / tar.bz2 / tar.xz (+ .tgz/.tbz2/.txz)
            import tarfile

            mode: Any = {"gz": "r:gz", "tgz": "r:gz", "bz2": "r:bz2", "tbz2": "r:bz2", "xz": "r:xz", "txz": "r:xz"}.get(
                ext.lstrip("."), "r:"
            )
            with tarfile.open(path, mode) as tf:
                for member in tf.getmembers():
                    _account(member.size)

    # ------------------------------------------------------------------
    # Extraction
    # ------------------------------------------------------------------

    @staticmethod
    def _extract(path: str, dest_dir: str) -> None:
        ext = Path(path).suffix.lower()
        stem = Path(path).stem.lower()

        if ext == ".zip":
            import zipfile

            with zipfile.ZipFile(path, "r") as zf:
                for member in zf.infolist():
                    if not _is_safe_member(dest_dir, member.filename):
                        logger.warning("Skipping unsafe zip member: %s", member.filename)
                        continue
                    zf.extract(member, dest_dir)
        elif ext == ".rar":
            try:
                import rarfile

                with rarfile.RarFile(path) as rf:
                    for member in rf.infolist():
                        fname = getattr(member, "filename", "") or ""
                        if not _is_safe_member(dest_dir, fname):
                            logger.warning("Skipping unsafe rar member: %s", fname)
                            continue
                        rf.extract(member, dest_dir)
            except Exception:
                logger.warning("rarfile extraction failed for %s, trying unrar", path)
                import subprocess as sp

                sp.run(
                    ["unrar", "x", "-y", path, dest_dir + "/"],
                    capture_output=True,
                    timeout=120,
                )
        elif ext in (".gz", ".bz2", ".xz") or stem.endswith(".tar") or ext in (".tgz", ".tbz2", ".txz"):
            if ext in (".gz", ".tgz"):
                _tar_extract(path, "r:gz", dest_dir)
            elif ext in (".bz2", ".tbz2"):
                _tar_extract(path, "r:bz2", dest_dir)
            elif ext in (".xz", ".txz"):
                _tar_extract(path, "r:xz", dest_dir)
            else:
                _tar_extract(path, "r:", dest_dir)
        elif ext == ".tar":
            _tar_extract(path, "r:", dest_dir)
        else:
            raise ValueError(f"Unsupported archive format: {ext}")

    # ------------------------------------------------------------------
    # Recursive processing
    # ------------------------------------------------------------------

    def _process_dir(self, directory: str, depth: int, stored_ids: list[str]) -> None:
        if depth > self.max_depth:
            logger.warning("Max archive depth %s reached, skipping nested content", self.max_depth)
            return
        for entry in sorted(os.listdir(directory)):
            full_path = os.path.join(directory, entry)
            if os.path.isdir(full_path):
                self._process_dir(full_path, depth, stored_ids)
            elif os.path.isfile(full_path):
                if _is_archive(full_path):
                    self._process_nested_archive(full_path, depth, stored_ids)
                else:
                    if self.process_member is None:
                        raise RuntimeError("ArchiveProcessor.process_member must be provided by the caller")
                    ids = self.process_member(full_path, entry) or []
                    stored_ids.extend(ids)

    def _process_nested_archive(self, path: str, depth: int, stored_ids: list[str]) -> None:
        """Bounds-checked, safe extraction for an archive nested inside this one."""
        self._check_bounds(path)
        nested_dir = tempfile.mkdtemp(prefix="mmrag_nested_")
        try:
            self._extract(path, nested_dir)
            self._process_dir(nested_dir, depth + 1, stored_ids)
        except Exception as e:
            logger.warning("Failed to extract nested archive %s: %s", path, e)
        finally:
            shutil.rmtree(nested_dir, ignore_errors=True)
