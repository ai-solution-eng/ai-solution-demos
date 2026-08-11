import logging
import re
import wave
from io import BytesIO

import numpy as np
import webrtcvad

__all__ = [
    "ResponseStreamParser",
    "SentenceBuffer",
    "VADProcessor",
    "assign_speaker",
    "audio_bytes_to_wave_bytesio",
    "convert_audio_to_wav",
    "extract_asr_text",
    "format_timestamp",
    "get_audio_format_safe",
    "split_audio_by_speech_energy",
    "write_bytes_to_wav",
]

logger = logging.getLogger(__name__)

SUPPORTED_AUDIO_FORMATS = {
    "mp3": "mp3",
    "wav": "wav",
    "m4a": "m4a",
    "flac": "flac",
    "ogg": "ogg",
    "aac": "aac",
    "wma": "wma",
    "aiff": "aiff",
    "ape": "ape",
}


def audio_bytes_to_wave_bytesio(audio_data: bytes, sample_rate: int = 16000) -> BytesIO:
    audio_buffer = BytesIO()
    with wave.open(audio_buffer, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(audio_data)
    audio_buffer.seek(0)
    return audio_buffer


def write_bytes_to_wav(audio_data: bytes, file_name: str, sample_rate: int = 16000):
    with wave.open(file_name, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(audio_data)


def convert_audio_to_wav(file_path: str, sample_rate: int = 16000) -> tuple[bytes, float]:
    from pydub import AudioSegment

    audio = AudioSegment.from_file(file_path)
    audio = audio.set_frame_rate(sample_rate)
    audio = audio.set_channels(1)
    audio = audio.set_sample_width(2)
    duration = len(audio) / 1000.0
    wav_buffer = BytesIO()
    audio.export(wav_buffer, format="wav")
    wav_buffer.seek(0)
    return wav_buffer.read(), duration


def extract_asr_text(result) -> str:
    if hasattr(result, "text"):
        return result.text.strip() if result.text else ""
    elif isinstance(result, dict):
        return result.get("text", "").strip()
    elif isinstance(result, str):
        return result.strip()
    elif result:
        return str(result).strip()
    return ""


def format_timestamp(seconds: float) -> str:
    minutes = int(seconds // 60)
    secs = seconds % 60
    return f"{minutes:02d}:{secs:06.3f}"


def get_audio_format_safe(filename: str | None) -> str | None:
    if not filename:
        return None
    ext = filename.lower().split(".")[-1]
    return SUPPORTED_AUDIO_FORMATS.get(ext)


def assign_speaker(start_time: float, end_time: float, existing_transcripts: list, max_speakers: int) -> int:
    if not existing_transcripts:
        return 1
    duration = end_time - start_time
    last_segment = existing_transcripts[-1]
    last_speaker = last_segment.get("speaker", "Speaker_1")
    last_end = last_segment.get("end", 0)
    last_start = last_segment.get("start", 0)
    last_duration = last_end - last_start
    gap = start_time - last_end
    if gap < 0.5:
        return int(last_speaker.split("_")[1])
    if duration < 1.5:
        return int(last_speaker.split("_")[1])
    if last_duration > 10 and duration < 3:
        last_num = int(last_speaker.split("_")[1])
        return ((last_num - 1 + 1) % max_speakers) + 1
    if gap < 3.0:
        return int(last_speaker.split("_")[1])
    last_num = int(last_speaker.split("_")[1])
    next_num = (last_num % max_speakers) + 1
    return next_num


def split_audio_by_speech_energy(np_audio: np.ndarray, sample_rate: int = 16000) -> list[tuple]:
    segments: list[tuple] = []
    window_size = int(sample_rate * 0.5)
    energies = []
    for i in range(0, len(np_audio) - window_size, window_size):
        window = np_audio[i : i + window_size]
        energy = np.sqrt(np.mean(window.astype(float) ** 2))
        energies.append((i, energy))
    if not energies:
        return segments
    threshold = np.mean([e for _, e in energies]) * 0.5
    in_speech = False
    speech_start = 0
    speech_frames: list[tuple[int, int]] = []
    for i, (idx, energy) in enumerate(energies):
        if energy > threshold and not in_speech:
            in_speech = True
            speech_start = idx
        elif energy <= threshold and in_speech:
            if speech_frames:
                last_end = speech_frames[-1][1]
                gap = speech_start - last_end
                if gap < sample_rate * 2:
                    speech_frames[-1] = (speech_frames[-1][0], idx + window_size)
                else:
                    speech_frames.append((speech_start, idx + window_size))
            else:
                speech_frames.append((speech_start, idx + window_size))
            in_speech = False
    if in_speech and speech_frames:
        speech_frames[-1] = (speech_frames[-1][0], len(np_audio))
    elif in_speech:
        speech_frames.append((speech_start, len(np_audio)))
    for start, end in speech_frames:
        start_time = start / sample_rate
        end_time = end / sample_rate
        if end_time - start_time >= 0.3:
            segments.append((start_time, end_time, np_audio[start:end]))
    return segments


class ResponseStreamParser:
    def __init__(self) -> None:
        self.full_response = ""

    def __call__(self, response) -> str | None:
        if not isinstance(response, str):
            logger.warning(f"ResponseStreamParser got response {response}. Converting to a list.")
            response = str(response)
        self.full_response += response
        return response


class VADProcessor:
    def __init__(
        self,
        aggression: int = 3,
        sample_rate: int = 16_000,
        frame_duration: int = 30,
        energy_threshold: float = 100.0,
        min_speech_ms: float = 300.0,
    ):
        self.vad = webrtcvad.Vad(aggression)
        self.buffer = bytearray()
        self.is_speaking = False
        self.speech_onset_detected = False
        self.silence_counter = 0
        self.silence_threshold = 40
        self.min_speech_frames = int(min_speech_ms / frame_duration)
        self.sample_rate = sample_rate
        self.frame_duration = frame_duration
        self.frame_size = int(self.sample_rate * self.frame_duration / 1000) * 2
        self.samples_per_frame = self.frame_size // 2
        self.max_audio_bytes = self.sample_rate * 20 * 2
        self.speech_buffer = bytearray()
        self.speech_frame_count = 0
        # Energy pre-filter: reject frames below this RMS value (filters keyboard clicks, etc.)
        # For int16 audio, RMS range is 0~23170 (32767/sqrt(2));
        # typical noise RMS ~10-100, keyboard clicks ~500-5000, speech ~500-15000.
        self.energy_threshold = energy_threshold
        # Zero-crossing rate threshold: reject frames where ZCR is too low
        # (mechanical noise like chair creaks, 50Hz hum). 0.02 passes all human
        # speech including low-pitched voices, while still filtering pure hum.
        self.min_zcr = 0.02
        # Warmup counter - discard first N frames to let audio settle
        self.warmup_frames = 5
        self.frames_received = 0
        # Interrupt debounce: require N consecutive voice frames before signalling onset
        self.onset_confirm_frames = 10
        self.onset_frame_counter = 0
        # Buffer frames during onset count so they aren't lost when onset fires
        self.onset_buffer = bytearray()
        self._total_frames = 0

    def reset(self) -> None:
        """Clear all speech-detection state.

        Called when the user sends text input so stale VAD state (from
        background audio) doesn't immediately interrupt the text-initiated
        generation.
        """
        self.is_speaking = False
        self.speech_frame_count = 0
        self.speech_buffer = bytearray()
        self.onset_frame_counter = 0
        self.onset_buffer = bytearray()
        self.speech_onset_detected = False
        self.silence_counter = 0

    def _frame_rms(self, frame: bytes) -> float:
        """Compute RMS energy of a PCM16 frame."""
        arr = np.frombuffer(frame, dtype=np.int16).astype(float)
        if len(arr) == 0:
            return 0.0
        return float(np.sqrt(np.mean(arr**2)))

    def _frame_zcr(self, frame: bytes) -> float:
        """Compute zero-crossing rate per sample for a PCM16 frame.
        Low ZCR indicates low-frequency mechanical noise (chair, bumps, etc.).
        """
        arr = np.frombuffer(frame, dtype=np.int16)
        if len(arr) == 0:
            return 0.0
        # Remove DC offset before counting zero crossings
        arr_f = arr.astype(np.float64)
        arr_f -= arr_f.mean()
        zero_crossings = np.sum(np.diff(np.signbit(arr_f)))
        return zero_crossings / len(arr)

    def add_audio(self, audio_data: bytes) -> list[bytes]:
        self.buffer.extend(audio_data)
        speech_segments = []

        # Skip warmup frames
        while len(self.buffer) >= self.frame_size:
            if self.frames_received < self.warmup_frames:
                self.buffer = self.buffer[self.frame_size :]
                self.frames_received += 1
                if self.frames_received == self.warmup_frames:
                    logger.info(f"VAD warmup complete: {self.warmup_frames} frames discarded")
                continue

            frame = bytes(self.buffer[: self.frame_size])
            self.buffer = self.buffer[self.frame_size :]
            self._total_frames += 1
            if self._total_frames % 100 == 0:
                logger.info(f"VAD heartbeat: {self._total_frames} frames processed, speaking={self.is_speaking}")

            # Pre-filter: skip frames below energy threshold or with low ZCR
            # (low ZCR = mechanical noise like chair creaks, bumps, footsteps)
            frame_energy = self._frame_rms(frame)
            frame_zcr = self._frame_zcr(frame)
            if frame_energy < self.energy_threshold or frame_zcr < self.min_zcr:
                # Only low-energy frames count toward silence.
                # High-energy but low-ZCR frames are NOT silence (e.g. low-frequency voice
                # components, machinery hum) — skip them without advancing the silence counter.
                if self.is_speaking and frame_energy < self.energy_threshold:
                    self.silence_counter += 1
                    if self.silence_counter < self.silence_threshold:
                        self.speech_buffer.extend(frame)
                    if self.silence_counter >= self.silence_threshold:
                        self.is_speaking = False
                        self.speech_onset_detected = False
                        if self.speech_frame_count >= self.min_speech_frames:
                            logger.info(
                                f"VAD segment produced: {self.speech_frame_count} speech frames,"
                                f" {len(self.speech_buffer)} bytes"
                            )
                            speech_segments.append(bytes(self.speech_buffer))
                        self.speech_buffer = bytearray()
                        self.speech_frame_count = 0
                elif not self.is_speaking and frame_zcr < self.min_zcr and frame_energy >= self.energy_threshold:
                    self.onset_frame_counter = 0
                continue

            try:
                is_voice = self.vad.is_speech(frame, self.sample_rate)
            except Exception as e:
                logger.info(f"VAD Error: {e}")
                continue

            if is_voice:
                self.silence_counter = 0
                if not self.is_speaking:
                    self.onset_frame_counter += 1
                    if len(self.onset_buffer) < self.max_audio_bytes:
                        self.onset_buffer.extend(frame)
                    if self.onset_frame_counter >= self.onset_confirm_frames:
                        logger.info(f"VAD speech onset detected after {self.onset_frame_counter} voice frames")
                        self.is_speaking = True
                        self.speech_onset_detected = True
                        self.speech_buffer = self.onset_buffer
                        self.onset_buffer = bytearray()
                        self.speech_frame_count = self.onset_frame_counter
                if self.is_speaking and len(self.speech_buffer) < self.max_audio_bytes:
                    self.speech_buffer.extend(frame)
                    self.speech_frame_count += 1
            else:
                self.onset_frame_counter = 0
                self.onset_buffer = bytearray()
                if self.is_speaking:
                    self.silence_counter += 1
                    if self.silence_counter < self.silence_threshold:
                        self.speech_buffer.extend(frame)
                    if self.silence_counter >= self.silence_threshold:
                        self.is_speaking = False
                        self.speech_onset_detected = False
                        if self.speech_frame_count >= self.min_speech_frames:
                            logger.info(
                                f"VAD segment produced: {self.speech_frame_count} speech frames,"
                                f" {len(self.speech_buffer)} bytes"
                            )
                            speech_segments.append(bytes(self.speech_buffer))
                        else:
                            logger.info(
                                f"VAD segment too short: {self.speech_frame_count}"
                                f" frames < {self.min_speech_frames} min"
                            )
                        self.speech_buffer = bytearray()
                        self.speech_frame_count = 0

        return speech_segments


class SentenceBuffer:
    # Note: In regex alternation (|), order matters.
    _sentence_end_pattern = re.compile(r'[.,!?。！？]|[:;]\s|[.!?。！？]["\']?[\s\n]')

    def __init__(self):
        self.buffer = ""
        self.microbuffer = ""
        self.min_sentence_length = 10
        # Force-flush threshold (characters) - increased to 300 to reduce forced flushes
        self.force_flush_chars = 300
        # Minimum word boundary distance - only truncate at space if it's within this many chars of threshold
        self.min_word_boundary_distance = 20

    @property
    def sentence_end_pattern(self):
        return self._sentence_end_pattern

    def add_text(self, text: str) -> list[str]:
        """
        Adds text to the buffer and returns a list of all complete sentences
        found based on the end pattern. Force-flush only at sentence boundaries.
        """
        if not isinstance(text, str):
            logger.warning(f"Got non-str at the SentenceBuffer {text}. Converting.")
            text = str(text)

        self.buffer += text
        sentences: list[str] = []

        # Find all potential sentence endings in the current buffer
        matches = list(self.sentence_end_pattern.finditer(self.buffer))

        if not matches:
            # No sentence endings found – never force-flush mid-sentence
            return sentences

        start_index, last_match_end = 0, 0

        # Iterate through every match to split individual sentences
        for match in matches:
            end_index = match.end()
            # Extract the segment from the previous cut point to the current punctuation
            segment = self.microbuffer + self.buffer[start_index:end_index]
            # Clean the segment
            segment = segment.strip().replace("**", "")
            # Only add if it meets the minimum length requirement
            if len(segment) >= self.min_sentence_length:
                sentences.append(segment)
                self.microbuffer = ""
            else:
                self.microbuffer += segment
            # Move the start index for the next sentence
            start_index = end_index
            last_match_end = end_index

        # Update buffer to keep only the text after the last detected sentence end
        self.buffer = self.microbuffer + self.buffer[last_match_end:].strip()
        self.microbuffer = ""

        return sentences

    def flush(self) -> list[str]:
        """
        Returns any remaining text in the buffer as a final sentence chunk.
        Returns a List[str] for consistency with add_text.
        """
        if self.buffer.strip():
            remaining = self.buffer.strip()
            remaining = remaining.replace("**", "")
            self.buffer = ""
            self.microbuffer = ""
            return [remaining]
        return []
