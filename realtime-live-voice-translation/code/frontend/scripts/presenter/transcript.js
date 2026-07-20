(() => {
    const app = window.PresenterApp;
    const { refs } = app;
    const transcriptUi = window.RealtimeTranslationTranscript;

    function finalizedTranscriptItems() {
        return transcriptUi.finalizedTranscriptItems(app.transcript);
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

    function renderPlaceholderOnce() {
        if (app.transcript.length > 0) return;
        transcriptUi.renderTranscriptPanels({
            items: [],
            refs,
            placeholder: {
                label: "Awaiting live session",
                original: "Start the session to see real-time source transcript appear here.",
                translation: "Translated output will stream here as the session progresses."
            },
            timeSeparator: " • "
        });
    }

    function renderTranscriptView() {
        transcriptUi.renderTranscriptPanels({
            items: app.transcript,
            refs,
            placeholder: {
                label: "Awaiting live session",
                original: "Start the session to see real-time source transcript appear here.",
                translation: "Translated output will stream here as the session progresses."
            },
            timeSeparator: " • "
        });
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
        renderTranscriptView();
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
        renderPlaceholderOnce,
        renderTranscriptView,
        resetRecordingState,
        clearRecordingSessionState,
        resetUIAndContext
    });
})();
