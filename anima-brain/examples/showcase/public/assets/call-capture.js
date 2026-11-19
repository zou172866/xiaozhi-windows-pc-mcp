// 100ms PCM16 mono frames. Resample the actual AudioContext rate to 16kHz.
class CallCapture extends AudioWorkletProcessor {
  constructor() {
    super();
    this.samples = [];
    this.position = 0;
    this.frame = [];
    this.energy = 0;
  }
  process(inputs) {
    const input = inputs[0]?.[0];
    if (!input) return true;
    this.samples.push(...input);
    const step = sampleRate / 16000;
    while (this.position + 1 < this.samples.length) {
      const i = Math.floor(this.position),
        f = this.position - i;
      const v = this.samples[i] * (1 - f) + this.samples[i + 1] * f;
      this.frame.push(Math.round(Math.max(-1, Math.min(1, v)) * 32767));
      this.energy += v * v;
      this.position += step;
      if (this.frame.length === 1600) {
        const pcm = new Int16Array(this.frame);
        this.port.postMessage(
          { pcm: pcm.buffer, rms: Math.sqrt(this.energy / 1600) },
          [pcm.buffer],
        );
        this.frame = [];
        this.energy = 0;
      }
    }
    const used = Math.floor(this.position);
    this.samples.splice(0, used);
    this.position -= used;
    return true;
  }
}
registerProcessor("call-capture", CallCapture);
