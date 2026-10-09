// Runs on the audio thread. Plays the model's 24 kHz mono audio from a queue, resampled to the
// context's rate. "clear" drops everything queued (used when the user interrupts).
// Posts {playing} whenever playback starts or runs dry, so the UI can show "Speaking…".

const SOURCE_RATE = 24000;

class PlaybackProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.queue = [];
    this.chunk = null;
    this.index = 0; // fractional read position in this.chunk
    this.step = SOURCE_RATE / sampleRate;
    this.playing = false;
    this.port.onmessage = ({ data }) => {
      if (data.type === "audio") {
        this.queue.push(data.samples);
      } else if (data.type === "clear") {
        this.queue = [];
        this.chunk = null;
        this.index = 0;
      }
    };
  }

  process(_inputs, outputs) {
    const out = outputs[0][0];
    for (let i = 0; i < out.length; i++) {
      if (!this.chunk) {
        this.chunk = this.queue.shift() || null;
        this.index = 0;
        if (!this.chunk) {
          out[i] = 0;
          continue;
        }
      }
      // Linear interpolation between neighbouring source samples
      const j = Math.floor(this.index);
      const a = this.chunk[j];
      const b = j + 1 < this.chunk.length ? this.chunk[j + 1] : a;
      out[i] = a + (b - a) * (this.index - j);

      this.index += this.step;
      if (this.index >= this.chunk.length) {
        this.index -= this.chunk.length;
        this.chunk = this.queue.shift() || null;
        if (!this.chunk) this.index = 0;
      }
    }

    const playing = this.chunk !== null || this.queue.length > 0;
    if (playing !== this.playing) {
      this.playing = playing;
      this.port.postMessage({ playing });
    }
    return true;
  }
}

registerProcessor("playback-processor", PlaybackProcessor);
