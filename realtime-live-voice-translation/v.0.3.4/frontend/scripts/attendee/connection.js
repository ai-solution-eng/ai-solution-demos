(() => {
    const app = window.AttendeeApp;
    const { refs } = app;

    async function connectToRoom() {
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
            app.state.joinRejected = true;
            app.state.connected = false;
            app.setConnectionStatus("Connection failed", "error");
            return;
        }

        app.state.ws = new WebSocket(app.WS_URL);

        app.state.ws.onopen = () => {
            app.state.ws.send(JSON.stringify({
                type: "join",
                role: "attendee",
                room_id: app.state.roomId,
                target_language: app.state.targetLanguage
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
                    app.syncRecordingUI();
                    return;
                }
                if (msg.type === "snapshot") {
                    app.state.roomId = msg.room_id || app.state.roomId;
                    app.state.targetLanguage = msg.tgt || app.state.targetLanguage;
                    refs.languageSelectEl.value = app.state.targetLanguage;
                    app.state.recordingState = msg.recording_state || app.state.recordingState;
                    app.state.recordingSessionId = msg.recording_session_id || "";
                    app.state.canDownloadPackage = !!msg.can_download_package;
                    refs.roomInputEl.value = app.state.roomId;
                    app.setRoomLabel();
                    app.applySnapshot(msg.segments || []);
                    return;
                }
                if (msg.type !== "segment") return;
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
