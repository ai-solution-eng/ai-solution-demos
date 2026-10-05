(() => {
    const app = window.PresenterApp;
    const { refs, shared } = app;
    const exportUi = window.RealtimeTranslationExport;
    const transcriptUi = window.RealtimeTranslationTranscript;

    function pad2(value) {
        return String(value).padStart(2, "0");
    }

    function sessionStamp() {
        const date = new Date();
        return `${date.getFullYear()}-${pad2(date.getMonth() + 1)}-${pad2(date.getDate())}_${pad2(date.getHours())}-${pad2(date.getMinutes())}-${pad2(date.getSeconds())}`;
    }

    function buildTranscriptText(which) {
        return transcriptUi.buildTranscriptText(app.finalizedTranscriptItems(), which);
    }

    function buildParallelCsv() {
        return transcriptUi.buildParallelCsv(app.finalizedTranscriptItems());
    }

    function currentLlmConfigPayload() {
        const llm = {
            base_url: refs.llmBaseUrlEl.value.trim(),
            model: refs.llmModelEl.value.trim()
        };
        const key = refs.llmApiKeyEl.value.trim();
        if (key) llm.api_key = key;
        return llm;
    }

    function currentAsrConfigPayload() {
        const asr = {
            base_url: refs.asrBaseUrlEl.value.trim(),
            model: refs.asrModelEl.value.trim()
        };
        const key = refs.asrApiKeyEl.value.trim();
        if (key) asr.api_key = key;
        return asr;
    }

    function setExportOverlay(visible) {
        exportUi.setExportOverlay(refs, visible);
    }

    function updateExportProgress(progress = 0, stage = "Queued", detail = "") {
        exportUi.updateExportProgress(refs, progress, stage, detail);
    }

    // ---- Export language chooser (presenter picks package languages) ----

    function exportLanguageModalEls() {
        return {
            modal: document.getElementById("exportLanguagesModal"),
            list: document.getElementById("exportLanguagesList"),
            confirmBtn: document.getElementById("exportLanguagesConfirmBtn"),
            cancelBtn: document.getElementById("exportLanguagesCancelBtn")
        };
    }

    function languageLabel(code) {
        const option = shared.LANGUAGE_OPTIONS.find((entry) => entry.code === code);
        return option ? option.label : (code || "").toUpperCase();
    }

    async function fetchTouchedLanguages(roomId) {
        // The room state endpoint reports every language touched during the
        // meeting; the chooser defaults to all of them pre-selected.
        const response = await fetch(`${app.HTTP_BASE}/api/rooms/${encodeURIComponent(roomId)}`);
        if (!response.ok) return [];
        const state = await response.json().catch(() => ({}));
        return Array.isArray(state.used_translation_languages)
            ? state.used_translation_languages.filter((code) => typeof code === "string")
            : [];
    }

    function promptExportLanguages(touchedLanguages) {
        // Returns a Promise resolving to the selected language codes, or
        // null when cancelled.
        //
        // Lists EVERY language the app supports (the model does all
        // translation at export time, so any of them is fair game) —
        // languages touched during the meeting sort first and start
        // CHECKED; untouched ones are offered unchecked. Select all/Clear
        // mass-toggle the checkboxes.
        const els = exportLanguageModalEls();
        const touched = new Set(touchedLanguages || []);
        const all = [...shared.LANGUAGE_OPTIONS];
        const ordered = [
            ...all.filter((language) => touched.has(language.code)),
            ...all.filter((language) => !touched.has(language.code))
        ];

        els.list.innerHTML = ordered.map((language) => {
            const checked = touched.has(language.code) ? " checked" : "";
            const touchedMark = touched.has(language.code)
                ? `<span class="export-language-touched">used</span>`
                : "";
            return `
                <label class="export-language-option">
                    <input type="checkbox" value="${shared.escapeHtml(language.code)}"${checked} />
                    <span class="export-language-name">
                        ${shared.escapeHtml(language.label)}
                        <span class="export-language-native">${shared.escapeHtml(language.nativeLabel)}</span>
                        <span class="export-language-code">(${shared.escapeHtml(language.code)})</span>
                        ${touchedMark}
                    </span>
                </label>
            `;
        }).join("");

        els.modal.hidden = false;

        return new Promise((resolve) => {
            function cleanup() {
                els.modal.hidden = true;
                els.confirmBtn.removeEventListener("click", onConfirm);
                els.cancelBtn.removeEventListener("click", onCancel);
                selectAllBtn.removeEventListener("click", onSelectAll);
                clearBtn.removeEventListener("click", onClear);
            }
            function checkedInputs() {
                return Array.from(els.list.querySelectorAll("input[type=checkbox]"));
            }
            function onSelectAll() {
                checkedInputs().forEach((input) => { input.checked = true; });
            }
            function onClear() {
                checkedInputs().forEach((input) => { input.checked = false; });
            }
            function onConfirm() {
                const selected = checkedInputs()
                    .filter((input) => input.checked)
                    .map((input) => input.value);
                cleanup();
                resolve(selected);
            }
            function onCancel() {
                cleanup();
                resolve(null);
            }
            const selectAllBtn = document.getElementById("exportLanguagesSelectAllBtn");
            const clearBtn = document.getElementById("exportLanguagesClearBtn");
            els.confirmBtn.addEventListener("click", onConfirm);
            els.cancelBtn.addEventListener("click", onCancel);
            if (selectAllBtn) selectAllBtn.addEventListener("click", onSelectAll);
            if (clearBtn) clearBtn.addEventListener("click", onClear);
        });
    }

    async function fetchGeneratedDocuments() {
        if (!app.state.roomId) {
            throw new Error("No active room is available.");
        }
        const response = await fetch(`${app.HTTP_BASE}/api/rooms/${encodeURIComponent(app.state.roomId)}/export-documents`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                target_language: refs.tgtLangEl.value,
                llm: currentLlmConfigPayload()
            })
        });

        if (!response.ok) {
            const detail = await response.text().catch(() => "");
            throw new Error(detail || `Export failed with status ${response.status}`);
        }

        return await response.json();
    }

    async function startMeetingPackageJob(audioBlob, recordedItems = [], fullItems = []) {
        const formData = new FormData();
        const ext = app.extensionForMimeType(audioBlob?.type || app.state.recordingMimeType);
        formData.append("audio", audioBlob, `voice_recording.${ext}`);
        formData.append("transcript_json", JSON.stringify(recordedItems || []));
        formData.append("documents_transcript_json", JSON.stringify(fullItems || []));
        formData.append("llm_json", JSON.stringify(currentLlmConfigPayload()));
        formData.append("asr_json", JSON.stringify(currentAsrConfigPayload()));

        const response = await fetch(`${app.HTTP_BASE}/api/export-package/start`, {
            method: "POST",
            body: formData
        });

        if (!response.ok) {
            const detail = await response.text().catch(() => "");
            throw new Error(detail || `Export failed with status ${response.status}`);
        }

        return await response.json();
    }

    async function pollMeetingPackageJob(jobId) {
        return await exportUi.pollMeetingPackageJob({
            httpBase: app.HTTP_BASE,
            jobId,
            onProgress: updateExportProgress
        });
    }

    async function downloadMeetingPackageJob(jobId, fallbackName) {
        return await exportUi.downloadMeetingPackageBlob({
            httpBase: app.HTTP_BASE,
            jobId,
            fallbackName: fallbackName || "meeting_package.zip"
        });
    }

    async function downloadLegacyPackage(stamp) {
        const originalText = buildTranscriptText("original");
        const translationText = buildTranscriptText("translation");
        const generated = await fetchGeneratedDocuments();
        const zip = new JSZip();

        zip.file(`transcript_original_${stamp}.txt`, originalText);
        zip.file(`transcript_translation_${stamp}.txt`, translationText);
        zip.file(`transcript_parallel_${stamp}.csv`, buildParallelCsv());

        if (Array.isArray(generated?.documents)) {
            generated.documents.forEach((doc) => {
                if (!doc?.filename || !doc?.content) return;
                zip.file(doc.filename, doc.content);
            });
        }

        await app.addVoiceRecordingFiles(zip, stamp);
        return await zip.generateAsync({ type: "blob" });
    }

    async function buildLightweightMeetingZip() {
        return await exportUi.buildDocumentsZip({
            loadDocuments: fetchGeneratedDocuments,
            onProgress: updateExportProgress,
            emptyMessage: "Meeting documents are not available yet. Keep the conversation going for more content."
        });
    }

    async function downloadCurrentMeetingBundle(roomIdOverride = "") {
        const stamp = sessionStamp();
        const explicitRoomId = typeof roomIdOverride === "string" ? roomIdOverride.trim() : "";
        const roomId = explicitRoomId || app.state.roomId;
        const downloadPackageOnly = !!explicitRoomId && explicitRoomId !== app.state.roomId;
        refs.downloadBtn.disabled = true;
        if (refs.downloadPreviousRoomBtn && downloadPackageOnly) {
            refs.downloadPreviousRoomBtn.disabled = true;
        }
        setExportOverlay(true);

        try {
            if (!downloadPackageOnly && app.state.recordingState === "recording" && (app.state.canDownloadPackage || app.state.recordingSessionId)) {
                throw new Error("Pause recording or stop live before downloading the full package.");
            }
            if (downloadPackageOnly || app.state.canDownloadPackage || app.state.recordingSessionId) {
                // Ask the presenter which languages the package should
                // include. The chooser lists every supported language;
                // touched ones are pre-selected. Selection may be empty
                // (the server then falls back to source+target).
                const touched = downloadPackageOnly
                    ? []
                    : await fetchTouchedLanguages(roomId);
                const selectedLanguages = await promptExportLanguages(touched);
                if (selectedLanguages === null) {
                    app.setStatus("Download cancelled");
                    return;
                }

                app.setStatus("Preparing package…");
                updateExportProgress(6, "Queued", "Export job created. Waiting for the backend to start processing.");
                const response = await fetch(`${app.HTTP_BASE}/api/rooms/${encodeURIComponent(roomId)}/export-package/start`, {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({
                        target_language: refs.tgtLangEl.value,
                        export_languages: selectedLanguages,
                        llm: currentLlmConfigPayload(),
                        asr: currentAsrConfigPayload()
                    })
                });
                if (!response.ok) {
                    const detail = await response.text().catch(() => "");
                    throw new Error(detail || `Export failed with status ${response.status}`);
                }
                const started = await response.json();
                const finalStatus = await pollMeetingPackageJob(started.job_id);
                const { blob, filename } = await downloadMeetingPackageJob(started.job_id, finalStatus.archive_name || `meeting_package_${stamp}.zip`);
                shared.triggerBlobDownload(blob, filename || `meeting_package_${stamp}.zip`);
                updateExportProgress(100, "Done", "Meeting package downloaded.");
                app.setStatus("Meeting package downloaded.");
            } else {
                throw new Error("Recording is required before download is available.");
            }
        } catch (error) {
            console.error(error);
            app.setStatus("Export failed");
            alert(`Could not generate the download. ${error?.message || ""}`.trim());
        } finally {
            setTimeout(() => {
                setExportOverlay(false);
                updateExportProgress(0, "Queued", "Preparing export.");
            }, 600);
            app.syncDownloadAvailability();
            app.updatePreviousRoomNote();
        }
    }

    Object.assign(app, {
        sessionStamp,
        buildTranscriptText,
        buildParallelCsv,
        currentLlmConfigPayload,
        currentAsrConfigPayload,
        setExportOverlay,
        updateExportProgress,
        fetchGeneratedDocuments,
        startMeetingPackageJob,
        pollMeetingPackageJob,
        downloadMeetingPackageJob,
        downloadLegacyPackage,
        buildLightweightMeetingZip,
        downloadCurrentMeetingBundle
    });
})();
