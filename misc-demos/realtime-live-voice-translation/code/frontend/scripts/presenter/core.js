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
        asrModelEl: document.getElementById("asrModel"),
        asrBaseUrlEl: document.getElementById("asrBaseUrl"),
        asrApiKeyEl: document.getElementById("asrApiKey"),
        toggleKeysBtnAsr: document.getElementById("toggleKeysBtnAsr"),
        llmModelEl: document.getElementById("llmModel"),
        llmBaseUrlEl: document.getElementById("llmBaseUrl"),
        llmApiKeyEl: document.getElementById("llmApiKey"),
        exportOverlayEl: document.getElementById("exportOverlay"),
        exportBarEl: document.getElementById("exportBar"),
        exportStageEl: document.getElementById("exportStage"),
        exportDetailEl: document.getElementById("exportDetail"),
        exportPercentEl: document.getElementById("exportPercent"),
        toggleKeysBtnLLM: document.getElementById("toggleKeysBtnLLM"),
        ttsModelEl: document.getElementById("ttsModel"),
        ttsBaseUrlEl: document.getElementById("ttsBaseUrl"),
        ttsApiKeyEl: document.getElementById("ttsApiKey"),
        ttsVoiceEl: document.getElementById("ttsVoice"),
        ttsManageBtnEl: document.getElementById("ttsManageBtn"),
        ttsVoiceModalEl: document.getElementById("ttsVoiceModal"),
        ttsModalCloseBtnEl: document.getElementById("ttsModalCloseBtn"),
        ttsVoiceListEl: document.getElementById("ttsVoiceList"),
        ttsVoiceNameInputEl: document.getElementById("ttsVoiceNameInput"),
        ttsVoiceRefTextEl: document.getElementById("ttsVoiceRefText"),
        ttsVoiceRefTextErrorEl: document.getElementById("ttsRefTextError"),
        ttsConsentCheckEl: document.getElementById("ttsConsentCheck"),
        ttsUploadFileBtnEl: document.getElementById("ttsUploadFileBtn"),
        ttsRecordBtnEl: document.getElementById("ttsRecordBtn"),
        ttsFileInputEl: document.getElementById("ttsFileInput"),
        ttsRecordStatusEl: document.getElementById("ttsRecordStatus"),
        ttsWaveCanvasEl: document.getElementById("ttsWaveCanvas"),
        ttsAudioToolsEl: document.getElementById("ttsAudioTools"),
        ttsReplayBtnEl: document.getElementById("ttsReplayBtn"),
        ttsTranscribeStatusEl: document.getElementById("ttsTranscribeStatus"),
        ttsAudioPlayerEl: document.getElementById("ttsAudioPlayer"),
        ttsAcceptBtnEl: document.getElementById("ttsAcceptBtn"),
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
        copyPhoneLinkBtn: document.getElementById("copyPhoneLinkBtn"),
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
        canvas: document.getElementById("waveCanvas")
    };

    const canvasCtx = refs.canvas.getContext("2d");
    refs.canvas.width = 720;
    refs.canvas.height = 180;

    const HTTP_BASE = shared.resolveBackendHttpBase();
    const WS_URL = shared.resolveBackendWsUrl(HTTP_BASE);
    const ROOM_COOKIE_NAME = "realtime-voice-room-id";
    const PRESENTER_TOKEN_COOKIE_NAME = "realtime-voice-presenter-token";
    const RECOVERY_CODE_COOKIE_NAME = "realtime-voice-recovery-code";

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
            presenterToken: "",
            recoveryCode: "",
            joined: false
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

    app.attendeeLink = function attendeeLink(page = "attendee.html") {
        const roomId = app.ensurePresenterRoomId();
        if (!roomId) return "";
        const url = new URL(`/${page}`, window.location.origin);
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
        if (responsePayload.presenter_token) {
            app.state.presenterToken = String(responsePayload.presenter_token);
            app.setCookie(PRESENTER_TOKEN_COOKIE_NAME, app.state.presenterToken);
        }
        if (responsePayload.recovery_code) {
            app.state.recoveryCode = String(responsePayload.recovery_code);
            app.setCookie(RECOVERY_CODE_COOKIE_NAME, app.state.recoveryCode);
        }
        return createdRoomId;
    };

    app.showRoomCredentials = function showRoomCredentials() {
        // The credentials PANEL no longer exists (the chip's ⧉ copies the
        // full block instead). Kept as a no-op for call-site compatibility;
        // the values live only in cookies/state.
    };

    app.showReentryPrompt = function showReentryPrompt() {
        // Shown when the room state suggests the presenter may need to
        // reclaim access (join rejected by auth).
        const note = document.getElementById("reentryNote");
        if (note) note.hidden = false;
    };

    app.recoverRoomAccess = async function recoverRoomAccess(roomId, credential) {
        // Credential: the recovery code OR a previously saved room token —
        // the server accepts either as proof of ownership.
        const trimmed = (credential || "").trim();
        const looksLikeToken = /^[0-9a-f]{40}$/i.test(trimmed);
        const body = looksLikeToken
            ? { presenter_token: trimmed }
            : { recovery_code: trimmed };
        const response = await fetch(`${HTTP_BASE}/api/rooms/${encodeURIComponent(roomId)}/presenter-token`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body)
        });
        if (!response.ok) {
            const detail = await response.text().catch(() => "");
            throw new Error(detail || `Recovery failed with status ${response.status}`);
        }
        const payload = await response.json();
        if (!payload.presenter_token) throw new Error("The server did not return a presenter token.");
        app.state.presenterToken = String(payload.presenter_token);
        app.state.roomId = roomId;
        app.setCookie(PRESENTER_TOKEN_COOKIE_NAME, app.state.presenterToken);
        app.setCookie(ROOM_COOKIE_NAME, roomId);
        app.updateRoomBadge();
        return app.state.presenterToken;
    };

    app.initializePresenterRoomId = async function initializePresenterRoomId() {
        const existing = decodeURIComponent(app.getCookie(ROOM_COOKIE_NAME) || "").trim();
        const existingToken = decodeURIComponent(app.getCookie(PRESENTER_TOKEN_COOKIE_NAME) || "").trim();
        const roomId = await app.requestNewRoomId(existing);
        if (!app.state.presenterToken && existingToken) {
            app.state.presenterToken = existingToken;
        }
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
        // Fetch the backend defaults from whichever path the current
        // deployment actually exposes:
        //   1. /api/defaults  — gateway-safe route (backend with the fix)
        //   2. /defaults      — direct uvicorn (local dev, old backend)
        // Both can return HTML depending on deployment state (nginx
        // try_files fallback / 404 page), so validate the content type and
        // body before parsing — a routing gap then degrades to a single
        // clear warning instead of a JSON SyntaxError.
        const candidates = [`${HTTP_BASE}/api/defaults`, `${HTTP_BASE}/defaults`];
        let defaults = null;
        for (const url of candidates) {
            try {
                const response = await fetch(url, { cache: "no-store" });
                if (!response.ok) continue;
                const contentType = (response.headers.get("content-type") || "").toLowerCase();
                if (!contentType.includes("application/json")) continue;
                const body = await response.text();
                if (!body.trim().startsWith("{")) continue;
                defaults = JSON.parse(body);
                if (defaults && typeof defaults === "object") break;
                defaults = null;
            } catch {
                // try the next candidate
            }
        }
        if (!defaults) {
            console.warn("Could not load defaults: no JSON response from /api/defaults or /defaults (deployment routing?)");
            return;
        }

        // Apply the server defaults ONLY to fields the presenter has not
        // touched yet. The language pair is the presenter's deliberate
        // selection: if they already picked/swap'd before this late-arriving
        // response (cold incognito fetches can take seconds), re-applying
        // the env defaults would clobber their choice — and the next Connect
        // would then join with the clobbered en/es pair. Model/URL fields
        // keep the simpler guard: a placeholder-empty field is adoptable,
        // but a non-empty one means the presenter typed something.
        const srcTouched = refs.srcLangEl.dataset.userSelected === "true";
        const tgtTouched = refs.tgtLangEl.dataset.userSelected === "true";
        if (!srcTouched && defaults.src) refs.srcLangEl.value = defaults.src;
        if (!tgtTouched && defaults.tgt) refs.tgtLangEl.value = defaults.tgt;
        if ((!srcTouched || !tgtTouched)) {
            app.applyLanguagePair(refs.srcLangEl.value, refs.tgtLangEl.value);
        }

        if (defaults.asr?.base_url) refs.asrBaseUrlEl.value = defaults.asr.base_url;
        if (defaults.asr?.model) refs.asrModelEl.value = defaults.asr.model;

        if (defaults.llm?.base_url) refs.llmBaseUrlEl.value = defaults.llm.base_url;
        if (defaults.llm?.model) refs.llmModelEl.value = defaults.llm.model;

        if (defaults.tts?.base_url) refs.ttsBaseUrlEl.value = defaults.tts.base_url;
        if (defaults.tts?.model) refs.ttsModelEl.value = defaults.tts.model;
        if (defaults.tts?.voice) refs.ttsVoiceEl.value = defaults.tts.voice;

        if (defaults.asr?.has_api_key) refs.asrApiKeyEl.placeholder = "ASR_API_KEY (set on server)";
        if (defaults.llm?.has_api_key) refs.llmApiKeyEl.placeholder = "LLM_API_KEY (set on server)";
        if (defaults.tts?.has_api_key) refs.ttsApiKeyEl.placeholder = "TTS_API_KEY (set on server)";
    };

    app.sendConfig = function sendConfig() {
        if (!app.state.ws || app.state.ws.readyState !== WebSocket.OPEN) return;

        const asrKey = refs.asrApiKeyEl.value.trim();
        const llmKey = refs.llmApiKeyEl.value.trim();
        const ttsKey = refs.ttsApiKeyEl.value.trim();

        const payload = {
            type: "config",
            src: refs.srcLangEl.value,
            tgt: refs.tgtLangEl.value,
            asr: {
                base_url: refs.asrBaseUrlEl.value.trim(),
                model: refs.asrModelEl.value.trim()
            },
            llm: {
                base_url: refs.llmBaseUrlEl.value.trim(),
                model: refs.llmModelEl.value.trim()
            },
            tts: {
                base_url: refs.ttsBaseUrlEl.value.trim(),
                model: refs.ttsModelEl.value.trim(),
                voice: refs.ttsVoiceEl.value.trim()
            }
        };

        if (asrKey) payload.asr.api_key = asrKey;
        if (llmKey) payload.llm.api_key = llmKey;
        if (ttsKey) payload.tts.api_key = ttsKey;

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
