import captureUrl from "./capture-worklet.js?url";

export const micSupported = () => Boolean(navigator.mediaDevices?.getUserMedia && window.AudioWorkletNode);

export function micError(e) {
  if (e.name === "NotAllowedError") {
    return "Microphone access was blocked. Allow it in the browser's site settings and try again.";
  }
  if (e.name === "NotFoundError") return "No microphone was found.";
  return `Couldn't start the microphone: ${e.message || e.name}`;
}

/**
 * Opens the microphone on an AudioContext and calls onChunk({pcm, level}) every 40 ms,
 * where pcm is an ArrayBuffer of 16 kHz mono PCM16 and level is its loudness (RMS, 0..1).
 * Returns the MediaStream; stop its tracks (and close the context) to release the mic.
 */
export async function openMic(ctx, onChunk) {
  const stream = await navigator.mediaDevices.getUserMedia({
    audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 },
  });
  try {
    await ctx.audioWorklet.addModule(captureUrl);
    const capture = new AudioWorkletNode(ctx, "capture-processor", { numberOfOutputs: 0 });
    capture.port.onmessage = ({ data }) => onChunk(data);
    ctx.createMediaStreamSource(stream).connect(capture);
  } catch (e) {
    stream.getTracks().forEach((t) => t.stop());
    throw e;
  }
  return stream;
}
