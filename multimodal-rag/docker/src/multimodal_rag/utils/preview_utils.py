import base64
import os
import re
from pathlib import Path
from typing import Any


def save_media_preview(
    docs: list[dict[str, Any]] | dict[str, Any],
    output_dir: str = "preview_output",
    name: str = "preview",
) -> str:
    """Save processed media (images/videos) to disk and generate an HTML preview.

    Parameters
    ----------
    docs:
        One or more document dicts returned by ``VideoProcessor.process()``
        or ``ImageProcessor.process()``.  Each dict may contain ``image`` or
        ``video`` keys with base64 data URLs.
    output_dir:
        Directory to write files into (created if missing).
    name:
        Base name for the HTML preview file (``{name}.html``).

    Returns
    -------
    str
        Absolute path to the generated HTML preview file.
    """
    if isinstance(docs, dict):
        docs = [docs]

    out = Path(output_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)

    html_parts: list[str] = [
        "<!DOCTYPE html>",
        "<html><head><meta charset='utf-8'>",
        f"<title>{name} — Preview</title>",
        "<style>",
        "body{font-family:sans-serif;margin:2em;background:#1a1a1a;color:#e0e0e0}",
        ".doc{border:1px solid #444;border-radius:8px;padding:1.5em;margin:1em 0;background:#2a2a2a}",
        ".meta{font-size:0.9em;color:#aaa;margin-bottom:0.5em}",
        ".meta span{margin-right:1.5em}",
        "video{max-width:100%;max-height:400px;border-radius:4px}",
        "img{max-width:100%;max-height:400px;border-radius:4px}",
        "h2{margin-top:0;color:#fff}",
        "</style></head><body>",
        f"<h1>{name} — Preview</h1>",
    ]

    for i, doc in enumerate(docs):
        text = doc.get("text", "")
        source = doc.get("source", "")
        ts_start = doc.get("timestamp_start")
        ts_end = doc.get("timestamp_end")

        html_parts.append("<div class='doc'>")
        html_parts.append(f"<h2>Segment {i}</h2>")
        html_parts.append("<div class='meta'>")
        if source:
            html_parts.append(f"<span>📁 {source}</span>")
        if ts_start is not None and ts_end is not None:
            html_parts.append(f"<span>⏱ {ts_start}s – {ts_end}s</span>")
        if text:
            html_parts.append(f"<span>{text}</span>")
        html_parts.append("</div>")

        for media_key in ("image", "video"):
            raw = doc.get(media_key)
            if not raw:
                continue
            urls = [raw] if isinstance(raw, str) else raw
            for j, data_url in enumerate(urls):
                fname = _save_data_url(data_url, out, f"{name}_seg{i}_{media_key}{j}")
                if fname:
                    rel = os.path.relpath(fname, out)
                    if media_key == "video":
                        html_parts.append(f"<video controls><source src='{rel}' type='video/mp4'></video>")
                    else:
                        html_parts.append(f"<img src='{rel}' alt='seg{i}_{j}'>")

        html_parts.append("</div>")

    html_parts.append("</body></html>")

    html_path = out / f"{name}.html"
    html_path.write_text("\n".join(html_parts), encoding="utf-8")
    print(f"Preview saved: file://{html_path}")
    return str(html_path)


def _save_data_url(data_url: str, out_dir: Path, stem: str) -> str | None:
    """Decode a base64 data URL and write it to *out_dir*.

    Returns the absolute path to the written file, or ``None`` on failure.
    """
    try:
        m = re.match(r"data:([^;]+);base64,(.+)", data_url)
        if not m:
            return None
        mime, b64_data = m.group(1), m.group(2)
        raw = base64.b64decode(b64_data)

        ext = _mime_to_ext(mime)
        fpath = out_dir / f"{stem}.{ext}"
        fpath.write_bytes(raw)
        return str(fpath)
    except Exception:
        return None


def _mime_to_ext(mime: str) -> str:
    return {"video/mp4": "mp4", "image/jpeg": "jpg", "image/png": "png", "image/gif": "gif", "image/webp": "webp"}.get(
        mime, "bin"
    )
