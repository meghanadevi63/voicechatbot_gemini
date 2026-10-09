import { useCallback, useEffect, useRef, useState } from "react";
import useDictation from "../voice/useDictation.js";
import { MicIcon, SendIcon, StopIcon, WaveIcon } from "./icons.jsx";
import { LevelMeter } from "./VoiceBar.jsx";

const MAX_HEIGHT = 200;

/**
 * onSend(text, {dictated}): dictated is true when some of the text came from the mic
 * (the answer is then spoken as well). onHandsFree starts a hands-free conversation;
 * onDictateStart is called when dictation begins (e.g. to stop a spoken answer). Both mic buttons
 * are off during a hands-free conversation.
 */
export default function Composer({ onSend, disabled, onHandsFree, onDictateStart, handsFreeActive }) {
  const [text, setTextState] = useState("");
  const textRef = useRef(""); // latest text, readable right after awaiting dictation
  const dictatedRef = useRef(false);
  const [sending, setSending] = useState(false);
  const ref = useRef(null);

  const setText = useCallback((value) => {
    textRef.current = value;
    setTextState(value);
  }, []);

  // Each transcribed pause is appended to whatever is in the box (typed or dictated)
  const appendTranscript = useCallback(
    (words) => {
      const current = textRef.current;
      setText(current && !/\s$/.test(current) ? `${current} ${words}` : current + words);
      dictatedRef.current = true;
    },
    [setText]
  );
  const dictation = useDictation({ onText: appendTranscript });
  const dictating = dictation.recording || dictation.transcribing > 0;

  // Grow the textarea with its content, up to MAX_HEIGHT
  useEffect(() => {
    const el = ref.current;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, MAX_HEIGHT)}px`;
  }, [text]);

  useEffect(() => {
    if (!disabled) ref.current?.focus();
  }, [disabled]);

  const submit = async () => {
    if (disabled || sending) return;
    if (dictating) {
      // Sending while dictating: stop and wait for the last words to arrive
      setSending(true);
      await dictation.stop();
      setSending(false);
    }
    const question = textRef.current.trim();
    if (!question) return;
    onSend(question, { dictated: dictatedRef.current });
    setText("");
    dictatedRef.current = false;
  };

  const toggleDictation = () => {
    if (dictation.recording) {
      dictation.stop();
    } else {
      onDictateStart?.();
      dictation.start();
    }
  };

  let status = null;
  if (dictation.recording) status = "Listening… pause and your words appear in the box";
  else if (dictation.transcribing) status = "Transcribing…";

  return (
    <div className="composer-wrap">
      {(status || dictation.error) && (
        <div className={`dictation-status ${dictation.error && !status ? "bad" : ""}`} role="status">
          {dictation.recording && <LevelMeter levelRef={dictation.levelRef} active />}
          <span>{status || dictation.error}</span>
        </div>
      )}
      <form
        className={`composer ${dictation.recording ? "recording" : ""}`}
        onSubmit={(e) => {
          e.preventDefault();
          submit();
        }}
      >
        <textarea
          ref={ref}
          rows={1}
          value={text}
          placeholder={dictation.recording ? "Listening…" : "Ask a question about your documents"}
          onChange={(e) => {
            setText(e.target.value);
            if (!e.target.value) dictatedRef.current = false;
          }}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
              e.preventDefault();
              submit();
            }
          }}
        />
        <button
          className={`icon-btn mic-btn ${dictation.recording ? "on" : ""}`}
          type="button"
          onClick={toggleDictation}
          disabled={handsFreeActive}
          aria-label={dictation.recording ? "Stop dictation" : "Dictate"}
          title={dictation.recording ? "Stop dictation" : "Dictate: speak, and your words appear in the box"}
        >
          {dictation.recording ? <StopIcon /> : <MicIcon />}
        </button>
        <button
          className="icon-btn mic-btn"
          type="button"
          onClick={onHandsFree}
          disabled={handsFreeActive || dictating}
          aria-label="Start hands-free voice chat"
          title="Hands-free voice chat: talk and hear the answers"
        >
          <WaveIcon />
        </button>
        <button
          className="send-btn"
          type="submit"
          disabled={disabled || sending || (!text.trim() && !dictating)}
          aria-label="Send"
        >
          <SendIcon />
        </button>
      </form>
      <p className="hint">
        Enter to send, Shift+Enter for a new line. Mic to dictate (answers are also spoken), waveform for a hands-free
        voice chat.
      </p>
    </div>
  );
}
