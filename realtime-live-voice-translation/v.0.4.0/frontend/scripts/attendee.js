(() => {
    const app = window.AttendeeApp;
    const { refs } = app;

    refs.connectBtn.onclick = () => app.connectToRoom();
    if (refs.downloadBtn) refs.downloadBtn.onclick = () => app.startDownload();
    refs.languageSelectEl.onchange = () => {
        app.state.targetLanguage = refs.languageSelectEl.value;
        if (app.state.ws && app.state.ws.readyState === WebSocket.OPEN) {
            app.state.ws.send(JSON.stringify({
                type: "set_target_language",
                target_language: app.state.targetLanguage
            }));
        }
        app.renderTranscriptView();
    };

    app.populateLanguages();
    app.initializeLanguagePicker();
    app.setConnectionStatus("Disconnected", "warning");
    app.syncRecordingUI();
    app.setRoomLabel();
    app.renderPlaceholderOnce();
    app.loadDefaultsFromBackend();

    const params = new URLSearchParams(window.location.search);
    const initialRoom = params.get("room") || "";
    const initialLanguage = params.get("lang") || app.state.targetLanguage;
    if (initialLanguage) {
        app.setTargetLanguage(initialLanguage);
    }
    if (initialRoom) {
        refs.roomInputEl.value = initialRoom;
        app.connectToRoom();
    }
})();
