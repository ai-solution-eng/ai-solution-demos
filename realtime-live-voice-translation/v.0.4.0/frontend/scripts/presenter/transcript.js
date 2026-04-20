(() => {
    const app = window.PresenterApp;
    const { refs } = app;
    const transcriptUi = window.RealtimeTranslationTranscript;

    function finalizedTranscriptItems() {
        return transcriptUi.finalizedTranscriptItems(app.transcript);
    }

    function syncTranscriptHoldRefresh(refreshAtMs = null) {
        app.clearTranscriptHoldTimer();
        if (!refreshAtMs) return;
        const delayMs = Math.max(0, refreshAtMs - Date.now());
        app.state.transcriptHoldTimer = window.setTimeout(() => {
            app.state.transcriptHoldTimer = null;
            renderTranscriptView();
        }, delayMs);
    }

    function canDownloadNotes() {
        return finalizedTranscriptItems().length > 0;
    }

    function canDownloadAnything() {
        if (app.state.canDownloadPackage) return true;
        if (app.state.recordingSessionId) return true;
        return false;
    }

    function syncDownloadAvailability() {
        if (!refs.downloadBtn) return;
        refs.downloadBtn.disabled = !canDownloadAnything();
    }

    function upsertTranscriptSegment(segment) {
        return transcriptUi.upsertSegment(app.transcript, segment);
    }

    function upsertRecordedTranscript(segment) {
        const exportItem = transcriptUi.toExportTranscriptItem(segment);
        const index = app.recordedTranscript.findIndex((item) => item.segment_id === segment.segment_id);
        if (index === -1) {
            app.recordedTranscript.push({ segment_id: segment.segment_id, ...exportItem });
            return;
        }
        app.recordedTranscript[index] = { segment_id: segment.segment_id, ...exportItem };
    }

    function applySnapshotSegments(segments = []) {
        transcriptUi.replaceSegments(app.transcript, segments);
        renderTranscriptView();
        syncDownloadAvailability();
    }

    function renderFactCheckView() {
        transcriptUi.renderFactCheckPanel({
            items: app.transcript,
            refs,
            targetLanguage: refs.tgtLangEl?.value || ""
        });
    }

    function applyFactCheckResult(payload, { notify = false } = {}) {
        const updated = transcriptUi.upsertSegmentFactCheck(app.transcript, payload);
        renderFactCheckView();
        if (!updated) return;
        if (notify && app.shared.factCheckIsFlagged(updated.fact_check)) {
            if (refs.factCheckPanelEl) refs.factCheckPanelEl.open = true;
            app.showFactCheckToast();
        }
    }

    function renderPlaceholderOnce() {
        if (app.transcript.length > 0) return;
        transcriptUi.renderTranscriptPanels({
            finalHoldMs: 0,
            items: [],
            refs,
            placeholder: {
                label: "Awaiting live session",
                original: "Start the session to see real-time source transcript appear here.",
                translation: "Translated output will stream here as the session progresses."
            },
            timeSeparator: " • "
        });
        syncTranscriptHoldRefresh(null);
        renderFactCheckView();
    }

    function renderTranscriptView() {
        const renderState = transcriptUi.renderTranscriptPanels({
            items: app.transcript,
            refs,
            finalHoldMs: app.state.transcriptFinalHoldMs,
            placeholder: {
                label: "Awaiting live session",
                original: "Start the session to see real-time source transcript appear here.",
                translation: "Translated output will stream here as the session progresses."
            },
            timeSeparator: " • "
        });
        syncTranscriptHoldRefresh(renderState?.refreshAtMs || null);
        renderFactCheckView();
    }

    function resetRecordingState() {
        app.state.recordingApproved = false;
        app.state.recordingActive = false;
        app.state.recordingState = "idle";
        app.state.canDownloadPackage = false;
        app.state.recordingSessionId = "";
    }

    function clearRecordingSessionState() {
        app.state.recordingSessionId = "";
        app.state.recordingMimeType = "";
        app.state.recordingUploadQueue = Promise.resolve();
        app.state.recordingUploadError = null;
        app.state.recordingApproved = false;
        app.state.recordingActive = false;
        app.state.recordingState = "idle";
        app.state.canDownloadPackage = false;
        if (typeof app.syncPresenterRecordingUI === "function") {
            app.syncPresenterRecordingUI();
        }
    }

    function resetUIAndContext() {
        app.transcript.length = 0;
        app.recordedTranscript.length = 0;
        app.clearTranscriptHoldTimer();
        renderTranscriptView();
        app.hideFactCheckToast();
        syncDownloadAvailability();
        clearRecordingSessionState();
        requestAnimationFrame(() => refs.centerEl.scrollTo({ top: 0 }));
    }

    Object.assign(app, {
        finalizedTranscriptItems,
        canDownloadNotes,
        canDownloadAnything,
        syncDownloadAvailability,
        upsertTranscriptSegment,
        upsertRecordedTranscript,
        applySnapshotSegments,
        applyFactCheckResult,
        renderPlaceholderOnce,
        renderFactCheckView,
        renderTranscriptView,
        resetRecordingState,
        clearRecordingSessionState,
        resetUIAndContext
    });
})();
