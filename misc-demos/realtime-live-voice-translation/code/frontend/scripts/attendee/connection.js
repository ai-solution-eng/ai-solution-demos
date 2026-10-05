(() => {
    const app = window.AttendeeApp;
    const { refs } = app;

    async function connectToRoom(retryCount = 0) {
        const roomId = refs.roomInputEl.value.trim();
        if (!roomId) {
            alert("Enter a room code first.");
            return;
        }

        if (app.state.ws && app.state.ws.readyState === WebSocket.OPEN) {
            app.state.ws.close();
        }

        app.state.roomId = roomId;
        app.state.joinRejected = false;
        app.setRoomLabel();
        app.setConnectionStatus("Connecting", "warning");

        try {
            const verifiedRoomId = await app.ensureJoinableRoom(roomId);
            app.state.roomId = verifiedRoomId;
            refs.roomInputEl.value = verifiedRoomId;
            app.setRoomLabel();
        } catch (error) {
            // Autojoin robustness: an attendee may open the share link a
            // moment before the room is reachable (or while the backend is
            // mid-restart). Retry a couple of times with a short delay
            // before giving up; manual joins get the same courtesy.
            if (retryCount < 2) {
                app.setConnectionStatus("Retrying…", "warning");
                app.setFooterNote(`Room not ready — retrying (${retryCount + 2}/3)…`);
                setTimeout(() => {
                    connectToRoom(retryCount + 1);
                }, 1500);
                return;
            }
            app.state.joinRejected = true;
            app.state.connected = false;
            app.setConnectionStatus("Connection failed", "error");
            app.setFooterNote(error?.message || "Could not join the presenter room.");
            return;
        }

        app.state.ws = new WebSocket(app.WS_URL);

        app.state.ws.onopen = () => {
            app.state.ws.send(JSON.stringify({
                type: "join",
                role: "attendee",
                room_id: app.state.roomId,
                // Explicit choice ONLY: the boot default (core.js "es") is
                // NOT a choice — sending it as target_language would masquerade
                // as one and pin the attendee to the hardcoded default instead
                // of the room's current target. app.state.joinLangParam is set
                // ONLY when a real ?lang= URL param is present; a picker click
                // after joining goes via set_target_language instead.
                target_language: app.state.joinLangParam || ""
            }));
        };

        app.state.ws.onmessage = (event) => {
            try {
                const msg = JSON.parse(event.data);
                if (msg.type === "error") {
                    app.state.connected = false;
                    app.state.joinRejected = true;
                    app.setConnectionStatus("Connection failed", "error");
                    app.setFooterNote(msg.detail || "Could not join the presenter room.");
                    try {
                        app.state.ws?.close();
                    } catch {
                    }
                    return;
                }
                if (msg.type === "joined") {
                    app.state.connected = true;
                    app.state.roomId = msg.room_id || app.state.roomId;
                    refs.roomInputEl.value = app.state.roomId;
                    app.setRoomLabel();
                    app.setConnectionStatus("Connected", "connected");
                    // Flush any language choice made while the socket was
                    // down — otherwise the picker shows a language the
                    // server never learned.
                    if (app.state.pendingTargetLanguage) {
                        const pending = app.state.pendingTargetLanguage;
                        app.state.pendingTargetLanguage = "";
                        app.sendTargetLanguageToServer(pending);
                    }
                    if (app.tts) app.tts.onRoomJoined(msg);
                    return;
                }
                if (msg.type === "room_state") {
                    app.state.recordingState = msg.recording_state || app.state.recordingState;
                    app.state.recordingSessionId = msg.recording_session_id || "";
                    app.state.canDownloadPackage = !!msg.can_download_package;
                    if (msg.room_id) {
                        app.state.roomId = msg.room_id;
                        refs.roomInputEl.value = msg.room_id;
                        app.setRoomLabel();
                    }
                    // §2x: attendee targets are ATTENDEE-OWNED. The room's
                    // presenter_tgt is NOT adopted here — the attendee's
                    // starting target came from the link (?lang= or the
                    // room's target at join), and from then on only THE
                    // ATTENDEE changes it. (The old follow-the-presenter
                    // adoption made the attendee select track the
                    // presenter on every config broadcast.)
                    if (app.tts) app.tts.onRoomState(msg);
                    app.syncRecordingUI();
                    return;
                }
                if (msg.type === "snapshot") {
                    // The post-switch snapshot has arrived: any segments that
                    // still lack a translation are genuinely missing (not
                    // "about to be retranslated"), so clear the pending mask.
                    app.state.langSwitchPending = false;
                    app.state.roomId = msg.room_id || app.state.roomId;
                    // §2x: the snapshot's msg.tgt is THIS connection's own
                    // target (the server resolves it per-connection), so
                    // adopting it keeps the select in sync with what the
                    // server is actually translating for us — but only the
                    // attendee's own actions put a different value there.
                    if (msg.tgt) {
                        app.setTargetLanguage(msg.tgt, { notify: false });
                    }
                    app.state.recordingState = msg.recording_state || app.state.recordingState;
                    app.state.recordingSessionId = msg.recording_session_id || "";
                    app.state.canDownloadPackage = !!msg.can_download_package;
                    refs.roomInputEl.value = app.state.roomId;
                    app.setRoomLabel();
                    if (app.tts) app.tts.onRoomState(msg);
                    app.applySnapshot(msg.segments || []);
                    return;
                }
                if (msg.type !== "segment") return;
                // Revision guard (ported from the presenter client): the
                // server interleaves segment messages and full snapshots on
                // this connection, so a stale partial can arrive after a
                // newer revision and flicker the text backwards.
                const prevSegment = app.state.segments.find(
                    (item) => item.segment_id === msg.segment_id
                );
                if (prevSegment && Number(msg.revision || 0) < Number(prevSegment.revision || 0)) return;
                // The SERVER owns the truth about this connection's target:
                // every segment carries the target the server is actually
                // translating into (msg.tgt). If the local picker disagrees
                // (a choice that never reached the server — e.g. sent while
                // the socket was down), re-adopt the server's value so the
                // picker always shows what is REALLY being delivered.
                if (msg.tgt && msg.tgt !== app.state.targetLanguage) {
                    app.setTargetLanguage(msg.tgt, { notify: false });
                }
                app.upsertSegment({
                    segment_id: msg.segment_id,
                    revision: msg.revision,
                    status: msg.status,
                    is_final: !!msg.is_final,
                    original: msg.original || "",
                    translation: msg.translation || "",
                    src: msg.src || "",
                    tgt: msg.tgt || app.state.targetLanguage,
                    ts_ms: msg.ts_ms || Date.now()
                });
                app.renderTranscriptView();
                app.syncRecordingUI();
            } catch (error) {
                console.error(error);
            }
        };

        app.state.ws.onerror = () => {
            if (app.state.joinRejected) return;
            app.setConnectionStatus("Connection failed", "error");
            app.setFooterNote("Could not reach the presenter room.");
        };

        app.state.ws.onclose = () => {
            app.state.connected = false;
            if (app.state.joinRejected) return;
            app.setConnectionStatus("Disconnected", "warning");
        };
    }

    app.connectToRoom = connectToRoom;
})();
