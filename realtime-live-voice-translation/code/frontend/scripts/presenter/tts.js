(() => {
    const app = window.PresenterApp;
    const { refs, shared } = app;

    const state = {
        mediaRecorder: null,
        recordedChunks: [],
        isRecording: false,
        waveAnalyser: null,
        waveAnimId: null
    };

    function ttsHttpBase() {
        return app.HTTP_BASE;
    }

    function getRoomId() {
        return app.state.roomId || "";
    }

    function openVoiceModal() {
        refs.ttsVoiceModalEl.hidden = false;
        loadVoicesForModal();
    }

    function closeVoiceModal() {
        refs.ttsVoiceModalEl.hidden = true;
        stopRecording();
    }

    function syncUploadButtons() {
        const name = refs.ttsVoiceNameInputEl.value.trim();
        const consented = refs.ttsConsentCheckEl.checked;
        const enabled = name.length > 0 && consented;
        refs.ttsUploadFileBtnEl.disabled = !enabled;
        refs.ttsRecordBtnEl.disabled = !enabled;
    }

    async function loadVoicesForModal() {
        const roomId = getRoomId();
        if (!roomId) {
            refs.ttsVoiceListEl.innerHTML = "<p>Connect to a room first.</p>";
            return;
        }
        refs.ttsVoiceListEl.innerHTML = "<p>Loading voices...</p>";
        try {
            const resp = await fetch(`${ttsHttpBase()}/api/rooms/${encodeURIComponent(roomId)}/tts/voices`);
            if (!resp.ok) {
                refs.ttsVoiceListEl.innerHTML = "<p>Could not load voices.</p>";
                return;
            }
            const data = await resp.json();
            renderVoiceModalList(data);
        } catch (err) {
            refs.ttsVoiceListEl.innerHTML = "<p>Could not load voices.</p>";
        }
    }

    async function previewVoice(name) {
        const roomId = getRoomId();
        if (!roomId || !name) return;
        try {
            const resp = await fetch(`${ttsHttpBase()}/api/rooms/${encodeURIComponent(roomId)}/tts/voices/preview`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ voice: name })
            });
            if (!resp.ok) return;
            const blob = await resp.blob();
            const audioCtx = new AudioContext();
            if (audioCtx.state === "suspended") await audioCtx.resume();
            const arrayBuffer = await blob.arrayBuffer();
            const audioBuffer = await audioCtx.decodeAudioData(arrayBuffer);
            const source = audioCtx.createBufferSource();
            source.buffer = audioBuffer;
            source.connect(audioCtx.destination);
            source.start(0);
        } catch (err) {
            console.error("Preview failed:", err);
        }
    }

    async function loadVoices() {
        const roomId = getRoomId();
        if (!roomId) return;
        try {
            const resp = await fetch(`${ttsHttpBase()}/api/rooms/${encodeURIComponent(roomId)}/tts/voices`);
            if (!resp.ok) return;
            const data = await resp.json();
            renderVoiceSelect(data);
        } catch (err) {
            console.error("Failed to load TTS voices:", err);
        }
    }

    function renderVoiceSelect(data) {
        const currentVoice = refs.ttsVoiceEl.value || "alys";
        const seen = new Set();
        const voices = [];
        const uploaded = data.uploaded_voices || [];
        const builtin = data.voices || [];

        for (const v of uploaded) {
            const name = typeof v === "string" ? v : (v.name || v._id || "");
            if (name && !seen.has(name)) {
                seen.add(name);
                voices.push(name);
            }
        }
        for (const v of builtin) {
            const name = typeof v === "string" ? v : (v.name || v._id || "");
            if (name && !seen.has(name)) {
                seen.add(name);
                voices.push(name);
            }
        }

        refs.ttsVoiceEl.innerHTML = voices.map(v =>
            `<option value="${shared.escapeHtml(v)}"${v === currentVoice ? " selected" : ""}>${shared.escapeHtml(v)}</option>`
        ).join("");
    }

    function renderVoiceModalList(data) {
        const items = [];
        const seen = new Set();
        const uploaded = data.uploaded_voices || [];
        const builtin = data.voices || [];

        for (const v of uploaded) {
            const name = typeof v === "string" ? v : (v.name || v._id || "");
            if (name && !seen.has(name)) {
                seen.add(name);
                items.push({ name, label: name, type: "uploaded", ref_text: typeof v === "object" ? (v.ref_text || "") : "" });
            }
        }
        for (const v of builtin) {
            const name = typeof v === "string" ? v : (v.name || v._id || "");
            if (name && !seen.has(name)) {
                seen.add(name);
                items.push({ name, label: name, type: "built-in", ref_text: typeof v === "object" ? (v.ref_text || "") : "" });
            }
        }

        if (items.length === 0) {
            refs.ttsVoiceListEl.innerHTML = "<p>No voice profiles available. Upload or record one below.</p>";
            return;
        }

        refs.ttsVoiceListEl.innerHTML = items.map(item => {
            const deleteBtn = item.type === "uploaded"
                ? `<button class="tts-voice-delete" data-name="${shared.escapeHtml(item.name)}" type="button">Delete</button>`
                : "";
            const playBtn = `<button class="tts-voice-play" data-name="${shared.escapeHtml(item.name)}" type="button" title="Preview voice">\u25B6</button>`;
            const refBadge = item.ref_text ? `<span class="tts-voice-ref" title="${shared.escapeHtml(item.ref_text)}">ref</span>` : "";
            return `<div class="tts-voice-item">${playBtn}<span class="tts-voice-name">${shared.escapeHtml(item.label)}</span><span class="tts-voice-type">${item.type}</span>${refBadge}${deleteBtn}</div>`;
        }).join("");

        refs.ttsVoiceListEl.querySelectorAll(".tts-voice-play").forEach(btn => {
            btn.addEventListener("click", () => previewVoice(btn.dataset.name));
        });
        refs.ttsVoiceListEl.querySelectorAll(".tts-voice-delete").forEach(btn => {
            btn.addEventListener("click", async () => {
                const name = btn.dataset.name;
                if (!name) return;
                try {
                    await fetch(`${ttsHttpBase()}/api/rooms/${encodeURIComponent(getRoomId())}/tts/voices/${encodeURIComponent(name)}`, { method: "DELETE" });
                    loadVoicesForModal();
                } catch (err) {
                    console.error("Failed to delete voice:", err);
                }
            });
        });
    }

    async function uploadVoiceAudio(audioBlob, filename) {
        const name = refs.ttsVoiceNameInputEl.value.trim();
        if (!name || !refs.ttsConsentCheckEl.checked) return;

        const formData = new FormData();
        formData.append("name", name);
        formData.append("consent", `user-${name}-${Math.floor(Date.now() / 1000)}`);
        formData.append("audio_sample", audioBlob, filename || "sample.webm");

        refs.ttsUploadFileBtnEl.disabled = true;
        refs.ttsRecordBtnEl.disabled = true;

        try {
            const resp = await fetch(`${ttsHttpBase()}/api/rooms/${encodeURIComponent(getRoomId())}/tts/voices`, {
                method: "POST",
                body: formData
            });
            if (resp.ok) {
                refs.ttsVoiceNameInputEl.value = "";
                refs.ttsConsentCheckEl.checked = false;
                syncUploadButtons();
                await loadVoicesForModal();
            } else {
                const detail = await resp.text().catch(() => "");
                alert(`Upload failed: ${detail || resp.statusText}`);
            }
        } catch (err) {
            alert(`Upload failed: ${err.message}`);
        } finally {
            refs.ttsUploadFileBtnEl.disabled = false;
            refs.ttsRecordBtnEl.disabled = false;
            syncUploadButtons();
        }
    }

    function onFileSelected() {
        const file = refs.ttsFileInputEl.files[0];
        if (!file) return;
        uploadVoiceAudio(file, file.name);
        refs.ttsFileInputEl.value = "";
    }

    function stopWaveVisualizer() {
        if (state.waveAnimId) {
            cancelAnimationFrame(state.waveAnimId);
            state.waveAnimId = null;
        }
        state.waveAnalyser = null;
    }

    function startWaveVisualizer(stream, existingCtx) {
        try {
            const audioCtx = existingCtx || new AudioContext();
            if (audioCtx.state === "suspended") audioCtx.resume();
            const source = audioCtx.createMediaStreamSource(stream);
            const analyser = audioCtx.createAnalyser();
            analyser.fftSize = 64;
            source.connect(analyser);
            state.waveAnalyser = analyser;

            const canvas = refs.ttsWaveCanvasEl;
            if (!canvas) return;
            const ctx = canvas.getContext("2d");
            const bufferLength = analyser.frequencyBinCount;
            const dataArray = new Uint8Array(bufferLength);

            function draw() {
                if (!state.waveAnalyser) return;
                state.waveAnimId = requestAnimationFrame(draw);
                analyser.getByteTimeDomainData(dataArray);

                ctx.fillStyle = "rgba(0,0,0,0.15)";
                ctx.fillRect(0, 0, canvas.width, canvas.height);

                ctx.lineWidth = 2;
                ctx.strokeStyle = "#00ffcc";
                ctx.beginPath();
                const sliceWidth = canvas.width / bufferLength;
                let x = 0;
                for (let i = 0; i < bufferLength; i++) {
                    const v = dataArray[i] / 128.0;
                    const y = v * canvas.height / 2;
                    if (i === 0) ctx.moveTo(x, y);
                    else ctx.lineTo(x, y);
                    x += sliceWidth;
                }
                ctx.lineTo(canvas.width, canvas.height / 2);
                ctx.stroke();
            }
            draw();
        } catch (e) {
            console.warn("Wave visualizer failed:", e);
        }
    }

    function showRecordBar(show) {
        const bar = document.getElementById("ttsRecordBar");
        if (bar) bar.hidden = !show;
    }

    async function startRecording() {
        try {
            const audioCtx = new AudioContext();
            if (audioCtx.state === "suspended") audioCtx.resume();
            const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
            const mime = MediaRecorder.isTypeSupported("audio/webm") ? "audio/webm" : "";
            state.mediaRecorder = mime ? new MediaRecorder(stream, { mimeType: mime }) : new MediaRecorder(stream);
            state.recordedChunks = [];
            state.isRecording = true;
            showRecordBar(true);
            startWaveVisualizer(stream, audioCtx);

            state.mediaRecorder.ondataavailable = (e) => {
                if (e.data.size > 0) state.recordedChunks.push(e.data);
            };
            state.mediaRecorder.onstop = () => {
                stream.getTracks().forEach(t => t.stop());
                stopWaveVisualizer();
                showRecordBar(false);
                const blob = new Blob(state.recordedChunks, { type: mime || "audio/webm" });
                state.isRecording = false;
                refs.ttsRecordBtnEl.textContent = "Record voice";
                uploadVoiceAudio(blob, "recorded.webm");
            };

            state.mediaRecorder.start();
            refs.ttsRecordBtnEl.textContent = "Stop recording";
        } catch (err) {
            alert(`Cannot access microphone: ${err.message}`);
        }
    }

    function stopRecording() {
        if (state.isRecording && state.mediaRecorder && state.mediaRecorder.state !== "inactive") {
            state.mediaRecorder.stop();
        }
    }

    function toggleRecording() {
        if (state.isRecording) {
            stopRecording();
        } else {
            startRecording();
        }
    }

    function init() {
        refs.ttsManageBtnEl.addEventListener("click", openVoiceModal);
        refs.ttsModalCloseBtnEl.addEventListener("click", closeVoiceModal);
        refs.ttsUploadFileBtnEl.addEventListener("click", () => refs.ttsFileInputEl.click());
        refs.ttsFileInputEl.addEventListener("change", onFileSelected);
        refs.ttsRecordBtnEl.addEventListener("click", toggleRecording);
        refs.ttsVoiceNameInputEl.addEventListener("input", syncUploadButtons);
        refs.ttsConsentCheckEl.addEventListener("change", syncUploadButtons);
        document.addEventListener("keydown", (e) => {
            if (e.key === "Escape" && refs.ttsVoiceModalEl && !refs.ttsVoiceModalEl.hidden) {
                closeVoiceModal();
            }
        });
        syncUploadButtons();
    }

    app.tts = { openVoiceModal, closeVoiceModal, loadVoices };
    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})();
