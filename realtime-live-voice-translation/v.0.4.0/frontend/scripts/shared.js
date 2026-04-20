(() => {
    const DEFAULT_TRANSCRIPT_FINAL_HOLD_MS = 3000;
    const LANGUAGE_OPTIONS = [
        { code: "en", label: "English", nativeLabel: "English", popular: true },
        { code: "es", label: "Spanish", nativeLabel: "Español", popular: true },
        { code: "pt", label: "Portuguese", nativeLabel: "Português", popular: true },
        { code: "fr", label: "French", nativeLabel: "Français", popular: true },
        { code: "de", label: "German", nativeLabel: "Deutsch", popular: true },
        { code: "it", label: "Italian", nativeLabel: "Italiano", popular: true },
        { code: "zh", label: "Chinese", nativeLabel: "中文", popular: true },
        { code: "ja", label: "Japanese", nativeLabel: "日本語", popular: true },
        { code: "ko", label: "Korean", nativeLabel: "한국어", popular: true },
        { code: "ar", label: "Arabic", nativeLabel: "العربية", popular: true, rtl: true },
        { code: "hi", label: "Hindi", nativeLabel: "हिन्दी", popular: true },
        { code: "vi", label: "Vietnamese", nativeLabel: "Tiếng Việt", popular: true },
        { code: "ru", label: "Russian", nativeLabel: "Русский", popular: true },
        { code: "da", label: "Danish", nativeLabel: "Dansk", popular: false },
        { code: "fi", label: "Finnish", nativeLabel: "Suomi", popular: false },
        { code: "ms", label: "Malay", nativeLabel: "Bahasa Melayu", popular: false },
        { code: "km", label: "Khmer", nativeLabel: "ភាសាខ្មែរ", popular: false },
        { code: "th", label: "Thai", nativeLabel: "ภาษาไทย", popular: false },
        { code: "tr", label: "Turkish", nativeLabel: "Türkçe", popular: false },
        { code: "no", label: "Norwegian", nativeLabel: "Norsk", popular: false }
    ];

    const languageMap = new Map(LANGUAGE_OPTIONS.map((language) => [language.code, language]));

    function resolveBackendHttpBase() {
        const params = new URLSearchParams(window.location.search);
        const explicit = (params.get("backend") || "").trim();
        if (explicit) return explicit.replace(/\/$/, "");

        const isLocalHost = ["localhost", "127.0.0.1"].includes(window.location.hostname);
        if (isLocalHost && window.location.port && window.location.port !== "8000") {
            return `${window.location.protocol}//${window.location.hostname}:8000`;
        }

        return window.location.origin;
    }

    function resolveBackendWsUrl(httpBase) {
        const base = new URL(httpBase);
        base.protocol = base.protocol === "https:" ? "wss:" : "ws:";
        base.pathname = "/ws/translate";
        base.search = "";
        base.hash = "";
        return base.toString();
    }

    function escapeHtml(text) {
        return (text || "")
            .replaceAll("&", "&amp;")
            .replaceAll("<", "&lt;")
            .replaceAll(">", "&gt;")
            .replaceAll('"', "&quot;")
            .replaceAll("'", "&#039;");
    }

    function parseTranscriptFinalHoldMs(value, fallback = DEFAULT_TRANSCRIPT_FINAL_HOLD_MS) {
        const parsed = Number.parseInt(value, 10);
        if (!Number.isFinite(parsed)) return fallback;
        return Math.max(0, parsed);
    }

    function languageName(code) {
        const normalized = (code || "").trim().toLowerCase();
        return languageMap.get(normalized)?.label || normalized.toUpperCase() || "Unknown";
    }

    function formatTurnTime(ts) {
        if (!ts) return "";
        return new Date(ts).toLocaleTimeString([], {
            hour: "2-digit",
            minute: "2-digit",
            second: "2-digit"
        });
    }

    function statusLabel(item) {
        if (item?.is_final) return "Final";
        if (item?.status === "refining") return "Refining";
        return "Listening";
    }

    function statusToneClass(item) {
        if (item?.is_final) return "is-final";
        if (item?.status === "refining") return "is-refining";
        return "is-listening";
    }

    function isValidRoomCode(roomId) {
        return /^[a-z0-9-]{8,64}$/i.test((roomId || "").trim());
    }

    function factCheckLabel(status) {
        const normalized = (status || "").trim().toLowerCase();
        if (normalized === "likely_false") return "Likely controversial or fake";
        if (normalized === "controversial") return "Controversial";
        if (normalized === "error") return "Check failed";
        return "Reviewed";
    }

    function factCheckToneClass(status) {
        const normalized = (status || "").trim().toLowerCase();
        if (normalized === "likely_false") return "is-likely-false";
        if (normalized === "controversial") return "is-controversial";
        if (normalized === "error") return "is-fact-check-error";
        return "is-reviewed";
    }

    function factCheckIsFlagged(result) {
        const normalized = (result?.status || "").trim().toLowerCase();
        return normalized === "likely_false";
    }

    function extractFilenameFromResponse(response, fallback = "meeting_package.zip") {
        const disposition = response.headers.get("Content-Disposition") || "";
        const match = disposition.match(/filename="?([^";]+)"?/i);
        return match ? match[1] : fallback;
    }

    function triggerBlobDownload(blob, filename) {
        const url = URL.createObjectURL(blob);
        const anchor = document.createElement("a");
        anchor.href = url;
        anchor.download = filename;
        document.body.appendChild(anchor);
        anchor.click();
        anchor.remove();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
    }

    window.RealtimeTranslationShared = Object.freeze({
        DEFAULT_TRANSCRIPT_FINAL_HOLD_MS,
        LANGUAGE_OPTIONS,
        escapeHtml,
        factCheckIsFlagged,
        factCheckLabel,
        factCheckToneClass,
        extractFilenameFromResponse,
        formatTurnTime,
        isValidRoomCode,
        languageName,
        parseTranscriptFinalHoldMs,
        resolveBackendHttpBase,
        resolveBackendWsUrl,
        statusLabel,
        statusToneClass,
        triggerBlobDownload
    });
})();
