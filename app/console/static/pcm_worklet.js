/* AudioWorkletProcessor: boxcar anti-aliased resampling to 16kHz mono Int16 PCM (60ms = 960 samples/frame). */
class XiaozhiPcmCaptureProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this._targetRate = 16000;
    this._frameSamples = 960;
    this._out = new Int16Array(this._frameSamples);
    this._outIdx = 0;
    this._accSum = 0;
    this._accWeight = 0;
    this._energySum = 0;
  }

  process(inputs) {
    const input = inputs && inputs[0] && inputs[0][0];
    if (!input || input.length === 0) return true;

    const srcRate = sampleRate || 48000;
    const ratio = srcRate / this._targetRate;
    const len = input.length;

    for (let i = 0; i < len; i += 1) {
      const x = input[i];
      let rem = 1.0;
      while (rem > 1e-9) {
        const need = ratio - this._accWeight;
        if (rem >= need - 1e-9) {
          this._accSum += x * need;
          let sample = this._accSum / ratio;
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
          this._accSum = 0;
          this._accWeight = 0;
          rem = rem - need;
          if (rem < 1e-9) rem = 0;
        } else {
          this._accSum += x * rem;
          this._accWeight += rem;
          rem = 0;
        }
      }
    }

    return true;
  }
}

registerProcessor("xiaozhi-pcm-capture", XiaozhiPcmCaptureProcessor);
