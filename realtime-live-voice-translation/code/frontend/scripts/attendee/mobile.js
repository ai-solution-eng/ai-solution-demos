(() => {
    // Mobile browsers block audio playback until the page has had a user gesture.
    // The attendee page auto-joins from the room link, so TTS could otherwise stay
    // silent until the first tap. Prime the audio stack on the first interaction.
    const SILENT_WAV = "data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEAiBUAAIgVAAABAAgAZGF0YQAAAAA=";
    let unlocked = false;

    function unlockAudio() {
        if (unlocked) return;
        unlocked = true;

        try {
            const audio = new Audio(SILENT_WAV);
            audio.volume = 0;
            const played = audio.play();
            if (played && typeof played.catch === "function") played.catch(() => {});
        } catch (e) { /* non-fatal */ }

        try {
            const Ctx = window.AudioContext || window.webkitAudioContext;
            if (Ctx) {
                const ctx = new Ctx();
                if (ctx.state === "suspended") ctx.resume();
                const osc = ctx.createOscillator();
                const gain = ctx.createGain();
                gain.gain.value = 0.0001;
                osc.connect(gain);
                gain.connect(ctx.destination);
                osc.start();
                osc.stop(ctx.currentTime + 0.05);
                setTimeout(() => {
                    try { ctx.close(); } catch (e) { /* non-fatal */ }
                }, 250);
            }
        } catch (e) { /* non-fatal */ }
    }

    const options = { passive: true, once: true };
    window.addEventListener("touchstart", unlockAudio, options);
    window.addEventListener("click", unlockAudio, { once: true });
})();
