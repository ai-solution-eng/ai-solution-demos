import base64
import re
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any

from multimodal_rag.utils.logging_utils import logging

logger = logging.getLogger(__name__)


def _resize_image(raw_bytes: bytes, mime_type: str, max_pixels: int) -> bytes:
    """Downscale *raw_bytes* so width × height ≤ *max_pixels*, maintaining aspect ratio."""
    try:
        from PIL import Image
    except ImportError:
        logger.warning(
            "Pillow not installed — returning original %d bytes (image will not be resized)",
            len(raw_bytes),
        )
        return raw_bytes

    img: Image.Image = Image.open(BytesIO(raw_bytes))
    w, h = img.size
    if max_pixels <= 0 or w * h <= max_pixels:
        # Still need to convert non-RGB modes for format compatibility
        if img.mode not in ("RGB", "RGBA", "L", "LA", "P"):
            img = img.convert("RGB")
            buf = BytesIO()
            fmt = mime_type.split("/")[-1]
            pil_fmt = {"jpg": "JPEG", "tiff": "TIFF"}.get(fmt, fmt.upper())
            if pil_fmt not in ("JPEG", "PNG", "GIF", "BMP", "TIFF", "WEBP"):
                pil_fmt = "PNG"
            img.save(buf, format=pil_fmt)
            return buf.getvalue()
        return raw_bytes

    scale = (max_pixels / (w * h)) ** 0.5
    nw = max(1, int(w * scale))
    nh = max(1, int(h * scale))
    resized = img.resize((nw, nh), Image.Resampling.LANCZOS)

    # Convert non-RGB modes (e.g. CMYK) to RGB for format compatibility
    if resized.mode not in ("RGB", "RGBA", "L", "LA", "P"):
        resized = resized.convert("RGB")

    fmt = mime_type.split("/")[-1]
    pil_fmt = {"jpg": "JPEG", "tiff": "TIFF"}.get(fmt, fmt.upper())
    if pil_fmt not in ("JPEG", "PNG", "GIF", "BMP", "TIFF", "WEBP"):
        pil_fmt = "PNG"

    buf = BytesIO()
    resized.save(buf, format=pil_fmt)
    return buf.getvalue()


@dataclass
class ImageProcessor:
    """Process images into multimodal document dicts, entirely in memory.

    Parameters
    ----------
    max_pixels:
        Maximum pixel count *width × height* for the resized image.
        Images larger than this are downscaled. ``0`` disables resizing.
    """

    max_pixels: int = 720 * 720

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def process(self, image_path: str, caption: str = "") -> dict[str, Any]:
        """Read a local image file, resize, and return a document dict.

        Returns
        -------
        dict
            ``{"text": …, "image": "data:…", "source": …}``
        """
        raw_bytes, mime_type = self._read_file(image_path)
        data_url = self._to_data_url(raw_bytes, mime_type)
        return {
            "text": caption or f"[Image: {Path(image_path).name}]",
            "image": data_url,
            "source": image_path,
        }

    def process_url(self, url: str, caption: str = "") -> dict[str, Any]:
        """Process an image URL (HTTP, data, or local path) and return a document dict.

        HTTP(S) URLs are passed through without resizing (the server fetches them).
        Data URLs and local file paths are resized in memory.
        """
        if url.startswith(("http://", "https://")):
            return {"text": caption or f"[Image: {url}]", "image": url, "source": url}

        if url.startswith("data:"):
            raw_bytes, mime_type = self._decode_data_url(url)
        else:
            raw_bytes, mime_type = self._read_file(url)

        data_url = self._to_data_url(raw_bytes, mime_type)
        src = url if not url.startswith("data:") else "data_url"
        return {
            "text": caption or f"[Image: {Path(src).name if src != 'data_url' else 'image'}]",
            "image": data_url,
            "source": src,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _read_file(path: str) -> tuple[bytes, str]:
        p = path.removeprefix("file://")
        with open(p, "rb") as f:
            raw = f.read()
        mime, _ = ImageProcessor._guess_mime(p)
        return raw, mime or "image/jpeg"

    @staticmethod
    def _decode_data_url(url: str) -> tuple[bytes, str]:
        m = re.match(r"data:([^;]+);base64,(.+)", url)
        if m:
            return base64.b64decode(m.group(2)), m.group(1)
        raw = base64.b64decode(url.split(",", 1)[1])
        return raw, "image/png"

    @staticmethod
    def _guess_mime(path: str) -> tuple[str | None, str | None]:
        import mimetypes

        return mimetypes.guess_type(path)

    def _to_data_url(self, raw_bytes: bytes, mime_type: str) -> str:
        resized = _resize_image(raw_bytes, mime_type, self.max_pixels)
        b64 = base64.b64encode(resized).decode("utf-8")
        return f"data:{mime_type};base64,{b64}"
