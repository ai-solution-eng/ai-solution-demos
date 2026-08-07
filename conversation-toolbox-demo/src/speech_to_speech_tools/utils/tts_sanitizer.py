"""Text sanitization for TTS input.

LLM responses frequently contain tokens a TTS model will read aloud
verbatim and badly: ``...`` becomes "dot dot dot", ``**bold**`` becomes
"asterisk asterisk bold asterisk asterisk", and URLs / cloud-storage URIs
get spelled out character by character. This module strips those tokens
before the text reaches the synthesizer.
"""

import re

__all__ = ["sanitize_text_for_tts"]

# Markdown links / images: keep the label, drop the URL in the parens.
#   ![alt](url) -> "alt"   [text](url) -> "text"
_MD_LINK_RE = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")

# URIs with an explicit scheme. ``s3://`` and friends are the highest
# priority to remove - the model spells out every path segment.
_URL_RE = re.compile(
    r"(?:https?|ftp|s3|gs|abfs|abfss|wasb|wasbs|file|azure|sftp)://[^\s)\"'\]]+",
    re.IGNORECASE,
)

# Bare ``www.example.com/...`` addresses.
_WWW_RE = re.compile(r"\bwww\.[^\s)\"'\]]+", re.IGNORECASE)

# Ellipsis: three or more dots, or the unicode glyph.
_ELLIPSIS_RE = re.compile(r"\.{2,}|\u2026")

# Markdown emphasis / strong / code / strikethrough markers. Replaced with a
# space so words are not glued together (``state_of_the_art`` -> ``state of the art``).
_MD_MARKERS_RE = re.compile(r"[*_`~]")

# Whitespace runs left after stripping.
_WS_RE = re.compile(r"[ \t]{2,}")


def sanitize_text_for_tts(text: str) -> str:
    """Return *text* cleaned of tokens a TTS model would mispronounce.

    Removed / normalized:
      * Markdown links and images (``[label](url)`` -> ``label``)
      * URLs and cloud-storage URIs (``https://``, ``s3://``, ``gs://`` ...)
      * Bare ``www.`` addresses
      * Ellipses (``...``, ``\u2026``) -> single period (keeps a natural pause)
      * Markdown emphasis / strong / code / strikethrough markers (``*`` ``_`` `` ` `` ``~``)
    """
    if not text:
        return text

    text = _MD_LINK_RE.sub(r"\1", text)
    text = _URL_RE.sub(" ", text)
    text = _WWW_RE.sub(" ", text)
    text = _ELLIPSIS_RE.sub(". ", text)
    text = _MD_MARKERS_RE.sub(" ", text)
    text = _WS_RE.sub(" ", text)
    return text.strip()
