import json
import re
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from app.config import code_to_language
from app.schemas.api import TranscriptItem


def get_lang_name(code: str) -> str:
    return code_to_language.get(code, code)


def safe_json_loads(raw: str | None, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except Exception:
        return default


def clean_translation(text: str) -> str:
    return re.sub(r"</?[^>]+>", "", text or "").strip()


def clean_llm_document(text: str) -> str:
    cleaned = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", (text or "").strip())
    cleaned = re.sub(r"\s*```$", "", cleaned).strip()
    return cleaned


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip()).lower()


def looks_sentence_complete(text: str) -> bool:
    cleaned = re.sub(r"\s+", " ", (text or "").strip())
    if not cleaned:
        return False
    return bool(re.search(r'[.!?](?:["\'\)\]]+)?$', cleaned))


def split_words(text: str) -> list[str]:
    return [word for word in re.split(r"\s+", (text or "").strip()) if word]


def format_seconds_label(seconds: float) -> str:
    total_ms = max(0, int(round(seconds * 1000)))
    total_seconds, ms = divmod(total_ms, 1000)
    hours, rem = divmod(total_seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}.{ms:03d}"
    return f"{minutes:02d}:{secs:02d}.{ms:03d}"


def extract_json_string(text: str) -> str:
    text = clean_llm_document(text)
    if not text:
        return ""
    if text.startswith("{") or text.startswith("["):
        return text
    match = re.search(r"(\{.*\}|\[.*\])", text, flags=re.DOTALL)
    return match.group(1) if match else text


def to_plain_dict(obj: Any) -> dict[str, Any]:
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "model_dump"):
        try:
            return obj.model_dump()
        except Exception:
            pass
    if hasattr(obj, "dict"):
        try:
            return obj.dict()
        except Exception:
            pass
    return {}


def build_multilingual_source_text(items: list[TranscriptItem]) -> str:
    lines: list[str] = []
    for idx, item in enumerate(items, start=1):
        original = (item.original or "").strip()
        if not original:
            continue
        src_name = get_lang_name((item.src or "").strip().lower() or "unknown")
        timestamp = ""
        if item.ts_ms:
            try:
                timestamp = datetime.fromtimestamp(
                    item.ts_ms / 1000, ZoneInfo("America/Los_Angeles")
                ).strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                timestamp = ""
        prefix = f"[{idx}]"
        if timestamp:
            prefix += f" [{timestamp} UTC]"
        lines.append(f"{prefix} ({src_name}) {original}")
    return "\n".join(lines).strip()
