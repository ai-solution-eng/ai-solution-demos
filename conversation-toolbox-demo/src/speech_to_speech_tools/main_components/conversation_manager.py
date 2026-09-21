import asyncio
import base64
import json
import logging
import uuid
from datetime import datetime
from functools import partial
from os.path import join as pj
from typing import Any, Literal, cast

import numpy as np
from agents import RawResponsesStreamEvent, RunItemStreamEvent, Runner
from fastapi import WebSocket
from utils.audio_handling import (
    ResponseStreamParser,
    SentenceBuffer,
    VADProcessor,
    audio_bytes_to_wave_bytesio,
    write_bytes_to_wav,
)
from utils.pcai_model_classes import ChatModel, VoiceModel
from utils.redis_client import RedisClient
from utils.tts_sanitizer import sanitize_text_for_tts

from main_components.constants import (
    AUDIO_DIR,
    DEFAULT_CONFIG,
    TRANSCRIPTS_DIR,
    _hallucination_updater,
    resolve_api_key,
)

logger = logging.getLogger(__name__)


class ConversationSession:
    asr_function: Any = None
    current_config: dict[str, Any] = DEFAULT_CONFIG.copy()
    initialized: bool = False
    llm_class: Any = None
    llm_function: Any = None
    tts_function: Any = None
    sample_rate: int = 16000
    frame_duration: int = 30

    def __init__(
        self,
        websocket: WebSocket,
        rms_threshold: float | None = None,
        vad_aggression: int = 3,
    ):
        self.websocket = websocket
        self.session_id = str(uuid.uuid4())
        self.history: list[dict[str, str]] = []
        self.update_configs(self.current_config)
        self.initialized = True

        # VAD
        self.vad = VADProcessor(vad_aggression, self.sample_rate, self.frame_duration)
        self.rms_threshold = (
            rms_threshold if rms_threshold is not None else self.current_config.get("rmsThreshold", 200)
        )

        # TTS queue and flags
        self.tts_queue: asyncio.Queue = asyncio.Queue()
        self.tts_processor_running = False
        self.is_generating = False
        self.tts_task: asyncio.Task | None = None
        self.response_task: asyncio.Task | None = None
        self.is_interrupted = False
        self.voice_interrupt_enabled = True
        self.tts_enabled = True
        # Grace period (monotonic deadline) during which voice interrupts are
        # suppressed. Set when the user sends text input so the LLM response
        # isn't immediately cancelled by stale VAD state or mic noise.
        self.interrupt_suppressed_until: float = 0.0
        self.interrupt_lock = asyncio.Lock()
        self.websocket_lock = asyncio.Lock()

        # Audio recording
        self.audio_index = 0
        self.transcript_path = pj(TRANSCRIPTS_DIR, f"session_{self.session_id}.log")
        self._init_transcript_file()

        # Tool calls
        self.tools: dict | None = None
        self.tool_calls: dict | None = None
        self.save_audio_enabled = False

        # Task tracking for cleanup
        self.active_tasks: set[asyncio.Task] = set()
        self._tasks_lock = asyncio.Lock()

    # --------------------------------------------------------------------------
    # State persistence (Redis)
    # --------------------------------------------------------------------------
    async def save_state(self):
        r = await RedisClient.get_client()
        config_copy = dict(self.current_config)
        if "asrHallucinationPatternsSet" in config_copy:
            config_copy["asrHallucinationPatternsSet"] = list(config_copy["asrHallucinationPatternsSet"])
        state = {
            "history": self.history,
            "current_config": config_copy,
            "audio_index": self.audio_index,
        }
        await r.set(f"session:{self.session_id}", json.dumps(state))

    async def load_state(self):
        r = await RedisClient.get_client()
        data = await r.get(f"session:{self.session_id}")
        if data:
            state = json.loads(data)
            self.history = state.get("history", [])
            self.audio_index = state.get("audio_index", 0)
            config = state.get("current_config", {})
            if "asrHallucinationPatternsSet" in config:
                config["asrHallucinationPatternsSet"] = set(config["asrHallucinationPatternsSet"])
            self.current_config.update(config)

    async def clear_state(self):
        """Delete session data from Redis."""
        r = await RedisClient.get_client()
        await r.delete(f"session:{self.session_id}")

    # --------------------------------------------------------------------------
    # Transcript file
    # --------------------------------------------------------------------------
    def _init_transcript_file(self):
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(self.transcript_path, "w", encoding="utf-8") as f:
            f.write(f"=== Session Started: {self.session_id} at {timestamp} ===\n")

    async def _write_file_async(self, path, content, mode="a"):
        def write():
            with open(path, mode, encoding="utf-8") as f:
                f.write(content)

        await asyncio.to_thread(write)

    async def _send_status(self, stage: str, message: str):
        try:
            async with self.websocket_lock:
                if hasattr(self.websocket, "client_state") and self.websocket.client_state:
                    await self.websocket.send_json(
                        {
                            "type": "status_update",
                            "stage": stage,
                            "message": message,
                        }
                    )
        except (asyncio.CancelledError, RuntimeError):
            pass

    async def save_user_audio(self, audio_data: bytes):
        self.audio_index += 1
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        file_name = pj(AUDIO_DIR, f"{self.session_id}_{timestamp}_{self.audio_index}.wav")
        write_function = partial(
            write_bytes_to_wav,
            audio_data=audio_data,
            file_name=file_name,
            sample_rate=self.sample_rate,
        )
        try:
            await asyncio.to_thread(write_function)
            logger.info(f"💾 Saved audio: {file_name}")
        except Exception:
            logger.exception("Failed to save audio")

    async def log_transcript(self, role: str, text: str):
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        entry = f"[{timestamp}] {role}: {text}\n"
        try:
            await self._write_file_async(self.transcript_path, entry)
        except Exception:
            logger.exception("Failed to write transcript")

    # --------------------------------------------------------------------------
    # Configuration updates
    # --------------------------------------------------------------------------
    def _check_config_change(self, config_dict: dict[str, Any], keys: list[str]):
        # Only compare keys the caller actually supplied: absent keys mean
        # "leave unchanged", so they never trigger a rebuild on their own.
        # This lets the UI drop *ModelName from its payload entirely (the
        # server auto-discovers them) without forcing a rebuild on every
        # unrelated settings tweak.
        return (
            any(k in config_dict and self.current_config.get(k) != config_dict[k] for k in keys) or not self.initialized
        )

    def update_configs(self, config_dict: dict[str, Any]) -> None:
        if self._check_config_change(config_dict, ["remote"]):
            logger.info(f"Config Update: Remote Execution set to '{config_dict['remote']}'")
        model_usage = "remote" if config_dict.get("remote") else "local"
        model_usage = cast(Literal["local", "remote"], model_usage)

        # Key handling: Empty/placeholder (default) -> use backend key.
        # Placeholders like "YOUR_TTS_API_KEY_HERE" must never be sent as Bearer
        # tokens, so they are treated as empty and fall through to current_config.
        for api_key in ["asrApiKey", "llmApiKey", "ttsApiKey"]:
            config_dict[api_key] = resolve_api_key(
                config_dict.get(api_key, ""),
                self.current_config.get(api_key, ""),
            )

        if self._check_config_change(config_dict, ["asrApiKey", "asrBaseUrl", "asrModelName", "remote"]):
            logger.info(f"Updating ASR model: {config_dict.get('asrModelName') or '(auto)'}")
            try:
                asr = VoiceModel(
                    model_name=config_dict.get("asrModelName", ""),
                    url_remote=config_dict["asrBaseUrl"],
                    api_key=config_dict["asrApiKey"],
                    model_usage=model_usage,
                )
                self.asr_function = asr.asr_async_function
                logger.info(f"✓ ASR Updated. Current URL: {asr.base_url}")
            except Exception:
                logger.exception("✗ ASR Update Failed")

        if not config_dict.get("toolCalls"):
            config_dict["toolCalls"] = None

        if self._check_config_change(
            config_dict,
            ["llmApiKey", "llmBaseUrl", "llmModelName", "remote", "toolCalls"],
        ):
            logger.info(f"Updating LLM model: {config_dict.get('llmModelName') or '(auto)'}")
            try:
                self.llm_class = ChatModel(
                    model_name=config_dict.get("llmModelName", ""),
                    url_remote=config_dict["llmBaseUrl"],
                    api_key=config_dict["llmApiKey"],
                    model_usage=model_usage,
                    transport=str(config_dict.get("llmTransport") or "chat-completions"),
                )
                if config_dict["toolCalls"]:
                    try:
                        tool_calls = json.loads(config_dict["toolCalls"])
                        assert isinstance(tool_calls, dict) and len(tool_calls) > 0
                        self.tool_calls = tool_calls
                        logger.info(f"Tool calls enabled:\n{json.dumps(tool_calls)}")
                    except Exception as e:
                        logger.warning(f"Tool call parsing failed: {e}. Continuing without tools.")
                        config_dict["toolCalls"] = None
                        self.tool_calls = None
                else:
                    self.tool_calls = None
                self.llm_function = None
                logger.info(f"✓ LLM Updated. Current URL: {self.llm_class.base_url}")
            except Exception:
                logger.exception("✗ LLM Update Failed")

        if self._check_config_change(
            config_dict,
            ["ttsApiKey", "ttsBaseUrl", "ttsModelName", "ttsVoice", "remote"],
        ):
            logger.info(
                f"Updating TTS model: {config_dict.get('ttsModelName') or '(auto)'} ({config_dict['ttsVoice']})"
            )
            try:
                tts = VoiceModel(
                    model_name=config_dict.get("ttsModelName", ""),
                    url_remote=config_dict["ttsBaseUrl"],
                    api_key=config_dict["ttsApiKey"],
                    model_usage=model_usage,
                    model_type="TTS",
                    tts_voice=config_dict["ttsVoice"],
                )
                self.tts_function = tts.tts_async_function
                logger.info(f"✓ TTS Updated. Current URL: {tts.base_url}")
                asyncio.create_task(self._warmup_tts())
            except Exception:
                logger.exception("✗ TTS Update Failed")

        if self._check_config_change(config_dict, ["sampleRate"]):
            self.sample_rate = int(config_dict["sampleRate"])

        if self._check_config_change(config_dict, ["vadAggression"]):
            self.vad = VADProcessor(
                int(config_dict["vadAggression"]),
                sample_rate=self.sample_rate,
                frame_duration=self.frame_duration,
            )

        if self._check_config_change(config_dict, ["rmsThreshold"]):
            self.rms_threshold = int(config_dict["rmsThreshold"])
            logger.info(f"RMS threshold updated to {self.rms_threshold}")

        if self._check_config_change(config_dict, ["systemPrompt"]):
            new_prompt = {"role": "system", "content": config_dict["systemPrompt"]}
            if new_prompt not in self.history:
                self.history.append(new_prompt)

        if self._check_config_change(config_dict, ["asrHallucinationPatterns"]):
            self.current_config["asrHallucinationPatternsSet"] = _hallucination_updater(
                config_dict["asrHallucinationPatterns"]
            )

        self.current_config.update(config_dict)
        self.initialized = True

    # --------------------------------------------------------------------------
    # TTS processor (runs in background)
    # --------------------------------------------------------------------------
    async def tts_processor(self):
        """Background task that consumes TTS queue and generates speech."""
        while self.tts_processor_running:
            try:
                text = await asyncio.wait_for(self.tts_queue.get(), timeout=30.0)
                if text and self.tts_processor_running:
                    try:
                        await self.generate_tts(text)
                    except Exception:
                        logger.exception("TTS generation error")
                    finally:
                        self.tts_queue.task_done()
                else:
                    self.tts_queue.task_done()
            except TimeoutError:
                continue
            except asyncio.CancelledError:
                logger.info("TTS processor cancelled")
                break
            except Exception:
                logger.exception("TTS Processor Error")
                break

    # --------------------------------------------------------------------------
    # Inline ASR (avoids arq overhead for real-time path)
    # --------------------------------------------------------------------------
    async def _transcribe_inline(self, audio_data: bytes) -> str:
        from utils.audio_handling import extract_asr_text

        audio_buffer = audio_bytes_to_wave_bytesio(audio_data, sample_rate=self.sample_rate)
        language = self.current_config.get("language", "en")
        asr_kwargs = {"file": audio_buffer}
        if language:
            asr_kwargs["language"] = language
        result = await asyncio.wait_for(self.asr_function(**asr_kwargs), timeout=120.0)
        text = extract_asr_text(result)
        return text.strip()

    # --------------------------------------------------------------------------
    # Inline TTS (avoids arq overhead for real-time path)
    # --------------------------------------------------------------------------
    async def _warmup_tts(self) -> None:
        """Pre-warm the TTS endpoint so the first real call is fast.

        Cloned voices (e.g. fish s2 pro) load the voice embedding from disk
        on the first synthesis request, adding 5-10s of latency. Firing a
        short throwaway request at config time loads the embedding into GPU
        memory so the user's first conversational TTS call is ~2s instead.
        The audio is discarded; no data is sent to the client.
        """
        try:
            await asyncio.wait_for(self.tts_function(input="."), timeout=30.0)
            logger.info("✓ TTS pre-warmed (voice embedding loaded)")
        except Exception as e:
            logger.warning(f"TTS warm-up failed (non-fatal): {e}")

    async def generate_tts(self, text: str):
        if not self.tts_enabled:
            logger.info("TTS skipped - disabled")
            return

        try:
            if self.is_interrupted or not self.tts_processor_running:
                logger.info("TTS skipped - interrupted")
                return

            text = sanitize_text_for_tts(text)
            if not text:
                logger.info("TTS skipped - empty after sanitization")
                return

            await self._send_status("tts", "Synthesizing speech...")
            logger.info(f"TTS: '{text[:50]}...'")

            # Call TTS directly (inline, no arq)
            response_audio = await asyncio.wait_for(self.tts_function(input=text), timeout=120.0)
            audio_b64 = base64.b64encode(response_audio.content).decode("utf-8")

            if not audio_b64:
                logger.warning("TTS returned empty audio")
                return

            if self.is_interrupted or not self.tts_processor_running:
                logger.info("TTS cancelled after generation - interrupted")
                return

            async with self.websocket_lock:
                if hasattr(self.websocket, "client_state") and self.websocket.client_state:
                    await self.websocket.send_json(
                        {
                            "type": "audio",
                            "data": audio_b64,
                            "segment": True,
                        }
                    )
        except TimeoutError:
            logger.warning("TTS timed out")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("TTS error (non-fatal)")

    # --------------------------------------------------------------------------
    # Task cancellation helpers
    # --------------------------------------------------------------------------
    async def _cancel_all_tasks(self) -> None:
        """Cancel all active tasks without deadlock."""
        async with self._tasks_lock:
            tasks_to_cancel = list(self.active_tasks)
            self.active_tasks.clear()

        for task in tasks_to_cancel:
            if not task.done():
                task.cancel()
        if tasks_to_cancel:
            await asyncio.gather(*tasks_to_cancel, return_exceptions=True)

    async def interrupt_generation(self):
        await self.interrupt_lock.acquire()
        try:
            if self.is_interrupted:
                return
            if self.is_generating:
                logger.info("⚡ User interrupted AI")
                self.is_interrupted = True
                self.tts_processor_running = False
                self.is_generating = False

                await self._cancel_all_tasks()

                if self.tts_task and not self.tts_task.done():
                    self.tts_task.cancel()
                    try:
                        await self.tts_task
                    except asyncio.CancelledError:
                        pass
                    self.tts_task = None

                while not self.tts_queue.empty():
                    try:
                        self.tts_queue.get_nowait()
                        self.tts_queue.task_done()
                    except Exception:
                        break

                # Send interrupt signal to frontend
                try:
                    async with self.websocket_lock:
                        if hasattr(self.websocket, "client_state") and self.websocket.client_state:
                            await self.websocket.send_json({"type": "interrupt"})
                    logger.info("✓ Interrupt signal sent to frontend")
                except (asyncio.CancelledError, RuntimeError) as e:
                    logger.warning(f"Could not send interrupt signal: {e}")
        finally:
            self.interrupt_lock.release()

    # --------------------------------------------------------------------------
    # Speech processing – enqueue ASR, then handle LLM (inline) and TTS
    # --------------------------------------------------------------------------
    async def process_speech(self, audio_data: bytes):
        """Process audio input through ASR then LLM."""
        # Lock management – acquire and release manually to avoid deadlock on task cancellation
        try:
            await self.interrupt_lock.acquire()
            self.is_interrupted = False
        finally:
            self.interrupt_lock.release()

        current_task = asyncio.current_task()
        async with self._tasks_lock:
            if current_task is not None:
                self.active_tasks.add(current_task)

        try:
            if not self.tts_processor_running and self.is_generating:
                logger.info("⚡ Skipping speech processing - interrupt in progress")
                return

            if self.save_audio_enabled:
                await self.save_user_audio(audio_data)

            await self._send_status("vad", "Processing speech...")

            np_audio = np.frombuffer(audio_data, dtype=np.int16).astype(float)
            try:
                rms = np.sqrt(np.mean(np_audio**2))
                if rms < self.rms_threshold:
                    logger.info(f"Audio too quiet (RMS={rms:.1f} < threshold={self.rms_threshold}), skipping")
                    return
            except Exception as e:
                logger.info(f"Exception {e} during energy calculation. Skipping.")
                return

            # ---- ASR via inline call ----
            await self._send_status("asr", "Processing speech...")
            try:
                user_text = await self._transcribe_inline(audio_data)
            except TimeoutError:
                logger.warning("ASR timed out")
                try:
                    async with self.websocket_lock:
                        if hasattr(self.websocket, "client_state") and self.websocket.client_state:
                            await self.websocket.send_json(
                                {
                                    "type": "error",
                                    "message": "Transcription timed out.",
                                }
                            )
                except (asyncio.CancelledError, RuntimeError):
                    pass
                return
            except Exception:
                logger.exception("ASR error")
                return

            if not user_text or len(user_text) < 3:
                logger.info("ASR returned empty or too short text, resetting listening state")
                await self._send_status("vad", "Listening...")
                return

            if user_text.lower() in self.current_config.get("asrHallucinationPatternsSet", set()):
                logger.info(f"Hallucination filtered: '{user_text}'")
                await self._send_status("vad", "Listening...")
                return

            if user_text.startswith("*") and user_text.endswith("*"):
                logger.info(f"Sound effect filtered: '{user_text}'")
                await self._send_status("vad", "Listening...")
                return

            # Send typing indicator
            try:
                async with self.websocket_lock:
                    if hasattr(self.websocket, "client_state") and self.websocket.client_state:
                        await self.websocket.send_json({"type": "typing"})
            except (asyncio.CancelledError, RuntimeError):
                return

            # Send user transcript
            try:
                async with self.websocket_lock:
                    if hasattr(self.websocket, "client_state") and self.websocket.client_state:
                        await self.websocket.send_json(
                            {
                                "type": "transcript",
                                "role": "user",
                                "text": user_text,
                            }
                        )
            except (asyncio.CancelledError, RuntimeError):
                return

            self.history.append({"role": "user", "content": user_text})
            await self.log_transcript("USER", user_text)

            # ---- LLM response (still inline, but could be offloaded similarly) ----
            await self._process_llm_response()

        except asyncio.CancelledError:
            logger.info("process_speech cancelled")
            raise
        except Exception:
            logger.exception("process_speech error")
        finally:
            async with self._tasks_lock:
                if current_task is not None:
                    self.active_tasks.discard(current_task)

    async def process_text_direct(self, user_text: str):
        """Process text directly (bypassing ASR) and send to LLM."""
        current_task = asyncio.current_task()
        async with self._tasks_lock:
            if current_task is not None:
                self.active_tasks.add(current_task)

        try:
            async with self.interrupt_lock:
                self.is_interrupted = False

            if not self.tts_processor_running and self.is_generating:
                logger.info("⚡ Skipping - generation in progress")
                return

            user_text = str(user_text).strip()
            if not user_text or len(user_text) < 1:
                return

            self.history.append({"role": "user", "content": user_text})
            await self.log_transcript("USER", user_text)
            await self._process_llm_response()

        except asyncio.CancelledError:
            logger.info("process_text_direct cancelled")
            raise
        except Exception:
            logger.exception("process_text_direct error")
        finally:
            async with self._tasks_lock:
                if current_task is not None:
                    self.active_tasks.discard(current_task)

    # --------------------------------------------------------------------------
    # Shared LLM response processing
    # --------------------------------------------------------------------------
    @staticmethod
    def _parse_tool_calls_from_config(config: dict) -> dict | None:
        raw = config.get("toolCalls")
        if not raw:
            return None
        if isinstance(raw, dict):
            return raw if len(raw) > 0 else None
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) and len(parsed) > 0 else None
        except (json.JSONDecodeError, TypeError):
            return None

    async def _process_llm_response(self) -> None:
        sentence_buffer = SentenceBuffer()
        self.is_generating = True
        self.tts_processor_running = self.tts_enabled
        await self._send_status("llm", "Generating response...")
        if self.tts_enabled:
            if self.tts_task and not self.tts_task.done():
                self.tts_task.cancel()
                try:
                    await self.tts_task
                except asyncio.CancelledError:
                    pass
            self.tts_queue = asyncio.Queue()
            self.tts_task = asyncio.create_task(self.tts_processor())

        try:
            parser = ResponseStreamParser()
            if self.llm_function is None:
                logger.info("Initializing LLM function...")
                try:
                    assert self.llm_class is not None
                    tool_calls = self._parse_tool_calls_from_config(self.current_config)
                    if tool_calls:
                        logger.info(f"Using tool calls: {list(tool_calls.keys())}")
                        self.llm_function = await self.llm_class.aagent(tool_json=tool_calls)
                    else:
                        logger.info("No tool calls configured, using plain agent")
                        self.llm_function = await self.llm_class.aagent()
                    logger.info("✓ LLM function initialized")
                except Exception:
                    logger.exception("✗ LLM initialization failed")
                    try:
                        assert self.llm_class is not None
                        self.llm_function = await self.llm_class.aagent()
                        logger.info("✓ LLM initialized (fallback, no tools)")
                    except Exception as e2:
                        logger.exception("✗ LLM fallback also failed")
                        try:
                            async with self.websocket_lock:
                                if hasattr(self.websocket, "client_state") and self.websocket.client_state:
                                    await self.websocket.send_json(
                                        {
                                            "type": "error",
                                            "message": f"LLM initialization failed: {e2}",
                                        }
                                    )
                        except (asyncio.CancelledError, RuntimeError):
                            pass
                        return

            if self.llm_function is None:
                logger.exception("LLM function is still None after initialization attempts")
                try:
                    async with self.websocket_lock:
                        if hasattr(self.websocket, "client_state") and self.websocket.client_state:
                            await self.websocket.send_json({"type": "error", "message": "LLM not available"})
                except (asyncio.CancelledError, RuntimeError):
                    pass
                return

            streamed = Runner.run_streamed(self.llm_function, input=self.history)  # type: ignore[arg-type]
            # vLLM's /v1/responses emits each function_call twice (once with
            # a `call_...` ID from the Responses API layer, once with a
            # `chatcmpl-tool-...` ID from the Chat Completions layer). The
            # SDK fires tool_called for both, but only the chatcmpl variant
            # produces a tool_output. Track the last announced tool name and
            # skip consecutive duplicates; reset on tool_output so genuine
            # sequential calls to the same tool still announce.
            last_tool_called: str | None = None
            async for ev in streamed.stream_events():
                if not self.is_generating:
                    logger.info("Generation interrupted, breaking stream")
                    break
                current = asyncio.current_task()
                if current and current.cancelled():
                    logger.info("Task cancelled mid-stream")
                    break

                # --- Streaming text deltas (token-level from the model) ---
                # Typed events from Responses API. Text-only path; tool
                # calls/outputs are dispatched via RunItemStreamEvent below
                # because RawResponsesStreamEvent can't surface tool results
                # (in the Responses API, tool outputs are INPUT to the next
                # model turn, not OUTPUT streamed from the model).
                if isinstance(ev, RawResponsesStreamEvent):
                    raw = ev.data
                    raw_type = getattr(raw, "type", "?")
                    if raw_type != "response.output_text.delta":
                        continue
                    delta = getattr(raw, "delta", None)
                    if not delta:
                        continue
                    content = parser(delta)
                    if content is None:
                        continue
                    try:
                        async with self.websocket_lock:
                            if hasattr(self.websocket, "client_state") and self.websocket.client_state:
                                await self.websocket.send_json({"type": "stream_text", "text": content})
                    except (asyncio.CancelledError, RuntimeError) as e:
                        logger.info(f"WebSocket closed during send: {e}")
                        break

                    if self.tts_enabled and self.tts_processor_running:
                        sentences = sentence_buffer.add_text(content)
                        for sentence in sentences:
                            if len(sentence.strip()) >= 3 and self.tts_processor_running:
                                await self.tts_queue.put(sentence.strip())

                # --- Tool calls + tool outputs (item-level from the SDK) ---
                # Fires when the agent decides to call a tool ("tool_called")
                # or when a tool call returns ("tool_output"). These are the
                # higher-level abstractions over the raw function_call /
                # function_call_output events, and are the ONLY way to surface
                # tool results because results are not in the model's output
                # stream — the SDK emits them between model turns.
                elif isinstance(ev, RunItemStreamEvent):
                    if ev.name == "tool_called":
                        tool_name = getattr(ev.item, "tool_name", None) or "tool"
                        # Skip consecutive duplicate — vLLM's Responses API
                        # emits each function_call in both a `call_...` and
                        # `chatcmpl-tool-...` format, producing two tool_called
                        # events for a single actual invocation.
                        if tool_name == last_tool_called:
                            continue
                        last_tool_called = tool_name
                        # Trailing newline separates this announcement from
                        # the next event on the websocket (which is otherwise
                        # rendered on the same line by the frontend).
                        tool_str = f"Calling {tool_name}.\n"
                        try:
                            async with self.websocket_lock:
                                if hasattr(self.websocket, "client_state") and self.websocket.client_state:
                                    await self.websocket.send_json({"type": "stream_text", "text": tool_str})
                        except (asyncio.CancelledError, RuntimeError) as e:
                            logger.info(f"WebSocket closed during send: {e}")
                            break

                        # Queue announcement for TTS too. The new format has
                        # no JSON args to strip — the SDK exposes a clean
                        # tool_name, unlike LangChain's tool_call_chunks which
                        # required the _tool_call_for_tts regex cleanup.
                        if self.tts_enabled and self.tts_processor_running:
                            for sentence in sentence_buffer.add_text(tool_str):
                                if len(sentence.strip()) >= 3 and self.tts_processor_running:
                                    await self.tts_queue.put(sentence.strip())

                    elif ev.name == "tool_output":
                        # Reset the dedup tracker so a genuine subsequent
                        # call to the same tool still gets announced.
                        last_tool_called = None
                        # Extract the tool result text. The SDK wraps MCP
                        # tool outputs in {"type": "text", "text": "..."} ;
                        # function-tool outputs are plain strings.
                        output = getattr(ev.item, "output", "")
                        if isinstance(output, dict):
                            text_out = output.get("text") or str(output)
                        elif isinstance(output, str):
                            text_out = output
                        else:
                            text_out = str(output)
                        # Truncate long tool outputs so the chat UI isn't
                        # flooded with e.g. 10KB of search results.
                        if len(text_out) > 500:
                            text_out = text_out[:500] + "..."
                        tool_str = f"Result:\n{text_out}\n\n"
                        try:
                            async with self.websocket_lock:
                                if hasattr(self.websocket, "client_state") and self.websocket.client_state:
                                    await self.websocket.send_json({"type": "stream_text", "text": tool_str})
                        except (asyncio.CancelledError, RuntimeError) as e:
                            logger.info(f"WebSocket closed during send: {e}")
                            break
                        # Tool outputs are NOT sent to TTS — matches the
                        # original LangChain behavior where ToolMessages
                        # were displayed but not read aloud.

            # After the stream finishes, flush remaining text
            if self.tts_enabled and self.tts_processor_running:
                remaining = sentence_buffer.flush()
                if remaining and self.tts_processor_running:
                    await self.tts_queue.put(remaining[0].strip())
                if self.tts_task:
                    try:
                        # Wait for TTS to finish, but don't block forever
                        await asyncio.wait_for(self.tts_queue.join(), timeout=30.0)
                        try:
                            async with self.websocket_lock:
                                if hasattr(self.websocket, "client_state") and self.websocket.client_state:
                                    await self.websocket.send_json({"type": "response_end"})
                        except (asyncio.CancelledError, RuntimeError):
                            pass
                    except TimeoutError:
                        logger.warning("TTS queue join timed out – forcing next step")
                    except Exception:
                        pass

            try:
                async with self.websocket_lock:
                    if hasattr(self.websocket, "client_state") and self.websocket.client_state:
                        await self.websocket.send_json({"type": "response_complete"})
            except (asyncio.CancelledError, RuntimeError):
                pass

            self.history.append({"role": "assistant", "content": parser.full_response})
            await self.log_transcript("AI", parser.full_response)

        except asyncio.CancelledError:
            logger.info("LLM response processing cancelled")
            self.is_generating = False
            raise
        except Exception as e:
            logger.exception("LLM Error")
            if self.history and self.history[-1].get("role") == "user":
                self.history.pop()
            try:
                async with self.websocket_lock:
                    if hasattr(self.websocket, "client_state") and self.websocket.client_state:
                        await self.websocket.send_json({"type": "error", "message": f"LLM Error: {e!s}"})
            except (asyncio.CancelledError, RuntimeError):
                pass
        finally:
            self.is_generating = False
            self.tts_processor_running = False
            if self.tts_task and not self.tts_task.done():
                self.tts_task.cancel()
                try:
                    await self.tts_task
                except asyncio.CancelledError:
                    pass
            self.tts_task = None
            while not self.tts_queue.empty():
                try:
                    self.tts_queue.get_nowait()
                    self.tts_queue.task_done()
                except Exception:
                    break

    # --------------------------------------------------------------------------
    # Cleanup / close
    # --------------------------------------------------------------------------
    async def close(self):
        """Graceful shutdown: cancel listeners, save state, clean up."""
        logger.info(f"Closing session {self.session_id}")
        await self.save_state()

        await self._cancel_all_tasks()

        # Clean up any MCP servers held by the agent so we don't leak
        # streamable-http sessions across reconnects. terminate_on_close=False
        # (set in _get_mcp_servers) means this skips the session-DELETE that
        # the istio ingress can't handle, but we still wrap with a timeout
        # as defense-in-depth to never block shutdown.
        if self.llm_function is not None:
            for server in getattr(self.llm_function, "mcp_servers", []) or []:
                try:
                    await asyncio.wait_for(server.cleanup(), timeout=5.0)
                except TimeoutError:
                    logger.warning(f"MCP server cleanup timed out: {server.name}")
                except Exception as e:
                    logger.warning(f"MCP server cleanup error: {e}")
