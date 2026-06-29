class PCMWorkletProcessor extends AudioWorkletProcessor {
    constructor() {
        super();
        this.targetSr = 16000;
        this.srcSr = sampleRate;          // AudioContext SR (often 48000)
        this.ratio = this.srcSr / this.targetSr;

        this.frameMs = 32;
        this.frameSamples = Math.round(this.targetSr * this.frameMs / 1000); // 320
        this._in = [];
        this._resamp = [];
        this._t = 0.0;
    }

    _resamplePush(input) {
        for (let i = 0; i < input.length; i++) this._in.push(input[i]);

        while (this._t + 1 < this._in.length) {
            const i0 = Math.floor(this._t);
            const frac = this._t - i0;
            const s0 = this._in[i0];
            const s1 = this._in[i0 + 1];
            this._resamp.push(s0 + (s1 - s0) * frac);
            this._t += this.ratio;
        }

        const drop = Math.floor(this._t);
        if (drop > 0) {
            this._in.splice(0, drop);
            this._t -= drop;
        }
    }

    _floatToInt16PCM(floats) {
        const buf = new ArrayBuffer(floats.length * 2);
        const view = new DataView(buf);
        for (let i = 0; i < floats.length; i++) {
            let x = floats[i];
            if (x > 1) x = 1;
            if (x < -1) x = -1;
            const s = x < 0 ? x * 32768 : x * 32767;
            view.setInt16(i * 2, s, true);
        }
        return buf;
    }

    process(inputs) {
        const input = inputs[0];
        if (!input || !input[0] || input[0].length === 0) return true;

        const mono = input[0];
        this._resamplePush(mono);

        while (this._resamp.length >= this.frameSamples) {
            const frame = this._resamp.slice(0, this.frameSamples);
            this._resamp = this._resamp.slice(this.frameSamples);
            const pcmBuf = this._floatToInt16PCM(frame);
            this.port.postMessage(pcmBuf, [pcmBuf]);
        }
        return true;
    }
}

registerProcessor("pcm-worklet", PCMWorkletProcessor);