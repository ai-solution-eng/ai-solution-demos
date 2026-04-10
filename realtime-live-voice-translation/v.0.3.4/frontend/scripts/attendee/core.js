(() => {
    const shared = window.RealtimeTranslationShared;
    const refs = {
        roomInputEl: document.getElementById("roomInput"),
        languageSelectEl: document.getElementById("languageSelect"),
        connectBtn: document.getElementById("connectBtn"),
        downloadBtn: document.getElementById("downloadBtn"),
        connectionStatusEl: document.getElementById("connectionStatus"),
        recordingBannerEl: document.getElementById("recordingBanner"),
        currentRoomEl: document.getElementById("currentRoom"),
        heroRoomCodeEl: document.getElementById("heroRoomCode"),
        heroRoomChipEl: document.getElementById("heroRoomChip"),
        footerNoteEl: document.getElementById("footerNote"),
        stackEl: document.getElementById("stack"),
        centerEl: document.getElementById("center"),
        historyPanelEl: document.getElementById("historyPanel"),
        historyListEl: document.getElementById("historyList"),
        historyCountEl: document.getElementById("historyCount"),
        exportOverlayEl: document.getElementById("exportOverlay"),
        exportBarEl: document.getElementById("exportBar"),
        exportStageEl: document.getElementById("exportStage"),
        exportDetailEl: document.getElementById("exportDetail"),
        exportPercentEl: document.getElementById("exportPercent")
    };

    const HTTP_BASE = shared.resolveBackendHttpBase();
    const WS_URL = shared.resolveBackendWsUrl(HTTP_BASE);

    const app = {
        shared,
        refs,
        HTTP_BASE,
        WS_URL,
        state: {
            ws: null,
            roomId: "",
            targetLanguage: "es",
            recordingState: "idle",
            recordingSessionId: "",
            canDownloadPackage: false,
            connected: false,
            joinRejected: false,
            segments: []
        }
    };

    app.populateLanguages = function populateLanguages() {
        refs.languageSelectEl.innerHTML = shared.LANGUAGE_OPTIONS
            .map((language) => `<option value="${language.code}">${language.label}</option>`)
            .join("");
        refs.languageSelectEl.value = app.state.targetLanguage;
    };

    app.setFooterNote = function setFooterNote(text = "") {
        if (!refs.footerNoteEl) return;
        refs.footerNoteEl.textContent = text;
    };

    app.setConnectionStatus = function setConnectionStatus(text, tone = "warning") {
        refs.connectionStatusEl.textContent = text;
        refs.connectionStatusEl.dataset.tone = tone;
    };

    app.setExportOverlay = function setExportOverlay(visible) {
        refs.exportOverlayEl.classList.toggle("visible", !!visible);
    };

    app.updateExportProgress = function updateExportProgress(progress = 0, stage = "Queued", detail = "") {
        const pct = Math.max(0, Math.min(100, Number(progress) || 0));
        refs.exportBarEl.style.width = `${pct}%`;
        refs.exportStageEl.textContent = stage || "Working…";
        refs.exportDetailEl.textContent = detail || "";
        refs.exportPercentEl.textContent = `${Math.round(pct)}%`;
    };

    app.finalizedTranscriptItems = function finalizedTranscriptItems() {
        return app.state.segments
            .filter((item) => item?.is_final && ((item.original || "").trim() || (item.translation || "").trim()))
            .map((item) => ({
                original: item?.original || "",
                translation: item?.translation || "",
                src: item?.src || "",
                tgt: item?.tgt || app.state.targetLanguage || "",
                ts_ms: item?.ts_ms || null
            }));
    };

    app.canDownloadNotes = function canDownloadNotes() {
        return app.finalizedTranscriptItems().length > 0;
    };

    app.canDownloadAnything = function canDownloadAnything() {
        if (app.state.canDownloadPackage) return true;
        if (app.state.recordingSessionId) return true;
        return false;
    };

    app.ensureJoinableRoom = async function ensureJoinableRoom(roomId) {
        const normalized = (roomId || "").trim().toLowerCase();
        if (!shared.isValidRoomCode(normalized)) {
            throw new Error("Enter a valid presenter room code.");
        }
        const response = await fetch(`${HTTP_BASE}/api/rooms/${encodeURIComponent(normalized)}`, {
            method: "GET",
            cache: "no-store"
        });
        if (response.ok) return normalized;
        const detail = await response.text().catch(() => "");
        throw new Error(detail || "That room does not exist. Check the presenter room code and try again.");
    };

    app.setRecordingBanner = function setRecordingBanner(visible) {
        if (!refs.recordingBannerEl) return;
        refs.recordingBannerEl.classList.toggle("visible", !!visible);
    };

    app.syncRecordingUI = function syncRecordingUI() {
        app.setRecordingBanner(app.state.recordingState === "recording");
        if (refs.downloadBtn) refs.downloadBtn.disabled = !app.canDownloadAnything();
    };

    app.setRoomLabel = function setRoomLabel() {
        const roomText = app.state.roomId ? `Room ${app.state.roomId}` : "Room not joined";
        refs.currentRoomEl.textContent = roomText;
        refs.currentRoomEl.dataset.tone = app.state.roomId ? "ready" : "warning";
        if (refs.heroRoomCodeEl) refs.heroRoomCodeEl.textContent = app.state.roomId || "Not joined";
        if (refs.heroRoomChipEl) refs.heroRoomChipEl.dataset.tone = app.state.roomId ? "ready" : "warning";
    };

    window.AttendeeApp = app;
})();
