from pathlib import Path
from typing import Any

import numpy as np

try:
    import torch
except ModuleNotFoundError:
    torch = None

from app.config import (
    EXPORT_VAD_PAD_MS,
    LIVE_COMPLETE_SILENCE_MS,
    SAMPLE_RATE,
    SPEECH_THRESHOLD,
)
from app.utils.text import to_plain_dict

_local_repo_candidates = [
    Path(__file__).resolve().parents[2] / "silero-vad",
    Path("/app/silero-vad"),
]

model = None
_get_speech_timestamps = None
_vad_iterator_cls = None
_vad_loaded = False


def _load_vad() -> None:
    global model, _get_speech_timestamps, _vad_iterator_cls, _vad_loaded

    if _vad_loaded:
        return
    if torch is None:
        raise RuntimeError("torch is not installed")

    print("Loading Silero VAD...")
    last_error: Exception | None = None
    for repo_path in _local_repo_candidates:
        try:
            if repo_path.exists():
                model_obj, utils = torch.hub.load(
                    repo_or_dir=str(repo_path), model="silero_vad", source="local"
                )
                break
        except Exception as exc:
            last_error = exc
    else:
        if last_error is not None:
            print(f"Error: {last_error}")
        model_obj, utils = torch.hub.load(
            repo_or_dir="snakers4/silero-vad",
            model="silero_vad",
            force_reload=False,
            trust_repo=True,
        )

    get_speech_timestamps, _save_audio, _read_audio, vad_iterator_cls, _collect_chunks = utils
    model_obj.eval()
    model = model_obj
    _get_speech_timestamps = get_speech_timestamps
    _vad_iterator_cls = vad_iterator_cls
    _vad_loaded = True


class VADIterator:
    def __init__(self, _model: Any = None, *args: Any, **kwargs: Any) -> None:
        _load_vad()
        self._impl = _vad_iterator_cls(model, *args, **kwargs)

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self._impl(*args, **kwargs)

    def reset_states(self) -> None:
        self._impl.reset_states()


def get_speech_timestamps_safe(pcm16: np.ndarray) -> list[dict[str, int]]:
    if len(pcm16) == 0:
        return []
    if torch is None:
        return []
    _load_vad()
    audio_tensor = torch.from_numpy(pcm16.astype(np.float32) / 32768.0)
    try:
        timestamps = _get_speech_timestamps(
            audio_tensor,
            model,
            threshold=SPEECH_THRESHOLD,
            sampling_rate=SAMPLE_RATE,
            min_silence_duration_ms=LIVE_COMPLETE_SILENCE_MS,
            speech_pad_ms=EXPORT_VAD_PAD_MS,
        )
    except TypeError:
        timestamps = _get_speech_timestamps(
            audio_tensor,
            model,
            threshold=SPEECH_THRESHOLD,
            sampling_rate=SAMPLE_RATE,
        )
    return [
        to_plain_dict(ts) if not isinstance(ts, dict) else ts for ts in (timestamps or [])
    ]
