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
from app.utils.auth import verify_presenter_token
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
    remember_room_translation_language,
    serialize_room_state,
)
from app.utils.text import looks_sentence_complete, normalize_text

router = APIRouter()


@router.websocket("/ws/translate")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    print("Client connected")

    cfg = build_default_runtime_config()

    asr_client = make_client(cfg["asr"]["base_url"], cfg["asr"]["api_key"])
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
        if not room_id:
            return
        # Target-language authority:
        #   presenter → the room's presenter_tgt (they control it);
        #   attendee  → their OWN chosen target_language (kept across
        #               presenter source swaps by design — the attendee
        #               reads in the language THEY picked).
        normalized = normalize_room_id(room_id)
        async with ROOMS_LOCK:
            room = ROOMS.get(normalized)
            conn = None
            if room is not None:
                conn = next(
                    (c for c in room.get("connections") or [] if c.get("websocket") is websocket),
                    None,
                )
            if role == "presenter":
                target = (room.get("presenter_tgt") if room else None) or cfg["tgt"]
            else:
                target = (conn or {}).get("target_language") or (
                    room.get("presenter_tgt") if room else None
                ) or attendee_target_language
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
            presenter_target = room.get("presenter_tgt") or cfg["tgt"]
        failed: list[WebSocket] = []
        for connection in connections:
            conn_role = connection.get("role") or "attendee"
            # Presenter → room presenter_tgt; attendee → their OWN chosen
            # target (kept across presenter source swaps by design).
            conn_target = (
                presenter_target
                if conn_role == "presenter"
                else connection.get("target_language") or presenter_target
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

        # Resolve per-connection targets first, then translate ALL missing
        # languages in PARALLEL (gather) instead of serially inside the send
        # loop: with live per-attendee partial translation restored (see
        # below), a serial loop would re-introduce head-of-line blocking —
        # one attendee's LLM call delaying every other attendee's segment.
        #
        # Target-language authority (final §2u semantics):
        #   presenter  → the room's presenter_tgt (theirs to control);
        #   attendee   → their OWN chosen target_language. The attendee's
        #                choice SURVIVES presenter source swaps on purpose:
        #                the presenter changing what they speak must not
        #                change what the attendee reads. The per-connection
        #                field is updated live by set_target_language and
        #                synced to the room's presenter_tgt when the
        #                PRESENTER changes it (so a fresh attendee joining
        #                mid-room starts in the room's current target).
        def _target_for(connection: dict[str, Any]) -> str:
            conn_role = connection.get("role") or "attendee"
            return (
                presenter_tgt
                if conn_role == "presenter"
                else connection.get("target_language") or presenter_tgt
            )

        needs_translation: dict[str, str] = {}
        for connection in connections:
            target_language = _target_for(connection)
            if target_language == segment.get("src"):
                continue
            if (segment.get("translations") or {}).get(target_language, "").strip():
                continue
            if (segment.get("original") or "").strip():
                needs_translation[target_language] = segment.get("original") or ""

        async def _translate(target_language: str, original: str) -> tuple[str, str]:
            try:
                translated = await ensure_room_translation(
                    normalized,
                    segment_id=segment.get("segment_id") or "",
                    revision=int(segment.get("revision") or 0),
                    original=original,
                    src=segment.get("src") or cfg["src"],
                    target_language=target_language,
                    llm_client=llm_client,
                    llm_model=cfg["llm"]["model"],
                )
                return target_language, translated
            except Exception:
                # A failed per-language translation must not break the
                # broadcast: ship empty for that language (the client keeps
                # showing whatever it has) and continue.
                print(f"Per-language translation failed for {target_language}")
                traceback.print_exc()
                return target_language, ""

        if needs_translation:
            results = await asyncio.gather(
                *(_translate(lang, original) for lang, original in needs_translation.items())
            )
            for lang, translated in results:
                if translated:
                    segment.setdefault("translations", {})[lang] = translated

        failed: list[WebSocket] = []
        for connection in connections:
            target_language = _target_for(connection)
            translation = (segment.get("translations") or {}).get(target_language, "")
            if target_language == segment.get("src"):
                translation = segment.get("original") or ""
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
            # A final was requested while this partial was in flight. If no
            # final result has actually been emitted yet, promoting this
            # near-final partial beats suppressing it: with a slow LLM the
            # suppression path left viewers on "Translating…" for the whole
            # final round. Track final-emission with a sentinel in the
            # segment state; a later final snapshot (whose text differs)
            # still supersedes this one normally.
            state = emitted_segments.get(segment_id) or {}
            if not state.get("is_final_emitted"):
                is_final = True
                print(f"Promoted in-flight partial to final for {segment_id}")
            else:
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

        if not translation and segment_state.get("translation"):
            translation = segment_state["translation"]

        segment_state["revision"] = int(segment_state.get("revision", 0)) + 1
        segment_state["original"] = original
        segment_state["translation"] = translation
        segment_state["is_final"] = is_final
        if is_final:
            segment_state["is_final_emitted"] = True
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
        # Broadcast failures (including a throw from the inline
        # ensure_room_translation LLM call for a missing attendee language)
        # must not unwind emit_segment_update — that would skip recording +
        # persistence below, silently losing the finalized segment. Log and
        # continue instead; the segment text is already committed to room
        # state, so the next snapshot/broadcast repairs the UI.
        try:
            await broadcast_segment_to_room(room_segment)
        except Exception:
            print("Segment broadcast failed (continuing to persistence)")
            traceback.print_exc()

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
            # Fire-and-forget: the DB write must not gate the next ASR/LLM
            # cycle (a Postgres hiccup would stall the live path). Errors are
            # logged inside persist_finalized_segment; a lost write only
            # affects recovery/history, never the live translation.
            asyncio.create_task(
                persist_finalized_segment(
                    normalized_room_id,
                    segment_id=segment_id,
                    revision=segment_state["revision"],
                    included_in_recording=recorded_for_export,
                    source_text=original,
                    source_language=src,
                    translations_json=dict(room_segment.get("translations") or {}),
                    ts_ms=ts_ms,
                )
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

        asr_drop_reason: list[str] = []
        original = ""
        try:
            original = await transcribe_snapshot(
                pcm16_sentence=snapshot["pcm16_sentence"],
                src=snapshot["src"],
                asr_client=asr_client,
                asr_model=cfg["asr"]["model"],
                drop_reason=asr_drop_reason,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            print("Live transcription failed")
            traceback.print_exc()
            asr_drop_reason.append("asr_error")

        is_final_snapshot = bool(snapshot.get("is_final"))
        if snapshot.get("epoch") != session_epoch:
            return

        if not original:
            # ASR produced nothing usable. For FINALS this must still close
            # the segment: previously the card stayed "Awaiting translated
            # output." with a FINAL chip forever (no error, no log). Close it
            # with whatever the last partial had so the UI shows an honest
            # end state; silent/too-short drops are expected (breathing,
            # clicks) and just finalize empty — the card disappears rather
            # than lying about a translation that never came.
            reason = asr_drop_reason[0] if asr_drop_reason else "unknown"
            if is_final_snapshot:
                previous_state = emitted_segments.get(snapshot["segment_id"]) or {}
                prev_original = (previous_state.get("original") or "").strip()
                prev_translation = (previous_state.get("translation") or "").strip()
                print(
                    f"Final ASR empty (reason={reason!r}) for {snapshot['segment_id']}; "
                    f"closing with previous partial text={bool(prev_original)}"
                )
                if prev_original or prev_translation:
                    await emit_segment_update(
                        segment_id=snapshot["segment_id"],
                        src=snapshot["src"],
                        tgt=snapshot["tgt"],
                        original=prev_original,
                        translation=prev_translation,
                        ts_ms=snapshot["ts_ms"],
                        is_final=True,
                    )
            else:
                print(f"Partial ASR empty (reason={reason!r}) for {snapshot['segment_id']}")
            return

        # Pre-translate dedupe: if ASR returned the same text as the last
        # emitted revision of this segment, the LLM re-translation would be
        # discarded by emit_segment_update's dedupe anyway — skip the call and
        # reuse the previous translation. Cuts ~30-60% of live LLM calls with
        # zero accuracy change. Finals always re-translate so the persisted
        # final text is fresh.
        previous_state = emitted_segments.get(snapshot["segment_id"]) or {}
        translation = ""
        if (
            not snapshot.get("is_final")
            and normalize_text(original) == normalize_text(previous_state.get("original", ""))
            and previous_state.get("translation")
        ):
            translation = previous_state["translation"]
        else:
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
            # Promote-in-place: when the final is requested while a
            # speculative partial (queued at VAD end, before the silence
            # gate) is still pending, convert that queued snapshot to final
            # instead of queueing a duplicate. Otherwise the slow partial
            # completes first, is suppressed by segment_final_requested, and
            # the user stares at "Translating…" until the final's own full
            # ASR+LLM round finishes — the failure seen live with a slow LLM
            # (2-6.5s per call). Promotion keeps ONE chain of work whose
            # result is emitted as the final.
            if pending_snapshot is not None and pending_snapshot.get("segment_id") == segment_id:
                pending_snapshot["is_final"] = True
                print("Final promoted onto queued speculative partial")
                reset_active_stream_state()
                return
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
            # Low-level receive() RETURNS a disconnect message instead of
            # raising WebSocketDisconnect — falling through previously left
            # the handler looping on a dead socket ("Cannot call receive once
            # a disconnect message has been received" on the NEXT event, and
            # no "joined" reply for the client that had already hung up).
            if msg.get("type") == "websocket.disconnect":
                print("Client disconnected (disconnect message)")
                return
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
                    if role == "presenter" and requested_room_id and not verify_presenter_token(
                        requested_room_id, payload.get("presenter_token")
                    ):
                        # Attendees are unaffected: joining an existing room with
                        # the room code alone stays open by design. Presenters
                        # joining an EXISTING room must present the token that
                        # POST /api/rooms issued at creation time. A join with no
                        # room id bootstraps a brand-new room and is allowed
                        # (equivalent to creating a room; it cannot touch one).
                        await send_json({
                            "type": "error",
                            "detail": "Presenter authentication failed. Reopen the presenter page to get a fresh link.",
                        })
                        await websocket.close(code=1008)
                        return
                    if role == "attendee":
                        if not is_valid_room_id(requested_room_id):
                            await send_json({"type": "error", "detail": "Enter a valid presenter room code."})
                            await websocket.close(code=1008)
                            return
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
                                return
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
                        # The presenter's UI pair WINS at join: the join
                        # message carries whatever the presenter selected
                        # (or swapped) before connecting — a new room is
                        # created with THAT pair, and re-entering an
                        # existing room intentionally re-asserts it. (The
                        # old behavior adopted the room's/default pair and
                        # reset the presenter's UI on every Connect.)
                        requested_src = str(payload.get("src") or "").strip().lower()
                        requested_tgt = str(payload.get("target_language") or "").strip().lower()
                        if requested_src in code_to_language:
                            room["src"] = requested_src
                        if requested_tgt in code_to_language:
                            room["presenter_tgt"] = requested_tgt
                        cfg["src"] = room["src"]
                        cfg["tgt"] = room["presenter_tgt"]
                        cfg["asr"].update(room.get("asr") or {})
                        cfg["llm"].update(room.get("llm") or {})
                        attendee_target_language = cfg["tgt"]
                        has_custom_target = False
                        remember_room_translation_language(room, cfg["tgt"])
                    else:
                        # Attendee target resolution (§2u final): an EXPLICIT
                        # join request (picker choice or ?lang= param) makes
                        # the language theirs — it survives presenter target
                        # changes. Without one, they follow the room's
                        # current presenter_tgt.
                        requested = str(payload.get("target_language") or "").strip().lower()
                        has_custom_target = requested in code_to_language
                        attendee_target_language = (
                            requested if has_custom_target
                            else room.get("presenter_tgt") or DEFAULT_TARGET_LANGUAGE
                        )
                    asr_client = make_client(cfg["asr"]["base_url"], cfg["asr"]["api_key"])
                    llm_client = make_client(cfg["llm"]["base_url"], cfg["llm"]["api_key"])
                    # target_language is the attendee's current view language;
                    # has_custom_target marks whether THEY chose it (vs
                    # following the room) — see the config follow-sync.
                    await register_room_connection(
                        room_id,
                        websocket=websocket,
                        role=role,
                        target_language=attendee_target_language,
                        send_lock=send_lock,
                        has_custom_target=has_custom_target,
                    )
                    if role == "presenter":
                        await sync_room_persisted_session(room_id)
                    else:
                        # Register the attendee's chosen language as "touched"
                        # for the export/report (spec: every language touched
                        # by any participant is translated at EXPORT time —
                        # never retro-translated live).
                        async with ROOMS_LOCK:
                            join_room = ROOMS.get(room_id)
                            if join_room is not None:
                                remember_room_translation_language(join_room, attendee_target_language)
                    await send_json(
                        {
                            "type": "joined",
                            "role": role,
                            "room_id": room_id,
                            "src": cfg["src"],
                            "tgt": cfg["tgt"] if role == "presenter" else attendee_target_language,
                            "tts_default_voice": room.get("tts_default_voice") or "",
                            "tts_configured": bool((room.get("tts") or {}).get("base_url")),
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
                    # Guard against a stale presenter UI silently flipping the
                    # room language: a presenter page that reloaded (or never
                    # joined this room's session) can carry backend-default
                    # languages in its form fields. Only apply src/tgt when the
                    # payload EXPLICITLY carries them (field was present and
                    # non-empty). Omitted language fields leave the room pair
                    # untouched. This keeps sendConfig()-on-rejoin from
                    # reverting a language the presenter set before reloading.
                    if "src" in payload and src in code_to_language:
                        cfg["src"] = src
                    if "tgt" in payload and tgt in code_to_language:
                        cfg["tgt"] = tgt

                    w = payload.get("asr") or {}
                    if isinstance(w, dict):
                        if w.get("base_url") is not None:
                            new_url = str(w.get("base_url") or "").strip()
                            if new_url:
                                cfg["asr"]["base_url"] = new_url
                        if w.get("api_key") is not None:
                            new_key = str(w.get("api_key") or "").strip()
                            if new_key:
                                cfg["asr"]["api_key"] = new_key
                        if w.get("model") is not None:
                            cfg["asr"]["model"] = str(w.get("model") or "").strip()

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

                    t = payload.get("tts") or {}
                    if isinstance(t, dict):
                        if t.get("base_url") is not None:
                            new_url = str(t.get("base_url") or "").strip()
                            if new_url:
                                cfg["tts"]["base_url"] = new_url
                        if t.get("api_key") is not None:
                            new_key = str(t.get("api_key") or "").strip()
                            if new_key:
                                cfg["tts"]["api_key"] = new_key
                        if t.get("model") is not None:
                            cfg["tts"]["model"] = str(t.get("model") or "").strip()
                        if t.get("voice") is not None:
                            cfg["tts"]["voice"] = str(t.get("voice") or "").strip()

                    asr_client = make_client(cfg["asr"]["base_url"], cfg["asr"]["api_key"])
                    llm_client = make_client(cfg["llm"]["base_url"], cfg["llm"]["api_key"])

                    async with ROOMS_LOCK:
                        room = ROOMS.get(room_id)
                        if room is not None:
                            room["src"] = cfg["src"]
                            room["presenter_tgt"] = cfg["tgt"]
                            room["asr"] = dict(cfg["asr"])
                            room["llm"] = dict(cfg["llm"])
                            room["tts"] = dict(cfg["tts"])
                            room["tts_default_voice"] = cfg["tts"].get("voice") or ""
                            # Register the NEW target as touched for the export
                            # package (swap en->ja then ja->en = both languages
                            # available at export time).
                            remember_room_translation_language(room, cfg["tgt"])
                            # Attendee targets are ATTENDEE-OWNED from the
                            # moment of join (final §2x semantics, user):
                            # the presenter's target choice reaches attendees
                            # only through the shared link (?lang= param).
                            # After that, presenter target changes NEVER
                            # rewrite an attendee's connection — each
                            # attendee controls their own target for the
                            # rest of the session. The SOURCE still tracks
                            # the presenter (room["src"] above governs what
                            # everyone hears/transcribes).
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
                            "asr": {"base_url": cfg["asr"]["base_url"], "model": cfg["asr"]["model"]},
                            "llm": {"base_url": cfg["llm"]["base_url"], "model": cfg["llm"]["model"]},
                            "tts": {"base_url": cfg["tts"]["base_url"], "model": cfg["tts"]["model"], "voice": cfg["tts"]["voice"]},
                        }
                    )
                    await broadcast_room_state(room_id)
                    await send_snapshot_to_current_client()
                    continue

                if payload.get("type") == "set_target_language":
                    # Attendee chooses their OWN translation language (§2u):
                    # the choice is EXPLICIT, so it survives presenter target
                    # changes (has_custom_target) — even if it coincides with
                    # the room's current target.
                    requested = str(payload.get("target_language") or "").strip().lower()
                    if role != "attendee" or not room_id or requested not in code_to_language:
                        continue
                    attendee_target_language = requested
                    async with ROOMS_LOCK:
                        room = ROOMS.get(room_id)
                        if room is not None:
                            for connection in room.get("connections") or []:
                                if connection.get("websocket") is websocket:
                                    connection["target_language"] = requested
                                    connection["has_custom_target"] = True
                                    break
                            # A touched language counts for the export package
                            # even before any translation is stored.
                            remember_room_translation_language(room, requested)
                            room["updated_at"] = time.time()
                    await send_snapshot_to_current_client()
                    continue

                if payload.get("type") == "tts_config":
                    if not room_id:
                        continue
                    muted = bool(payload.get("muted"))
                    voice = str(payload.get("voice") or "").strip()
                    async with ROOMS_LOCK:
                        room = ROOMS.get(room_id)
                        if room is not None:
                            for connection in room.get("connections") or []:
                                if connection.get("websocket") is websocket:
                                    connection["tts_muted"] = muted
                                    if voice:
                                        connection["tts_voice"] = voice
                                    break
                            room["updated_at"] = time.time()
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
                    # Speculative partial: start transcribing + translating the
                    # near-final audio NOW, during the tail-silence window,
                    # instead of only after it elapses. The final snapshot still
                    # runs afterwards and corrects anything this got wrong, so
                    # accuracy is unaffected; viewers see a near-final
                    # translation ~1-2s earlier. Bypasses the cadence gates on
                    # purpose. Guard: only when enough audio exists to be worth
                    # an ASR call.
                    sentence_ms_now = int(len(sentence_pcm) / SAMPLE_RATE * 1000)
                    if active_segment_id is not None and sentence_ms_now >= LIVE_MIN_PARTIAL_AUDIO_MS:
                        queue_snapshot(
                            segment_id=active_segment_id,
                            ts_ms=int(active_segment_started_ts or int(time.time() * 1000)),
                            src=cfg["src"],
                            tgt=cfg["tgt"],
                            is_final=False,
                        )
                        last_partial_analysis_at = time.monotonic()

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
                        # The stale partial's result would be suppressed by the
                        # segment_final_requested guard anyway — cancel it so
                        # the final doesn't queue behind ~1-2.5s of dead ASR+LLM.
                        await cancel_analysis()
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
                sentence_ms_gate = int(len(sentence_pcm) / SAMPLE_RATE * 1000)
                # Short utterances (below the partial-audio gate) can never have
                # emitted a partial, so latest_original is empty and the
                # incomplete threshold would apply unconditionally — forcing
                # every "yes/OK/sure" to wait the full incomplete gate for no
                # reason. Use the complete threshold when no partial text
                # exists to be wrong about.
                if not latest_original or sentence_ms_gate < LIVE_MIN_PARTIAL_AUDIO_MS:
                    required_silence_ms = LIVE_COMPLETE_SILENCE_MS
                elif not looks_sentence_complete(latest_original):
                    required_silence_ms = LIVE_INCOMPLETE_FINALIZE_MS
                else:
                    required_silence_ms = LIVE_COMPLETE_SILENCE_MS
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
                        # The stale partial's result would be suppressed by the
                        # segment_final_requested guard anyway — cancel it so
                        # the final doesn't queue behind ~1-2.5s of dead ASR+LLM.
                        await cancel_analysis()
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
