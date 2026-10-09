import { useCallback, useEffect, useRef, useState } from "react";
import captureUrl from "./capture-worklet.js?url";
import playbackUrl from "./playback-worklet.js?url";

// Same origin by default (Vite proxy in dev, FastAPI in prod), like api.js
const BASE = (import.meta.env.VITE_BACKEND_URL || "").replace(/\/$/, "");
const EMPTY_CAPTION = { user: "", assistant: "" };
const RELEASE_TAIL_MS = 300; // push-to-talk: keep sending this long after release

function liveUrl(params) {
  const origin = BASE
    ? BASE.replace(/^http/, "ws")
    : `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}`;
  const query = new URLSearchParams(Object.entries(params).filter(([, v]) => v)).toString();
  return `${origin}/live${query ? `?${query}` : ""}`;
}

function startError(e) {
  if (e.name === "NotAllowedError") {
    return "Microphone access was blocked. Allow it in the browser's site settings and try again.";
  }
  if (e.name === "NotFoundError") return "No microphone was found.";
  return `Couldn't start voice chat: ${e.message || e.name}`;
}

function closeError(event) {
  if (event.reason) return event.reason;
  return event.code === 1006 ? "Can't reach the backend. Is it running?" : "The voice connection closed.";
}

// The first event of a new turn clears the previous turn's caption (which stays up until then,
// because playback usually outlasts the turn_complete event)
function beginTurn(s) {
  if (!s.turnDone) return;
  s.turnDone = false;
  s.caption = { ...EMPTY_CAPTION };
}

function pcm16ToFloat32(buffer) {
  const pcm = new Int16Array(buffer, 0, buffer.byteLength >> 1);
  const out = new Float32Array(pcm.length);
  for (let i = 0; i < pcm.length; i++) out[i] = pcm[i] / 0x8000;
  return out;
}

/**
 * One voice conversation over the backend's /live WebSocket (see voice_plan.md).
 *
 * state: idle | connecting | listening | thinking | speaking | error
 * talk: vad (hands-free) | ptt (push-to-talk: pressTalk / releaseTalk; held is true while pressed)
 * caption: live transcript of the current turn ({user, assistant})
 * levelRef: current mic loudness (0..1), read by the level meter without re-rendering
 * onTurn({user, assistant, sources, interrupted}) is called when the model finishes a turn.
 */
export default function useVoiceSession({ onTurn } = {}) {
  const [state, setState] = useState("idle");
  const [error, setError] = useState(null);
  const [muted, setMuted] = useState(false);
  const [talk, setTalk] = useState("vad");
  const [held, setHeld] = useState(false);
  const [caption, setCaption] = useState(EMPTY_CAPTION);
  const levelRef = useRef(0);
  const sessionRef = useRef(null);
  const onTurnRef = useRef(onTurn);
  onTurnRef.current = onTurn;

  const teardown = useCallback((s) => {
    s.closed = true;
    clearTimeout(s.releaseTimer);
    levelRef.current = 0;
    try {
      s.ws?.close();
    } catch {
      // already closed
    }
    s.stream?.getTracks().forEach((t) => t.stop());
    s.ctx.close().catch(() => {});
    if (sessionRef.current === s) sessionRef.current = null;
  }, []);

  const fail = useCallback(
    (s, message) => {
      if (s.closed) return;
      teardown(s);
      setError((prev) => prev || message);
      setState("error");
    },
    [teardown]
  );

  // Derive the visible state from what the session is doing right now
  const refresh = useCallback((s) => {
    if (s.closed || !s.ready) return;
    const speaking = s.playing || (s.gotAudio && !s.turnDone);
    setState(speaking ? "speaking" : s.thinking ? "thinking" : "listening");
  }, []);

  const handleEvent = useCallback(
    (s, event) => {
      switch (event.type) {
        case "ready":
          s.ready = true;
          refresh(s);
          break;
        case "input_transcript":
          beginTurn(s);
          s.turn.user += event.text;
          s.caption.user += event.text;
          setCaption({ ...s.caption });
          break;
        case "output_transcript":
          beginTurn(s);
          s.turn.assistant += event.text;
          s.caption.assistant += event.text;
          setCaption({ ...s.caption });
          break;
        case "tool_call":
          beginTurn(s);
          s.thinking = true;
          refresh(s);
          break;
        case "sources":
          s.turn.sources = event.sources;
          break;
        case "interrupted":
          // The user talked over the answer: drop the queued audio and listen again
          s.playback.port.postMessage({ type: "clear" });
          s.turn.interrupted = true;
          s.gotAudio = false;
          s.discardAudio = false;
          refresh(s);
          break;
        case "turn_complete": {
          const { user, assistant } = s.turn;
          if (user.trim()) s.history.push({ role: "user", content: user.trim() });
          if (assistant.trim()) s.history.push({ role: "assistant", content: assistant.trim() });
          onTurnRef.current?.({ ...s.turn });
          s.turn = { user: "", assistant: "", sources: [], interrupted: false };
          s.turnDone = true;
          s.thinking = false;
          s.gotAudio = false;
          s.discardAudio = false;
          refresh(s);
          break;
        }
        case "error":
          setError(event.message);
          break;
      }
    },
    [refresh]
  );

  const start = useCallback(
    async ({ answer, talk = "vad", history = [] } = {}) => {
      if (sessionRef.current) return;
      setError(null);
      setMuted(false);
      setHeld(false);
      setTalk(talk);
      setCaption(EMPTY_CAPTION);
      setState("connecting");

      if (!navigator.mediaDevices?.getUserMedia || !window.AudioWorkletNode) {
        setError("Voice chat needs a secure (https) page in a browser with microphone support.");
        setState("error");
        return;
      }

      // Created before any await, while we're still inside the click, so the browser allows audio
      const s = {
        ctx: new AudioContext(),
        answer,
        talk,
        // Conversation so far (seeded history + finished voice turns), reused when switching talk mode
        history: [...history],
        ready: false,
        muted: false,
        held: false, // push-to-talk button is down (or in its release tail)
        releasing: false,
        discardAudio: false, // drop the rest of an answer the user talked over in push-to-talk
        releaseTimer: null,
        playing: false,
        thinking: false,
        gotAudio: false,
        turnDone: true,
        closed: false,
        caption: { ...EMPTY_CAPTION },
        turn: { user: "", assistant: "", sources: [], interrupted: false },
      };
      sessionRef.current = s;

      try {
        s.stream = await navigator.mediaDevices.getUserMedia({
          audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 },
        });
        // Ended while the permission prompt was open: release the mic we just got
        if (s.closed) return teardown(s);
        await Promise.all([s.ctx.audioWorklet.addModule(captureUrl), s.ctx.audioWorklet.addModule(playbackUrl)]);
        if (s.closed) return teardown(s);

        s.playback = new AudioWorkletNode(s.ctx, "playback-processor", {
          numberOfInputs: 0,
          outputChannelCount: [1],
        });
        s.playback.port.onmessage = ({ data }) => {
          s.playing = data.playing;
          refresh(s);
        };
        s.playback.connect(s.ctx.destination);

        const capture = new AudioWorkletNode(s.ctx, "capture-processor", { numberOfOutputs: 0 });
        capture.port.onmessage = ({ data }) => {
          // Hands-free streams unless muted; push-to-talk streams only while the button is held
          const live = s.talk === "ptt" ? s.held : !s.muted;
          levelRef.current = live ? data.level : 0;
          if (live && s.ready && s.ws.readyState === WebSocket.OPEN) s.ws.send(data.pcm);
        };
        s.ctx.createMediaStreamSource(s.stream).connect(capture);
        await s.ctx.resume();

        const ws = new WebSocket(liveUrl({ answer, talk }));
        ws.binaryType = "arraybuffer";
        s.ws = ws;
        ws.onopen = () => ws.send(JSON.stringify({ type: "history", turns: history }));
        ws.onmessage = ({ data }) => {
          if (s.closed) return;
          if (typeof data === "string") {
            handleEvent(s, JSON.parse(data));
            return;
          }
          if (s.discardAudio) return;
          beginTurn(s);
          s.gotAudio = true;
          s.thinking = false;
          const samples = pcm16ToFloat32(data);
          s.playback.port.postMessage({ type: "audio", samples }, [samples.buffer]);
          refresh(s);
        };
        ws.onclose = (event) => fail(s, closeError(event));
      } catch (e) {
        fail(s, startError(e));
      }
    },
    [fail, handleEvent, refresh, teardown]
  );

  const stop = useCallback(() => {
    const s = sessionRef.current;
    if (s?.ws?.readyState === WebSocket.OPEN) s.ws.send(JSON.stringify({ type: "end" }));
    if (s) teardown(s);
    setState("idle");
    setError(null);
    setMuted(false);
    setHeld(false);
    setCaption(EMPTY_CAPTION);
  }, [teardown]);

  // Push-to-talk: button (or Space) pressed
  const pressTalk = useCallback(() => {
    const s = sessionRef.current;
    if (!s?.ready || s.closed || s.talk !== "ptt") return;
    clearTimeout(s.releaseTimer);
    if (s.held && !s.releasing) return;
    // Pressed again during the release tail: still the same utterance, so no new activity_start
    if (!s.held) s.ws.send(JSON.stringify({ type: "activity_start" }));
    s.held = true;
    s.releasing = false;
    if (s.gotAudio && !s.turnDone) {
      // Talking over the answer: stop it now and ignore the rest of it
      s.discardAudio = true;
      s.turn.interrupted = true;
    }
    s.playback.port.postMessage({ type: "clear" });
    s.gotAudio = false;
    s.thinking = false;
    setHeld(true);
    refresh(s);
  }, [refresh]);

  // Push-to-talk: released. Keep sending for a moment so the last word isn't cut off.
  const releaseTalk = useCallback(() => {
    const s = sessionRef.current;
    if (!s?.held || s.releasing) return;
    s.releasing = true;
    setHeld(false);
    s.releaseTimer = setTimeout(() => {
      s.held = false;
      s.releasing = false;
      if (!s.closed && s.ws.readyState === WebSocket.OPEN) s.ws.send(JSON.stringify({ type: "activity_end" }));
    }, RELEASE_TAIL_MS);
  }, []);

  // Talk mode is fixed per Gemini session: reconnect with the conversation so far.
  // Called from a click, so the new AudioContext is allowed to play.
  const switchTalk = useCallback(
    (mode) => {
      const s = sessionRef.current;
      if (!s || s.talk === mode) return;
      const { answer, history } = s;
      stop();
      start({ answer, talk: mode, history });
    },
    [start, stop]
  );

  const toggleMute = useCallback(() => {
    const s = sessionRef.current;
    if (!s) return;
    s.muted = !s.muted;
    s.stream?.getAudioTracks().forEach((t) => (t.enabled = !s.muted));
    // Muting mid-sentence: let Gemini end the turn now instead of waiting for silence
    if (s.muted && s.ready && s.ws.readyState === WebSocket.OPEN) {
      s.ws.send(JSON.stringify({ type: "audio_stream_end" }));
    }
    setMuted(s.muted);
  }, []);

  // Close the session if the component goes away (e.g. hot reload)
  useEffect(() => () => sessionRef.current && teardown(sessionRef.current), [teardown]);

  return {
    state,
    error,
    muted,
    talk,
    held,
    caption,
    levelRef,
    start,
    stop,
    toggleMute,
    pressTalk,
    releaseTalk,
    switchTalk,
  };
}
