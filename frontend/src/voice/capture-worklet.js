// Runs on the audio thread. Downsamples the microphone to 16 kHz mono PCM16 (what the
// Gemini Live API expects) and posts 40 ms chunks plus a loudness level for the meter.

const TARGET_RATE = 16000;
const CHUNK_SAMPLES = 640; // 40 ms at 16 kHz

class CaptureProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.ratio = sampleRate / TARGET_RATE; // input samples per output sample
    this.pos = 0;
    this.sum = 0;
    this.count = 0;
    this.sumSquares = 0;
    this.out = new Int16Array(CHUNK_SAMPLES);
    this.outLength = 0;
  }

  process(inputs) {
    const input = inputs[0][0];
    if (!input) return true;

    for (let i = 0; i < input.length; i++) {
      // Average each window of input samples: a simple low-pass that avoids aliasing
      this.sum += input[i];
      this.count++;
      this.pos++;
      if (this.pos < this.ratio) continue;

      this.pos -= this.ratio;
      const s = Math.max(-1, Math.min(1, this.sum / this.count));
      this.sum = 0;
      this.count = 0;
      this.sumSquares += s * s;
      this.out[this.outLength++] = s < 0 ? s * 0x8000 : s * 0x7fff;

      if (this.outLength === CHUNK_SAMPLES) {
        const level = Math.sqrt(this.sumSquares / CHUNK_SAMPLES);
        this.port.postMessage({ pcm: this.out.buffer, level }, [this.out.buffer]);
        this.out = new Int16Array(CHUNK_SAMPLES);
        this.outLength = 0;
        this.sumSquares = 0;
      }
    }
    return true;
  }
}

registerProcessor("capture-processor", CaptureProcessor);
