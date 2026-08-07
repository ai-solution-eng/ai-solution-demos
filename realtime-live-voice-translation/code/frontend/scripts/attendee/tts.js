(() => {
    const app = window.AttendeeApp;
    const { refs, shared } = app;

    const state = {
        audioQueue: [],
        isPlaying: false,
        mediaRecorder: null,
        recordedChunks: [],
        isRecording: false,
        voicesLoaded: false,
        waveAnalyser: null,
        waveAnimId: null,
        stagedBlob: null,
        stagedFilename: "",
        stagedUrl: ""
    };

    function ttsHttpBase() {
        return app.HTTP_BASE;
    }

    function sendTtsConfig() {
        if (!app.state.ws || app.state.ws.readyState !== WebSocket.OPEN) return;
        app.state.ws.send(JSON.stringify({
            type: "tts_config",
            muted: app.state.ttsMuted,
            voice: app.state.ttsVoice || app.state.ttsDefaultVoice
        }));
    }

    function updateMuteUI() {
        const muted = app.state.ttsMuted;
        refs.ttsMuteBtnEl.setAttribute("aria-pressed", String(!muted));
        refs.ttsMuteBtnEl.classList.toggle("tts-active", !muted);
        const labelEl = refs.ttsMuteBtnEl.querySelector(".tts-mute-label");
        if (labelEl) labelEl.textContent = muted ? "TTS: Off" : "TTS: On";
    }

    function toggleMute() {
        app.state.ttsMuted = !app.state.ttsMuted;
        updateMuteUI();
        sendTtsConfig();
        if (app.state.ttsMuted) {
            state.audioQueue = [];
            state.isPlaying = false;
        }
    }

    function syncUploadButtons() {
        const name = refs.ttsVoiceNameInputEl.value.trim();
        const consented = refs.ttsConsentCheckEl.checked;
        const hasAudio = !!state.stagedBlob;
        refs.ttsUploadFileBtnEl.disabled = !(name.length > 0 && consented);
        refs.ttsRecordBtnEl.disabled = !(name.length > 0 && consented);
        refs.ttsAcceptBtnEl.disabled = !(name.length > 0 && consented && hasAudio);
    }

    function setTranscribeStatus(text, isError) {
        const el = refs.ttsTranscribeStatusEl;
        if (el) {
            el.textContent = text || "";
            el.hidden = !text;
            el.style.color = isError ? "#ff1744" : "";
        }
    }

    function showRefTextError(message) {
        const el = refs.ttsVoiceRefTextErrorEl;
        if (!el) return;
        if (message) {
            el.textContent = message;
            el.hidden = false;
        } else {
            el.textContent = "";
            el.hidden = true;
        }
    }

    function clearStagedAudio() {
        if (state.stagedUrl) URL.revokeObjectURL(state.stagedUrl);
        state.stagedBlob = null;
        state.stagedFilename = "";
        state.stagedUrl = "";
        if (refs.ttsAudioToolsEl) refs.ttsAudioToolsEl.hidden = true;
        if (refs.ttsAudioPlayerEl) refs.ttsAudioPlayerEl.removeAttribute("src");
        setTranscribeStatus("");
        showRefTextError("");
    }

    async function stageAudioBlob(blob, filename) {
        if (state.stagedUrl) URL.revokeObjectURL(state.stagedUrl);
        state.stagedBlob = blob;
        state.stagedFilename = filename || "sample.webm";
        state.stagedUrl = URL.createObjectURL(blob);
        if (refs.ttsAudioPlayerEl) {
            refs.ttsAudioPlayerEl.src = state.stagedUrl;
        }
        if (refs.ttsAudioToolsEl) refs.ttsAudioToolsEl.hidden = false;
        refs.ttsVoiceRefTextEl.value = "";
        showRefTextError("");
        syncUploadButtons();

        if (!app.state.roomId) {
            setTranscribeStatus("Connect to a room to transcribe.", true);
            return;
        }

        setTranscribeStatus("Transcribing audio...");
        refs.ttsAcceptBtnEl.disabled = true;
        const formData = new FormData();
        formData.append("audio_sample", blob, state.stagedFilename);
        try {
            const resp = await fetch(`${ttsHttpBase()}/api/rooms/${encodeURIComponent(app.state.roomId)}/tts/voices/transcribe`, {
                method: "POST",
                body: formData
            });
            if (!resp.ok) {
                const detail = await resp.text().catch(() => "");
                showRefTextError(`Auto-transcription failed (${resp.status}). You can type the transcript below.`);
                setTranscribeStatus("");
                return;
            }
            const data = await resp.json();
            const text = (data && data.text || "").trim();
            refs.ttsVoiceRefTextEl.value = text;
            setTranscribeStatus(text ? "Transcript ready — review and edit below." : "No text detected — type the transcript below.");
        } catch (err) {
            showRefTextError(`Auto-transcription failed: ${err.message}. You can type the transcript below.`);
            setTranscribeStatus("");
        } finally {
            syncUploadButtons();
        }
    }

    async function loadVoices() {
        if (!app.state.roomId || !app.state.ttsConfigured) return;
        try {
            const resp = await fetch(`${ttsHttpBase()}/api/rooms/${encodeURIComponent(app.state.roomId)}/tts/voices`);
            if (!resp.ok) return;
            const data = await resp.json();
            renderVoiceSelect(data);
            state.voicesLoaded = true;
        } catch (err) {
            console.error("Failed to load TTS voices:", err);
        }
    }

    function renderVoiceSelect(data) {
        const seen = new Set();
        const voices = [];
        const uploaded = data.uploaded_voices || [];
        const builtin = data.voices || [];

        for (const v of uploaded) {
            const name = typeof v === "string" ? v : (v.name || v._id || "");
            if (name && !seen.has(name)) {
                seen.add(name);
                voices.push({ name, label: `${name} (uploaded)`, isDefault: false });
            }
        }
        for (const v of builtin) {
            const name = typeof v === "string" ? v : (v.name || v._id || "");
            if (name && !seen.has(name)) {
                seen.add(name);
                voices.push({ name, label: name, isDefault: false });
            }
        }

        const defaultVoice = app.state.ttsDefaultVoice || "";
        if (defaultVoice && !seen.has(defaultVoice)) {
            voices.unshift({ name: defaultVoice, label: `${defaultVoice} (room default)`, isDefault: true });
        }

        const currentVoice = app.state.ttsVoice || app.state.ttsDefaultVoice || "";
        refs.ttsVoiceSelectEl.innerHTML = voices.map(v =>
            `<option value="${shared.escapeHtml(v.name)}"${v.name === currentVoice ? " selected" : ""}>${shared.escapeHtml(v.label)}</option>`
        ).join("");

        if (!app.state.ttsVoice && currentVoice) {
            app.state.ttsVoice = currentVoice;
        }
    }

    function onVoiceSelectChange() {
        app.state.ttsVoice = refs.ttsVoiceSelectEl.value;
        sendTtsConfig();
    }

    function openVoiceModal() {
        refs.ttsVoiceModalEl.hidden = false;
        loadVoicesForModal();
    }

    function closeVoiceModal() {
        refs.ttsVoiceModalEl.hidden = true;
        stopRecording();
        clearStagedAudio();
    }

    async function loadVoicesForModal() {
        if (!app.state.roomId || !app.state.ttsConfigured) {
            refs.ttsVoiceListEl.innerHTML = "<p>TTS is not configured for this room.</p>";
            return;
        }
        refs.ttsVoiceListEl.innerHTML = "<p>Loading voices...</p>";
        try {
            const resp = await fetch(`${ttsHttpBase()}/api/rooms/${encodeURIComponent(app.state.roomId)}/tts/voices`);
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
        if (!app.state.roomId || !name) return;
        try {
            const resp = await fetch(`${ttsHttpBase()}/api/rooms/${encodeURIComponent(app.state.roomId)}/tts/voices/preview`, {
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
                    await fetch(`${ttsHttpBase()}/api/rooms/${encodeURIComponent(app.state.roomId)}/tts/voices/${encodeURIComponent(name)}`, { method: "DELETE" });
                    loadVoicesForModal();
                    loadVoices();
                } catch (err) {
                    console.error("Failed to delete voice:", err);
                }
            });
        });
    }

    async function uploadVoiceAudio(audioBlob, filename) {
        const name = refs.ttsVoiceNameInputEl.value.trim();
        const refText = (refs.ttsVoiceRefTextEl.value || "").trim();
        if (!name || !refs.ttsConsentCheckEl.checked || !audioBlob) return;
        if (!refText) {
            showRefTextError("Please provide the transcript of the audio sample before saving.");
            refs.ttsVoiceRefTextEl.focus();
            return;
        }

        const formData = new FormData();
        formData.append("name", name);
        formData.append("consent", `user-${name}-${Math.floor(Date.now() / 1000)}`);
        formData.append("ref_text", refText);
        formData.append("audio_sample", audioBlob, filename || "sample.webm");

        refs.ttsAcceptBtnEl.disabled = true;
        setTranscribeStatus("Saving voice...");

        try {
            const resp = await fetch(`${ttsHttpBase()}/api/rooms/${encodeURIComponent(app.state.roomId)}/tts/voices`, {
                method: "POST",
                body: formData
            });
            if (resp.ok) {
                refs.ttsVoiceNameInputEl.value = "";
                refs.ttsVoiceRefTextEl.value = "";
                refs.ttsConsentCheckEl.checked = false;
                clearStagedAudio();
                syncUploadButtons();
                await loadVoicesForModal();
                await loadVoices();
            } else {
                const detail = await resp.text().catch(() => "");
                showRefTextError(`Upload failed: ${detail || resp.statusText}`);
            }
        } catch (err) {
            showRefTextError(`Upload failed: ${err.message}`);
        } finally {
            setTranscribeStatus("");
            syncUploadButtons();
        }
    }

    function acceptStagedVoice() {
        if (!state.stagedBlob) return;
        uploadVoiceAudio(state.stagedBlob, state.stagedFilename);
    }

    function onFileSelected() {
        const file = refs.ttsFileInputEl.files[0];
        if (!file) return;
        refs.ttsFileInputEl.value = "";
        stageAudioBlob(file, file.name);
    }

    function replayStagedAudio() {
        const player = refs.ttsAudioPlayerEl;
        if (!player || !state.stagedUrl) return;
        player.currentTime = 0;
        player.play().catch((err) => console.warn("Replay failed:", err));
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
                stageAudioBlob(blob, "recorded.webm");
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

    async function handleFinalSegment(segment) {
        if (app.state.ttsMuted || !app.state.connected || !app.state.roomId || !app.state.ttsConfigured) return;
        const text = (segment.translation || "").trim();
        if (!text) return;

        const voice = app.state.ttsVoice || app.state.ttsDefaultVoice || "";
        if (!voice) return;

        const lang = segment.tgt || app.state.targetLanguage || "";
        const segmentId = segment.segment_id || "";

        try {
            const resp = await fetch(`${ttsHttpBase()}/api/rooms/${encodeURIComponent(app.state.roomId)}/tts/generate`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ segment_id: segmentId, lang, voice, text })
            });
            if (!resp.ok) return;
            const blob = await resp.blob();
            const url = URL.createObjectURL(blob);
            state.audioQueue.push(url);
            if (!state.isPlaying) playNext();
        } catch (err) {
            console.error("TTS fetch failed:", err);
        }
    }

    function playNext() {
        if (state.audioQueue.length === 0) {
            state.isPlaying = false;
            return;
        }
        state.isPlaying = true;
        const url = state.audioQueue.shift();
        const audio = new Audio(url);
        audio.onended = () => {
            URL.revokeObjectURL(url);
            playNext();
        };
        audio.onerror = () => {
            URL.revokeObjectURL(url);
            playNext();
        };
        audio.play().catch(() => {
            URL.revokeObjectURL(url);
            playNext();
        });
    }

    function syncTtsUI() {
        const enabled = app.state.connected && app.state.ttsConfigured;
        refs.ttsBarEl.style.display = enabled ? "" : "none";
        refs.ttsMuteBtnEl.disabled = !enabled;
        refs.ttsVoiceSelectEl.disabled = !enabled;
        refs.ttsManageBtnEl.disabled = !enabled;
        updateMuteUI();
    }

    function onRoomJoined(msg) {
        if (msg.tts_default_voice !== undefined) {
            app.state.ttsDefaultVoice = msg.tts_default_voice || "";
        }
        if (msg.tts_configured !== undefined) {
            app.state.ttsConfigured = !!msg.tts_configured;
        }
        syncTtsUI();
        if (app.state.ttsConfigured) {
            loadVoices();
        }
    }

    function onRoomState(msg) {
        if (msg.tts_default_voice !== undefined) {
            app.state.ttsDefaultVoice = msg.tts_default_voice || "";
        }
        if (msg.tts_configured !== undefined) {
            app.state.ttsConfigured = !!msg.tts_configured;
        }
        syncTtsUI();
    }

    function init() {
        refs.ttsMuteBtnEl.addEventListener("click", toggleMute);
        refs.ttsVoiceSelectEl.addEventListener("change", onVoiceSelectChange);
        refs.ttsManageBtnEl.addEventListener("click", openVoiceModal);
        refs.ttsModalCloseBtnEl.addEventListener("click", closeVoiceModal);
        refs.ttsUploadFileBtnEl.addEventListener("click", () => refs.ttsFileInputEl.click());
        refs.ttsFileInputEl.addEventListener("change", onFileSelected);
        refs.ttsRecordBtnEl.addEventListener("click", toggleRecording);
        refs.ttsAcceptBtnEl.addEventListener("click", acceptStagedVoice);
        refs.ttsReplayBtnEl.addEventListener("click", replayStagedAudio);
        refs.ttsVoiceNameInputEl.addEventListener("input", syncUploadButtons);
        refs.ttsVoiceRefTextEl.addEventListener("input", () => {
            showRefTextError("");
            syncUploadButtons();
        });
        refs.ttsConsentCheckEl.addEventListener("change", syncUploadButtons);
        document.addEventListener("keydown", (e) => {
            if (e.key === "Escape" && refs.ttsVoiceModalEl && !refs.ttsVoiceModalEl.hidden) {
                closeVoiceModal();
            }
        });
        refs.ttsBarEl.style.display = "none";
        syncUploadButtons();
        syncTtsUI();
    }

    app.tts = {
        handleFinalSegment,
        syncTtsUI,
        onRoomJoined,
        onRoomState,
        sendTtsConfig,
        init
    };

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})();
