import io
import os
import subprocess
import tempfile
import wave
from typing import Any

import numpy as np

try:
    import torch
except ModuleNotFoundError:
    torch = None

from app.config import SAMPLE_RATE


def pcm16le_bytes_to_float_tensor(data: bytes) -> Any:
    if torch is None:
        raise RuntimeError("torch is required for live VAD processing")
    pcm = np.frombuffer(data, dtype=np.int16)
    if pcm.size == 0:
        return torch.empty(0, dtype=torch.float32)
    return torch.from_numpy(pcm.astype(np.float32) / 32768.0)


def wav_bytesio_from_pcm16(pcm16: np.ndarray, sr: int = SAMPLE_RATE) -> io.BytesIO:
    if pcm16.dtype != np.int16:
        pcm16 = pcm16.astype(np.int16)

    buf = io.BytesIO()
    buf.name = "audio.wav"
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(pcm16.tobytes())
    buf.seek(0)
    return buf


def rms_dbfs(pcm16: np.ndarray) -> float:
    x = pcm16.astype(np.float32) / 32768.0
    rms = np.sqrt(np.mean(x * x) + 1e-12)
    return 20 * np.log10(rms + 1e-12)


def ffmpeg_convert_to_pcm_wav(input_bytes: bytes, suffix: str) -> tuple[np.ndarray, float]:
    with tempfile.TemporaryDirectory() as tmpdir:
        input_path = os.path.join(tmpdir, f"input{suffix or '.bin'}")
        output_path = os.path.join(tmpdir, "normalized.wav")
        with open(input_path, "wb") as f:
            f.write(input_bytes)

        cmd = [
            "ffmpeg",
            "-y",
            "-i",
            input_path,
            "-ac",
            "1",
            "-ar",
            str(SAMPLE_RATE),
            "-f",
            "wav",
            output_path,
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError((proc.stderr or proc.stdout or "ffmpeg failed").strip())

        with wave.open(output_path, "rb") as wf:
            frames = wf.readframes(wf.getnframes())
            sr = wf.getframerate()
            if sr != SAMPLE_RATE:
                raise RuntimeError(f"Unexpected sample rate after conversion: {sr}")
            samples = np.frombuffer(frames, dtype=np.int16).copy()
        duration_s = len(samples) / float(SAMPLE_RATE) if len(samples) else 0.0
        return samples, duration_s
