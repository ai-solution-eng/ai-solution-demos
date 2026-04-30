"""Video Service for handling video streaming, uploads, and frame extraction."""

import asyncio
import base64
import os
import uuid
from typing import Any, AsyncGenerator, Dict, List, Optional

import cv2
import structlog
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse

from common.logging_config import setup_logging

# Configure structured logging
setup_logging("video-service")
logger = structlog.get_logger(__name__)

app = FastAPI(title="Video Service")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

ASSETS_DIR = "/app/assets/videos"
CUSTOM_UPLOAD_DIR = "/app/data/videos"

os.makedirs(ASSETS_DIR, exist_ok=True)
os.makedirs(CUSTOM_UPLOAD_DIR, exist_ok=True)


@app.get("/health")
async def health_check() -> Dict[str, str]:
    """Health check endpoint.
    
    Returns:
        A dictionary indicating the health status.
    """
    return {"status": "healthy"}


@app.get("/api/v1/video/list")
async def list_videos() -> Dict[str, Any]:
    """Lists all available baked-in videos and any custom uploaded videos.
    
    Returns:
        A list of video objects with metadata.
    """
    videos: List[Dict[str, str]] = []

    # Baked-in videos
    if os.path.exists(ASSETS_DIR):
        for f in os.listdir(ASSETS_DIR):
            if f.endswith((".mp4", ".mov")):
                videos.append(
                    {"filename": f, "source": "baked", "url": f"/api/v1/video/stream/{f}"}
                )

    # Custom uploaded video (taking only the most recent)
    if os.path.exists(CUSTOM_UPLOAD_DIR):
        custom_files = [f for f in os.listdir(CUSTOM_UPLOAD_DIR) if f.endswith((".mp4", ".mov"))]
        if custom_files:
            latest_custom = sorted(
                custom_files,
                key=lambda x: os.path.getmtime(os.path.join(CUSTOM_UPLOAD_DIR, x)),
                reverse=True,
            )[0]
            videos.append(
                {
                    "filename": latest_custom,
                    "source": "custom",
                    "url": f"/api/v1/video/stream/{latest_custom}",
                }
            )

    return {"status": "success", "data": videos}


@app.post("/api/v1/video/upload")
async def upload_video(file: UploadFile = File(...)) -> Dict[str, Any]:
    """Uploads a single custom video, replacing the prior custom video.
    
    Args:
        file: The uploaded video file.
        
    Returns:
        A success message and the filename of the uploaded video.
        
    Raises:
        HTTPException: If the file format is not supported.
    """
    if not file.filename:
        raise HTTPException(status_code=400, detail="No filename provided")

    if not file.filename.endswith((".mp4", ".mov")):
        raise HTTPException(status_code=400, detail="Only .mp4 and .mov files are allowed")

    # Clear existing custom videos to only keep one
    for ex_f in os.listdir(CUSTOM_UPLOAD_DIR):
        try:
            os.remove(os.path.join(CUSTOM_UPLOAD_DIR, ex_f))
        except Exception as e:
            logger.warning("failed_to_remove_old_video", filename=ex_f, error=str(e))

    ext = os.path.splitext(file.filename)[1]
    safe_filename = f"custom_upload_{uuid.uuid4().hex[:8]}{ext}"
    filepath = os.path.join(CUSTOM_UPLOAD_DIR, safe_filename)

    content = await file.read()
    with open(filepath, "wb") as buffer:
        buffer.write(content)

    logger.info("custom_video_uploaded", filename=safe_filename)
    return {
        "status": "success",
        "message": "Video uploaded successfully",
        "data": {"filename": safe_filename},
    }


async def generate_mjpeg_stream(file_path: str) -> AsyncGenerator[bytes, None]:
    """Async Generator for MJPEG stream from video file using threadpool for blocking CV2 ops.
    
    Args:
        file_path: Path to the video file.
        
    Yields:
        JPEG frames as bytes for MJPEG streaming.
        
    Raises:
        Exception: If the video cannot be opened.
    """
    # Open cap in threadpool as it can be blocking
    cap = await asyncio.to_thread(cv2.VideoCapture, file_path)
    if not cap.isOpened():
        raise Exception("Failed to open video")

    try:
        while True:
            # Run blocking read in thread
            ret, frame = await asyncio.to_thread(cap.read)
            if not ret:
                # Loop video
                await asyncio.to_thread(cap.set, cv2.CAP_PROP_POS_FRAMES, 0)
                continue

            # Resize for bandwidth efficiency - dashboard grid doesn't need full HD
            # Target 640px width which is plenty for the 2x2 grid
            h, w = frame.shape[:2]
            if w > 640:
                aspect = h / w
                new_w = 640
                new_h = int(new_w * aspect)
                frame = await asyncio.to_thread(cv2.resize, frame, (new_w, new_h))

            # Run blocking encode in thread - use slightly lower quality for bandwidth efficiency
            # Quality 55 is a good sweet spot for demo visuals over limited bandwidth
            ret, buffer = await asyncio.to_thread(
                cv2.imencode, ".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 55]
            )
            if not ret:
                await asyncio.sleep(0.05)
                continue

            frame_bytes = buffer.tobytes()
            yield (
                b"--frame\r\n" b"Content-Type: image/jpeg\r\n\r\n" + frame_bytes + b"\r\n"
            )

            # Target ~20 FPS for better balance between smoothness and bandwidth
            # 0.05s = 20 FPS
            await asyncio.sleep(0.05)
    finally:
        await asyncio.to_thread(cap.release)


async def _extract_frames(
    filename: str, count: int = 1, interval: int = 30, resize: Optional[tuple] = None
) -> List[Any]:
    """Internal helper to extract frames from a video file.
    
    Args:
        filename: Name of the video file.
        count: Number of frames to extract.
        interval: Number of frames to skip between extractions.
        resize: Optional tuple (width, height) to resize frames.
        
    Returns:
        A list of OpenCV frames.
    """
    file_path = None
    if os.path.exists(os.path.join(CUSTOM_UPLOAD_DIR, filename)):
        file_path = os.path.join(CUSTOM_UPLOAD_DIR, filename)
    elif os.path.exists(os.path.join(ASSETS_DIR, filename)):
        file_path = os.path.join(ASSETS_DIR, filename)

    if not file_path:
        logger.warning("video_not_found", filename=filename)
        return []

    cap = await asyncio.to_thread(cv2.VideoCapture, file_path)
    if not cap.isOpened():
        logger.warning("failed_to_open_video", filename=filename)
        return []

    frames = []
    extracted = 0
    try:
        while extracted < count:
            ret, frame = await asyncio.to_thread(cap.read)
            if not ret:
                # Wrap around if EOF reached
                await asyncio.to_thread(cap.set, cv2.CAP_PROP_POS_FRAMES, 0)
                ret, frame = await asyncio.to_thread(cap.read)
                if not ret:
                    break

            if resize:
                frame = await asyncio.to_thread(cv2.resize, frame, resize)

            frames.append(frame)
            extracted += 1

            if extracted < count and interval > 1:
                # Fast forward by skipping frames
                current = await asyncio.to_thread(cap.get, cv2.CAP_PROP_POS_FRAMES)
                await asyncio.to_thread(cap.set, cv2.CAP_PROP_POS_FRAMES, current + interval - 1)
    finally:
        await asyncio.to_thread(cap.release)

    return frames


@app.get("/api/v1/video/stream/{filename}")
async def stream_video(filename: str) -> StreamingResponse:
    """Streams the video as MJPEG.
    
    Args:
        filename: Name of the video file to stream.
        
    Returns:
        A StreamingResponse with the MJPEG stream.
        
    Raises:
        HTTPException: If the video file is not found.
    """
    file_path = None
    if os.path.exists(os.path.join(CUSTOM_UPLOAD_DIR, filename)):
        file_path = os.path.join(CUSTOM_UPLOAD_DIR, filename)
    elif os.path.exists(os.path.join(ASSETS_DIR, filename)):
        file_path = os.path.join(ASSETS_DIR, filename)

    if not file_path:
        raise HTTPException(status_code=404, detail="Video not found")

    return StreamingResponse(
        generate_mjpeg_stream(file_path), media_type="multipart/x-mixed-replace; boundary=frame"
    )


@app.get("/api/v1/video/raw/{filename}")
async def get_raw_video(filename: str) -> FileResponse:
    """Returns the raw video file for browser-native playback (MP4/MOV).
    
    Args:
        filename: Name of the video file.
        
    Returns:
        A FileResponse with the raw video file.
        
    Raises:
        HTTPException: If the video file is not found.
    """
    file_path = None
    if os.path.exists(os.path.join(CUSTOM_UPLOAD_DIR, filename)):
        file_path = os.path.join(CUSTOM_UPLOAD_DIR, filename)
    elif os.path.exists(os.path.join(ASSETS_DIR, filename)):
        file_path = os.path.join(ASSETS_DIR, filename)

    if not file_path:
        raise HTTPException(status_code=404, detail="Video not found")

    return FileResponse(file_path)


@app.get("/api/v1/video/frame/{filename}")
async def get_frame(filename: str) -> Dict[str, Any]:
    """Returns the first frame of the video as base64 JPEG.
    
    Args:
        filename: Name of the video file.
        
    Returns:
        A dictionary containing the base64 encoded frame.
        
    Raises:
        HTTPException: If the video or frame cannot be read.
    """
    frames = await _extract_frames(filename, count=1)
    if not frames:
        raise HTTPException(status_code=404, detail="Could not extract frame or video not found")

    ret, buffer = await asyncio.to_thread(cv2.imencode, ".jpg", frames[0])
    if not ret:
        raise HTTPException(status_code=500, detail="Failed to encode frame")

    b64_str = base64.b64encode(buffer).decode("utf-8")
    return {"status": "success", "data": {"image_b64": f"data:image/jpeg;base64,{b64_str}"}}


@app.get("/api/v1/video/frames/{filename}")
async def get_frames(filename: str, count: int = 5, interval: int = 30) -> Dict[str, Any]:
    """Returns multiple frames of the video as base64 JPEGs for sequence context.
    
    Args:
        filename: Name of the video file.
        count: Number of frames to extract.
        interval: Number of frames to skip between extractions.
        
    Returns:
        A dictionary containing a list of base64 encoded frames.
        
    Raises:
        HTTPException: If frames could not be extracted.
    """
    frames = await _extract_frames(filename, count=count, interval=interval)
    if not frames:
        raise HTTPException(status_code=404, detail="Could not extract frames")

    all_b64: List[str] = []
    for frame in frames:
        ret, buffer = await asyncio.to_thread(
            cv2.imencode, ".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 60]
        )
        if ret:
            b64_str = base64.b64encode(buffer).decode("utf-8")
            all_b64.append(f"data:image/jpeg;base64,{b64_str}")

    return {"status": "success", "data": {"images_b64": all_b64}}


@app.get("/api/v1/video/combined-context")
async def get_combined_context(vids: str, count: int = 8, interval: int = 15) -> Dict[str, Any]:
    """Returns a single base64 string for an MP4 video containing frames from multiple videos.
    
    Used for creating a temporary context video for VLM models.
    
    Args:
        vids: Comma-separated list of filenames.
        count: Frames per video to extract.
        interval: Frame skip interval.
        
    Returns:
        A dictionary containing the base64 encoded MP4 video.
        
    Raises:
        HTTPException: If no videos are specified or no frames could be extracted.
    """
    import tempfile

    video_list = [v.strip() for v in vids.split(",") if v.strip()]
    if not video_list:
        raise HTTPException(status_code=400, detail="No videos specified")

    all_frames = []
    for filename in video_list:
        v_frames = await _extract_frames(filename, count=count, interval=interval, resize=(512, 512))
        all_frames.extend(v_frames)

    if not all_frames:
        raise HTTPException(
            status_code=404, detail="No frames could be extracted from provided videos"
        )

    # Create temporary video file
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        # Target 2 FPS as per user model specification
        out = await asyncio.to_thread(cv2.VideoWriter, tmp_path, fourcc, 2.0, (512, 512))
        for frame in all_frames:
            await asyncio.to_thread(out.write, frame)
        await asyncio.to_thread(out.release)

        with open(tmp_path, "rb") as f:
            video_bytes = f.read()

        b64_str = base64.b64encode(video_bytes).decode("utf-8")
        return {"status": "success", "data": {"video_b64": f"data:video/mp4;base64,{b64_str}"}}
    except Exception as e:
        logger.error("combined_video_generation_failed", error=str(e))
        raise HTTPException(status_code=500, detail=f"Failed to generate video context: {str(e)}")
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass


@app.get("/api/v1/video/combined-frames")
async def get_combined_frames(vids: str, count: int = 2, interval: int = 30) -> Dict[str, Any]:
    """Returns a list of base64 images from multiple videos.
    
    Args:
        vids: Comma-separated list of filenames.
        count: Frames per video to extract.
        interval: Frame skip interval.
        
    Returns:
        A dictionary containing a list of base64 encoded images.
        
    Raises:
        HTTPException: If no videos are specified.
    """
    video_list = [v.strip() for v in vids.split(",") if v.strip()]
    if not video_list:
        raise HTTPException(status_code=400, detail="No videos specified")

    all_frames_b64: List[str] = []
    for filename in video_list:
        v_frames = await _extract_frames(filename, count=count, interval=interval)
        for frame in v_frames:
            ret, buffer = await asyncio.to_thread(
                cv2.imencode, ".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 60]
            )
            if ret:
                b64_str = base64.b64encode(buffer).decode("utf-8")
                all_frames_b64.append(f"data:image/jpeg;base64,{b64_str}")

    return {"status": "success", "data": {"images_b64": all_frames_b64}}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8000,
        reload=True if os.environ.get("ENV") == "development" else False,
    )


