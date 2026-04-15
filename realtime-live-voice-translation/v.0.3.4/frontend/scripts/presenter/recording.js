(() => {
    const app = window.PresenterApp;
    const { refs } = app;

    function setRecordingBanner(mode) {
        if (!refs.recordingBannerEl) return;
        refs.recordingBannerEl.classList.toggle("visible", mode === "recording");
    }

    function showRecordConsentModal(show) {
        if (!refs.recordModalEl) return;
        refs.recordModalEl.classList.toggle("visible", !!show);
    }

    function showNewRoomConfirmModal(show) {
        if (!refs.newRoomConfirmModalEl) return;
        refs.newRoomConfirmModalEl.classList.toggle("visible", !!show);
    }

    function updateRecordButtonState() {
        if (!refs.recordBtn) return;
        const hasLiveSession = !!app.state.joined
            && !!app.state.ws
            && app.state.ws.readyState === WebSocket.OPEN
            && !!app.state.micStream;
        const canStartRecording = hasLiveSession && refs.startBtn.disabled;
        const canResumeRecording = hasLiveSession && app.state.recordingState === "paused";
        const recordingBusy = app.state.recordingState === "recording" || app.state.recordingActive;
        refs.recordBtn.disabled = !(canStartRecording || canResumeRecording) || recordingBusy;

        if (recordingBusy) {
            refs.recordBtn.textContent = "Recording";
        } else if (app.state.recordingState === "paused") {
            refs.recordBtn.textContent = "Resume recording";
        } else if (app.state.canDownloadPackage) {
            refs.recordBtn.textContent = "Recorded";
        } else {
            refs.recordBtn.textContent = "Record";
        }
    }

    function updateStopButtonState() {
        if (!refs.stopBtn) return;
        const sessionActive = refs.startBtn.disabled || app.state.recordingState === "paused";
        if (app.state.recordingState === "recording" && app.state.recordingActive) {
            refs.stopBtn.textContent = "Pause recording";
        } else if (app.state.recordingState === "paused") {
            refs.stopBtn.textContent = "Stop live";
        } else {
            refs.stopBtn.textContent = "Stop";
        }
        refs.stopBtn.disabled = !sessionActive;
    }

    function syncPresenterRecordingUI() {
        setRecordingBanner(app.state.recordingState);
        updateRecordButtonState();
        updateStopButtonState();
        app.syncDownloadAvailability();
        app.updatePreviousRoomNote();
    }

    async function activateRecordingSession({ resetExisting = false } = {}) {
        if (!app.state.micStream) {
            alert("Start the live session before enabling recording.");
            return false;
        }

        if (resetExisting) {
            app.recordedTranscript.length = 0;
            app.resetRecordingState();
        }

        try {
            await requestRoomRecordingStart();
            sendRecordingBoundaryMarker();
            await startVoiceRecording(app.state.micStream);
            app.state.recordingApproved = true;
            app.state.recordingActive = true;
            app.state.recordingState = "recording";
            app.state.canDownloadPackage = false;
            syncPresenterRecordingUI();
            app.setStatus("Recording in progress");
            return true;
        } catch (error) {
            console.error(error);
            if (app.state.recordingState === "recording" && !app.state.recordingActive) {
                try {
                    await requestRoomRecordingPause();
                } catch (pauseError) {
                    console.error(pauseError);
                }
            }
            alert(`Could not start recording. ${error?.message || "Please check microphone permissions and try again."}`.trim());
            return false;
        }
    }

    async function beginRecordingAfterConsent() {
        try {
            await activateRecordingSession({ resetExisting: true });
        } finally {
            showRecordConsentModal(false);
        }
    }

    async function resumeRecording() {
        await activateRecordingSession({ resetExisting: false });
    }

    function getPreferredRecordingMimeType() {
        const candidates = [
            "audio/webm;codecs=opus",
            "audio/webm",
            "audio/ogg;codecs=opus",
            "audio/ogg",
            "audio/mp4"
        ];
        for (const type of candidates) {
            if (window.MediaRecorder && MediaRecorder.isTypeSupported && MediaRecorder.isTypeSupported(type)) {
                return type;
            }
        }
        return "";
    }

    function extensionForMimeType(mimeType) {
        if (!mimeType) return "webm";
        if (mimeType.includes("ogg")) return "ogg";
        if (mimeType.includes("mp4")) return "mp4";
        if (mimeType.includes("mpeg")) return "mp3";
        return "webm";
    }

    function hasVoiceRecording() {
        return !!app.state.recordingSessionId;
    }

    async function waitForRecordingUploads() {
        await Promise.resolve(app.state.recordingUploadQueue);
        if (app.state.recordingUploadError) {
            const error = app.state.recordingUploadError;
            app.state.recordingUploadError = null;
            throw error;
        }
    }

    function queueRecordingChunkUpload(chunk) {
        const uploadTask = Promise.resolve(app.state.recordingUploadQueue).then(async () => {
            if (!chunk || chunk.size === 0) return;
            if (!app.state.roomId) throw new Error("No active room is available.");
            if (!app.state.recordingSessionId) throw new Error("The room recording session is not ready yet.");

            const ext = extensionForMimeType(chunk.type || app.state.recordingMimeType);
            const formData = new FormData();
            formData.append("audio", chunk, `chunk.${ext}`);
            formData.append("recording_session_id", app.state.recordingSessionId);
            formData.append("client_session_id", app.state.presenterClientSessionId);
            formData.append("mime_type", chunk.type || app.state.recordingMimeType || "audio/webm");

            const response = await fetch(`${app.HTTP_BASE}/api/rooms/${encodeURIComponent(app.state.roomId)}/recording/chunk`, {
                method: "POST",
                body: formData
            });
            if (!response.ok) {
                const detail = await response.text().catch(() => "");
                throw new Error(detail || `Recording chunk upload failed with status ${response.status}`);
            }
        });

        app.state.recordingUploadQueue = uploadTask.catch((error) => {
            console.error(error);
            if (!app.state.recordingUploadError) {
                app.state.recordingUploadError = error;
            }
        });

        return uploadTask;
    }

    async function requestRoomRecordingStart() {
        if (!app.state.roomId) throw new Error("No active room is available.");
        const response = await fetch(`${app.HTTP_BASE}/api/rooms/${encodeURIComponent(app.state.roomId)}/recording/start`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ client_session_id: app.state.presenterClientSessionId })
        });
        if (!response.ok) {
            const detail = await response.text().catch(() => "");
            throw new Error(detail || `Recording start failed with status ${response.status}`);
        }
        const payload = await response.json();
        app.setRoomState(payload);
        return payload;
    }

    async function requestRoomRecordingPause() {
        if (!app.state.roomId) throw new Error("No active room is available.");
        const response = await fetch(`${app.HTTP_BASE}/api/rooms/${encodeURIComponent(app.state.roomId)}/recording/pause`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ client_session_id: app.state.presenterClientSessionId })
        });
        if (!response.ok) {
            const detail = await response.text().catch(() => "");
            throw new Error(detail || `Recording pause failed with status ${response.status}`);
        }
        const payload = await response.json();
        app.setRoomState(payload);
        return payload;
    }

    async function requestRoomRecordingFinalize({ roomId = app.state.roomId, recordingSessionId = app.state.recordingSessionId, applyState = true } = {}) {
        if (!roomId) throw new Error("No active room is available.");
        if (!recordingSessionId) throw new Error("No paused recording is available to stop.");
        const response = await fetch(`${app.HTTP_BASE}/api/rooms/${encodeURIComponent(roomId)}/recording/finalize`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ recording_session_id: recordingSessionId })
        });
        if (!response.ok) {
            const detail = await response.text().catch(() => "");
            throw new Error(detail || `Recording stop failed with status ${response.status}`);
        }
        const payload = await response.json();
        if (applyState) app.setRoomState(payload);
        return payload;
    }

    function sendRecordingBoundaryMarker() {
        if (!app.state.ws || app.state.ws.readyState !== WebSocket.OPEN) return;
        app.state.ws.send(JSON.stringify({ type: "recording_boundary" }));
    }

    async function startVoiceRecording(stream) {
        if (!window.MediaRecorder) {
            throw new Error("MediaRecorder is not available in this browser.");
        }

        app.state.recordingUploadQueue = Promise.resolve();
        app.state.recordingUploadError = null;

        const mimeType = getPreferredRecordingMimeType();
        app.state.recordingMimeType = mimeType;

        const options = mimeType ? { mimeType } : undefined;
        const recorder = new MediaRecorder(stream, options);
        recorder.ondataavailable = (event) => {
            if (event.data && event.data.size > 0) {
                queueRecordingChunkUpload(event.data);
            }
        };
        recorder.start(1000);
        app.state.mediaRecorder = recorder;
    }

    async function stopVoiceRecording() {
        if (!app.state.mediaRecorder) {
            await waitForRecordingUploads();
            return;
        }
        const recorder = app.state.mediaRecorder;
        app.state.mediaRecorder = null;

        await new Promise((resolve) => {
            const previousOnStop = recorder.onstop;
            recorder.onstop = () => {
                if (typeof previousOnStop === "function") previousOnStop();
                resolve();
            };
            try {
                recorder.stop();
            } catch {
                resolve();
            }
        });
        app.state.recordingActive = false;
        await waitForRecordingUploads();
    }

    async function stopLiveSession({ statusText = "Connected" } = {}) {
        app.setStatus("Stopping…");
        await stopVoiceRecording();

        if (app.state.animationId) cancelAnimationFrame(app.state.animationId);
        app.state.animationId = null;

        if (app.state.workletNode) {
            try {
                app.state.workletNode.disconnect();
            } catch {
            }
        }
        app.state.workletNode = null;

        if (app.state.audioCtx) {
            try {
                await app.state.audioCtx.close();
            } catch {
            }
        }
        app.state.audioCtx = null;

        if (app.state.micStream) {
            for (const track of app.state.micStream.getTracks()) track.stop();
        }
        app.state.micStream = null;
        app.state.analyser = null;

        app.state.recordingActive = false;
        syncPresenterRecordingUI();

        app.setStatus(statusText);
        refs.startBtn.disabled = false;
        updateStopButtonState();
    }

    async function pauseRecordingOnly() {
        app.setStatus("Pausing recording…");
        await stopVoiceRecording();
        app.state.recordingActive = false;
        await requestRoomRecordingPause();
        app.setStatus("Live");
        syncPresenterRecordingUI();
    }

    async function addVoiceRecordingFiles() {
        return;
    }

    async function startLiveSession() {
        if (!app.state.ws || app.state.ws.readyState !== WebSocket.OPEN) return;
        app.sendConfig();

        try {
            app.setStatus("Requesting mic…");

            app.state.micStream = await navigator.mediaDevices.getUserMedia({
                audio: {
                    deviceId: refs.micSelect?.value ? { exact: refs.micSelect.value } : undefined,
                    echoCancellation: true,
                    noiseSuppression: true,
                    autoGainControl: true
                }
            });

            app.state.audioCtx = new (window.AudioContext || window.webkitAudioContext)();
            if (app.state.audioCtx.state === "suspended") {
                await app.state.audioCtx.resume().catch(() => {
                });
            }
            await app.state.audioCtx.audioWorklet.addModule("./pcm-worklet.js");

            const source = app.state.audioCtx.createMediaStreamSource(app.state.micStream);
            app.state.workletNode = new AudioWorkletNode(app.state.audioCtx, "pcm-worklet");

            app.state.analyser = app.state.audioCtx.createAnalyser();
            app.state.analyser.fftSize = 256;
            source.connect(app.state.analyser);
            app.drawWaveform();

            app.state.workletNode.port.onmessage = (event) => {
                if (!app.state.ws || app.state.ws.readyState !== WebSocket.OPEN) return;
                app.state.ws.send(event.data);
            };

            const gain = app.state.audioCtx.createGain();
            gain.gain.value = 0.0;

            source.connect(app.state.workletNode);
            app.state.workletNode.connect(gain).connect(app.state.audioCtx.destination);

            app.state.recordingActive = false;
            refs.startBtn.disabled = true;
            syncPresenterRecordingUI();
            app.setStatus("Live");
            updateStopButtonState();
        } catch (error) {
            console.error(error);
            app.setStatus("Error");
            syncPresenterRecordingUI();
        }
    }

    async function handleStopAction() {
        try {
            if (app.state.recordingState === "recording" && app.state.recordingActive) {
                await pauseRecordingOnly();
                return;
            }
            if (app.state.recordingState === "paused") {
                await stopLiveSession({ statusText: "Connected" });
                return;
            }
            await stopLiveSession();
        } catch (error) {
            console.error(error);
            app.setStatus("Error");
            alert(`Could not finish the recording session. ${error?.message || ""}`.trim());
        }
    }

    async function createConfirmedNewRoom() {
        if (app.state.recordingActive || app.state.recordingState === "recording" || !refs.stopBtn.disabled) {
            alert("Stop the current live session before creating a new room.");
            return;
        }

        const shouldReconnect = !!(app.state.ws && app.state.ws.readyState === WebSocket.OPEN);
        const previousRoomId = app.state.roomId;
        const previousRecordingSessionId = app.state.recordingSessionId;
        const shouldFinalizePreviousRoom = !!(previousRoomId && previousRecordingSessionId);
        refs.newRoomBtn.disabled = true;
        refs.connectBtn.disabled = true;
        app.setStatus("Creating room…");

        try {
            if (shouldFinalizePreviousRoom) {
                app.setStatus("Finalizing room…");
                await requestRoomRecordingFinalize({
                    roomId: previousRoomId,
                    recordingSessionId: previousRecordingSessionId,
                    applyState: false
                });
                app.state.previousRoomId = previousRoomId;
            } else {
                app.state.previousRoomId = "";
            }

            const roomId = await app.requestNewRoomId();
            app.state.roomId = roomId;
            app.presenterRoomInitPromise = Promise.resolve(roomId);
            app.setCookie(app.ROOM_COOKIE_NAME, roomId);
            app.updateRoomBadge();
            app.setPresenterRoomInputValue(roomId);
            app.updatePreviousRoomNote();
            app.resetUIAndContext();

            if (app.state.ws) {
                try {
                    app.state.ws.close();
                } catch {
                }
            }
            app.state.ws = null;
            app.state.joined = false;

            if (shouldReconnect) {
                app.openPresenterConnection();
            } else {
                app.setStatus("Idle");
                refs.connectBtn.disabled = false;
            }
        } catch (error) {
            console.error(error);
            app.setStatus("Error");
            refs.connectBtn.disabled = false;
            alert(`Could not create a new room. ${error?.message || ""}`.trim());
        } finally {
            refs.newRoomBtn.disabled = false;
        }
    }

    Object.assign(app, {
        setRecordingBanner,
        showRecordConsentModal,
        showNewRoomConfirmModal,
        updateRecordButtonState,
        updateStopButtonState,
        syncPresenterRecordingUI,
        activateRecordingSession,
        beginRecordingAfterConsent,
        resumeRecording,
        getPreferredRecordingMimeType,
        extensionForMimeType,
        hasVoiceRecording,
        waitForRecordingUploads,
        queueRecordingChunkUpload,
        requestRoomRecordingStart,
        requestRoomRecordingPause,
        requestRoomRecordingFinalize,
        sendRecordingBoundaryMarker,
        startVoiceRecording,
        stopVoiceRecording,
        stopLiveSession,
        pauseRecordingOnly,
        addVoiceRecordingFiles,
        startLiveSession,
        handleStopAction,
        createConfirmedNewRoom
    });
})();
