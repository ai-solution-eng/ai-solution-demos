(() => {
    const app = window.AttendeeApp;
    const { refs } = app;

    // Pending language choice: if the attendee changes their target while
    // the WebSocket is not open (reconnecting/dropped), the choice is
    // buffered here and flushed to the server on the next successful
    // connect. Without this, the picker shows a language the server never
    // learned — the chip/segments diverge from the selection.
    app.state.pendingTargetLanguage = "";

    app.sendTargetLanguageToServer = function sendTargetLanguageToServer(code) {
        if (app.state.ws && app.state.ws.readyState === WebSocket.OPEN) {
            app.state.ws.send(JSON.stringify({
                type: "set_target_language",
                target_language: code
            }));
            return true;
        }
        app.state.pendingTargetLanguage = code;
        return false;
    };

    refs.connectBtn.onclick = () => app.connectToRoom();
    if (refs.downloadBtn) refs.downloadBtn.onclick = () => app.startDownload();
    refs.languageSelectEl.onchange = () => {
        app.state.targetLanguage = refs.languageSelectEl.value;
        app.sendTargetLanguageToServer(app.state.targetLanguage);
        app.renderTranscriptView();
    };

    app.populateLanguages();
    app.initializeLanguagePicker();
    app.setConnectionStatus("Disconnected", "warning");
    app.syncRecordingUI();
    app.setRoomLabel();
    app.renderPlaceholderOnce();

    const params = new URLSearchParams(window.location.search);
    const initialRoom = params.get("room") || "";
    const initialLanguage = (params.get("lang") || "").trim().toLowerCase();
    if (initialLanguage) {
        // A REAL ?lang= URL param is the attendee's explicit starting choice
        // (the presenter's link decides it). Recorded separately from
        // targetLanguage so the join message only carries EXPLICIT choices —
        // the boot default must not masquerade as one.
        app.state.joinLangParam = initialLanguage;
        app.setTargetLanguage(initialLanguage);
    }
    if (initialRoom) {
        refs.roomInputEl.value = initialRoom;
        app.connectToRoom();
    }
})();
