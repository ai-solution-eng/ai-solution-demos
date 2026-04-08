(() => {
    const app = window.AttendeeApp;
    const { refs, shared } = app;
    const exportUi = window.RealtimeTranslationExport;

    async function pollMeetingPackageJob(jobId) {
        return await exportUi.pollMeetingPackageJob({
            httpBase: app.HTTP_BASE,
            jobId,
            onProgress: app.updateExportProgress
        });
    }

    async function downloadMeetingPackageJob(jobId, fallbackName) {
        const { blob, filename } = await exportUi.downloadMeetingPackageBlob({
            httpBase: app.HTTP_BASE,
            jobId,
            fallbackName
        });
        shared.triggerBlobDownload(blob, filename);
    }

    async function fetchGeneratedDocuments() {
        if (!app.state.roomId) {
            throw new Error("No active room is available.");
        }
        const response = await fetch(`${app.HTTP_BASE}/api/rooms/${encodeURIComponent(app.state.roomId)}/export-documents`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ target_language: app.state.targetLanguage })
        });
        if (!response.ok) {
            const detail = await response.text().catch(() => "");
            throw new Error(detail || `Export failed with status ${response.status}`);
        }
        return await response.json();
    }

    async function buildLightweightMeetingZip() {
        return await exportUi.buildDocumentsZip({
            loadDocuments: fetchGeneratedDocuments,
            onProgress: app.updateExportProgress,
            emptyMessage: "Meeting documents are not available yet. Keep the conversation going for more content."
        });
    }

    async function startDownload() {
        if (!app.state.roomId || !app.canDownloadAnything()) return;
        if (refs.downloadBtn) refs.downloadBtn.disabled = true;
        app.setExportOverlay(true);
        try {
            if (app.state.canDownloadPackage || app.state.recordingSessionId) {
                const response = await fetch(`${app.HTTP_BASE}/api/rooms/${encodeURIComponent(app.state.roomId)}/export-package/start`, {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ target_language: app.state.targetLanguage })
                });
                if (!response.ok) {
                    const detail = await response.text().catch(() => "");
                    throw new Error(detail || `Export failed with status ${response.status}`);
                }
                const started = await response.json();
                app.updateExportProgress(6, "Queued", "Export job created. Waiting for the backend to start processing.");
                const finalStatus = await pollMeetingPackageJob(started.job_id);
                await downloadMeetingPackageJob(finalStatus.job_id || started.job_id, finalStatus.archive_name || "meeting_package.zip");
            } else {
                throw new Error("Recording is required before download is available.");
            }
        } catch (error) {
            console.error(error);
            alert(`Could not download the bundle. ${error?.message || ""}`.trim());
        } finally {
            setTimeout(() => {
                app.setExportOverlay(false);
                app.updateExportProgress(0, "Queued", "Preparing export.");
            }, 600);
            app.syncRecordingUI();
        }
    }

    Object.assign(app, {
        pollMeetingPackageJob,
        downloadMeetingPackageJob,
        fetchGeneratedDocuments,
        buildLightweightMeetingZip,
        startDownload
    });
})();
