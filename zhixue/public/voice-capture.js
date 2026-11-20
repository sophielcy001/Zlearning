class VoiceCaptureProcessor extends AudioWorkletProcessor {
  constructor({ processorOptions }) {
    super();
    this.remaining = processorOptions.maxSamples;
    this.chunk = new Float32Array(4096);
    this.used = 0;
    this.active = true;
    this.port.onmessage = event => {
      if (event.data === 'stop') this.finish();
    };
  }

  flush() {
    if (!this.used) return;
    const samples = this.used === this.chunk.length ? this.chunk : this.chunk.slice(0, this.used);
    this.port.postMessage(samples, [samples.buffer]);
    this.chunk = new Float32Array(4096);
    this.used = 0;
  }

  finish() {
    if (!this.active) return;
    this.active = false;
    this.flush();
    this.port.postMessage(null);
  }

  process(inputs) {
    if (!this.active) return false;
    const input = inputs[0]?.[0];
    const length = Math.min(input?.length ?? 0, this.remaining);
    this.remaining -= length;
    for (let offset = 0; offset < length;) {
      const count = Math.min(length - offset, this.chunk.length - this.used);
      this.chunk.set(input.subarray(offset, offset + count), this.used);
      this.used += count;
      offset += count;
      if (this.used === this.chunk.length) this.flush();
    }
    if (this.remaining === 0) this.finish();
    return this.active;
  }
}

registerProcessor('zhixue-voice-capture', VoiceCaptureProcessor);
