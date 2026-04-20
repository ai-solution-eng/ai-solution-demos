(() => {
    const app = window.AttendeeApp;
    const { refs, shared } = app;
    const transcriptUi = window.RealtimeTranslationTranscript;

    function upsertSegment(segment) {
        transcriptUi.upsertSegment(app.state.segments, segment);
    }

    function applySnapshot(segments) {
        app.state.segments = Array.isArray(segments) ? segments.map((segment) => ({ ...segment })) : [];
        renderTranscriptView();
        app.syncRecordingUI();
    }

    function renderFactCheckView() {
        transcriptUi.renderFactCheckPanel({
            items: app.state.segments,
            refs,
            targetLanguage: app.state.targetLanguage
        });
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

    function applyFactCheckResult(payload, { notify = false } = {}) {
        const updated = transcriptUi.upsertSegmentFactCheck(app.state.segments, payload);
        renderFactCheckView();
        if (!updated) return;
        if (notify && shared.factCheckIsFlagged(updated.fact_check)) {
            if (refs.factCheckPanelEl) refs.factCheckPanelEl.open = true;
            app.showFactCheckToast();
        }
    }

    function renderPlaceholderOnce() {
        if (app.state.segments.length > 0) return;
        transcriptUi.renderTranscriptPanels({
            finalHoldMs: 0,
            items: [],
            refs,
            targetLanguage: app.state.targetLanguage,
            placeholder: {
                label: "Waiting for conversation",
                original: "The source transcript will appear here when the presenter starts speaking.",
                translation: (targetLanguage) => `Translation will appear here in ${shared.escapeHtml(shared.languageName(targetLanguage))}.`
            }
        });
        syncTranscriptHoldRefresh(null);
        renderFactCheckView();
    }

    function renderTranscriptView() {
        const renderState = transcriptUi.renderTranscriptPanels({
            items: app.state.segments,
            refs,
            targetLanguage: app.state.targetLanguage,
            finalHoldMs: app.state.transcriptFinalHoldMs,
            placeholder: {
                label: "Waiting for conversation",
                original: "The source transcript will appear here when the presenter starts speaking.",
                translation: (targetLanguage) => `Translation will appear here in ${shared.escapeHtml(shared.languageName(targetLanguage))}.`
            }
        });
        syncTranscriptHoldRefresh(renderState?.refreshAtMs || null);
        renderFactCheckView();
    }

    function buildTranscriptText(which) {
        return transcriptUi.buildTranscriptText(app.finalizedTranscriptItems(), which);
    }

    function buildParallelCsv() {
        return transcriptUi.buildParallelCsv(app.finalizedTranscriptItems());
    }

    Object.assign(app, {
        upsertSegment,
        applySnapshot,
        applyFactCheckResult,
        renderPlaceholderOnce,
        renderFactCheckView,
        renderTranscriptView,
        buildTranscriptText,
        buildParallelCsv
    });
})();
