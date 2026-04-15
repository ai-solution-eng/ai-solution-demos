(() => {
    const shared = window.RealtimeTranslationShared;
    const refs = {
        connectBtn: document.getElementById("connectBtn"),
        startBtn: document.getElementById("startBtn"),
        stopBtn: document.getElementById("stopBtn"),
        downloadBtn: document.getElementById("downloadBtn"),
        clearBtn: document.getElementById("clearBtn"),
        recordBtn: document.getElementById("recordBtn"),
        micSelect: document.getElementById("micSelect"),
        swapBtn: document.getElementById("swapBtn"),
        srcLangEl: document.getElementById("srcLang"),
        tgtLangEl: document.getElementById("tgtLang"),
        srcLangFieldEl: document.getElementById("srcLangField"),
        tgtLangFieldEl: document.getElementById("tgtLangField"),
        srcLangTriggerEl: document.getElementById("srcLangTrigger"),
        tgtLangTriggerEl: document.getElementById("tgtLangTrigger"),
        srcLangPickerEl: document.getElementById("srcLangPicker"),
        tgtLangPickerEl: document.getElementById("tgtLangPicker"),
        srcLangSearchEl: document.getElementById("srcLangSearch"),
        tgtLangSearchEl: document.getElementById("tgtLangSearch"),
        srcLangListEl: document.getElementById("srcLangList"),
        tgtLangListEl: document.getElementById("tgtLangList"),
        srcLangEmptyEl: document.getElementById("srcLangEmpty"),
        tgtLangEmptyEl: document.getElementById("tgtLangEmpty"),
        whisperModelEl: document.getElementById("whisperModel"),
        whisperBaseUrlEl: document.getElementById("whisperBaseUrl"),
        whisperApiKeyEl: document.getElementById("whisperApiKey"),
        toggleKeysBtnWhisper: document.getElementById("toggleKeysBtnWhisper"),
        llmModelEl: document.getElementById("llmModel"),
        llmBaseUrlEl: document.getElementById("llmBaseUrl"),
        llmApiKeyEl: document.getElementById("llmApiKey"),
        exportOverlayEl: document.getElementById("exportOverlay"),
        exportBarEl: document.getElementById("exportBar"),
        exportStageEl: document.getElementById("exportStage"),
        exportDetailEl: document.getElementById("exportDetail"),
        exportPercentEl: document.getElementById("exportPercent"),
        toggleKeysBtnLLM: document.getElementById("toggleKeysBtnLLM"),
        recordingBannerEl: document.getElementById("recordingBanner"),
        recordModalEl: document.getElementById("recordConsentModal"),
        recordAcknowledgeBtn: document.getElementById("recordAcknowledgeBtn"),
        recordDeclineBtn: document.getElementById("recordDeclineBtn"),
        newRoomConfirmModalEl: document.getElementById("newRoomConfirmModal"),
        newRoomConfirmBtn: document.getElementById("newRoomConfirmBtn"),
        newRoomCancelBtn: document.getElementById("newRoomCancelBtn"),
        roomCodeEl: document.getElementById("roomCode"),
        roomCodeChipEl: document.getElementById("roomCodeChip"),
        newRoomBtn: document.getElementById("newRoomBtn"),
        copyRoomLinkBtn: document.getElementById("copyRoomLinkBtn"),
        previousRoomNoteEl: document.getElementById("previousRoomNote"),
        previousRoomCodeEl: document.getElementById("previousRoomCode"),
        downloadPreviousRoomBtn: document.getElementById("downloadPreviousRoomBtn"),
        presenterRoomInputEl: document.getElementById("presenterRoomInput"),
        statusEl: document.getElementById("status"),
        logEl: document.getElementById("log"),
        stackEl: document.getElementById("stack"),
        centerEl: document.getElementById("center"),
        historyPanelEl: document.getElementById("historyPanel"),
        historyListEl: document.getElementById("historyList"),
        historyCountEl: document.getElementById("historyCount"),
        factCheckPanelEl: document.getElementById("factCheckPanel"),
        factCheckListEl: document.getElementById("factCheckList"),
        factCheckCountEl: document.getElementById("factCheckCount"),
        factCheckToastEl: document.getElementById("factCheckToast"),
        canvas: document.getElementById("waveCanvas")
    };

    const canvasCtx = refs.canvas.getContext("2d");
    refs.canvas.width = 720;
    refs.canvas.height = 180;

    const HTTP_BASE = shared.resolveBackendHttpBase();
    const WS_URL = shared.resolveBackendWsUrl(HTTP_BASE);
    const ROOM_COOKIE_NAME = "realtime-voice-room-id";

    const app = {
        shared,
        refs,
        canvasCtx,
        HTTP_BASE,
        WS_URL,
        ROOM_COOKIE_NAME,
        presenterRoomInitPromise: null,
        transcript: [],
        recordedTranscript: [],
        state: {
            ws: null,
            audioCtx: null,
            workletNode: null,
            micStream: null,
            analyser: null,
            animationId: null,
            mediaRecorder: null,
            presenterClientSessionId: window.crypto?.randomUUID?.() || `presenter-${Date.now()}`,
            recordingSessionId: "",
            recordingMimeType: "",
            recordingUploadQueue: Promise.resolve(),
            recordingUploadError: null,
            recordingApproved: false,
            recordingActive: false,
            recordingState: "idle",
            canDownloadPackage: false,
            roomId: "",
            previousRoomId: "",
            joined: false,
            factCheckToastTimer: null
        }
    };

    app.log = function log(message) {
        refs.logEl.textContent += message + "\n";
        refs.logEl.scrollTop = refs.logEl.scrollHeight;
    };

    app.setStatus = function setStatus(status) {
        refs.statusEl.textContent = status;

        const lower = (status || "").toLowerCase();
        let tone = "warning";
        if (lower.includes("error") || lower.includes("failed")) tone = "error";
        else if (lower.includes("live")) tone = "live";
        else if (lower.includes("connected")) tone = "connected";
        else if (lower.includes("idle") || lower.includes("disconnected") || lower.includes("requesting") || lower.includes("connecting") || lower.includes("recording")) tone = "warning";
        else tone = "ready";

        refs.statusEl.dataset.tone = tone;
    };

    app.setCookie = function setCookie(name, value, days = 7) {
        const expires = new Date(Date.now() + days * 24 * 60 * 60 * 1000).toUTCString();
        document.cookie = `${name}=${encodeURIComponent(value)}; expires=${expires}; path=/; SameSite=Lax`;
    };

    app.getCookie = function getCookie(name) {
        const prefix = `${name}=`;
        return document.cookie
            .split(";")
            .map((part) => part.trim())
            .find((part) => part.startsWith(prefix))
            ?.slice(prefix.length) || "";
    };

    app.ensurePresenterRoomId = function ensurePresenterRoomId() {
        const existing = (app.state.roomId || decodeURIComponent(app.getCookie(ROOM_COOKIE_NAME) || "")).trim();
        if (!existing) return "";
        app.state.roomId = existing;
        app.setCookie(ROOM_COOKIE_NAME, existing);
        app.updateRoomBadge();
        return existing;
    };

    app.attendeeLink = function attendeeLink() {
        const roomId = app.ensurePresenterRoomId();
        if (!roomId) return "";
        const url = new URL("/attendee.html", window.location.origin);
        url.searchParams.set("room", roomId);
        url.searchParams.set("lang", (refs.tgtLangEl?.value || "es").trim().toLowerCase() || "es");
        if (HTTP_BASE && HTTP_BASE !== window.location.origin) {
            url.searchParams.set("backend", HTTP_BASE);
        }
        return url.toString();
    };

    app.requestNewRoomId = async function requestNewRoomId(existingRoomId = "") {
        const requestPayload = existingRoomId ? { room_id: existingRoomId } : {};
        const response = await fetch(`${HTTP_BASE}/api/rooms`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(requestPayload)
        });
        if (!response.ok) {
            const detail = await response.text().catch(() => "");
            throw new Error(detail || `Room creation failed with status ${response.status}`);
        }
        const responsePayload = await response.json();
        const createdRoomId = (responsePayload.room_id || "").trim();
        if (!createdRoomId) throw new Error("Room creation did not return a room code.");
        return createdRoomId;
    };

    app.initializePresenterRoomId = async function initializePresenterRoomId() {
        const existing = decodeURIComponent(app.getCookie(ROOM_COOKIE_NAME) || "").trim();
        const roomId = await app.requestNewRoomId(existing);
        app.state.roomId = roomId;
        app.setCookie(ROOM_COOKIE_NAME, roomId);
        app.updateRoomBadge();
        return roomId;
    };

    app.ensureBackendPresenterRoomId = async function ensureBackendPresenterRoomId() {
        if (app.presenterRoomInitPromise) return await app.presenterRoomInitPromise;
        app.presenterRoomInitPromise = app.initializePresenterRoomId().catch((error) => {
            app.presenterRoomInitPromise = null;
            throw error;
        });
        const roomId = await app.presenterRoomInitPromise;
        app.presenterRoomInitPromise = Promise.resolve(roomId);
        return roomId;
    };

    app.updateRoomBadge = function updateRoomBadge() {
        const roomId = app.state.roomId || decodeURIComponent(app.getCookie(ROOM_COOKIE_NAME) || "").trim() || "Unassigned";
        if (refs.roomCodeEl) refs.roomCodeEl.textContent = roomId;
        if (refs.roomCodeChipEl) refs.roomCodeChipEl.title = roomId;
    };

    app.setPresenterRoomInputValue = function setPresenterRoomInputValue(roomId = "") {
        if (!refs.presenterRoomInputEl) return;
        refs.presenterRoomInputEl.value = roomId || "";
    };

    app.getRequestedPresenterRoomId = function getRequestedPresenterRoomId() {
        return (refs.presenterRoomInputEl?.value || "").trim().toLowerCase();
    };

    app.updatePreviousRoomNote = function updatePreviousRoomNote() {
        if (!refs.previousRoomNoteEl || !refs.previousRoomCodeEl || !refs.downloadPreviousRoomBtn) return;
        const hasPreviousRoom = !!app.state.previousRoomId;
        refs.previousRoomNoteEl.hidden = !hasPreviousRoom;
        refs.previousRoomCodeEl.textContent = app.state.previousRoomId || "-";
        refs.downloadPreviousRoomBtn.disabled = !hasPreviousRoom;
    };

    app.currentRoomHasResumableRecording = function currentRoomHasResumableRecording() {
        return !!app.state.recordingSessionId && app.state.recordingState !== "stopped";
    };

    app.setRoomState = function setRoomState(payload = {}) {
        if (Object.prototype.hasOwnProperty.call(payload, "recording_state")) {
            app.state.recordingState = payload.recording_state || "idle";
        }
        if (Object.prototype.hasOwnProperty.call(payload, "recording_session_id")) {
            app.state.recordingSessionId = payload.recording_session_id || "";
        }
        app.state.canDownloadPackage = !!payload.can_download_package;
        if (payload.room_id) {
            app.state.roomId = payload.room_id;
            app.setCookie(ROOM_COOKIE_NAME, payload.room_id);
            app.updateRoomBadge();
            app.setPresenterRoomInputValue(payload.room_id);
        }
        if (app.state.recordingState !== "recording") {
            app.state.recordingActive = false;
        }
        if (typeof app.syncPresenterRecordingUI === "function") {
            app.syncPresenterRecordingUI();
        }
    };

    app.loadDefaultsFromBackend = async function loadDefaultsFromBackend() {
        try {
            const response = await fetch(`${HTTP_BASE}/defaults`);
            if (!response.ok) return;
            const defaults = await response.json();

            if (defaults.src || defaults.tgt) {
                app.applyLanguagePair(defaults.src || refs.srcLangEl.value, defaults.tgt || refs.tgtLangEl.value);
            }

            if (defaults.whisper?.base_url) refs.whisperBaseUrlEl.value = defaults.whisper.base_url;
            if (defaults.whisper?.model) refs.whisperModelEl.value = defaults.whisper.model;

            if (defaults.llm?.base_url) refs.llmBaseUrlEl.value = defaults.llm.base_url;
            if (defaults.llm?.model) refs.llmModelEl.value = defaults.llm.model;

            if (defaults.whisper?.has_api_key) refs.whisperApiKeyEl.placeholder = "WHISPER_API_KEY (set on server)";
            if (defaults.llm?.has_api_key) refs.llmApiKeyEl.placeholder = "LLM_API_KEY (set on server)";
        } catch (error) {
            console.warn("Could not load defaults:", error);
        }
    };

    app.hideFactCheckToast = function hideFactCheckToast() {
        if (app.state.factCheckToastTimer) {
            clearTimeout(app.state.factCheckToastTimer);
            app.state.factCheckToastTimer = null;
        }
        if (!refs.factCheckToastEl) return;
        refs.factCheckToastEl.classList.remove("visible");
    };

    app.showFactCheckToast = function showFactCheckToast(
        message = "Some statements may be misleading or false"
    ) {
        if (!refs.factCheckToastEl) return;
        refs.factCheckToastEl.textContent = message;
        refs.factCheckToastEl.classList.add("visible");
        if (app.state.factCheckToastTimer) clearTimeout(app.state.factCheckToastTimer);
        app.state.factCheckToastTimer = setTimeout(() => {
            refs.factCheckToastEl.classList.remove("visible");
            app.state.factCheckToastTimer = null;
        }, 4200);
    };

    app.sendConfig = function sendConfig() {
        if (!app.state.ws || app.state.ws.readyState !== WebSocket.OPEN) return;

        const whisperKey = refs.whisperApiKeyEl.value.trim();
        const llmKey = refs.llmApiKeyEl.value.trim();

        const payload = {
            type: "config",
            src: refs.srcLangEl.value,
            tgt: refs.tgtLangEl.value,
            whisper: {
                base_url: refs.whisperBaseUrlEl.value.trim(),
                model: refs.whisperModelEl.value.trim()
            },
            llm: {
                base_url: refs.llmBaseUrlEl.value.trim(),
                model: refs.llmModelEl.value.trim()
            }
        };

        if (whisperKey) payload.whisper.api_key = whisperKey;
        if (llmKey) payload.llm.api_key = llmKey;

        app.state.ws.send(JSON.stringify(payload));
    };

    app.drawWaveform = function drawWaveform() {
        if (!app.state.analyser) return;
        app.state.animationId = requestAnimationFrame(app.drawWaveform);

        const bufferLength = app.state.analyser.frequencyBinCount;
        const dataArray = new Uint8Array(bufferLength);
        app.state.analyser.getByteTimeDomainData(dataArray);

        canvasCtx.fillStyle = "rgba(0,0,0,0.2)";
        canvasCtx.fillRect(0, 0, refs.canvas.width, refs.canvas.height);

        canvasCtx.lineWidth = 2;
        canvasCtx.strokeStyle = "#00ffcc";
        canvasCtx.beginPath();

        const sliceWidth = refs.canvas.width / bufferLength;
        let x = 0;

        for (let index = 0; index < bufferLength; index += 1) {
            const value = dataArray[index] / 128.0;
            const y = value * refs.canvas.height / 2;
            if (index === 0) canvasCtx.moveTo(x, y);
            else canvasCtx.lineTo(x, y);
            x += sliceWidth;
        }

        canvasCtx.lineTo(refs.canvas.width, refs.canvas.height / 2);
        canvasCtx.stroke();
    };

    app.populateMics = async function populateMics() {
        try {
            const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
            stream.getTracks().forEach((track) => track.stop());
        } catch {
        }

        const devices = await navigator.mediaDevices.enumerateDevices();
        const mics = devices.filter((device) => device.kind === "audioinput");

        refs.micSelect.innerHTML = "";
        for (const mic of mics) {
            const option = document.createElement("option");
            option.value = mic.deviceId;
            option.textContent = mic.label || `Microphone (${mic.deviceId.slice(0, 6)}…)`;
            refs.micSelect.appendChild(option);
        }

        if (mics.length === 0) {
            const option = document.createElement("option");
            option.value = "";
            option.textContent = "No microphones found";
            refs.micSelect.appendChild(option);
        }
    };

    window.PresenterApp = app;
})();
