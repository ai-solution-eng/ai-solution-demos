import asyncio
import base64
import mimetypes
import os
import re
import weakref
from collections.abc import Awaitable, Callable, Sequence
from math import ceil
from typing import Any, overload

import httpx
from openai.types.create_embedding_response import CreateEmbeddingResponse

from .general_tools import list_chunker, sync_wrapper_safe
from .logging_utils import logging

logger = logging.getLogger(__name__)

__all__ = ["InputConversion", "MultiModalEmbeddings", "MultiModalReranker"]


_MEDIA_KEY_PATTERN = re.compile(r"^(image|video|audio)$")


def _media_type_from_key(key: str) -> str | None:
    """Return 'image', 'video', or 'audio' if *key* matches, else None."""
    m = _MEDIA_KEY_PATTERN.match(key)
    return m.group(1) if m else None


def _classify_url(url: str) -> str | None:
    """Return media category ('image', 'video', 'audio') for a URL string, or None."""
    # Base64 data URI
    if url.startswith("data:"):
        mime = url[5:].split(";")[0].split(",")[0]
        if mime.startswith("image/"):
            return "image"
        if mime.startswith("video/"):
            return "video"
        if mime.startswith("audio/"):
            return "audio"
        return None
    # Local file path
    if not url.startswith(("http://", "https://", "file://")) and os.path.isfile(url):
        mime = _detect_media_type(url)
        if mime.startswith("image/"):
            return "image"
        if mime.startswith("video/"):
            return "video"
        if mime.startswith("audio/"):
            return "audio"
        return None
    # Remote URL
    guessed_mime, _ = mimetypes.guess_type(url)
    if guessed_mime:
        if guessed_mime.startswith("image/"):
            return "image"
        if guessed_mime.startswith("video/"):
            return "video"
        if guessed_mime.startswith("audio/"):
            return "audio"
    return None


def _detect_media_type(file_path: str) -> str:
    """
    Detect the media MIME type (image, video or audio) of *file_path*.

    The function first tries ``mimetypes.guess_type`` – that works for
    almost every file that has a proper extension.  If the guess is not a
    known media type, the file header is examined for common “magic”
    bytes that identify the container/codec.

    Parameters
    ----------
    file_path : str
    Path to the file whose type is to be determined.

    Returns
    -------
    str
    A MIME type string such as ``'image/png'``, ``'video/mp4'``,
    ``'audio/mpeg'`` … or ``'application/octet-stream'`` if the type
    could not be determined.

    Notes
    -----
    * The function works in Python 3.13+ (no ``imghdr`` required).
    * Only the first 32 bytes of the file are read, so it is safe for
    large files.
    * The detection is not exhaustive – it covers the most common
    formats on typical desktop/mobile platforms.
    """
    # ------------------------------------------------------------------
    # 1️⃣  Try the built‑in mime‑type guessing based on the file name
    # ------------------------------------------------------------------
    mime, _ = mimetypes.guess_type(file_path)
    if mime and mime.startswith(("image/", "video/", "audio/")):
        return mime

    # ------------------------------------------------------------------
    # 2️⃣  Read the file header (enough for all known signatures)
    # ------------------------------------------------------------------
    with open(file_path, "rb") as f:
        header: bytes = f.read(32)  # 32 bytes cover every signature below

    # ------------------------------------------------------------------
    # 3️⃣  Image signatures (original detection kept for completeness)
    # ------------------------------------------------------------------
    if header.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if header.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if header[:2] == b"BM":
        return "image/bmp"
    if header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return "image/webp"

    # ------------------------------------------------------------------
    # 4️⃣  Video signatures
    # ------------------------------------------------------------------
    # MP4 / M4V / 3GP / MOV – container starts with the 'ftyp' box
    if header[4:8] == b"ftyp":
        # A more fine‑grained detection could inspect the brand (e.g. 'M4V ',
        # 'mp42', …) but for a generic helper we just return the generic
        # video/mp4 container type.
        return "video/mp4"

    # AVI
    if header[:4] == b"RIFF" and header[8:12] == b"AVI ":
        return "video/x-msvideo"

    # Matroska / WebM
    if header[:4] == b"\x1a\x45\xdf\xa3":
        # WebM is a subset of Matroska; we report the broader container.
        return "video/x-matroska"

    # Flash Video
    if header[:3] == b"FLV":
        return "video/x-flv"

    # MPEG‑1/2 video (including VOB)
    if header[:4] == b"\x00\x00\x01\xba" or header[:4] == b"\x00\x00\x01\xb3":
        return "video/mpeg"

    # Ogg container – can hold video (e.g. Theora) or audio (e.g. Vorbis).
    # The safest generic fallback is video/ogg; the earlier mimetypes
    # guess will normally catch the more specific audio/ogg case.
    if header[:4] == b"OggS":
        # If you need perfect video vs. audio discrimination, parse the
        # Ogg page header (see Ogg spec) and look at the first payload byte.
        # For a simple helper we return video/ogg when no extension was known.
        return "video/ogg"

    # ------------------------------------------------------------------
    # 5️⃣  Audio signatures
    # ------------------------------------------------------------------
    # WAV
    if header[:4] == b"RIFF" and header[8:12] == b"WAVE":
        return "audio/wave"

    # AIFF
    if header[:4] == b"FORM" and header[8:12] == b"AIFF":
        return "audio/aiff"

    # FLAC
    if header[:4] == b"fLaC":
        return "audio/flac"

    # MP3 with ID3v2 tag
    if header[:3] == b"ID3":
        return "audio/mpeg"

    # MP3 without ID3 – MPEG audio frame sync (0xFFF…)
    if header[0] == 0xFF and (header[1] & 0xE0) == 0xE0:
        return "audio/mpeg"

    # AAC in ADTS container
    if header[0] == 0xFF and (header[1] & 0xF0) == 0xF0:
        return "audio/aac"

    # Ogg‑Vorbis / Ogg‑Opus – already handled above as generic video/ogg.
    # If you want a more precise audio/ogg detection, parse the Ogg page
    # header and look at the first payload byte (0x01 ⇒ Vorbis, 0x4F ⇒ Opus).
    # For a quick fallback we treat it as audio/ogg when mimetypes didn’t know.
    if header[:4] == b"OggS":
        # Because we already used the Ogg test for video, we reach here only
        # when we intentionally want audio – but the code never gets here.
        return "audio/ogg"

    # MIDI
    if header[:4] == b"MThd":
        return "audio/midi"

    # AMR / AMR‑WB
    if header[:5] == b"#!AMR" or header[:7] == b"#!AMR-WB":
        return "audio/amr"

    # AC3 audio
    if header[:2] == b"\x0b\x77":
        return "audio/ac3"

    # DTS audio
    if header[:4] == b"\x7f\xfe\x80\x01":
        return "audio/dts"

    # ------------------------------------------------------------------
    # 6️⃣  Nothing matched – generic fallback
    # ------------------------------------------------------------------
    return "application/octet-stream"


class InputConversion:
    def __init__(
        self,
        # typing requires circular import. Is pcai_model_classes.EmbeddingModel
        embedding_model,
        # Only applies for html examples
        convert_to_base64: bool = False,
        max_image_size: int = 0,
        max_video_frames: int = 0,
    ) -> None:
        self.emb = embedding_model
        self.convert_to_base64 = convert_to_base64
        self.max_image_size = max_image_size
        self.max_video_frames = max_video_frames

    @staticmethod
    def _resize_image(raw_bytes: bytes, mime_type: str, max_pixels: int) -> bytes:
        """Downscale *raw_bytes* so width × height ≤ *max_pixels*, maintaining aspect ratio.

        Also converts CMYK and other non-RGB modes to RGB, since PNG (the
        default output format) doesn't support CMYK and the embedding
        endpoint expects RGB input.
        """
        try:
            from PIL import Image
        except ImportError:
            return raw_bytes

        import io

        img = Image.open(io.BytesIO(raw_bytes))

        # Convert CMYK (and other non-RGB modes) to RGB — PNG doesn't
        # support CMYK, and the embedding endpoint expects RGB.
        needs_rgb_convert = img.mode not in ("RGB", "RGBA", "L", "LA", "P")

        w, h = img.size
        if max_pixels <= 0 or w * h <= max_pixels:
            # No resize needed — but still re-encode if we converted the mode
            if not needs_rgb_convert:
                return raw_bytes
            resized = img.convert("RGB")
        else:
            scale = (max_pixels / (w * h)) ** 0.5
            new_w = max(1, int(w * scale))
            new_h = max(1, int(h * scale))
            base = img.convert("RGB") if needs_rgb_convert else img
            resized = base.resize((new_w, new_h), Image.Resampling.LANCZOS)

        fmt = mime_type.split("/")[-1]
        pil_fmt = {"jpg": "JPEG", "tiff": "TIFF"}.get(fmt, fmt.upper())
        if pil_fmt not in ("JPEG", "PNG", "GIF", "BMP", "TIFF", "WEBP"):
            pil_fmt = "PNG"

        buf = io.BytesIO()
        resized.save(buf, format=pil_fmt)
        return buf.getvalue()

    @staticmethod
    def _extract_video_frames(video_bytes: bytes, num_frames: int, max_pixels: int = 0) -> list[bytes]:
        """Extract *num_frames* evenly-spaced frames from *video_bytes*.

        Returns a list of JPEG-encoded frame bytes, each optionally downscaled
        so width × height ≤ *max_pixels*.  Uses the same pixel-budget scaling
        as :meth:`_resize_image` (aspect-ratio-aware).  Tries PyAV first,
        falls back to ffmpeg subprocess.
        """
        import io as _io

        frames: list[bytes] | None = None

        # -- PyAV backend ----------------------------------------------------
        try:
            import av

            container = av.open(_io.BytesIO(video_bytes))
            stream = container.streams.video[0]
            # Estimate total frame count
            try:
                total = stream.frames
            except Exception:
                total = 0
            if total <= 0 and stream.duration and stream.time_base:
                avg_fps = float(stream.average_rate) if stream.average_rate else 30.0
                total = int(stream.duration * stream.time_base * avg_fps)
            if total <= 0:
                total = 300  # fallback guess

            step = max(1, total // num_frames)
            wanted = set(range(0, total, step)) if total > 1 else {0}

            extracted: list[bytes] = []
            for i, frame in enumerate(container.decode(video=0)):  # type: ignore[union-attr]
                if i in wanted:
                    pil_img = frame.to_image()
                    buf = _io.BytesIO()
                    pil_img.save(buf, format="JPEG", quality=85)
                    extracted.append(buf.getvalue())
                    if len(extracted) >= num_frames:
                        break
            container.close()

            if extracted:
                frames = extracted
        except Exception:
            logger.debug("PyAV frame extraction failed, trying ffmpeg fallback", exc_info=True)

        # -- ffmpeg pipe backend ---------------------------------------------
        if frames is None:
            try:
                import json
                import subprocess

                # Probe duration
                probe = subprocess.run(
                    [
                        "ffprobe",
                        "-v",
                        "error",
                        "-show_entries",
                        "format=duration",
                        "-of",
                        "json",
                        "-",
                    ],
                    input=video_bytes,
                    capture_output=True,
                    timeout=30,
                    check=False,
                )
                info = json.loads(probe.stdout)
                duration = float(info.get("format", {}).get("duration", 10))
                fps = max(0.1, num_frames / duration) if duration > 0 else 1.0

                proc = subprocess.run(
                    [
                        "ffmpeg",
                        "-v",
                        "error",
                        "-i",
                        "-",
                        "-vf",
                        f"fps={fps}",
                        "-f",
                        "image2pipe",
                        "-vcodec",
                        "mjpeg",
                        "-q:v",
                        "3",
                        "-",
                    ],
                    input=video_bytes,
                    capture_output=True,
                    timeout=120,
                    check=False,
                )

                # Parse concatenated JPEG stream
                raw = proc.stdout
                parsed: list[bytes] = []
                start = 0
                while start < len(raw) - 1:
                    if raw[start] != 0xFF or raw[start + 1] != 0xD8:
                        start += 1
                        continue
                    end = raw.find(b"\xff\xd9", start + 2)
                    if end == -1:
                        break
                    end += 2
                    parsed.append(raw[start:end])
                    start = end

                if parsed:
                    frames = parsed[:num_frames]
            except Exception:
                logger.debug("ffmpeg frame extraction also failed", exc_info=True)

        if not frames:
            logger.warning("Could not extract video frames; sending raw video")
            return []

        # -- Resize each frame if needed ------------------------------------
        if max_pixels > 0:
            try:
                from PIL import Image
            except ImportError:
                return frames

            resized: list[bytes] = []
            for raw_frame in frames:
                pil = Image.open(_io.BytesIO(raw_frame))
                w, h = pil.size
                if w * h > max_pixels:
                    scale = (max_pixels / (w * h)) ** 0.5
                    nw = max(1, int(w * scale))
                    nh = max(1, int(h * scale))
                    out = pil.resize((nw, nh), Image.Resampling.LANCZOS)
                else:
                    out = pil
                buf = _io.BytesIO()
                out.save(buf, format="JPEG", quality=85)
                resized.append(buf.getvalue())
            return resized

        return frames

    async def _fetch_video_frames(self, url: str, num_frames: int | None = None) -> list[tuple[str, str]]:
        """Download video, extract frames, return list of (base64_data, mime_type)."""
        if url.startswith(("http://", "https://")):
            response = await self.emb.http_async_client.get(url, follow_redirects=True)
            response.raise_for_status()
            video_bytes = response.content
        else:
            path = url.removeprefix("file://")
            with open(path, "rb") as f:
                video_bytes = f.read()

        max_px = self.emb.mm_processor_kwargs.get("max_pixels", 0) if hasattr(self.emb, "mm_processor_kwargs") else 0
        nf = num_frames if num_frames is not None else self.max_video_frames
        frames = self._extract_video_frames(video_bytes, nf, max_px)
        if not frames:
            return []

        results: list[tuple[str, str]] = []
        for frame_bytes in frames:
            b64 = base64.b64encode(frame_bytes).decode("utf-8")
            results.append((b64, "image/jpeg"))
        return results

    async def _fetch_obj_async(self, url: str) -> tuple[str, str]:
        """
        Fetch media bytes and return (base64_data, mime_type).

        If ``self.max_image_size > 0`` and the fetched resource is an image,
        it is downscaled so the longer edge does not exceed that value.
        """
        if url.startswith(("http://", "https://")):
            response = await self.emb.http_async_client.get(url, follow_redirects=True)
            response.raise_for_status()
            raw_bytes = response.content
            content_type = response.headers.get("content-type", "application/octet-stream")
            mime_type = content_type.split(";")[0].strip()
        else:
            url = url.removeprefix("file://")
            with open(url, "rb") as f:
                raw_bytes = f.read()
            mime_type = _detect_media_type(url)

        raw_len = len(raw_bytes)
        max_px = self.emb.mm_processor_kwargs.get("max_pixels", 0) if hasattr(self.emb, "mm_processor_kwargs") else 0
        if max_px > 0 and mime_type.startswith("image/"):
            before = len(raw_bytes)
            raw_bytes = self._resize_image(raw_bytes, mime_type, max_px)
            logger.info(
                "embedder resize: %s mime=%s max_px=%d → %d → %d bytes",
                url[:80],
                mime_type,
                max_px,
                before,
                len(raw_bytes),
            )
        else:
            logger.warning(
                "embedder NO-RESIZE: %s mime=%s max_px=%s raw=%d bytes "
                "(will be sent to vLLM as-is and may exceed audio_filesize_mb cap)",
                url[:80],
                mime_type,
                max_px,
                raw_len,
            )

        obj_data = base64.b64encode(raw_bytes).decode("utf-8")
        return obj_data, mime_type

    async def _pull_data_async(self, requests: list[dict[str, Any]]) -> list[dict[str, Any]]:
        for item in requests:
            audio_urls: list[str] = item.pop("_audio_urls", [])
            image_urls: list[str] = item.pop("_image_urls", [])
            video_urls: list[str] = item.pop("_video_urls", [])

            audio_tasks = [self._fetch_obj_async(u) for u in audio_urls]
            image_tasks = [self._fetch_obj_async(u) for u in image_urls]

            audio_results = await asyncio.gather(*audio_tasks) if audio_tasks else []
            image_results = await asyncio.gather(*image_tasks) if image_tasks else []

            # Video: extract frames client-side when configured, otherwise send raw
            _max_frames = self.max_video_frames or (
                self.emb.mm_processor_kwargs.get("max_frames", 0) if hasattr(self.emb, "mm_processor_kwargs") else 0
            )
            if _max_frames > 0 and video_urls:
                video_frame_tasks = [self._fetch_video_frames(u, _max_frames) for u in video_urls]
                video_frame_results = await asyncio.gather(*video_frame_tasks) if video_frame_tasks else []
                for frame_list in video_frame_results:
                    for obj_data, mime_type in frame_list:
                        item["content"].insert(
                            0,
                            {
                                "type": "image_url",
                                "image_url": {"url": f"data:{mime_type};base64,{obj_data}"},
                            },
                        )
            else:
                video_tasks = [self._fetch_obj_async(u) for u in video_urls]
                video_results = await asyncio.gather(*video_tasks) if video_tasks else []
                for obj_data, mime_type in video_results:
                    item["content"].insert(
                        0,
                        {
                            "type": "video_url",
                            "video_url": {"url": f"data:{mime_type};base64,{obj_data}"},
                        },
                    )

            for obj_data, mime_type in audio_results:
                item["content"].insert(
                    0,
                    {
                        "type": "audio_url",
                        "audio_url": {"url": f"data:{mime_type};base64,{obj_data}"},
                    },
                )
            for obj_data, mime_type in image_results:
                item["content"].insert(
                    0,
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime_type};base64,{obj_data}"},
                    },
                )

        return requests

    def _add_raw_url(self, requests: list[dict[str, Any]]) -> list[dict[str, Any]]:
        for input_dict in requests:
            for url in input_dict.pop("_audio_urls", []):
                if url.startswith(("http://", "https://", "data:")):
                    input_dict["content"].insert(0, {"type": "audio_url", "audio_url": {"url": url}})
                else:
                    path = url.removeprefix("file://")
                    with open(path, "rb") as f:
                        b64 = base64.b64encode(f.read()).decode("utf-8")
                    input_dict["content"].insert(
                        0,
                        {
                            "type": "audio_url",
                            "audio_url": {"url": f"data:audio/mpeg;base64,{b64}"},
                        },
                    )

            for url in input_dict.pop("_image_urls", []):
                if url.startswith(("http://", "https://", "data:")):
                    # Remote URL or model-ready data URL (tier 3) — pass
                    # through without client-side resize; the server applies
                    # mm_processor_kwargs if needed (no-op at tier 3).
                    input_dict["content"].insert(0, {"type": "image_url", "image_url": {"url": url}})
                else:
                    # Local file — decode, resize, re-encode
                    path = url.removeprefix("file://")
                    with open(path, "rb") as f:
                        raw_bytes = f.read()
                    mime_type = _detect_media_type(path)

                    try:
                        max_px = (
                            self.emb.mm_processor_kwargs.get("max_pixels", 0)
                            if hasattr(self.emb, "mm_processor_kwargs")
                            else 0
                        )
                        before = len(raw_bytes)
                        resized = self._resize_image(raw_bytes, mime_type, max_px)
                        logger.info(
                            "embedder resize (sync): %s mime=%s max_px=%d → %d → %d bytes",
                            url[:80],
                            mime_type,
                            max_px,
                            before,
                            len(resized),
                        )
                        b64 = base64.b64encode(resized).decode("utf-8")
                        input_dict["content"].insert(
                            0,
                            {
                                "type": "image_url",
                                "image_url": {"url": f"data:{mime_type};base64,{b64}"},
                            },
                        )
                    except Exception as img_exc:
                        logger.warning("Skipping unreadable image %s: %s", url[:80], img_exc)

            for url in input_dict.pop("_video_urls", []):
                if url.startswith(("http://", "https://", "data:")):
                    input_dict["content"].insert(0, {"type": "video_url", "video_url": {"url": url}})
                else:
                    path = url.removeprefix("file://")
                    with open(path, "rb") as f:
                        b64 = base64.b64encode(f.read()).decode("utf-8")
                    input_dict["content"].insert(
                        0,
                        {
                            "type": "video_url",
                            "video_url": {"url": f"data:video/mp4;base64,{b64}"},
                        },
                    )

        return requests

    @staticmethod
    def _add_extra_inputs(requests: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
        return [
            [
                {
                    "role": "system",
                    "content": [
                        {"type": "text", "text": "Represent the user's input."},
                    ],
                },
                x,
                {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": ""},
                    ],
                },
            ]
            for x in requests
        ]

    def __call__(
        self,
        inputs: list[str | dict[str, Any]],
        add_conversational_elements: bool = True,
    ) -> list[list[dict[str, Any]]] | list[dict[str, Any]]:
        return sync_wrapper_safe(
            self.acall,
            {
                "inputs": inputs,
                "add_conversational_elements": add_conversational_elements,
            },
        )

    async def acall(
        self,
        inputs: list[str | dict[str, Any]],
        add_conversational_elements: bool = True,
    ) -> list[list[dict[str, Any]]] | list[dict[str, Any]]:
        """
        Convert inputs to OpenAI-compatible message format (fully async, parallel image fetching).

        The idea is to convert an input friendly format, like
        {
            'text': '...',
            'image': '...',
            'video': '...',
        }

        to the required format to achieve parity with the local variants.

        [{'role': 'system',
        'content': [{'type': 'text', 'text': "Represent the user's input."}]},
        {'role': 'user',
        'content': [{'type': 'image_url', 'image_url': {'url': '...'}},
        {'type': 'text',
            'text': '...'}]},
        {'role': 'assistant', 'content': [{'type': 'text', 'text': '...'}]}]

        By default, the user-side pulls data to avoid proxy issues.

        Supported input forms:

        * ``"A plain text string."``
        * ``"https://example.com/image.jpg"``  – bare URL auto-detected as media
        * ``"data:image/png;base64,…"``        – bare base64 data URI auto-detected as media
        * ``"/path/to/image.jpg"``             – bare local path auto-detected as media
        * ``{"text": "…", "image": "…"}``       – single image
        * ``{"text": "…", "image": ["…", "…"]}``  – multiple images
        * ``{"text": "…", "video": "…"}``       – single video
        * ``{"text": "…", "video": ["…", "…"]}``  – multiple videos
        * Same for ``audio``
        """
        new_request: list[dict[str, Any]] = []

        for dictionary in inputs:
            content = []

            if isinstance(dictionary, str):
                # Detect bare URLs that point to media files
                media_type = _classify_url(dictionary)
                if media_type is not None:
                    if media_type not in self.emb.allowable_modalities:
                        logger.warning("Model does not support %ss. Skipping bare URL.", media_type)
                        # fall through as text
                    else:
                        new_request.append(
                            {
                                "role": "user",
                                "content": [],
                                "_audio_urls": ([dictionary] if media_type == "audio" else []),
                                "_image_urls": ([dictionary] if media_type == "image" else []),
                                "_video_urls": ([dictionary] if media_type == "video" else []),
                            }
                        )
                        continue

                # Treat as plain text
                new_request.append(
                    {
                        "role": "user",
                        "content": [{"type": "text", "text": dictionary}],
                        "_audio_urls": [],
                        "_image_urls": [],
                        "_video_urls": [],
                    }
                )
                continue

            # -- dict input ----------------------------------------------------
            has_text = bool(dictionary.get("text"))
            if has_text:
                content.append({"type": "text", "text": dictionary["text"]})

            audio_urls: list[str] = []
            image_urls: list[str] = []
            video_urls: list[str] = []

            for key, val in dictionary.items():
                if key == "text" or val is None:
                    continue
                media_type = _media_type_from_key(key)
                if media_type is None:
                    continue
                if media_type not in self.emb.allowable_modalities:
                    logger.warning("Model does not support %ss. Skipping key %r.", media_type, key)
                    continue
                urls = val if isinstance(val, list) else [val]
                if media_type == "audio":
                    audio_urls.extend(urls)
                elif media_type == "image":
                    image_urls.extend(urls)
                else:
                    video_urls.extend(urls)

            # Ensure at least some text is present — media-only docs can
            # produce degenerate embeddings (identical for every image-only
            # input), which causes all-but-one to be deduplicated.
            if not has_text and (audio_urls or image_urls or video_urls):
                labels = []
                if image_urls:
                    labels.append("Image")
                if video_urls:
                    labels.append("Video")
                if audio_urls:
                    labels.append("Audio")
                content.insert(0, {"type": "text", "text": f"[{' & '.join(labels)} media]"})

            new_request.append(
                {
                    "role": "user",
                    "content": content,
                    "_audio_urls": audio_urls,
                    "_image_urls": image_urls,
                    "_video_urls": video_urls,
                }
            )

        # Parallel fetch all media
        requests = (
            (await self._pull_data_async(new_request)) if self.convert_to_base64 else self._add_raw_url(new_request)
        )

        if add_conversational_elements:
            return self._add_extra_inputs(requests)

        return requests


class _QueryBatcher:
    """Dynamic micro-batcher for concurrent text-only embedding queries.

    When multiple search requests call ``aembed_query`` concurrently, each
    normally fires its own HTTP request to the embedding endpoint.  vLLM's
    pooling runner processes these sequentially, so N concurrent queries
    take N × latency.

    This batcher collects queries arriving within a short window (default
    5 ms) or until ``max_batch_size`` accumulate, then sends them as a
    single ``/v1/embeddings`` call with ``input=[q1, q2, ...]``.  vLLM
    processes the batch in one forward pass, yielding up to ~10x higher
    throughput under concurrent load.

    Each caller awaits a ``Future`` and receives its individual embedding
    vector — the batching is transparent.
    """

    def __init__(
        self,
        embed_fn: "Callable[[list[str]], Awaitable[list[list[float]]]]",
        max_batch_size: int = 32,
        max_wait_ms: float = 5.0,
    ) -> None:
        self._embed_fn = embed_fn
        self._max_batch_size = max(1, max_batch_size)
        self._max_wait = max(0.001, max_wait_ms / 1000.0)
        self._queue: list[tuple[str, asyncio.Future[list[float]]]] = []
        # asyncio.Lock() must be created inside a running event loop.
        # Lazily initialised on first submit() / _flush() call.
        self._lock: asyncio.Lock | None = None
        self._flush_task: asyncio.Task[None] | None = None

    def _get_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def submit(self, text: str) -> list[float]:
        """Submit a single text query and await its embedding."""
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[list[float]] = loop.create_future()
        flush_now = False

        async with self._get_lock():
            self._queue.append((text, fut))
            if len(self._queue) >= self._max_batch_size:
                flush_now = True
            elif self._flush_task is None:
                # Schedule a timed flush so a partial batch doesn't wait forever.
                self._flush_task = loop.create_task(self._timed_flush())

        if flush_now:
            await self._flush()

        return await fut

    async def _timed_flush(self) -> None:
        """Wait for the max wait window, then flush whatever has accumulated."""
        try:
            await asyncio.sleep(self._max_wait)
        except asyncio.CancelledError:
            return
        # Pass our own task so _flush() knows not to cancel us mid-flight.
        await self._flush(caller_task=asyncio.current_task())

    async def _flush(self, caller_task: "asyncio.Task[None] | None" = None) -> None:
        """Drain the queue and send one batched embedding request.

        *caller_task* is the task that triggered this flush (e.g. the
        ``_timed_flush`` task). It is NOT cancelled — only a stale timed
        flush that didn't fire is cancelled, and only when it differs from
        the caller.
        """
        async with self._get_lock():
            if not self._queue:
                return
            batch = self._queue[:]
            self._queue.clear()
            # Cancel a pending timed flush only if this flush was NOT
            # triggered by that timer (otherwise we'd cancel ourselves).
            if self._flush_task is not None and not self._flush_task.done() and self._flush_task is not caller_task:
                self._flush_task.cancel()
            self._flush_task = None

        texts = [t for t, _ in batch]
        futs = [f for _, f in batch]

        # Settle every caller's future from the embedding result — even if the
        # task that triggered this flush is cancelled (e.g. a client disconnect),
        # so no queued caller is left hanging forever.
        def _settle(f: asyncio.Future) -> None:
            try:
                embeddings = f.result()
                if len(embeddings) != len(futs):
                    raise RuntimeError(f"Batch embedding returned {len(embeddings)} results for {len(futs)} queries")
                for fut, emb in zip(futs, embeddings):
                    if not fut.done():
                        fut.set_result(emb)
            except BaseException as exc:  # settle callers regardless
                for fut in futs:
                    if not fut.done():
                        fut.set_exception(exc)

        embed_task: asyncio.Future[list[list[float]]] = asyncio.ensure_future(self._embed_fn(texts))
        embed_task.add_done_callback(_settle)
        await asyncio.shield(embed_task)


class MultiModalEmbeddings:
    chunk_size: int = 64
    "Multimodal embeddings wrapper (embed_documents / embed_query) for vLLM-style endpoints."

    # typing requires circular import. Is pcai_model_classes.EmbeddingModel
    def __init__(
        self,
        embedding_model,
        convert_to_base64: bool = False,
        max_image_size: int = 0,
        max_video_frames: int = 0,
    ):
        self.emb = embedding_model
        self.input_conversion = InputConversion(
            embedding_model,
            convert_to_base64=convert_to_base64,
            max_image_size=max_image_size,
            max_video_frames=max_video_frames,
        )
        # Dynamic micro-batcher for concurrent text-only search queries.
        # Collects queries arriving within a short window and sends them as
        # a single /v1/embeddings request with input=[q1, q2, ...], which
        # vLLM processes in one forward pass instead of N sequential ones.
        #
        # One batcher per event loop — the MCP server and the REST API
        # (via sync_wrapper_safe / background loop) each have their own
        # event loop, and asyncio primitives cannot span loops.
        self._batcher_max_size = int(os.environ.get("EMBEDDING_QUERY_BATCH_SIZE", "32"))
        self._batcher_max_wait_ms = float(os.environ.get("EMBEDDING_QUERY_BATCH_WAIT_MS", "5"))
        self._batchers: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, _QueryBatcher] = (
            weakref.WeakKeyDictionary()
        )
        # Optional shared (cross-process) embedding query batcher.  When set,
        # text-only queries are POSTed here instead of being aggregated in
        # this process's local _QueryBatcher.  The remote aggregator collects
        # queries from every worker/pod into one /v1/embeddings call, so
        # batch size is independent of the number of app processes.
        self._batch_url = os.environ.get("RAG_EMBED_BATCH_URL", "").rstrip("/")
        self._batch_client: httpx.AsyncClient | None = None

    async def _embed_single_message_async(self, input_dict: list[dict[str, Any]]) -> list[float]:
        """Helper to hit your endpoint for a single input_dict fragment."""
        response = await self.emb.async_client.post(
            "/embeddings",
            cast_to=CreateEmbeddingResponse,  # Ensure this type is imported in your script
            body={
                "model": self.emb.model_name,
                "messages": input_dict,  # Assuming the endpoint accepts the string input_dict here
                "encoding_format": "float",
                "continue_final_message": True,
                "add_special_tokens": True,
                "mm_processor_kwargs": self.emb.mm_processor_kwargs,
            },
        )
        # Parse the vector array out of your specific CreateEmbeddingResponse structure
        # (Update 'response.embedding' to match your actual response object schema)
        assert len(response.data) == 1
        return response.data[0].embedding

    # -- text-only batch helpers -----------------------------------------------

    _CHAT_TEMPLATE_PREFIX = "<|im_start|>system\nRepresent the user's input.<|im_end|>\n<|im_start|>user\n"
    _CHAT_TEMPLATE_SUFFIX = "<|im_end|>\n<|im_start|>assistant\n<|im_end|>"

    @staticmethod
    async def _empty_list() -> list[list[float]]:
        return []

    @classmethod
    def _fmt_chat_template(cls, text: str) -> str:
        """Format *text* into the Qwen3-VL-Embedding chat template string.

        This produces the same token sequence the server would generate
        internally from the ``messages`` format (system → user → assistant),
        allowing text-only docs to be batched via the standard ``input``
        field instead of individual ``messages`` requests.
        """
        return cls._CHAT_TEMPLATE_PREFIX + text + cls._CHAT_TEMPLATE_SUFFIX

    def _is_text_only(self, doc: str | dict[str, Any]) -> bool:
        """Return True if *doc* has no media that the model supports."""
        if isinstance(doc, str):
            media_type = _classify_url(doc)
            if media_type is None:
                return True
            return media_type not in self.emb.allowable_modalities

        if not isinstance(doc, dict):
            return True

        for key in ("image", "video", "audio"):
            val = doc.get(key)
            if val is not None:
                media_type = _media_type_from_key(key)
                if media_type and media_type in self.emb.allowable_modalities:
                    return False
        return True

    async def _embed_text_batch_async(self, texts: list[str]) -> list[list[float]]:
        """Batch-embed text-only docs via a single ``input`` request.

        Uses :meth:`_fmt_chat_template` to format each text into the
        Qwen3-VL-Embedding chat template, then sends all of them in one
        ``/v1/embeddings`` call.  This reduces N HTTP requests to 1 for
        text-only documents.

        The resulting embeddings have >0.996 cosine similarity with the
        per-doc ``messages`` format — well within embedding noise.
        """
        formatted = [self._fmt_chat_template(t) for t in texts]
        response = await self.emb.async_client.post(
            "/embeddings",
            cast_to=CreateEmbeddingResponse,
            body={
                "model": self.emb.model_name,
                "input": formatted,
                "encoding_format": "float",
            },
        )
        return [d.embedding for d in response.data]

    # -- remote (shared) query batcher -----------------------------------------

    def _batch_http_client(self) -> httpx.AsyncClient:
        if self._batch_client is None:
            self._batch_client = httpx.AsyncClient(timeout=300.0)
        return self._batch_client

    async def _aembed_query_remote(self, text: str) -> list[float]:
        """Embed a single query via the shared cross-process batcher.

        The batcher aggregates concurrent queries from every worker/pod into
        one ``/v1/embeddings`` call to the embedding endpoint, so batch size
        does not depend on the number of app processes.
        """
        resp = await self._batch_http_client().post(self._batch_url, json={"text": text})
        resp.raise_for_status()
        return resp.json()["embedding"]

    # -- embed_documents --------------------------------------------------------
    # First call is the Langchain expected input.

    @overload
    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    @overload
    def embed_documents(self, texts: list[str | dict[str, Any]]) -> list[list[float]]: ...

    def embed_documents(self, texts: Sequence[str | dict[str, Any]]) -> list[list[float]]:
        """Synchronous fallback that executes the async loop safely."""
        return sync_wrapper_safe(self.aembed_documents, {"texts": texts})

    # -- aembed_documents -------------------------------------------------------

    @overload
    async def aembed_documents(self, texts: list[str]) -> list[list[float]]: ...

    @overload
    async def aembed_documents(self, texts: list[str | dict[str, Any]]) -> list[list[float]]: ...

    async def aembed_documents(self, texts: Sequence[str | dict[str, Any]]) -> list[list[float]]:
        """Asynchronously embed a list of documents.

        Text-only documents are batched into a single ``input`` request
        (1 HTTP call per sub-batch).  Multimodal documents use the
        ``messages`` format individually (N HTTP calls), since the
        endpoint doesn't support batched multimodal input.

        Results are merged in original order.
        """
        outputs: list[list[float]] = []
        for data in list_chunker(texts, self.chunk_size):
            # -- Split into text-only and multimodal ---------------------------
            text_indices: list[int] = []
            text_texts: list[str] = []
            mm_indices: list[int] = []
            mm_docs: list[str | dict[str, Any]] = []

            for i, doc in enumerate(data):
                if self._is_text_only(doc):
                    text_indices.append(i)
                    text_texts.append(doc if isinstance(doc, str) else (doc.get("text") or ""))
                else:
                    mm_indices.append(i)
                    mm_docs.append(doc)

            results: list[list[float] | None] = [None] * len(data)

            # -- Run text batch and multimodal requests concurrently -----------
            async def _embed_mm() -> list[list[float]]:
                if not mm_docs:
                    return []
                converted = await self.input_conversion.acall(mm_docs)  # type: ignore[arg-type]
                tasks = [self._embed_single_message_async(x) for x in converted]  # type: ignore[arg-type]
                return await asyncio.gather(*tasks)

            text_task = self._embed_text_batch_async(text_texts) if text_texts else self._empty_list()
            mm_task = _embed_mm()

            text_embs, mm_embs = await asyncio.gather(text_task, mm_task)

            for j, idx in enumerate(text_indices):
                results[idx] = text_embs[j]
            for j, idx in enumerate(mm_indices):
                results[idx] = mm_embs[j]

            # Guard against silent data loss: every doc must have received
            # an embedding.  If any are None, raise so the caller can retry
            # the batch rather than silently dropping documents.
            missing = [i for i, e in enumerate(results) if e is None]
            if missing:
                raise RuntimeError(
                    f"Embedding API returned incomplete results: {len(missing)} "
                    f"of {len(results)} docs missing embeddings (indices: "
                    f"{missing[:10]}{'...' if len(missing) > 10 else ''})"
                )
            outputs.extend(results)  # type: ignore[arg-type]

        return outputs

    # -- embed_query ------------------------------------------------------------

    @overload
    def embed_query(self, text: str) -> list[float]: ...

    @overload
    def embed_query(self, text: str | dict[str, Any]) -> list[float]: ...

    def embed_query(self, text: str | dict[str, Any]) -> list[float]:
        """Synchronous fallback that executes the async loop safely."""
        return sync_wrapper_safe(self.aembed_query, {"text": text})

    # -- aembed_query -----------------------------------------------------------

    @overload
    async def aembed_query(self, text: str) -> list[float]: ...

    @overload
    async def aembed_query(self, text: str | dict[str, Any]) -> list[float]: ...

    async def aembed_query(self, text: str | dict[str, Any]) -> list[float]:
        """Asynchronously embed a single query input_dict.

        Text-only string queries are routed through a dynamic micro-batcher
        so that concurrent search requests share a single HTTP call to the
        embedding endpoint (vLLM processes batched ``input`` in one forward
        pass).  Multimodal queries (dict with image/video/audio) bypass the
        batcher and use the per-request ``messages`` format.
        """
        # Text-only fast path: batch with other concurrent queries
        if isinstance(text, str) and self._is_text_only(text):
            if self._batch_url:
                try:
                    return await self._aembed_query_remote(text)
                except Exception as exc:
                    # Shared batcher down? Fall back to the local per-loop
                    # batcher so search keeps working during an outage.
                    logger.warning("Shared embed batcher unavailable (%s); falling back to local batching", exc)
            loop = asyncio.get_running_loop()
            batcher = self._batchers.get(loop)
            if batcher is None:
                batcher = _QueryBatcher(
                    embed_fn=self._embed_text_batch_async,
                    max_batch_size=self._batcher_max_size,
                    max_wait_ms=self._batcher_max_wait_ms,
                )
                self._batchers[loop] = batcher
            return await batcher.submit(text)

        # Multimodal path: individual messages-format request
        inputs: list[list[dict[str, Any]]] = await self.input_conversion.acall([text])  # type: ignore[assignment]
        return await self._embed_single_message_async(inputs[0])


class MultiModalReranker:
    chunk_size: int = 64
    "Multimodal reranker wrapper (score / rerank) for vLLM-style endpoints."

    # Template constants for text-only query/document formatting.
    # See: https://docs.vllm.ai/projects/ascend/en/latest/tutorials/models/Qwen3-VL-Reranker.html
    # Found it to be irrelevant. VLLM does this already. So it is internally skipped.
    _PREFIX = (
        "<|im_start|>system\n"
        "Judge whether the Document meets the requirements based on the Query "
        'and the Instruct provided. Note that the answer can only be "yes" or '
        '"no".<|im_end|>\n<|im_start|>user\n'
    )
    _SUFFIX = "<|im_end|>\n<|im_start|>assistant\n"
    _INSTRUCTION = "Given a search query, retrieve relevant candidates that answer the query."

    @staticmethod
    def _format_query(query: str | dict[str, Any]) -> str | dict[str, Any]:
        if isinstance(query, str):
            return f"{MultiModalReranker._PREFIX}<Instruct>: {MultiModalReranker._INSTRUCTION}\n<Query>: {query}\n"
        return query

    @staticmethod
    def _format_document(doc: str | dict[str, Any]) -> str | dict[str, Any]:
        if isinstance(doc, str):
            return f"<Document>: {doc}{MultiModalReranker._SUFFIX}"
        return doc

    # typing requires circular import. Is pcai_model_classes.EmbeddingModel
    def __init__(
        self,
        embedding_model,
        convert_to_base64: bool = False,
        max_image_size: int = 0,
        max_video_frames: int = 0,
    ):
        self.emb = embedding_model
        # Avoid warning that `/score` and `rerank` are not part of the default openai api. Removes '/v1'
        self.emb.base_url = self.emb.base_url[:-3]
        self.input_conversion = InputConversion(
            embedding_model,
            convert_to_base64=convert_to_base64,
            max_image_size=max_image_size,
            max_video_frames=max_video_frames,
        )

    @staticmethod
    def _remove_extra_input_components(
        inputs: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        adjusted_inputs = []
        for x in inputs:
            # Pull out text elements as strings
            if len(x["content"]) == 1 and "text" in x["content"][0]:
                adjusted_inputs.append(x["content"][0]["text"])
                continue
            if "role" in x:
                x.pop("role")
            adjusted_inputs.append(x)
        return adjusted_inputs

    async def _prepare_inputs(
        self,
        query: str | dict[str, Any] | list[str | dict[str, Any]],
        documents: str | dict[str, Any] | list[str | dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:

        if isinstance(query, list):
            N = len(query)
        else:
            query, N = [query], 1

        if not isinstance(documents, list):
            documents = [documents]

        input_dicts = query + documents

        inputs: list[dict[str, Any]] = []
        for x in list_chunker(input_dicts, self.chunk_size):
            inputs.extend(
                await self.input_conversion.acall(x, add_conversational_elements=False)  # type: ignore[arg-type]
            )

        adjusted_inputs = self._remove_extra_input_components(inputs=inputs)

        return adjusted_inputs[:N], adjusted_inputs[N:]

    async def _score_one_query(
        self,
        query: str | dict[str, Any],
        documents: str | dict[str, Any] | Sequence[str | dict[str, Any]],
    ) -> list[float]:
        """Helper to hit your endpoint for a single input_dict fragment."""
        response = await self.emb.async_client.post(
            "/score",
            cast_to=list,
            body={
                "model": self.emb.model_name,
                "text_1": query,
                "text_2": documents,
                "mm_processor_kwargs": self.emb.mm_processor_kwargs,
            },
        )
        return [x["score"] for x in response["data"]]

    async def ascore(
        self,
        query: str | dict[str, Any] | list[str | dict[str, Any]],
        documents: str | list[str | dict[str, Any]],
    ) -> list[list[float]] | list[float]:
        """Asynchronously get scores for different comparisons."""
        assert query, f"Query needs to be non-empty. Got {query}"
        assert documents, f"Documents needs to be non-empty. Got {documents}"

        query_dicts, document_dicts = await self._prepare_inputs(query, documents)

        outputs, tasks, current_idx, task_length = [], [], 0, 0

        Q = len(query_dicts)
        D = len(document_dicts)
        N = Q * D

        num_chunks = ceil(N / self.chunk_size)
        optimal_chunk_size = ceil(N / num_chunks)

        # Add grouped document queries such that their sum per iteration is optimal chunk size
        for q in query_dicts:
            while True:
                max_num_tasks = optimal_chunk_size - task_length
                current_documents = document_dicts[current_idx : current_idx + max_num_tasks]

                tasks.append(self._score_one_query(q, current_documents))
                task_length += len(current_documents)

                if task_length >= optimal_chunk_size:
                    outputs.extend(await asyncio.gather(*tasks))
                    tasks, task_length = [], 0

                current_idx = current_idx + max_num_tasks
                if current_idx >= len(document_dicts):
                    current_idx = 0
                    break

        if tasks:
            outputs.extend(await asyncio.gather(*tasks))

        # Expand and rechunk to ensure queries line-up!
        flat_outputs = [y for x in outputs for y in x]
        assert len(flat_outputs) == N
        final = list_chunker(flat_outputs, D, optimize=False)
        assert len(final) == len(query_dicts)

        if Q == 1:
            return final[0]

        return final

    def score(
        self,
        query: str | dict[str, Any] | list[str | dict[str, Any]],
        documents: str | list[str | dict[str, Any]],
    ):
        return sync_wrapper_safe(self.ascore, {"query": query, "documents": documents})

    async def _rerank_one_query(
        self, query: str | dict[str, Any], documents: Sequence[str | dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Helper to hit your endpoint for a single input_dict fragment."""
        response = await self.emb.async_client.post(
            "/rerank",
            cast_to=list,
            body={
                "model": self.emb.model_name,
                "query": query,
                "documents": documents,
                "mm_processor_kwargs": self.emb.mm_processor_kwargs,
            },
        )
        return response["results"]

    async def arerank(
        self,
        query: str | dict[str, Any] | list[str | dict[str, Any]],
        documents: str | list[str | dict[str, Any]],
    ) -> list[list[dict[str, Any]]]:
        """Asynchronously get scores for different comparisons."""
        assert query, f"Query needs to be non-empty. Got {query}"
        assert documents, f"Documents needs to be non-empty. Got {documents}"

        query_dicts, document_dicts = await self._prepare_inputs(query, documents)

        D = len(document_dicts)
        outputs = []

        # NOTE: can be larger than chunk_size.
        query_gp_size = max(1, self.chunk_size // D)

        # Add grouped document queries such that their sum per iteration is optimal chunk size
        for q_gp in list_chunker(query_dicts, query_gp_size):
            tasks = []
            for q in q_gp:
                tasks.append(self._rerank_one_query(q, document_dicts))

            outputs.extend(await asyncio.gather(*tasks))

        return outputs

    def rerank(
        self,
        query: str | dict[str, Any] | list[str | dict[str, Any]],
        documents: str | list[str | dict[str, Any]],
    ):
        return sync_wrapper_safe(self.arerank, {"query": query, "documents": documents})
