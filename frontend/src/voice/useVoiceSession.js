import { useCallback, useEffect, useRef, useState } from "react";
import { tidyTranscript } from "../language.js";
import { micError, micSupported, openMic } from "./mic.js";
import playbackUrl from "./playback-worklet.js?url";

// Same origin by default (Vite proxy in dev, FastAPI in prod), like api.js
const BASE = (import.meta.env.VITE_BACKEND_URL || "").replace(/\/$/, "");
const EMPTY_CAPTION = { user: "", assistant: "" };
const AUTO_CLOSE_DELAY_MS = 500; // spoken replies: close this long after playback ends

function liveUrl(params) {
  const origin = BASE
    ? BASE.replace(/^http/, "ws")
    : `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}`;
  const query = new URLSearchParams(Object.entries(params).filter(([, v]) => v)).toString();
  return `${origin}/live${query ? `?${query}` : ""}`;
}

function closeError(event) {
  if (event.reason) return event.reason;
  return event.code === 1006 ? "Can't reach the backend. Is it running?" : "The voice connection closed.";
}

const newTurn = () => ({ user: "", assistant: "", ragAnswer: "", sources: [], interrupted: false, language: null });

// Marks the first event of a new turn (clears any caption left from an unfinished one)
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
 * A Gemini Live session over the backend's /live WebSocket (see voice_plan.md). Two kinds:
 *  - hands-free (mic: true): a spoken conversation; Gemini detects pauses and the user can talk over answers
 *  - spoken reply (mic: false, text): speaks the answer to one dictated question, then closes itself
 *
 * state: idle | connecting | listening | thinking | speaking | reconnecting | error
 * (reconnecting: the backend is replacing a dropped or expiring Gemini connection)
 * levelRef: current mic loudness (0..1), read by the level meter without re-rendering
 * caption: live transcript of the turn in progress; cleared once the turn completes
 * onTurn({user, assistant, sources, interrupted, language}) is called when the model finishes a turn
 * (in relay mode, assistant is the RAG answer text; language is the turn's detected language, if any).
 */
export default function useVoiceSession({ onTurn } = {}) {
  const [state, setState] = useState("idle");
  const [error, setError] = useState(null);
  const [muted, setMuted] = useState(false);
  const [withMic, setWithMic] = useState(false);
  const [caption, setCaption] = useState(EMPTY_CAPTION);
  const levelRef = useRef(0);
  const sessionRef = useRef(null);
  const onTurnRef = useRef(onTurn);
  onTurnRef.current = onTurn;

  const teardown = useCallback((s) => {
    s.closed = true;
    clearTimeout(s.closeTimer);
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

  const finish = useCallback(
    (s) => {
      if (s.closed) return;
      if (s.ws?.readyState === WebSocket.OPEN) s.ws.send(JSON.stringify({ type: "end" }));
      teardown(s);
      setState("idle");
      setCaption(EMPTY_CAPTION);
    },
    [teardown]
  );

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
  const refresh = useCallback(
    (s) => {
      if (s.closed || !s.ready) return;
      const speaking = s.playing || (s.gotAudio && !s.turnDone);
      setState(s.reconnecting ? "reconnecting" : speaking ? "speaking" : s.thinking ? "thinking" : "listening");
      // A spoken reply closes itself once the answer has been played
      clearTimeout(s.closeTimer);
      if (!s.mic && !s.awaitingReply && !speaking && !s.reconnecting) {
        s.closeTimer = setTimeout(() => !s.playing && finish(s), AUTO_CLOSE_DELAY_MS);
      }
    },
    [finish]
  );

  const handleEvent = useCallback(
    (s, event) => {
      switch (event.type) {
        case "ready":
          s.ready = true;
          if (s.text) s.ws.send(JSON.stringify({ type: "text", text: s.text }));
          refresh(s);
          break;
        case "reconnecting":
          // The backend is opening a new Gemini connection and will ask an unanswered question again,
          // so the partial answer is dropped; the user's words stay
          s.reconnecting = true;
          s.playback.port.postMessage({ type: "clear" });
          s.turn = { ...newTurn(), user: s.turn.user, language: s.turn.language };
          s.caption = { ...s.caption, assistant: "" };
          setCaption({ ...s.caption });
          s.thinking = false;
          s.gotAudio = false;
          refresh(s);
          break;
        case "resumed":
          s.reconnecting = false;
          refresh(s);
          break;
        case "input_transcript":
          beginTurn(s);
          s.turn.user += event.text;
          s.caption.user = tidyTranscript(s.caption.user + event.text);
          setCaption({ ...s.caption });
          break;
        case "output_transcript":
          beginTurn(s);
          s.turn.assistant += event.text;
          s.caption.assistant = tidyTranscript(s.caption.assistant + event.text);
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
        case "language":
          s.turn.language = event.language;
          break;
        case "rag_answer":
          s.turn.ragAnswer = event.text;
          break;
        case "interrupted":
          // The user talked over the answer: drop the queued audio and listen again
          s.playback.port.postMessage({ type: "clear" });
          s.turn.interrupted = true;
          s.gotAudio = false;
          refresh(s);
          break;
        case "turn_complete": {
          const { interrupted, ragAnswer, sources, language } = s.turn;
          const user = tidyTranscript(s.turn.user).trim();
          // Relay mode: keep the RAG text, unless the user cut it off (then keep what was actually said)
          const assistant = tidyTranscript((!interrupted && ragAnswer) || s.turn.assistant).trim();
          onTurnRef.current?.({ user, assistant, sources, interrupted, language });
          // The finished turn now lives in the chat, so the live caption can go
          s.turn = newTurn();
          s.caption = { ...EMPTY_CAPTION };
          setCaption(EMPTY_CAPTION);
          s.turnDone = true;
          s.thinking = false;
          s.gotAudio = false;
          s.awaitingReply = false;
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

  /**
   * answer: agent | rag; history: conversation so far as [{role, content}]
   * mic: true for hands-free; false with text for a spoken reply to that text
   * lang: the conversation's language, if known (a hint for Gemini)
   */
  const start = useCallback(
    async ({ answer, history = [], mic = true, text = "", lang = null } = {}) => {
      if (sessionRef.current) teardown(sessionRef.current); // one session at a time
      setError(null);
      setMuted(false);
      setWithMic(mic);
      setCaption(EMPTY_CAPTION);
      setState("connecting");

      if (!window.AudioWorkletNode || (mic && !micSupported())) {
        setError("Voice chat needs a secure (https) page in a browser with microphone support.");
        setState("error");
        return;
      }

      // Created before any await, while we're still inside the click, so the browser allows audio
      const s = {
        ctx: new AudioContext(),
        answer,
        history,
        lang,
        mic,
        text,
        awaitingReply: Boolean(text),
        ready: false,
        reconnecting: false,
        muted: false,
        playing: false,
        thinking: false,
        gotAudio: false,
        turnDone: true,
        closed: false,
        closeTimer: null,
        caption: { ...EMPTY_CAPTION },
        turn: newTurn(),
      };
      sessionRef.current = s;

      try {
        await s.ctx.audioWorklet.addModule(playbackUrl);
        if (s.closed) return;
        s.playback = new AudioWorkletNode(s.ctx, "playback-processor", {
          numberOfInputs: 0,
          outputChannelCount: [1],
        });
        s.playback.port.onmessage = ({ data }) => {
          s.playing = data.playing;
          refresh(s);
        };
        s.playback.connect(s.ctx.destination);

        if (mic) {
          s.stream = await openMic(s.ctx, ({ pcm, level }) => {
            const live = !s.muted;
            levelRef.current = live ? level : 0;
            if (live && s.ready && s.ws.readyState === WebSocket.OPEN) s.ws.send(pcm);
          });
          // Ended while the permission prompt was open: release the mic we just got
          if (s.closed) return teardown(s);
        }
        await s.ctx.resume();

        const ws = new WebSocket(liveUrl({ answer, talk: "vad", lang }));
        ws.binaryType = "arraybuffer";
        s.ws = ws;
        ws.onopen = () => ws.send(JSON.stringify({ type: "history", turns: history }));
        ws.onmessage = ({ data }) => {
          if (s.closed) return;
          if (typeof data === "string") {
            handleEvent(s, JSON.parse(data));
            return;
          }
          beginTurn(s);
          s.gotAudio = true;
          s.thinking = false;
          const samples = pcm16ToFloat32(data);
          s.playback.port.postMessage({ type: "audio", samples }, [samples.buffer]);
          refresh(s);
        };
        ws.onclose = (event) => fail(s, closeError(event));
      } catch (e) {
        fail(s, mic ? micError(e) : `Couldn't start the spoken answer: ${e.message || e.name}`);
      }
    },
    [fail, handleEvent, refresh, teardown]
  );

  const stop = useCallback(() => {
    const s = sessionRef.current;
    if (s) finish(s);
    setState("idle");
    setError(null);
    setMuted(false);
    setCaption(EMPTY_CAPTION);
  }, [finish]);

  // The answer mode is fixed per Gemini session: a hands-free session reconnects with the
  // conversation so far. Called from a click, so the new AudioContext is allowed to play.
  const switchAnswer = useCallback(
    (answer, history, lang) => {
      const s = sessionRef.current;
      if (!s?.mic || s.answer === answer) return;
      start({ answer, history, mic: true, lang });
    },
    [start]
  );

  const toggleMute = useCallback(() => {
    const s = sessionRef.current;
    if (!s?.mic) return;
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

  return { state, error, muted, withMic, caption, levelRef, start, stop, toggleMute, switchAnswer };
}
