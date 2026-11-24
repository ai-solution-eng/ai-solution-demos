"""Tests for video_sampling. The ffmpeg tests run only where ffmpeg is installed (the app image has it)."""

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from video_sampling import _parse_frame_rate, plan_frame_sampling, prepare_video_for_model, probe_video

HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
needs_ffmpeg = pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg and ffprobe are not installed")


@pytest.mark.parametrize(
    ("value", "expected"),
    [("30000/1001", 29.97), ("25/1", 25.0), ("24", 24.0), ("0/0", None), ("0/1", None), ("", None), ("n/a", None)],
)
def test_parse_frame_rate(value: str, expected: float | None) -> None:
    result = _parse_frame_rate(value)
    if expected is None:
        assert result is None
    else:
        assert result == pytest.approx(expected, abs=0.01)


@pytest.mark.parametrize(
    ("duration", "source_fps", "num_frames", "fps", "max_duration", "expected"),
    [
        # Short clip, well under the frame cap: keep all of it at the requested rate.
        (6.0, 29.97, 64, 1.0, 600.0, (6.0, 1.0)),
        # Asking for more than the source rate would only repeat frames.
        (6.0, 30.0, 512, 60.0, 600.0, (6.0, 30.0)),
        # Too many frames at 2 fps: spread 64 frames over the 120 s clip instead.
        (120.0, 30.0, 64, 2.0, 600.0, (120.0, 64 / 120)),
        # max_duration cuts the clip before the frame cap is worked out.
        (1000.0, 30.0, 512, 1.0, 100.0, (100.0, 1.0)),
        (1000.0, 30.0, 512, 1.0, 600.0, (600.0, 512 / 600)),
        # Unknown duration: keep the requested rate and let ffmpeg's frame cap apply.
        (None, None, 64, 2.0, 600.0, (600.0, 2.0)),
        # Unknown source rate: nothing to cap against.
        (10.0, None, 64, 2.0, 600.0, (10.0, 2.0)),
    ],
)
def test_plan_frame_sampling(
    duration: float | None,
    source_fps: float | None,
    num_frames: int,
    fps: float,
    max_duration: float,
    expected: tuple[float, float],
) -> None:
    clip_seconds, sample_fps = plan_frame_sampling(duration, source_fps, num_frames, fps, max_duration)
    assert clip_seconds == pytest.approx(expected[0])
    assert sample_fps == pytest.approx(expected[1])


def _make_video(path: Path, seconds: int = 4, size: str = "321x241", rate: int = 30) -> None:
    # yuv444p, because yuv420p cannot hold the odd size that the sampling must even out.
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"testsrc=duration={seconds}:size={size}:rate={rate}",
            "-c:v", "libx264", "-pix_fmt", "yuv444p", str(path),
        ],
        check=True,
    )


def _frames_and_size(path: Path) -> tuple[int, int, int]:
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames",
            "-show_entries", "stream=nb_read_frames,width,height", "-of", "json", str(path),
        ],
        check=True, capture_output=True, text=True,
    )
    stream = json.loads(result.stdout)["streams"][0]
    return int(stream["nb_read_frames"]), int(stream["width"]), int(stream["height"])


@pytest.fixture
def isolated_tempdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point tempfile.gettempdir() at a fresh folder so cached outputs do not leak between tests."""
    out = tmp_path / "out"
    out.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(out))
    return out


@needs_ffmpeg
def test_probe_video(tmp_path: Path) -> None:
    source = tmp_path / "source.mp4"
    _make_video(source)
    duration, frame_rate = probe_video(str(source))
    assert duration == pytest.approx(4.0, abs=0.1)
    assert frame_rate == pytest.approx(30.0)


@needs_ffmpeg
def test_prepare_video_caps_frames_and_evens_size(tmp_path: Path, isolated_tempdir: Path) -> None:
    source = tmp_path / "source.mp4"
    _make_video(source)

    # 4 s at 2 fps would be 8 frames; the cap of 6 spreads them over the clip instead.
    output = Path(prepare_video_for_model(str(source), num_frames=6, fps=2.0, max_duration=600.0))

    assert output.parent == isolated_tempdir
    frames, width, height = _frames_and_size(output)
    assert frames == 6
    assert (width, height) == (320, 240)
    # The same source and settings reuse the cached file.
    assert prepare_video_for_model(str(source), num_frames=6, fps=2.0, max_duration=600.0) == str(output)
    assert not list(isolated_tempdir.glob("*.part.mp4"))


@needs_ffmpeg
def test_prepare_video_reports_unreadable_file(tmp_path: Path, isolated_tempdir: Path) -> None:
    broken = tmp_path / "broken.mp4"
    broken.write_bytes(b"this is not a video")

    with pytest.raises(RuntimeError, match="ffmpeg could not read this video"):
        prepare_video_for_model(str(broken), num_frames=8, fps=1.0, max_duration=60.0)
    assert not list(isolated_tempdir.iterdir())
