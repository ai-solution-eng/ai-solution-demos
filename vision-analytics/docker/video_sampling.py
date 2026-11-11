"""Sample a video with ffmpeg before it is sent to the vision language model.

The app does the sampling because recent vLLM releases reject per-request media_io_kwargs
unless the server runs with --trust-request-mm-kwargs.
"""

import hashlib
import json
import logging
import os
import shutil
import subprocess
import tempfile
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

FFPROBE_TIMEOUT_SECONDS = 30
FFMPEG_TIMEOUT_SECONDS = 300


def _parse_frame_rate(value: str) -> Optional[float]:
    """Turn an ffprobe frame rate such as "30000/1001" into frames per second, or None if unknown."""
    try:
        numerator, _, denominator = value.partition("/")
        rate = float(numerator) / float(denominator or 1)
    except (ValueError, ZeroDivisionError):
        return None
    return rate if rate > 0 else None


def probe_video(path: str) -> Tuple[Optional[float], Optional[float]]:
    """
    Read a video's duration and frame rate with ffprobe.

    Args:
        path: Path of the video file.

    Returns:
        A tuple of (duration in seconds, frames per second); either is None if ffprobe cannot read it.
    """
    ffprobe_path = shutil.which("ffprobe") or "/usr/bin/ffprobe"
    cmd = [
        ffprobe_path, "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=avg_frame_rate:format=duration",
        "-of", "json",
        path,
    ]
    try:
        result = subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=FFPROBE_TIMEOUT_SECONDS)
        info = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        logger.warning(f"Could not read the duration and frame rate of {path}: {e}")
        return None, None

    try:
        duration: Optional[float] = float(info.get("format", {}).get("duration", ""))
    except ValueError:
        duration = None
    streams = info.get("streams") or [{}]
    frame_rate = _parse_frame_rate(str(streams[0].get("avg_frame_rate", "")))
    return (duration if duration and duration > 0 else None), frame_rate


def plan_frame_sampling(
    duration: Optional[float],
    source_fps: Optional[float],
    num_frames: int,
    fps: float,
    max_duration: float,
) -> Tuple[float, float]:
    """
    Work out how much of a video to keep and at what frame rate.

    The clip is cut to max_duration seconds and sampled at fps, but never faster than the
    source, which would only repeat frames. If that gives more than num_frames frames, the
    frame rate is lowered so that num_frames frames are spread evenly over the whole clip.
    When the duration is unknown, ffmpeg's frame cap applies instead.

    Args:
        duration: The video's duration in seconds, or None if unknown.
        source_fps: The video's own frame rate, or None if unknown.
        num_frames: The most frames to keep.
        fps: The frame rate to sample at.
        max_duration: The most seconds to keep, from the start of the video.

    Returns:
        A tuple of (seconds to keep, frame rate to sample at).
    """
    sample_fps = min(fps, source_fps) if source_fps else fps
    clip_seconds = min(duration, max_duration) if duration else max_duration
    if duration and clip_seconds * sample_fps > num_frames:
        return clip_seconds, num_frames / clip_seconds
    return clip_seconds, sample_fps


def prepare_video_for_model(input_path: str, num_frames: int, fps: float, max_duration: float) -> str:
    """
    Sample a video with ffmpeg so the request carries only the frames the model should see.

    The app samples here because recent vLLM releases (0.31, for one) reject per-request media_io_kwargs unless
    the server runs with --trust-request-mm-kwargs, which a shared endpoint should not. The output
    is H.264 in yuv420p with even dimensions and no audio, which also avoids NVCV INVALID_DATA_TYPE
    errors from GPU video decoders. The model server may still sample fewer frames under its own limits.

    Args:
        input_path: Path of the source video.
        num_frames: The most frames to keep.
        fps: The frame rate to sample at.
        max_duration: The most seconds to keep, from the start of the video.

    Returns:
        The path of the prepared video in the temporary directory (reused while the source
        file and the settings stay the same).

    Raises:
        RuntimeError: If ffmpeg cannot sample the video.
    """
    duration, source_fps = probe_video(input_path)
    clip_seconds, sample_fps = plan_frame_sampling(duration, source_fps, num_frames, fps, max_duration)

    file_stat = os.stat(input_path)
    cache_key = (
        f"{input_path}_{file_stat.st_mtime}_{file_stat.st_size}"
        f"_{num_frames}_{sample_fps:.6f}_{clip_seconds:.3f}"
    )
    path_hash = hashlib.md5(cache_key.encode()).hexdigest()[:12]
    output_path = os.path.join(tempfile.gettempdir(), f"model_input_{path_hash}.mp4")
    if os.path.exists(output_path):
        return output_path

    # ffmpeg writes to a part file first, so a failed run never leaves a broken cached video.
    part_path = os.path.join(tempfile.gettempdir(), f"model_input_{path_hash}.part.mp4")
    ffmpeg_path = shutil.which("ffmpeg") or "/usr/bin/ffmpeg"
    cmd = [
        ffmpeg_path, "-y", "-t", f"{clip_seconds:.3f}", "-i", input_path,
        "-vf", f"fps={sample_fps:.6f},scale=trunc(iw/2)*2:trunc(ih/2)*2",
        "-frames:v", str(num_frames),
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "ultrafast", "-crf", "25",
        "-an", part_path,
    ]
    logger.info(
        f"Sampling {input_path}: first {clip_seconds:.1f}s at {sample_fps:.3f} fps, at most {num_frames} frames"
    )
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=FFMPEG_TIMEOUT_SECONDS)
        os.replace(part_path, output_path)
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(
            f"Sampling the video took longer than {FFMPEG_TIMEOUT_SECONDS} s. "
            "Lower max_duration, or trim the video and try again."
        ) from e
    except subprocess.CalledProcessError as e:
        last_line = (e.stderr or "").strip().splitlines()[-1:] or ["no output"]
        raise RuntimeError(
            f"ffmpeg could not read this video ({last_line[0]}). Convert it to MP4 (H.264) and try again."
        ) from e
    finally:
        if os.path.exists(part_path):
            os.remove(part_path)
    return output_path
