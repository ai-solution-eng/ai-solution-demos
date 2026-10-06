(() => {
    const app = window.PresenterApp;
    const { refs, shared } = app;

    function bindSettingsToggles() {
        refs.toggleKeysBtnAsr.onclick = () => {
            const show = refs.asrApiKeyEl.type === "password";
            refs.asrApiKeyEl.type = show ? "text" : "password";
            refs.toggleKeysBtnAsr.textContent = show ? "Hide key" : "Show key";
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
        const bindCopyButton = (button, page, defaultLabel, copiedLabel) => {
            if (!button) return;
            button.onclick = async () => {
                try {
                    await app.ensureBackendPresenterRoomId();
                    await navigator.clipboard.writeText(app.attendeeLink(page));
                    button.textContent = copiedLabel;
                    setTimeout(() => {
                        button.textContent = defaultLabel;
                    }, 1200);
                } catch (error) {
                    console.error(error);
                    const fallbackLink = app.attendeeLink(page);
                    if (fallbackLink) {
                        alert(`Copy this attendee link:\n\n${fallbackLink}`);
                    } else {
                        alert("Could not prepare the attendee link.");
                    }
                }
            };
        };

        bindCopyButton(refs.copyRoomLinkBtn, "attendee.html", "Copy attendee link", "Link copied");
        bindCopyButton(refs.copyPhoneLinkBtn, "attendee-mobile.html", "Copy phone link", "Phone link copied");

        // Inline ⧉ copy button inside the room-code chip: copies the FULL
        // credential block (room code + token + recovery code) in one click —
        // everything needed to re-enter the room from any browser. Values are
        // never displayed on screen; they live in cookies/state only.
        const copyRoomCodeBtn = document.getElementById("copyRoomCodeBtn");
        if (copyRoomCodeBtn) {
            copyRoomCodeBtn.onclick = async (event) => {
                event.stopPropagation();
                const roomId = app.state.roomId
                    || app.getCookie(app.ROOM_COOKIE_NAME) || "";
                if (!roomId || roomId === "Unassigned") {
                    alert("No room code yet — the room is still being prepared.");
                    return;
                }
                const token = app.state.presenterToken || app.getCookie("realtime-voice-presenter-token") || "";
                const recovery = app.state.recoveryCode || app.getCookie("realtime-voice-recovery-code") || "";
                const block = [
                    `Room number: ${roomId}`,
                    token ? `Room token: ${token}` : null,
                    recovery ? `Recovery code: ${recovery}` : null
                ].filter(Boolean).join("\n");
                try {
                    await navigator.clipboard.writeText(block);
                    copyRoomCodeBtn.classList.add("copied");
                    copyRoomCodeBtn.textContent = "✓";
                    setTimeout(() => {
                        copyRoomCodeBtn.classList.remove("copied");
                        copyRoomCodeBtn.textContent = "⧉";
                    }, 1200);
                } catch (error) {
                    alert(`Copy failed — copy manually:\n\n${block}`);
                }
            };
        }

        // The credentials panel is gone (privacy + space): the chip's ⧉ is
        // the single copy surface — it copies the room code, token, and
        // recovery code together as one block.
        // Recovery flow: reclaim presenter access with recovery code OR room token
        const recoverBtn = document.getElementById("recoverTokenBtn");
        const recoveryInput = document.getElementById("recoveryCodeInput");
        if (recoverBtn && recoveryInput) {
            recoverBtn.onclick = async () => {
                const roomId = (app.getRequestedPresenterRoomId()
                    || app.state.roomId
                    || app.getCookie(app.ROOM_COOKIE_NAME) || "").trim().toLowerCase();
                const credential = recoveryInput.value.trim();
                if (!roomId) { alert("Enter the room code first (Room code field above)."); return; }
                if (!credential) { alert("Enter the room's recovery code or the saved room token."); return; }
                recoverBtn.disabled = true;
                try {
                    await app.recoverRoomAccess(roomId, credential);
                    recoveryInput.value = "";
                    app.setStatus("Access recovered");
                    app.log(`Presenter access to room ${roomId} recovered — click Connect to join.`);
                    const note = document.getElementById("reentryNote");
                    if (note) note.hidden = true;
                } catch (error) {
                    console.error(error);
                    alert(`Recovery failed: ${error?.message || error}`);
                } finally {
                    recoverBtn.disabled = false;
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
        refs.asrBaseUrlEl.onchange = () => app.sendConfig();
        refs.asrModelEl.onchange = () => app.sendConfig();
        refs.asrApiKeyEl.onchange = () => app.sendConfig();
        refs.llmBaseUrlEl.onchange = () => app.sendConfig();
        refs.llmModelEl.onchange = () => app.sendConfig();
        refs.llmApiKeyEl.onchange = () => app.sendConfig();
        refs.swapBtn.onclick = () => {
            // A deliberate presenter action: protect the pair from the
            // late-arriving /api/defaults (the Connect-resets-languages race).
            refs.srcLangEl.dataset.userSelected = "true";
            refs.tgtLangEl.dataset.userSelected = "true";
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
    // Restore the presenter's LAST used pair (cookie) BEFORE anything else
    // can write the pickers: a reload wipes the in-memory selection, and
    // without this the UI falls back to markup defaults (en/es) — which the
    // join then faithfully sends, flipping the room ("Connect resets my
    // languages"). The cookie pair IS the presenter's last deliberate state.
    const savedPair = decodeURIComponent(app.getCookie("realtime-voice-lang-pair") || "");
    const [savedSrc, savedTgt] = savedPair.split("|");
    if (savedSrc && savedTgt) {
        app.applyLanguagePair(savedSrc, savedTgt);
        // The restored pair IS a deliberate selection (persisted from the
        // presenter's last pick): protect it from the late-arriving
        // /api/defaults exactly like a fresh picker click would be.
        refs.srcLangEl.dataset.userSelected = "true";
        refs.tgtLangEl.dataset.userSelected = "true";
    }
    app.ensureBackendPresenterRoomId().then(() => {
        app.showRoomCredentials();
    }).catch((error) => {
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
