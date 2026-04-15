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

    function currentWhisperConfigPayload() {
        const whisper = {
            base_url: refs.whisperBaseUrlEl.value.trim(),
            model: refs.whisperModelEl.value.trim()
        };
        const key = refs.whisperApiKeyEl.value.trim();
        if (key) whisper.api_key = key;
        return whisper;
    }

    function setExportOverlay(visible) {
        exportUi.setExportOverlay(refs, visible);
    }

    function updateExportProgress(progress = 0, stage = "Queued", detail = "") {
        exportUi.updateExportProgress(refs, progress, stage, detail);
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
        formData.append("whisper_json", JSON.stringify(currentWhisperConfigPayload()));

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
                app.setStatus("Preparing package…");
                updateExportProgress(6, "Queued", "Export job created. Waiting for the backend to start processing.");
                const response = await fetch(`${app.HTTP_BASE}/api/rooms/${encodeURIComponent(roomId)}/export-package/start`, {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({
                        target_language: refs.tgtLangEl.value,
                        llm: currentLlmConfigPayload(),
                        whisper: currentWhisperConfigPayload()
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
        currentWhisperConfigPayload,
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
