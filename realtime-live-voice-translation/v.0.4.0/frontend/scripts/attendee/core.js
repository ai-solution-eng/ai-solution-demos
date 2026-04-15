(() => {
    const shared = window.RealtimeTranslationShared;
    const refs = {
        roomInputEl: document.getElementById("roomInput"),
        languageFieldEl: document.getElementById("languageField"),
        languageSelectEl: document.getElementById("languageSelect"),
        languageTriggerEl: document.getElementById("languageTrigger"),
        languagePickerEl: document.getElementById("languagePicker"),
        languageSearchEl: document.getElementById("languageSearch"),
        languageListEl: document.getElementById("languageList"),
        languageEmptyEl: document.getElementById("languageEmpty"),
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
        factCheckPanelEl: document.getElementById("factCheckPanel"),
        factCheckListEl: document.getElementById("factCheckList"),
        factCheckCountEl: document.getElementById("factCheckCount"),
        factCheckToastEl: document.getElementById("factCheckToast"),
        exportOverlayEl: document.getElementById("exportOverlay"),
        exportBarEl: document.getElementById("exportBar"),
        exportStageEl: document.getElementById("exportStage"),
        exportDetailEl: document.getElementById("exportDetail"),
        exportPercentEl: document.getElementById("exportPercent")
    };

    const HTTP_BASE = shared.resolveBackendHttpBase();
    const WS_URL = shared.resolveBackendWsUrl(HTTP_BASE);
    const languageMap = new Map(shared.LANGUAGE_OPTIONS.map((language) => [language.code, language]));

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
            segments: [],
            factCheckToastTimer: null
        }
    };

    function normalizeSearchText(text) {
        return (text || "")
            .toLowerCase()
            .normalize("NFD")
            .replace(/[\u0300-\u036f]/g, "")
            .trim();
    }

    function getLanguageOption(code) {
        return languageMap.get(code) || {
            code: code || "",
            label: code || "Unknown",
            nativeLabel: code || "Unknown",
            popular: false,
            rtl: false
        };
    }

    function languageMetaText(language) {
        return `${language.nativeLabel} · ${language.code.toUpperCase()}`;
    }

    function updateLanguageTrigger() {
        const language = getLanguageOption(refs.languageSelectEl.value);
        const labelEl = refs.languageTriggerEl.querySelector("[data-language-label]");
        const metaEl = refs.languageTriggerEl.querySelector("[data-language-meta]");
        if (labelEl) labelEl.textContent = language.label;
        if (metaEl) metaEl.textContent = languageMetaText(language);
    }

    function renderLanguageOption(language, selectedCode) {
        const selected = language.code === selectedCode;
        const rtlClass = language.rtl ? " is-rtl" : "";
        return `
            <button class="language-option${rtlClass}" type="button" data-code="${language.code}"
                aria-selected="${selected ? "true" : "false"}">
                <span class="language-option-main">
                    <span class="language-option-label">${shared.escapeHtml(language.label)}</span>
                    <span class="language-option-native">${shared.escapeHtml(language.nativeLabel)}</span>
                </span>
                <span class="language-option-code">${shared.escapeHtml(language.code)}</span>
            </button>
        `;
    }

    function renderLanguagePicker() {
        const selectedCode = refs.languageSelectEl.value;
        const query = normalizeSearchText(refs.languageSearchEl.value);
        const filtered = shared.LANGUAGE_OPTIONS.filter((language) => {
            if (!query) return true;
            const haystack = normalizeSearchText(`${language.label} ${language.nativeLabel} ${language.code}`);
            return haystack.includes(query);
        });

        refs.languageListEl.innerHTML = filtered.map((language) => renderLanguageOption(language, selectedCode)).join("");
        refs.languageEmptyEl.hidden = filtered.length > 0;
    }

    function closeLanguagePicker({ focusTrigger = false } = {}) {
        refs.languageFieldEl.dataset.open = "false";
        refs.languagePickerEl.hidden = true;
        refs.languageTriggerEl.setAttribute("aria-expanded", "false");
        refs.languageSearchEl.value = "";
        renderLanguagePicker();
        if (focusTrigger) refs.languageTriggerEl.focus();
    }

    function openLanguagePicker() {
        refs.languageFieldEl.dataset.open = "true";
        renderLanguagePicker();
        refs.languagePickerEl.hidden = false;
        refs.languageTriggerEl.setAttribute("aria-expanded", "true");
        refs.languageSearchEl.focus();
        refs.languageSearchEl.select();
    }

    function toggleLanguagePicker() {
        if (refs.languagePickerEl.hidden) {
            openLanguagePicker();
            return;
        }
        closeLanguagePicker({ focusTrigger: true });
    }

    function visibleLanguageButtons() {
        return Array.from(refs.languageListEl.querySelectorAll("button.language-option"));
    }

    function focusLanguageButton(direction, currentButton = null) {
        const buttons = visibleLanguageButtons();
        if (buttons.length === 0) return;
        const currentIndex = currentButton ? buttons.indexOf(currentButton) : -1;
        const nextIndex = currentIndex === -1
            ? (direction > 0 ? 0 : buttons.length - 1)
            : (currentIndex + direction + buttons.length) % buttons.length;
        buttons[nextIndex].focus();
    }

    function selectLanguage(code) {
        if (!app.setTargetLanguage(code, { notify: true })) return;
        closeLanguagePicker({ focusTrigger: true });
    }

    app.populateLanguages = function populateLanguages() {
        const initialCode = languageMap.has(app.state.targetLanguage)
            ? app.state.targetLanguage
            : (shared.LANGUAGE_OPTIONS[0]?.code || "");
        if (initialCode) app.setTargetLanguage(initialCode);
    };

    app.setTargetLanguage = function setTargetLanguage(code, { notify = false } = {}) {
        const normalized = (code || "").trim().toLowerCase();
        if (!languageMap.has(normalized)) return false;

        refs.languageSelectEl.value = normalized;
        app.state.targetLanguage = normalized;
        updateLanguageTrigger();
        renderLanguagePicker();

        if (notify) {
            refs.languageSelectEl.dispatchEvent(new Event("change", { bubbles: true }));
        }
        return true;
    };

    app.initializeLanguagePicker = function initializeLanguagePicker() {
        refs.languageFieldEl.dataset.open = "false";
        updateLanguageTrigger();
        renderLanguagePicker();

        refs.languageTriggerEl.addEventListener("click", toggleLanguagePicker);
        refs.languageTriggerEl.addEventListener("keydown", (event) => {
            if (event.key === "ArrowDown" || event.key === "Enter" || event.key === " ") {
                event.preventDefault();
                openLanguagePicker();
            }
        });

        refs.languageSearchEl.addEventListener("input", renderLanguagePicker);
        refs.languageSearchEl.addEventListener("keydown", (event) => {
            if (event.key === "ArrowDown") {
                event.preventDefault();
                focusLanguageButton(1);
            } else if (event.key === "ArrowUp") {
                event.preventDefault();
                focusLanguageButton(-1);
            } else if (event.key === "Escape") {
                event.preventDefault();
                closeLanguagePicker({ focusTrigger: true });
            }
        });

        refs.languageListEl.addEventListener("click", (event) => {
            const button = event.target.closest("button[data-code]");
            if (!button) return;
            selectLanguage(button.dataset.code || "");
        });

        refs.languageListEl.addEventListener("keydown", (event) => {
            const button = event.target.closest("button[data-code]");
            if (!button) return;

            if (event.key === "ArrowDown") {
                event.preventDefault();
                focusLanguageButton(1, button);
            } else if (event.key === "ArrowUp") {
                event.preventDefault();
                focusLanguageButton(-1, button);
            } else if (event.key === "Enter" || event.key === " ") {
                event.preventDefault();
                selectLanguage(button.dataset.code || "");
            }
        });

        document.addEventListener("click", (event) => {
            if (!refs.languageFieldEl.contains(event.target)) closeLanguagePicker();
        });

        document.addEventListener("keydown", (event) => {
            if (event.key === "Escape" && !refs.languagePickerEl.hidden) {
                closeLanguagePicker();
            }
        });
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
