import { useCallback, useEffect, useRef, useState } from "react";
import { transcribe } from "../api.js";
import { micError, micSupported, openMic } from "./mic.js";

// Speech detection on the 40 ms chunks from the mic (levels are RMS of samples in -1..1)
const START_LEVEL = 0.02; // louder than this counts as speech
const STOP_LEVEL = 0.012; // quieter than this counts as silence
const START_CHUNKS = 3; // 120 ms of speech starts a clip
const PAUSE_CHUNKS = 18; // 720 ms of silence ends it
const PREROLL_CHUNKS = 8; // keep 320 ms before the speech started, so the first word isn't clipped
const MAX_CHUNKS = 750; // 30 s: send long monologues in pieces

function concat(chunks) {
  const out = new Int16Array(chunks.reduce((n, c) => n + c.length, 0));
  let offset = 0;
  for (const c of chunks) {
    out.set(c, offset);
    offset += c.length;
  }
  return out.buffer;
}

/**
 * Dictation: while recording, each stretch of speech (ended by a pause) is transcribed
 * by the backend and passed to onText(text, language), in order. language is the hint sent with each clip.
 *
 * recording: mic is on; transcribing: clips still waiting for text; levelRef: mic loudness.
 * stop() returns a promise that resolves once every clip has been transcribed.
 */
export default function useDictation({ onText, language }) {
  const [recording, setRecording] = useState(false);
  const [transcribing, setTranscribing] = useState(0);
  const [error, setError] = useState(null);
  const levelRef = useRef(0);
  const sessionRef = useRef(null);
  const queueRef = useRef(Promise.resolve());
  const onTextRef = useRef(onText);
  onTextRef.current = onText;
  const languageRef = useRef(language);
  languageRef.current = language;

  const sendClip = useCallback((chunks) => {
    const pcm = concat(chunks);
    setTranscribing((n) => n + 1);
    const request = transcribe(pcm, languageRef.current); // start now; results are still applied in order
    request.catch(() => {}); // handled below, once its turn in the queue comes
    queueRef.current = queueRef.current
      .then(() => request)
      .then(({ text, language }) => text && onTextRef.current(text, language))
      .catch((e) => setError(`Couldn't transcribe: ${e.message}`))
      .finally(() => setTranscribing((n) => n - 1));
  }, []);

  const onChunk = useCallback(
    (s, { pcm, level }) => {
      const chunk = new Int16Array(pcm);
      levelRef.current = level;
      if (!s.inSpeech) {
        s.preroll.push(chunk);
        if (s.preroll.length > PREROLL_CHUNKS) s.preroll.shift();
        s.loudRun = level > START_LEVEL ? s.loudRun + 1 : 0;
        if (s.loudRun >= START_CHUNKS) {
          s.inSpeech = true;
          s.clip = [...s.preroll];
          s.preroll = [];
          s.quietRun = 0;
        }
        return;
      }
      s.clip.push(chunk);
      s.quietRun = level < STOP_LEVEL ? s.quietRun + 1 : 0;
      if (s.quietRun >= PAUSE_CHUNKS || s.clip.length >= MAX_CHUNKS) {
        sendClip(s.clip);
        s.inSpeech = false;
        s.clip = [];
        s.loudRun = 0;
      }
    },
    [sendClip]
  );

  const release = useCallback((s) => {
    s.closed = true;
    levelRef.current = 0;
    s.stream?.getTracks().forEach((t) => t.stop());
    s.ctx.close().catch(() => {});
    if (sessionRef.current === s) sessionRef.current = null;
  }, []);

  const start = useCallback(async () => {
    if (sessionRef.current) return;
    setError(null);
    if (!micSupported()) {
      setError("Dictation needs a secure (https) page in a browser with microphone support.");
      return;
    }
    // Created inside the click, before any await
    const s = { ctx: new AudioContext(), preroll: [], clip: [], inSpeech: false, loudRun: 0, quietRun: 0 };
    sessionRef.current = s;
    setRecording(true);
    try {
      s.stream = await openMic(s.ctx, (data) => !s.closed && onChunk(s, data));
      if (s.closed) release(s); // stopped while the permission prompt was open
    } catch (e) {
      release(s);
      setRecording(false);
      setError(micError(e));
    }
  }, [onChunk, release]);

  // Stop recording; the speech in progress is still transcribed. Resolves when all text is in.
  const stop = useCallback(() => {
    const s = sessionRef.current;
    if (s) {
      if (s.inSpeech && s.clip.length) sendClip(s.clip);
      release(s);
      setRecording(false);
    }
    return queueRef.current;
  }, [release, sendClip]);

  useEffect(() => () => sessionRef.current && release(sessionRef.current), [release]);

  return { recording, transcribing, error, levelRef, start, stop };
}
