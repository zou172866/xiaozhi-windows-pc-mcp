/* AudioWorkletProcessor: resamples browser mic audio to 16kHz mono Int16 PCM (60ms = 960 samples/frame). */
class XiaozhiPcmCaptureProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this._targetRate = 16000;
    this._frameSamples = 960;
    this._out = new Int16Array(this._frameSamples);
    this._outIdx = 0;
    this._phase = 0;
    this._lastSample = 0;
    this._energySum = 0;
  }

  process(inputs) {
    const input = inputs && inputs[0] && inputs[0][0];
    if (!input || input.length === 0) return true;

    const srcRate = sampleRate || 48000;
    const ratio = srcRate / this._targetRate;
    let pos = this._phase;
    const len = input.length;

    while (pos < len) {
      const idx0 = Math.floor(pos);
      const frac = pos - idx0;
      const s0 = idx0 < 0 ? this._lastSample : input[idx0];
      const s1 = idx0 + 1 < len ? input[idx0 + 1] : input[len - 1];
      let sample = s0 + (s1 - s0) * frac;
      if (sample > 1) sample = 1;
      else if (sample < -1) sample = -1;

      const pcm = sample < 0 ? Math.round(sample * 32768) : Math.round(sample * 32767);
      this._out[this._outIdx++] = pcm;
      this._energySum += sample * sample;

      if (this._outIdx >= this._frameSamples) {
        const rms = Math.sqrt(this._energySum / this._frameSamples);
        const frame = this._out;
        this._out = new Int16Array(this._frameSamples);
        this._outIdx = 0;
        this._energySum = 0;
        this.port.postMessage({ pcm: frame.buffer, rms }, [frame.buffer]);
      }
      pos += ratio;
    }

    this._lastSample = input[len - 1];
    this._phase = pos - len;
    return true;
  }
}

registerProcessor("xiaozhi-pcm-capture", XiaozhiPcmCaptureProcessor);
