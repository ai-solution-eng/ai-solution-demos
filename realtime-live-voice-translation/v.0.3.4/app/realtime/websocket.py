import asyncio
import json
import math
import re
import time
import traceback
import uuid
from typing import Any

import numpy as np
from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect

from app.audio.ffmpeg import pcm16le_bytes_to_float_tensor
from app.audio.vad import VADIterator, model
from app.config import (
    DEFAULT_TARGET_LANGUAGE,
    LIVE_COMPLETE_SILENCE_MS,
    LIVE_CONTEXT_TURNS,
    LIVE_INCOMPLETE_FINALIZE_MS,
    LIVE_MIN_PARTIAL_AUDIO_MS,
    LIVE_PARTIAL_UPDATE_MS,
    MAX_SENTENCE_MS,
    SAMPLE_RATE,
    build_default_runtime_config,
    code_to_language,
)
from app.realtime.session import build_room_snapshot
from app.schemas.api import TranscriptItem
from app.services.clients import make_client
from app.services.recovery import load_or_recover_room
from app.services.persistence import persist_finalized_segment, sync_room_persisted_session
from app.services.recordings import (
    append_room_recording_transcript_item,
    pause_room_recording,
    start_or_resume_room_recording,
)
from app.services.rooms import (
    broadcast_room_state,
    clear_room_session,
    register_room_connection,
    remove_room_segment,
    send_room_connection_payload,
    unregister_room_connection,
    update_room_segment,
)
from app.services.translation import ensure_room_translation, transcribe_snapshot, translate_live_text
from app.state.rooms import (
    ROOMS,
    ROOMS_LOCK,
    get_or_create_room,
    get_room_or_404,
    is_valid_room_id,
    normalize_client_session_id,
    normalize_room_id,
    serialize_room_state,
)
from app.utils.text import looks_sentence_complete, normalize_text

router = APIRouter()


@router.websocket("/ws/translate")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    print("Client connected")

    cfg = build_default_runtime_config()

    whisper_client = make_client(cfg["whisper"]["base_url"], cfg["whisper"]["api_key"])
    llm_client = make_client(cfg["llm"]["base_url"], cfg["llm"]["api_key"])
    send_lock = asyncio.Lock()
    room_id: str | None = None
    role = "presenter"
    client_session_id = uuid.uuid4().hex
    attendee_target_language = DEFAULT_TARGET_LANGUAGE

    vad_iterator = VADIterator(model, threshold=0.5, sampling_rate=SAMPLE_RATE)
    sentence_pcm = np.zeros((0,), dtype=np.int16)

    is_speaking = False
    saw_end = False
    silence_after_end_ms = 0
    segment_sequence = 0
    active_segment_id: str | None = None
    active_segment_started_ts: int | None = None
    last_partial_analysis_at = 0.0
    pending_snapshot: dict[str, Any] | None = None
    analysis_task: asyncio.Task | None = None
    recent_final_items: list[TranscriptItem] = []
    emitted_segments: dict[str, dict[str, Any]] = {}
    segment_final_requested: dict[str, bool] = {}
    session_epoch = 0

    async def send_json(payload: dict[str, Any]) -> None:
        async with send_lock:
            await websocket.send_json(payload)

    async def send_snapshot_to_current_client() -> None:
        nonlocal attendee_target_language
        if not room_id:
            return
        target = cfg["tgt"] if role == "presenter" else attendee_target_language
        snapshot = await build_room_snapshot(
            room_id,
            role=role,
            target_language=target,
            llm_client=llm_client,
            llm_model=cfg["llm"]["model"],
        )
        await send_json(snapshot)

    async def broadcast_snapshots_to_room() -> None:
        if not room_id:
            return
        normalized = normalize_room_id(room_id)
        async with ROOMS_LOCK:
            room = ROOMS.get(normalized)
            if room is None:
                return
            connections = list(room.get("connections") or [])
        failed: list[WebSocket] = []
        for connection in connections:
            conn_role = connection.get("role") or "attendee"
            conn_target = (
                cfg["tgt"]
                if conn_role == "presenter"
                else connection.get("target_language") or DEFAULT_TARGET_LANGUAGE
            )
            payload = await build_room_snapshot(
                normalized,
                role=conn_role,
                target_language=conn_target,
                llm_client=llm_client,
                llm_model=cfg["llm"]["model"],
            )
            ok = await send_room_connection_payload(connection, payload)
            if not ok and connection.get("websocket") is not None:
                failed.append(connection["websocket"])
        for stale_websocket in failed:
            await unregister_room_connection(normalized, stale_websocket)

    async def broadcast_segment_to_room(segment: dict[str, Any]) -> None:
        if not room_id:
            return
        normalized = normalize_room_id(room_id)
        async with ROOMS_LOCK:
            room = ROOMS.get(normalized)
            if room is None:
                return
            connections = list(room.get("connections") or [])
            presenter_tgt = room.get("presenter_tgt") or cfg["tgt"]
        failed: list[WebSocket] = []
        for connection in connections:
            conn_role = connection.get("role") or "attendee"
            target_language = (
                presenter_tgt
                if conn_role == "presenter"
                else connection.get("target_language") or presenter_tgt
            )
            translation = (segment.get("translations") or {}).get(target_language, "")
            if target_language == segment.get("src"):
                translation = segment.get("original") or ""
            elif (segment.get("original") or "").strip() and not translation:
                translation = await ensure_room_translation(
                    normalized,
                    segment_id=segment.get("segment_id") or "",
                    revision=int(segment.get("revision") or 0),
                    original=segment.get("original") or "",
                    src=segment.get("src") or cfg["src"],
                    target_language=target_language,
                    llm_client=llm_client,
                    llm_model=cfg["llm"]["model"],
                )
            payload = {
                "type": "segment",
                "segment_id": segment.get("segment_id") or "",
                "revision": int(segment.get("revision") or 0),
                "status": segment.get("status") or "listening",
                "is_final": bool(segment.get("is_final")),
                "original": segment.get("original") or "",
                "translation": translation,
                "src": segment.get("src") or cfg["src"],
                "tgt": target_language,
                "ts_ms": segment.get("ts_ms"),
            }
            ok = await send_room_connection_payload(connection, payload)
            if not ok and connection.get("websocket") is not None:
                failed.append(connection["websocket"])
        for stale_websocket in failed:
            await unregister_room_connection(normalized, stale_websocket)

    def next_segment_id() -> str:
        nonlocal segment_sequence
        segment_sequence += 1
        session_prefix = (
            re.sub(r"[^a-z0-9]", "", (client_session_id or "session").strip().lower())[:8]
            or "session"
        )
        return f"seg-{session_prefix}-{segment_sequence:04d}"

    def ensure_active_segment() -> tuple[str, int]:
        nonlocal active_segment_id, active_segment_started_ts
        if active_segment_id is None:
            active_segment_id = next_segment_id()
            active_segment_started_ts = int(time.time() * 1000)
        return active_segment_id, int(active_segment_started_ts or int(time.time() * 1000))

    def reset_active_stream_state() -> None:
        nonlocal sentence_pcm, is_speaking, saw_end, silence_after_end_ms
        nonlocal active_segment_id, active_segment_started_ts, last_partial_analysis_at
        sentence_pcm = np.zeros((0,), dtype=np.int16)
        is_speaking = False
        saw_end = False
        silence_after_end_ms = 0
        active_segment_id = None
        active_segment_started_ts = None
        last_partial_analysis_at = 0.0
        vad_iterator.reset_states()

    async def cancel_analysis() -> None:
        nonlocal analysis_task, pending_snapshot
        pending_snapshot = None
        task = analysis_task
        analysis_task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            print("Error while cancelling live analysis task")
            traceback.print_exc()

    async def emit_segment_update(
        *,
        segment_id: str,
        src: str,
        tgt: str,
        original: str,
        translation: str,
        ts_ms: int,
        is_final: bool,
    ) -> None:
        nonlocal recent_final_items
        original = (original or "").strip()
        translation = (translation or "").strip()
        if not original and not translation:
            return

        if not is_final and segment_final_requested.get(segment_id):
            return

        segment_state = emitted_segments.setdefault(
            segment_id,
            {"revision": 0, "original": "", "translation": "", "is_final": False},
        )

        previous_is_final = bool(segment_state.get("is_final"))
        if (
            normalize_text(original) == normalize_text(segment_state.get("original", ""))
            and normalize_text(translation) == normalize_text(segment_state.get("translation", ""))
            and previous_is_final == is_final
        ):
            return

        segment_state["revision"] = int(segment_state.get("revision", 0)) + 1
        segment_state["original"] = original
        segment_state["translation"] = translation
        segment_state["is_final"] = is_final
        status = "final"
        if not is_final:
            status = "listening" if segment_state["revision"] == 1 else "refining"

        room_segment = await update_room_segment(
            room_id or uuid.uuid4().hex,
            segment_id=segment_id,
            revision=segment_state["revision"],
            status=status,
            is_final=is_final,
            original=original,
            src=src,
            ts_ms=ts_ms,
            tgt=tgt,
            translation=translation,
        )
        await broadcast_segment_to_room(room_segment)

        if is_final and not previous_is_final and original:
            normalized_room_id = room_id or uuid.uuid4().hex
            await append_room_recording_transcript_item(
                normalized_room_id,
                segment_id=segment_id,
                item=TranscriptItem(
                    original=original,
                    translation=translation,
                    src=src,
                    tgt=tgt,
                    ts_ms=ts_ms,
                ),
            )
            recorded_for_export = False
            async with ROOMS_LOCK:
                room = ROOMS.get(normalize_room_id(normalized_room_id))
                if room is not None:
                    recorded_for_export = segment_id in (room.get("recording_segment_ids") or set())
            await persist_finalized_segment(
                normalized_room_id,
                segment_id=segment_id,
                revision=segment_state["revision"],
                included_in_recording=recorded_for_export,
                source_text=original,
                source_language=src,
                translations_json=dict(room_segment.get("translations") or {}),
                ts_ms=ts_ms,
            )

        if is_final and not previous_is_final and original and LIVE_CONTEXT_TURNS > 0:
            recent_final_items.append(
                TranscriptItem(
                    original=original,
                    translation=translation,
                    src=src,
                    tgt=tgt,
                    ts_ms=ts_ms,
                )
            )
            if len(recent_final_items) > LIVE_CONTEXT_TURNS:
                recent_final_items = recent_final_items[-LIVE_CONTEXT_TURNS:]

    async def analyze_snapshot(snapshot: dict[str, Any]) -> None:
        if snapshot.get("epoch") != session_epoch:
            return

        try:
            original = await transcribe_snapshot(
                pcm16_sentence=snapshot["pcm16_sentence"],
                src=snapshot["src"],
                whisper_client=whisper_client,
                whisper_model=cfg["whisper"]["model"],
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            print("Live transcription failed")
            traceback.print_exc()
            return

        if snapshot.get("epoch") != session_epoch or not original:
            return

        translation = ""
        try:
            translation = await translate_live_text(
                text=original,
                src=snapshot["src"],
                tgt=snapshot["tgt"],
                recent_items=snapshot.get("recent_items") or [],
                llm_client=llm_client,
                llm_model=cfg["llm"]["model"],
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            print("Live translation failed")
            traceback.print_exc()

        if snapshot.get("epoch") != session_epoch:
            return

        await emit_segment_update(
            segment_id=snapshot["segment_id"],
            src=snapshot["src"],
            tgt=snapshot["tgt"],
            original=original,
            translation=translation,
            ts_ms=snapshot["ts_ms"],
            is_final=bool(snapshot.get("is_final")),
        )

    async def process_pending_snapshots() -> None:
        nonlocal analysis_task, pending_snapshot
        try:
            while True:
                snapshot = pending_snapshot
                pending_snapshot = None
                if snapshot is None:
                    return
                await analyze_snapshot(snapshot)
        finally:
            analysis_task = None
            if pending_snapshot is not None:
                analysis_task = asyncio.create_task(process_pending_snapshots())

    def queue_snapshot(*, segment_id: str, ts_ms: int, src: str, tgt: str, is_final: bool) -> None:
        nonlocal pending_snapshot, analysis_task
        if sentence_pcm.size == 0:
            return
        if is_final:
            segment_final_requested[segment_id] = True
        pending_snapshot = {
            "epoch": session_epoch,
            "segment_id": segment_id,
            "ts_ms": ts_ms,
            "src": src,
            "tgt": tgt,
            "is_final": is_final,
            "pcm16_sentence": sentence_pcm.copy(),
            "recent_items": list(recent_final_items),
        }
        if analysis_task is None:
            analysis_task = asyncio.create_task(process_pending_snapshots())

    try:
        while True:
            msg = await websocket.receive()
            if msg.get("text") is not None:
                try:
                    payload = json.loads(msg["text"])
                except Exception:
                    continue

                if payload.get("type") == "join":
                    requested_role = str(payload.get("role") or "presenter").strip().lower()
                    role = "attendee" if requested_role == "attendee" else "presenter"
                    if role == "presenter":
                        client_session_id = normalize_client_session_id(payload.get("client_session_id"))
                    requested_target = str(payload.get("target_language") or cfg["tgt"]).strip().lower()
                    requested_room_id = str(payload.get("room_id") or "").strip().lower()
                    if role == "attendee":
                        if not is_valid_room_id(requested_room_id):
                            await send_json({"type": "error", "detail": "Enter a valid presenter room code."})
                            await websocket.close(code=1008)
                            continue
                        try:
                            room = await get_room_or_404(requested_room_id)
                        except HTTPException:
                            room = await load_or_recover_room(requested_room_id)
                            if room is None:
                                await send_json({
                                    "type": "error",
                                    "detail": "That room does not exist. Check the presenter room code and try again.",
                                })
                                await websocket.close(code=1008)
                                continue
                        room_id = room["room_id"]
                    else:
                        room = None
                        if requested_room_id:
                            room = await load_or_recover_room(requested_room_id)
                        if room is not None:
                            room_id = room["room_id"]
                        else:
                            room_id, room = await get_or_create_room(requested_room_id)
                    if role == "presenter":
                        cfg["src"] = room.get("src") or cfg["src"]
                        cfg["tgt"] = room.get("presenter_tgt") or cfg["tgt"]
                        cfg["whisper"].update(room.get("whisper") or {})
                        cfg["llm"].update(room.get("llm") or {})
                        attendee_target_language = cfg["tgt"]
                    else:
                        attendee_target_language = (
                            requested_target
                            if requested_target in code_to_language
                            else room.get("presenter_tgt") or DEFAULT_TARGET_LANGUAGE
                        )
                    whisper_client = make_client(cfg["whisper"]["base_url"], cfg["whisper"]["api_key"])
                    llm_client = make_client(cfg["llm"]["base_url"], cfg["llm"]["api_key"])
                    await register_room_connection(
                        room_id,
                        websocket=websocket,
                        role=role,
                        target_language=attendee_target_language,
                        send_lock=send_lock,
                    )
                    if role == "presenter":
                        await sync_room_persisted_session(room_id)
                    await send_json(
                        {
                            "type": "joined",
                            "role": role,
                            "room_id": room_id,
                            "src": cfg["src"],
                            "tgt": cfg["tgt"] if role == "presenter" else attendee_target_language,
                        }
                    )
                    await send_json(serialize_room_state(room))
                    await send_snapshot_to_current_client()
                    continue

                if payload.get("type") == "config":
                    if role != "presenter" or not room_id:
                        continue
                    src = (payload.get("src") or "").strip().lower()
                    tgt = (payload.get("tgt") or "").strip().lower()
                    if src in code_to_language:
                        cfg["src"] = src
                    if tgt in code_to_language:
                        cfg["tgt"] = tgt

                    w = payload.get("whisper") or {}
                    if isinstance(w, dict):
                        if w.get("base_url") is not None:
                            new_url = str(w.get("base_url") or "").strip()
                            if new_url:
                                cfg["whisper"]["base_url"] = new_url
                        if w.get("api_key") is not None:
                            new_key = str(w.get("api_key") or "").strip()
                            if new_key:
                                cfg["whisper"]["api_key"] = new_key
                        if w.get("model") is not None:
                            cfg["whisper"]["model"] = str(w.get("model") or "").strip()

                    l = payload.get("llm") or {}
                    if isinstance(l, dict):
                        if l.get("base_url") is not None:
                            new_url = str(l.get("base_url") or "").strip()
                            if new_url:
                                cfg["llm"]["base_url"] = new_url
                        if l.get("api_key") is not None:
                            new_key = str(l.get("api_key") or "").strip()
                            if new_key:
                                cfg["llm"]["api_key"] = new_key
                        if l.get("model") is not None:
                            cfg["llm"]["model"] = str(l.get("model") or "").strip()

                    whisper_client = make_client(cfg["whisper"]["base_url"], cfg["whisper"]["api_key"])
                    llm_client = make_client(cfg["llm"]["base_url"], cfg["llm"]["api_key"])

                    async with ROOMS_LOCK:
                        room = ROOMS.get(room_id)
                        if room is not None:
                            room["src"] = cfg["src"]
                            room["presenter_tgt"] = cfg["tgt"]
                            room["whisper"] = dict(cfg["whisper"])
                            room["llm"] = dict(cfg["llm"])
                            room["updated_at"] = time.time()

                    session_epoch += 1
                    recent_final_items = []
                    segment_final_requested = {}
                    await cancel_analysis()
                    reset_active_stream_state()
                    await sync_room_persisted_session(room_id)

                    await send_json(
                        {
                            "type": "ack",
                            "room_id": room_id,
                            "src": cfg["src"],
                            "tgt": cfg["tgt"],
                            "whisper": {"base_url": cfg["whisper"]["base_url"], "model": cfg["whisper"]["model"]},
                            "llm": {"base_url": cfg["llm"]["base_url"], "model": cfg["llm"]["model"]},
                        }
                    )
                    await broadcast_room_state(room_id)
                    await send_snapshot_to_current_client()
                    continue

                if payload.get("type") == "set_target_language":
                    if role != "attendee" or not room_id:
                        continue
                    requested = str(payload.get("target_language") or "").strip().lower()
                    if requested not in code_to_language:
                        continue
                    attendee_target_language = requested
                    async with ROOMS_LOCK:
                        room = ROOMS.get(room_id)
                        if room is not None:
                            for connection in room.get("connections") or []:
                                if connection.get("websocket") is websocket:
                                    connection["target_language"] = requested
                                    break
                            room["updated_at"] = time.time()
                    await send_snapshot_to_current_client()
                    continue

                if payload.get("type") == "recording_boundary":
                    if role != "presenter" or not room_id:
                        continue
                    stale_segment_id = active_segment_id
                    session_epoch += 1
                    await cancel_analysis()
                    reset_active_stream_state()
                    if stale_segment_id and not bool((emitted_segments.get(stale_segment_id) or {}).get("is_final")):
                        emitted_segments.pop(stale_segment_id, None)
                        segment_final_requested.pop(stale_segment_id, None)
                        removed = await remove_room_segment(room_id, segment_id=stale_segment_id)
                        if removed:
                            await broadcast_snapshots_to_room()
                    continue

                if payload.get("type") == "recording":
                    if role != "presenter" or not room_id:
                        continue
                    action = str(payload.get("action") or "").strip().lower()
                    if action == "start":
                        await start_or_resume_room_recording(room_id, client_session_id=client_session_id)
                        await broadcast_room_state(room_id)
                    if action == "pause":
                        await pause_room_recording(room_id, client_session_id=client_session_id)
                        await broadcast_room_state(room_id)
                    continue

                if payload.get("type") == "clear_session":
                    if role != "presenter" or not room_id:
                        continue
                    session_epoch += 1
                    recent_final_items = []
                    emitted_segments = {}
                    segment_final_requested = {}
                    await cancel_analysis()
                    reset_active_stream_state()
                    await clear_room_session(room_id)
                    await broadcast_room_state(room_id)
                    await broadcast_snapshots_to_room()
                continue

            data = msg.get("bytes")
            if role != "presenter" or not room_id or not data:
                continue

            pcm_chunk = np.frombuffer(data, dtype=np.int16)
            if pcm_chunk.size == 0:
                continue

            samples_t = pcm16le_bytes_to_float_tensor(data)
            if samples_t.numel() == 0:
                continue

            chunk_ms = int(len(pcm_chunk) / SAMPLE_RATE * 1000)
            vad_event = vad_iterator(samples_t)

            if vad_event is not None:
                if "start" in vad_event:
                    if not is_speaking:
                        print("👉 Speech started")
                        is_speaking = True
                        sentence_pcm = np.zeros((0,), dtype=np.int16)
                        active_segment_id, active_segment_started_ts = ensure_active_segment()
                    saw_end = False
                    silence_after_end_ms = 0

                if "end" in vad_event and is_speaking:
                    print("🟡 Speech ended (VAD). Waiting for tail silence…")
                    saw_end = True
                    silence_after_end_ms = 0

            if is_speaking:
                sentence_pcm = np.concatenate([sentence_pcm, pcm_chunk], axis=0)
                active_segment_id, active_segment_started_ts = ensure_active_segment()

            if is_speaking and saw_end:
                silence_after_end_ms += chunk_ms

            if is_speaking:
                sentence_ms = int(len(sentence_pcm) / SAMPLE_RATE * 1000)
                now = time.monotonic()
                if (
                    active_segment_id is not None
                    and sentence_ms >= LIVE_MIN_PARTIAL_AUDIO_MS
                    and now - last_partial_analysis_at >= LIVE_PARTIAL_UPDATE_MS / 1000.0
                ):
                    src, tgt = cfg["src"], cfg["tgt"]
                    queue_snapshot(
                        segment_id=active_segment_id,
                        ts_ms=int(active_segment_started_ts or int(time.time() * 1000)),
                        src=src,
                        tgt=tgt,
                        is_final=False,
                    )
                    last_partial_analysis_at = now

                if sentence_ms > MAX_SENTENCE_MS:
                    print(f"⚠️ Max sentence length reached; forcing flush. [{sentence_ms} ms]")
                    src, tgt = cfg["src"], cfg["tgt"]
                    segment_id = active_segment_id
                    started_ts = int(active_segment_started_ts or int(time.time() * 1000))
                    if segment_id is not None:
                        queue_snapshot(
                            segment_id=segment_id,
                            ts_ms=started_ts,
                            src=src,
                            tgt=tgt,
                            is_final=True,
                        )

                    reset_active_stream_state()
                    continue

            if is_speaking and saw_end:
                latest_original = ""
                if active_segment_id is not None:
                    latest_original = ((emitted_segments.get(active_segment_id) or {}).get("original") or "").strip()
                required_silence_ms = (
                    LIVE_INCOMPLETE_FINALIZE_MS
                    if latest_original and not looks_sentence_complete(latest_original)
                    else LIVE_COMPLETE_SILENCE_MS
                )
                if silence_after_end_ms >= required_silence_ms:
                    sentence_ms = int(len(sentence_pcm) / SAMPLE_RATE * 1000)
                    print(
                        "✅ End of sentence confirmed. Processing… "
                        f"[{sentence_ms} ms, silence={silence_after_end_ms} ms, required={required_silence_ms} ms]"
                    )

                    src, tgt = cfg["src"], cfg["tgt"]
                    segment_id = active_segment_id
                    started_ts = int(active_segment_started_ts or int(time.time() * 1000))
                    if segment_id is not None:
                        queue_snapshot(
                            segment_id=segment_id,
                            ts_ms=started_ts,
                            src=src,
                            tgt=tgt,
                            is_final=True,
                        )

                    reset_active_stream_state()

    except WebSocketDisconnect:
        print("Client disconnected")
    except Exception as exc:
        print(f"Fatal WS error: {exc}")
        try:
            await websocket.close()
        except Exception:
            pass
    finally:
        session_epoch += 1
        await cancel_analysis()
        await unregister_room_connection(room_id, websocket)
        if role == "presenter" and room_id:
            await pause_room_recording(room_id, client_session_id=client_session_id)
            await broadcast_room_state(room_id)
