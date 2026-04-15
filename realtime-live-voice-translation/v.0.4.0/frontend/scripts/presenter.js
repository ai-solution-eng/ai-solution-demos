(() => {
    const app = window.PresenterApp;
    const { refs, shared } = app;

    function bindSettingsToggles() {
        refs.toggleKeysBtnWhisper.onclick = () => {
            const show = refs.whisperApiKeyEl.type === "password";
            refs.whisperApiKeyEl.type = show ? "text" : "password";
            refs.toggleKeysBtnWhisper.textContent = show ? "Hide key" : "Show key";
        };

        refs.toggleKeysBtnLLM.onclick = () => {
            const show = refs.llmApiKeyEl.type === "password";
            refs.llmApiKeyEl.type = show ? "text" : "password";
            refs.toggleKeysBtnLLM.textContent = show ? "Hide key" : "Show key";
        };
    }

    function bindRecordingControls() {
        if (refs.recordBtn) {
            refs.recordBtn.onclick = async () => {
                if (refs.recordBtn.disabled) return;
                if (app.state.recordingState === "paused") {
                    await app.resumeRecording();
                    return;
                }
                app.showRecordConsentModal(true);
            };
        }
        if (refs.recordDeclineBtn) {
            refs.recordDeclineBtn.onclick = () => app.showRecordConsentModal(false);
        }
        if (refs.recordAcknowledgeBtn) {
            refs.recordAcknowledgeBtn.onclick = () => {
                app.beginRecordingAfterConsent();
            };
        }
        if (refs.recordModalEl) {
            refs.recordModalEl.addEventListener("click", (event) => {
                if (event.target === refs.recordModalEl) app.showRecordConsentModal(false);
            });
        }
    }

    function bindRoomControls() {
        if (refs.copyRoomLinkBtn) {
            refs.copyRoomLinkBtn.onclick = async () => {
                try {
                    await app.ensureBackendPresenterRoomId();
                    await navigator.clipboard.writeText(app.attendeeLink());
                    refs.copyRoomLinkBtn.textContent = "Link copied";
                    setTimeout(() => {
                        refs.copyRoomLinkBtn.textContent = "Copy attendee link";
                    }, 1200);
                } catch (error) {
                    console.error(error);
                    const fallbackLink = app.attendeeLink();
                    if (fallbackLink) {
                        alert(`Copy this attendee link:\n\n${fallbackLink}`);
                    } else {
                        alert("Could not prepare the attendee link.");
                    }
                }
            };
        }

        if (refs.newRoomBtn) {
            refs.newRoomBtn.onclick = () => {
                if (app.state.recordingActive || app.state.recordingState === "recording" || !refs.stopBtn.disabled) {
                    alert("Stop the current live session before creating a new room.");
                    return;
                }
                app.showNewRoomConfirmModal(true);
            };
        }

        if (refs.newRoomCancelBtn) {
            refs.newRoomCancelBtn.onclick = () => app.showNewRoomConfirmModal(false);
        }
        if (refs.newRoomConfirmBtn) {
            refs.newRoomConfirmBtn.onclick = async () => {
                app.showNewRoomConfirmModal(false);
                await app.createConfirmedNewRoom();
            };
        }
        if (refs.newRoomConfirmModalEl) {
            refs.newRoomConfirmModalEl.addEventListener("click", (event) => {
                if (event.target === refs.newRoomConfirmModalEl) app.showNewRoomConfirmModal(false);
            });
        }
    }

    function bindConfigInputs() {
        refs.srcLangEl.onchange = () => app.sendConfig();
        refs.tgtLangEl.onchange = () => app.sendConfig();
        refs.whisperBaseUrlEl.onchange = () => app.sendConfig();
        refs.whisperModelEl.onchange = () => app.sendConfig();
        refs.whisperApiKeyEl.onchange = () => app.sendConfig();
        refs.llmBaseUrlEl.onchange = () => app.sendConfig();
        refs.llmModelEl.onchange = () => app.sendConfig();
        refs.llmApiKeyEl.onchange = () => app.sendConfig();
        refs.swapBtn.onclick = () => {
            app.applyLanguagePair(refs.tgtLangEl.value, refs.srcLangEl.value, { emit: true, emitRole: "src" });
        };
    }

    function bindSessionControls() {
        refs.clearBtn.onclick = () => {
            if (app.currentRoomHasResumableRecording()) {
                const confirmed = window.confirm("This will discard the current room recording and reset the room to idle. Continue?");
                if (!confirmed) return;
            }
            app.resetUIAndContext();
            if (!app.state.ws || app.state.ws.readyState !== WebSocket.OPEN) return;
            app.state.ws.send(JSON.stringify({ type: "clear_session" }));
        };

        refs.downloadBtn.onclick = () => app.downloadCurrentMeetingBundle();
        if (refs.downloadPreviousRoomBtn) {
            refs.downloadPreviousRoomBtn.onclick = async () => {
                if (!app.state.previousRoomId) return;
                await app.downloadCurrentMeetingBundle(app.state.previousRoomId);
            };
        }

        refs.startBtn.onclick = () => app.startLiveSession();
        refs.stopBtn.onclick = () => app.handleStopAction();
        refs.connectBtn.onclick = () => {
            const requestedRoomId = app.getRequestedPresenterRoomId();
            if (requestedRoomId && !shared.isValidRoomCode(requestedRoomId)) {
                app.setStatus("Invalid room code");
                alert("Enter a valid room code using 8-64 lowercase letters, numbers, or hyphens.");
                return;
            }
            app.openPresenterConnection(requestedRoomId);
        };
    }

    app.initializeLanguagePickers();
    app.setPresenterRoomInputValue(decodeURIComponent(app.getCookie(app.ROOM_COOKIE_NAME) || "").trim());
    app.ensureBackendPresenterRoomId().catch((error) => {
        console.error(error);
        app.setStatus("Error");
    });
    app.updateRoomBadge();
    app.renderPlaceholderOnce();
    app.syncPresenterRecordingUI();
    app.populateMics().catch(console.error);
    app.loadDefaultsFromBackend();

    bindSettingsToggles();
    bindRecordingControls();
    bindRoomControls();
    bindConfigInputs();
    bindSessionControls();
})();
