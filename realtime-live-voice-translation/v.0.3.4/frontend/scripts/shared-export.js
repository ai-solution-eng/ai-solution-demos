(() => {
    const shared = window.RealtimeTranslationShared;

    function setExportOverlay(refs, visible) {
        refs.exportOverlayEl.classList.toggle("visible", !!visible);
    }

    function updateExportProgress(refs, progress = 0, stage = "Queued", detail = "") {
        const pct = Math.max(0, Math.min(100, Number(progress) || 0));
        refs.exportBarEl.style.width = `${pct}%`;
        refs.exportStageEl.textContent = stage || "Working…";
        refs.exportDetailEl.textContent = detail || "";
        refs.exportPercentEl.textContent = `${Math.round(pct)}%`;
    }

    async function pollMeetingPackageJob({ httpBase, jobId, onProgress }) {
        while (true) {
            const response = await fetch(`${httpBase}/api/export-package/status/${jobId}`, {
                method: "GET",
                cache: "no-store"
            });
            if (!response.ok) {
                const detail = await response.text().catch(() => "");
                throw new Error(detail || `Export status failed with status ${response.status}`);
            }
            const payload = await response.json();
            if (typeof onProgress === "function") {
                onProgress(payload.progress || 0, payload.stage || "Working…", payload.detail || "");
            }
            if (payload.status === "completed") return payload;
            if (payload.status === "failed") {
                throw new Error(payload.error || payload.detail || "Export failed.");
            }
            await new Promise((resolve) => setTimeout(resolve, 1000));
        }
    }

    async function downloadMeetingPackageBlob({ httpBase, jobId, fallbackName = "meeting_package.zip" }) {
        const response = await fetch(`${httpBase}/api/export-package/download/${jobId}`, {
            method: "GET"
        });
        if (!response.ok) {
            const detail = await response.text().catch(() => "");
            throw new Error(detail || `Export download failed with status ${response.status}`);
        }
        const blob = await response.blob();
        const filename = shared.extractFilenameFromResponse(response, fallbackName);
        return { blob, filename };
    }

    async function buildDocumentsZip({ loadDocuments, onProgress, emptyMessage }) {
        if (typeof onProgress === "function") {
            onProgress(15, "Preparing documents", "Collecting translated meeting documents for download.");
            onProgress(45, "Generating documents", "Creating transcripts, summary, and minutes from the room transcript.");
        }

        const generated = await loadDocuments();
        const docs = generated?.documents || [];
        if (docs.length === 0) {
            throw new Error(emptyMessage || "Meeting documents are not available yet.");
        }

        if (typeof onProgress === "function") {
            onProgress(80, "Building ZIP", "Packaging meeting documents for download.");
        }

        const zip = new JSZip();
        docs.forEach((doc, index) => {
            const filename = doc.filename || `notes_${index + 1}.txt`;
            zip.file(filename, doc.content || "");
        });
        return await zip.generateAsync({ type: "blob" });
    }

    window.RealtimeTranslationExport = Object.freeze({
        buildDocumentsZip,
        downloadMeetingPackageBlob,
        pollMeetingPackageJob,
        setExportOverlay,
        updateExportProgress
    });
})();
