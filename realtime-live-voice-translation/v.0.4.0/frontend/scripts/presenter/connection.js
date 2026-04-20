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
            const roomId = requestedRoomId || await app.ensureBackendPresenterRoomId();
            app.state.ws.send(JSON.stringify({
                type: "join",
                role: "presenter",
                room_id: roomId,
                target_language: refs.tgtLangEl.value,
                client_session_id: app.state.presenterClientSessionId
            }));
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
                if (msg.type === "joined") {
                    app.state.joined = true;
                    if (msg.room_id) {
                        app.state.roomId = msg.room_id;
                        app.setCookie(app.ROOM_COOKIE_NAME, msg.room_id);
                        app.updateRoomBadge();
                        app.setPresenterRoomInputValue(msg.room_id);
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
                    return;
                }
                if (msg.type === "fact_check") {
                    app.applyFactCheckResult({
                        segment_id: msg.segment_id,
                        revision: msg.revision,
                        fact_check: msg.fact_check || {}
                    }, { notify: true });
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
                    ts_ms,
                    finalized_at_ms: msg.finalized_at_ms ?? null,
                    fact_check: msg.fact_check || {}
                });

                app.renderTranscriptView();
                app.syncDownloadAvailability();
            } catch {
            }
        };
    }

    app.openPresenterConnection = openPresenterConnection;
})();
