import base64
import json
import subprocess as sp
from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from multimodal_rag.utils.logging_utils import logging

logger = logging.getLogger(__name__)


@dataclass
class VideoProcessor:
    """Split videos into overlapping timed segments, transcode each in memory.

    Parameters
    ----------
    fps:
        Frames per second for the transcoded output (default matches common
        ``mm_processor_kwargs``).
    max_pixels:
        Maximum pixel budget *width × height* per frame.  Output dimensions
        are computed dynamically from the source aspect ratio so the longer
        side gets proportionally more pixels.
    total_pixels:
        Total pixel budget across *all* frames of a segment.  When set (> 0),
        the effective per-frame budget is ``min(max_pixels, total_pixels / (seg_duration * fps))``.
        This matches VLM processor kwargs like ``total_pixels`` (e.g. 5 × 720 × 720),
        ensuring that longer segments don't exceed the model's total pixel capacity.
    segment_seconds:
        Duration in seconds of each output segment.
    overlap_seconds:
        Overlap in seconds between consecutive segments.
    target_frames:
        Target frame count per segment for *short* clips.  When > 0, the
        effective fps for a segment is raised to ``target_frames /
        seg_duration`` (never below ``fps``) so a 3-second clip is sampled
        with ~10 frames instead of being stuck at one frame per second.
        The configured ``fps`` remains a floor, so long segments keep their
        usual cadence.  ``0`` disables the dynamic boost (legacy behaviour).
    """

    fps: float = 1.0
    max_pixels: int = 720 * 720
    total_pixels: int = 0
    segment_seconds: int = 32
    overlap_seconds: int = 4
    target_frames: int = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def process(
        self,
        video_path: str,
        caption: str = "",
        save_dir: str | None = None,
    ) -> list[dict[str, Any]]:
        """Split a video into timed segments, transcode each, return doc dicts.

        Convenience wrapper around :meth:`process_iter` that materialises the
        full list.  Use the iterator directly for long videos to start
        embedding before transcoding finishes.
        """
        return list(self.process_iter(video_path, caption=caption, save_dir=save_dir))

    def process_iter(
        self,
        video_path: str,
        caption: str = "",
        save_dir: str | None = None,
    ) -> Generator[dict[str, Any], None, None]:
        """Generator: yield each video segment as soon as it's transcoded.

        This allows callers to start processing (e.g. embedding) the first
        segments while later segments are still being transcoded by ffmpeg.
        """
        name = Path(video_path).name

        meta = self._probe_metadata(video_path)
        if meta["duration"] <= 0:
            logger.warning("Could not determine duration for %s, skipping", name)
            return

        duration = meta["duration"]
        vw, vh = meta.get("width", 0), meta.get("height", 0)

        stride = self.segment_seconds - self.overlap_seconds
        if stride <= 0:
            stride = self.segment_seconds

        seg_count = 0
        t_start = 0.0
        while t_start < duration:
            remaining = duration - t_start
            if seg_count > 0 and remaining < self.overlap_seconds:
                break
            seg_duration = min(self.segment_seconds, remaining)
            t_end = t_start + seg_duration

            # -- Effective fps for this segment ---------------------------------
            # Short clips get a higher frame rate (toward *target_frames*)
            # so they aren't stuck at a single sample per second.  The
            # configured *fps* is a floor so long segments never drop below it.
            if self.target_frames > 0 and seg_duration > 0:
                eff_fps = max(self.fps, self.target_frames / seg_duration)
            else:
                eff_fps = self.fps
            # Round the effective fps: some ffmpeg builds reject long
            # non-terminating fractional fps values in the mp4 muxer
            # ("Not yet implemented in FFmpeg, patches welcome",
            # AVERROR_PATCHWELCOME), failing the whole segment transcode.
            eff_fps = round(eff_fps, 4)

            # -- Compute effective per-frame pixel budget for this segment ------
            if self.total_pixels > 0 and eff_fps > 0:
                num_frames = max(1, round(seg_duration * eff_fps))
                effective_max = min(self.max_pixels, self.total_pixels // num_frames)
            else:
                effective_max = self.max_pixels

            # -- Build scale filter from source dimensions ----------------------
            if vw > 0 and vh > 0 and vw * vh > effective_max:
                scale = (effective_max / (vw * vh)) ** 0.5
                new_w = max(2, (int(vw * scale) // 2) * 2)
                new_h = max(2, (int(vh * scale) // 2) * 2)
                scale_filter = f"scale={new_w}:{new_h}"
            else:
                scale_filter = ""

            mp4_bytes = self._transcode_segment(
                video_path,
                t_start,
                seg_duration,
                eff_fps,
                scale_filter,
            )
            if not mp4_bytes:
                t_start += stride
                continue

            if save_dir:
                save_path = Path(save_dir) / f"{Path(video_path).stem}_seg{seg_count}.mp4"
                save_path.parent.mkdir(parents=True, exist_ok=True)
                save_path.write_bytes(mp4_bytes)

            b64 = base64.b64encode(mp4_bytes).decode("utf-8")
            ts_text = f"[{t_start:.0f}s – {t_end:.0f}s]"
            doc_text = f"{caption or f'[Video: {name}]'} {ts_text}"
            yield {
                "text": doc_text,
                "video": f"data:video/mp4;base64,{b64}",
                "source": video_path,
                "timestamp_start": round(t_start, 1),
                "timestamp_end": round(t_end, 1),
            }

            seg_count += 1
            t_start += stride

        dyn = f", target {self.target_frames} frames/seg" if self.target_frames > 0 else ""
        logger.info(
            "%s: %d segments (%.0fs, %ds window, %ds overlap @ %g fps%s)",
            name,
            seg_count,
            duration,
            self.segment_seconds,
            self.overlap_seconds,
            self.fps,
            dyn,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _probe_metadata(path: str) -> dict[str, float | int]:
        """Return duration (s), width, height for a video using ffprobe."""
        try:
            proc = sp.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-show_format",
                    "-show_streams",
                    "-of",
                    "json",
                    path,
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            info = json.loads(proc.stdout)
            duration = float(info.get("format", {}).get("duration", 0))
            vw = vh = 0
            for s in info.get("streams", []):
                if s.get("codec_type") == "video":
                    vw = int(s.get("width", 0))
                    vh = int(s.get("height", 0))
                    break
            return {"duration": duration, "width": vw, "height": vh}
        except Exception as exc:
            logger.warning("ffprobe failed for %s: %s", path, exc)
            return {"duration": 0.0, "width": 0, "height": 0}

    @staticmethod
    def _transcode_segment(
        input_path: str,
        start: float,
        duration: float,
        fps: float,
        scale_filter: str,
    ) -> bytes:
        """Extract and transcode a segment from *input_path* to mp4 bytes.

        All processing happens in memory via pipe — no temporary files.
        If *scale_filter* is empty, no video filtering is applied.
        """
        cmd = [
            "ffmpeg",
            "-v",
            "error",
            "-ss",
            str(start),
            "-t",
            str(duration),
            "-i",
            input_path,
            # Keep the audio track: caption_with_asr transcribes the video
            # segment via the ASR model, and a silent (audio-less) segment
            # fails ASR with "Invalid or unsupported audio file".  The
            # fractional-fps muxer failure is handled by rounding *fps* in
            # process_iter(); normalise negative timestamps defensively.
            "-avoid_negative_ts",
            "make_zero",
        ]
        if scale_filter:
            cmd += ["-vf", f"fps={fps},{scale_filter}"]
        else:
            cmd += ["-vf", f"fps={fps}"]
        cmd += [
            "-f",
            "mp4",
            "-movflags",
            "frag_keyframe+empty_moov",
            "-vcodec",
            "libx264",
            "-preset",
            "fast",
            "-crf",
            "28",
            "-",
        ]
        logger.verbose(  # type: ignore[attr-defined]
            "ffmpeg %s", " ".join(str(a) if " " not in a else f"'{a}'" for a in cmd)
        )  # type: ignore[attr-defined]
        try:
            proc = sp.run(cmd, capture_output=True, timeout=300)
            if proc.returncode != 0:
                logger.warning(
                    "ffmpeg transcode failed at %.1fs (exit %d): %s",
                    start,
                    proc.returncode,
                    proc.stderr.decode(errors="replace")[:500],
                )
                return b""
            return proc.stdout
        except Exception as exc:
            logger.warning("ffmpeg transcode exception at %.1fs: %s", start, exc)
            return b""
