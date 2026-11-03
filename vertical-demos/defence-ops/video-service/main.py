"""Video Service for handling video streaming, uploads, and frame extraction."""

import asyncio
import base64
import os
import tempfile
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable
from pathlib import Path
from typing import Annotated, Any

import cv2
import structlog
from fastapi import FastAPI, File, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from common.logging_config import setup_logging

# Configure structured logging
setup_logging("video-service")
logger = structlog.get_logger(__name__)

app = FastAPI(title="Video Service")

VIDEO_SUFFIXES = (".mp4", ".mov")
ASSETS_DIR = Path("/app/assets/videos")
UPLOAD_DIR = Path(os.environ.get("UPLOAD_DIR", "/app/data/videos"))
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "200"))
MAX_FEEDS = 10

UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

FrameCount = Annotated[int, Query(ge=1, le=16)]
FrameInterval = Annotated[int, Query(ge=1, le=300)]


def _resolve_video(filename: str) -> Path | None:
    """Maps a bare file name to an uploaded or baked-in video; anything else is not found."""
    if Path(filename).name != filename or not filename.lower().endswith(VIDEO_SUFFIXES):
        return None
    for directory in (UPLOAD_DIR, ASSETS_DIR):
        candidate = directory / filename
        if candidate.is_file():
            return candidate
    return None


def _video_names(vids: str) -> list[str]:
    names = [v.strip() for v in vids.split(",") if v.strip()]
    if not names:
        raise HTTPException(status_code=400, detail="No videos specified")
    if len(names) > MAX_FEEDS:
        raise HTTPException(status_code=400, detail=f"At most {MAX_FEEDS} videos per request")
    return names


@app.get("/health")
async def health_check() -> dict[str, str]:
    """Health check endpoint."""
    return {"status": "healthy"}


@app.get("/api/v1/video/list")
async def list_videos() -> dict[str, Any]:
    """Lists all available baked-in videos and the most recent custom upload.

    Returns:
        A list of video objects with metadata.
    """
    videos: list[dict[str, str]] = [
        {"filename": f.name, "source": "baked", "url": f"/api/v1/video/stream/{f.name}"}
        for f in sorted(ASSETS_DIR.iterdir())
        if f.suffix.lower() in VIDEO_SUFFIXES
    ]
    uploads = sorted(
        (f for f in UPLOAD_DIR.iterdir() if f.suffix.lower() in VIDEO_SUFFIXES),
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )
    if uploads:
        latest = uploads[0].name
        videos.append(
            {"filename": latest, "source": "custom", "url": f"/api/v1/video/stream/{latest}"}
        )
    return {"status": "success", "data": videos}


UPLOAD_TOO_LARGE = f"Videos are limited to {MAX_UPLOAD_MB} MB; trim or compress it."


@app.middleware("http")
async def refuse_oversized_uploads(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Refuses an upload by its Content-Length before the multipart body is read."""
    if request.url.path == "/api/v1/video/upload":
        declared = request.headers.get("content-length", "0")
        if declared.isdigit() and int(declared) > (MAX_UPLOAD_MB + 1) * 1024 * 1024:
            return JSONResponse(status_code=413, content={"detail": UPLOAD_TOO_LARGE})
    return await call_next(request)


@app.post("/api/v1/video/upload")
async def upload_video(file: Annotated[UploadFile, File()]) -> dict[str, Any]:
    """Uploads a single custom video, replacing the prior custom video.

    The upload is written in 1 MiB chunks and refused above MAX_UPLOAD_MB.

    Args:
        file: The uploaded video file.

    Returns:
        A success message and the filename of the uploaded video.

    Raises:
        HTTPException: If the file is not a video or is too large.
    """
    max_bytes = MAX_UPLOAD_MB * 1024 * 1024
    if not file.filename or not file.filename.lower().endswith(VIDEO_SUFFIXES):
        raise HTTPException(status_code=400, detail="Only .mp4 and .mov files are allowed")

    suffix = Path(file.filename).suffix.lower()
    target = UPLOAD_DIR / f"custom_upload_{uuid.uuid4().hex[:8]}{suffix}"
    partial = target.with_name(target.name + ".part")
    written = 0
    try:
        with partial.open("wb") as out:
            while chunk := await file.read(1024 * 1024):
                written += len(chunk)
                if written > max_bytes:
                    raise HTTPException(status_code=413, detail=UPLOAD_TOO_LARGE)
                await asyncio.to_thread(out.write, chunk)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise

    # Keep only one custom video
    for old in UPLOAD_DIR.iterdir():
        if old != partial:
            old.unlink(missing_ok=True)
    partial.rename(target)

    logger.info("custom_video_uploaded", filename=target.name, bytes=written)
    return {
        "status": "success",
        "message": "Video uploaded successfully",
        "data": {"filename": target.name},
    }


async def generate_mjpeg_stream(file_path: Path) -> AsyncGenerator[bytes, None]:
    """Async Generator for MJPEG stream from video file using threadpool for blocking CV2 ops.

    Args:
        file_path: Path to the video file.

    Yields:
        JPEG frames as bytes for MJPEG streaming.

    Raises:
        RuntimeError: If the video cannot be opened.
    """
    cap = await asyncio.to_thread(cv2.VideoCapture, str(file_path))
    if not cap.isOpened():
        raise RuntimeError("Failed to open video")

    try:
        while True:
            ret, frame = await asyncio.to_thread(cap.read)
            if not ret:
                # Loop video
                await asyncio.to_thread(cap.set, cv2.CAP_PROP_POS_FRAMES, 0)
                continue

            # The 2x2 dashboard grid needs no more than 640 px width
            h, w = frame.shape[:2]
            if w > 640:
                frame = await asyncio.to_thread(cv2.resize, frame, (640, int(640 * h / w)))

            # Quality 55 balances demo visuals and bandwidth
            ret, buffer = await asyncio.to_thread(
                cv2.imencode, ".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 55]
            )
            if not ret:
                await asyncio.sleep(0.05)
                continue

            yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + buffer.tobytes() + b"\r\n"
            await asyncio.sleep(0.05)  # ~20 FPS
    finally:
        await asyncio.to_thread(cap.release)


async def _extract_frames(
    filename: str, count: int = 1, interval: int = 30, resize: tuple[int, int] | None = None
) -> list[Any]:
    """Extracts frames from a video, wrapping round at the end.

    Args:
        filename: Name of the video file.
        count: Number of frames to extract.
        interval: Number of frames to skip between extractions.
        resize: Optional (width, height) to resize frames to.

    Returns:
        A list of OpenCV frames; empty if the video is not found or unreadable.
    """
    file_path = _resolve_video(filename)
    if not file_path:
        logger.warning("video_not_found", filename=filename)
        return []

    cap = await asyncio.to_thread(cv2.VideoCapture, str(file_path))
    if not cap.isOpened():
        logger.warning("failed_to_open_video", filename=filename)
        return []

    frames: list[Any] = []
    try:
        while len(frames) < count:
            ret, frame = await asyncio.to_thread(cap.read)
            if not ret:
                await asyncio.to_thread(cap.set, cv2.CAP_PROP_POS_FRAMES, 0)
                ret, frame = await asyncio.to_thread(cap.read)
                if not ret:
                    break
            if resize:
                frame = await asyncio.to_thread(cv2.resize, frame, resize)
            frames.append(frame)
            if len(frames) < count and interval > 1:
                current = await asyncio.to_thread(cap.get, cv2.CAP_PROP_POS_FRAMES)
                await asyncio.to_thread(cap.set, cv2.CAP_PROP_POS_FRAMES, current + interval - 1)
    finally:
        await asyncio.to_thread(cap.release)
    return frames


async def _jpeg_data_urls(frames: list[Any]) -> list[str]:
    urls: list[str] = []
    for frame in frames:
        ret, buffer = await asyncio.to_thread(
            cv2.imencode, ".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 60]
        )
        if ret:
            urls.append(f"data:image/jpeg;base64,{base64.b64encode(buffer).decode('ascii')}")
    return urls


@app.get("/api/v1/video/stream/{filename}")
async def stream_video(filename: str) -> StreamingResponse:
    """Streams the video as MJPEG.

    Raises:
        HTTPException: If the video file is not found.
    """
    file_path = _resolve_video(filename)
    if not file_path:
        raise HTTPException(status_code=404, detail="Video not found")
    return StreamingResponse(
        generate_mjpeg_stream(file_path), media_type="multipart/x-mixed-replace; boundary=frame"
    )


@app.get("/api/v1/video/raw/{filename}")
async def get_raw_video(filename: str) -> FileResponse:
    """Returns the raw video file for browser-native playback (MP4/MOV).

    Raises:
        HTTPException: If the video file is not found.
    """
    file_path = _resolve_video(filename)
    if not file_path:
        raise HTTPException(status_code=404, detail="Video not found")
    return FileResponse(file_path)


@app.get("/api/v1/video/frame/{filename}")
async def get_frame(filename: str) -> dict[str, Any]:
    """Returns the first frame of the video as base64 JPEG.

    Raises:
        HTTPException: If the video or frame cannot be read.
    """
    frames = await _extract_frames(filename, count=1)
    if not frames:
        raise HTTPException(status_code=404, detail="Could not extract frame or video not found")
    ret, buffer = await asyncio.to_thread(cv2.imencode, ".jpg", frames[0])
    if not ret:
        raise HTTPException(status_code=500, detail="Failed to encode frame")
    b64_str = base64.b64encode(buffer).decode("ascii")
    return {"status": "success", "data": {"image_b64": f"data:image/jpeg;base64,{b64_str}"}}


@app.get("/api/v1/video/frames/{filename}")
async def get_frames(
    filename: str, count: FrameCount = 5, interval: FrameInterval = 30
) -> dict[str, Any]:
    """Returns multiple frames of the video as base64 JPEGs for sequence context.

    Raises:
        HTTPException: If frames could not be extracted.
    """
    frames = await _extract_frames(filename, count=count, interval=interval)
    if not frames:
        raise HTTPException(status_code=404, detail="Could not extract frames")
    return {"status": "success", "data": {"images_b64": await _jpeg_data_urls(frames)}}


@app.get("/api/v1/video/combined-context")
async def get_combined_context(
    vids: str, count: FrameCount = 8, interval: FrameInterval = 15
) -> dict[str, Any]:
    """Builds one 2 fps Motion-JPEG AVI from frames of several videos, for video-capable models.

    Motion JPEG because vLLM (Nemotron VL on MLIS) rejects OpenCV's mp4v MP4 as "Invalid or
    unsupported video file" and accepts MJPEG AVI (tested 2026-11-02); OpenCV's pip wheels
    cannot write H.264. Takes about 1 s per video (0.5-1.7 s for one, 8.6 s for ten).

    Args:
        vids: Comma-separated list of filenames.
        count: Frames per video to extract.
        interval: Frame skip interval.

    Returns:
        The clip as a data URL.

    Raises:
        HTTPException: If no videos are specified or no frames could be extracted.
    """
    all_frames: list[Any] = []
    for filename in _video_names(vids):
        all_frames.extend(
            await _extract_frames(filename, count=count, interval=interval, resize=(512, 512))
        )
    if not all_frames:
        raise HTTPException(
            status_code=404, detail="No frames could be extracted from provided videos"
        )

    with tempfile.NamedTemporaryFile(suffix=".avi", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        fourcc = cv2.VideoWriter.fourcc(*"MJPG")
        out = await asyncio.to_thread(cv2.VideoWriter, str(tmp_path), fourcc, 2.0, (512, 512))
        for frame in all_frames:
            await asyncio.to_thread(out.write, frame)
        await asyncio.to_thread(out.release)
        video_bytes = await asyncio.to_thread(tmp_path.read_bytes)
        b64_str = base64.b64encode(video_bytes).decode("ascii")
        clip = f"data:video/x-msvideo;base64,{b64_str}"
        return {"status": "success", "data": {"video_b64": clip}}
    except Exception as e:
        logger.error("combined_video_generation_failed", error=str(e))
        raise HTTPException(status_code=500, detail=f"Failed to generate video context: {e}") from e
    finally:
        tmp_path.unlink(missing_ok=True)


@app.get("/api/v1/video/combined-frames")
async def get_combined_frames(
    vids: str, count: FrameCount = 2, interval: FrameInterval = 30
) -> dict[str, Any]:
    """Returns still frames from several videos, for models that take images but not video.

    Args:
        vids: Comma-separated list of filenames.
        count: Frames per video to extract.
        interval: Frame skip interval.

    Returns:
        A dictionary containing a list of base64 encoded images.
    """
    all_frames: list[Any] = []
    for filename in _video_names(vids):
        all_frames.extend(await _extract_frames(filename, count=count, interval=interval))
    return {"status": "success", "data": {"images_b64": await _jpeg_data_urls(all_frames)}}
