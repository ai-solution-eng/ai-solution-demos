(() => {
    const app = window.PresenterApp;
    const { refs } = app;

    function openPresenterConnection(requestedRoomId = "") {
        app.state.ws = new WebSocket(app.WS_URL);
        app.state.ws.binaryType = "arraybuffer";
        app.state.joined = false;

        app.setStatus("Connecting…");
        app.log("Connecting to " + app.WS_URL);

        app.state.ws.onopen = async () => {
            try {
                const roomId = requestedRoomId || await app.ensureBackendPresenterRoomId();
                // The JOIN CARRIES the presenter's UI pair: whatever the
                // presenter selected (or swapped) before connecting is the
                // pair they intend. Without src/tgt here, a new room boots
                // with server defaults and the joined-adoption then resets
                // the UI to them ("Connect changes my languages" bug).
                app.state.ws.send(JSON.stringify({
                    type: "join",
                    role: "presenter",
                    room_id: roomId,
                    src: refs.srcLangEl.value,
                    target_language: refs.tgtLangEl.value,
                    client_session_id: app.state.presenterClientSessionId,
                    presenter_token: app.state.presenterToken || ""
                }));
            } catch (error) {
                // Room bootstrap (POST /api/rooms) failed: the join message
                // was never sent, so without this the connection would sit
                // open and silent forever ("nothing happens" on Connect).
                console.error("Presenter join bootstrap failed:", error);
                app.setStatus("Room setup failed");
                app.log("Could not prepare the room: " + (error?.message || error));
                try {
                    app.state.ws?.close();
                } catch {
                }
            }
        };

        app.state.ws.onclose = () => {
            app.setStatus("Disconnected");
            app.state.joined = false;
            refs.startBtn.disabled = true;
            refs.stopBtn.disabled = true;
            refs.connectBtn.disabled = false;
            refs.clearBtn.disabled = true;
            refs.swapBtn.disabled = true;
            app.state.ws = null;
            app.state.micStream = null;
            app.state.recordingActive = false;
            app.state.mediaRecorder = null;
            if (app.state.recordingState === "recording") {
                app.state.recordingState = "paused";
            }
            app.syncPresenterRecordingUI();
        };

        app.state.ws.onerror = () => app.setStatus("WS error");

        app.state.ws.onmessage = (event) => {
            try {
                const msg = JSON.parse(event.data);
                if (msg.type === "error") {
                    app.state.joined = false;
                    app.setStatus("Rejected");
                    app.log(msg.detail || "The server rejected the connection.");
                    // Auth rejection → surface the recovery escape hatch.
                    if ((msg.detail || "").toLowerCase().includes("authentication")) {
                        app.showReentryPrompt();
                    }
                    try {
                        app.state.ws?.close();
                    } catch {
                    }
                    return;
                }
                if (msg.type === "joined") {
                    app.state.joined = true;
                    if (msg.room_id) {
                        app.state.roomId = msg.room_id;
                        app.setCookie(app.ROOM_COOKIE_NAME, msg.room_id);
                        app.updateRoomBadge();
                        app.setPresenterRoomInputValue(msg.room_id);
                    }
                    // Adopt the ROOM's language pair before sending any config:
                    // the local UI may be stale (e.g. backend defaults after a
                    // page reload). Without this, sendConfig() below would
                    // silently flip the room (and every attendee) back to the
                    // stale UI language.
                    if (msg.src || msg.tgt) {
                        app.applyLanguagePair(msg.src || refs.srcLangEl.value, msg.tgt || refs.tgtLangEl.value);
                    }
                    app.setStatus("Connected");
                    refs.startBtn.disabled = false;
                    refs.connectBtn.disabled = true;
                    refs.clearBtn.disabled = false;
                    refs.swapBtn.disabled = false;
                    app.sendConfig();
                    app.syncPresenterRecordingUI();
                    return;
                }
                if (msg.type === "room_state") {
                    app.setRoomState(msg);
                    // Same stale-UI guard on room_state broadcasts: adopt the
                    // room's language pair whenever the server reports one.
                    if (msg.src || msg.presenter_tgt) {
                        app.applyLanguagePair(msg.src || refs.srcLangEl.value, msg.presenter_tgt || msg.tgt || refs.tgtLangEl.value);
                    }
                    return;
                }
                if (msg.type === "snapshot") {
                    if (msg.room_id) {
                        app.state.roomId = msg.room_id;
                        app.setCookie(app.ROOM_COOKIE_NAME, msg.room_id);
                        app.updateRoomBadge();
                        app.setPresenterRoomInputValue(msg.room_id);
                    }
                    if (msg.src || msg.presenter_tgt) {
                        app.applyLanguagePair(msg.src || refs.srcLangEl.value, msg.presenter_tgt || msg.tgt || refs.tgtLangEl.value);
                    }
                    app.setRoomState(msg);
                    app.applySnapshotSegments(Array.isArray(msg.segments) ? msg.segments : []);
                    return;
                }
                if (msg.type === "ack") {
                    if (msg.room_id) {
                        app.state.roomId = msg.room_id;
                        app.setCookie(app.ROOM_COOKIE_NAME, msg.room_id);
                        app.updateRoomBadge();
                        app.setPresenterRoomInputValue(msg.room_id);
                    }
                    if (app.tts && msg.tts && msg.tts.voice) {
                        refs.ttsVoiceEl.value = msg.tts.voice;
                    }
                    if (app.tts && app.tts.loadVoices) {
                        app.tts.loadVoices();
                    }
                    return;
                }
                if (msg.type !== "segment") return;

                const original = (msg.original ?? "").trim();
                const translation = (msg.translation ?? "").trim();
                const ts_ms = msg.ts_ms ?? Date.now();
                const src = (msg.src ?? refs.srcLangEl.value ?? "").trim();
                const tgt = (msg.tgt ?? refs.tgtLangEl.value ?? "").trim();
                const segment_id = (msg.segment_id ?? "").trim();
                const revision = Number(msg.revision ?? 0) || 0;
                const status = (msg.status ?? "listening").trim();
                const is_final = !!msg.is_final;

                if (!segment_id || (!original && !translation)) return;

                const current = app.transcript.find((item) => item.segment_id === segment_id);
                if (current && revision && revision < (current.revision || 0)) return;

                app.upsertTranscriptSegment({
                    segment_id,
                    revision,
                    status,
                    is_final,
                    original,
                    translation,
                    src,
                    tgt,
                    ts_ms
                });

                app.renderTranscriptView();
                app.syncDownloadAvailability();
            } catch {
            }
        };
    }

    app.openPresenterConnection = openPresenterConnection;
})();
