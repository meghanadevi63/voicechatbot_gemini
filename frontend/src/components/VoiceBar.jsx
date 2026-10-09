import { useEffect, useRef } from "react";
import { CloseIcon, MicIcon, MicOffIcon } from "./icons.jsx";

const LABELS = {
  connecting: "Connecting…",
  listening: "Listening…",
  thinking: "Searching the documents…",
  speaking: "Speaking…",
  error: "Voice chat stopped",
};

const TALK_MODES = [
  { value: "vad", label: "Hands-free" },
  { value: "ptt", label: "Push to talk" },
];

// Bar heights follow the mic level; animated straight from the ref so it doesn't re-render the app
const BAR_SHAPE = [0.55, 0.85, 1, 0.85, 0.55];

function LevelMeter({ levelRef, active }) {
  const bars = useRef([]);

  useEffect(() => {
    let frame;
    let smooth = 0;
    const tick = () => {
      const target = active ? Math.min(1, Math.sqrt(levelRef.current) * 2.5) : 0;
      smooth += (target - smooth) * 0.3;
      bars.current.forEach((bar, i) => {
        if (bar) bar.style.transform = `scaleY(${0.15 + 0.85 * smooth * BAR_SHAPE[i]})`;
      });
      frame = requestAnimationFrame(tick);
    };
    tick();
    return () => cancelAnimationFrame(frame);
  }, [levelRef, active]);

  return (
    <div className="level-meter" aria-hidden="true">
      {BAR_SHAPE.map((_, i) => (
        <span key={i} ref={(el) => (bars.current[i] = el)} />
      ))}
    </div>
  );
}

function isTyping(target) {
  return target.closest?.("textarea, input, select, [contenteditable='true']");
}

// Holding Space talks in push-to-talk mode, unless the user is typing somewhere
function useSpaceToTalk(enabled, press, release) {
  useEffect(() => {
    if (!enabled) return;
    const down = (e) => {
      if (e.code !== "Space" || isTyping(e.target)) return;
      e.preventDefault(); // don't scroll or click the focused button
      if (!e.repeat) press();
    };
    const up = (e) => {
      if (e.code !== "Space" || isTyping(e.target)) return;
      e.preventDefault();
      release();
    };
    window.addEventListener("keydown", down);
    window.addEventListener("keyup", up);
    window.addEventListener("blur", release); // don't get stuck talking when the window loses focus
    return () => {
      window.removeEventListener("keydown", down);
      window.removeEventListener("keyup", up);
      window.removeEventListener("blur", release);
    };
  }, [enabled, press, release]);
}

function HoldToTalk({ held, disabled, onPress, onRelease }) {
  return (
    <button
      className={`hold-btn ${held ? "held" : ""}`}
      disabled={disabled}
      onPointerDown={(e) => {
        if (e.button !== 0) return;
        e.currentTarget.setPointerCapture(e.pointerId); // keep getting pointerup if the finger slides off
        onPress();
      }}
      onPointerUp={onRelease}
      onPointerCancel={onRelease}
      onContextMenu={(e) => e.preventDefault()} // long-press menu on touch screens
      aria-pressed={held}
    >
      <MicIcon />
      {held ? "Release to send" : "Hold to talk"}
      <span className="hold-key">or hold Space</span>
    </button>
  );
}

export default function VoiceBar({ voice, onTalkChange }) {
  const { state, error, muted, talk, held, caption, levelRef, stop, toggleMute, pressTalk, releaseTalk } = voice;
  const ptt = talk === "ptt";
  const active = state !== "idle" && state !== "error";
  useSpaceToTalk(active && ptt, pressTalk, releaseTalk);
  if (state === "idle") return null;

  let label = LABELS[state];
  if (state === "listening" && ptt) label = held ? "Listening… release to send" : "Ready";
  else if (state === "listening" && muted) label = "Muted";

  return (
    <div className="voice-wrap">
      <div className={`voice-bar voice-${state}`}>
        <div className="voice-row">
          <LevelMeter levelRef={levelRef} active={state === "listening" && (ptt ? held : !muted)} />
          <span className="voice-label" role="status">
            {label}
          </span>
          <div className="voice-actions">
            {active && (
              <div className="segmented" role="group" aria-label="Talk mode">
                {TALK_MODES.map((m) => (
                  <button
                    key={m.value}
                    className={talk === m.value ? "on" : ""}
                    aria-pressed={talk === m.value}
                    onClick={() => onTalkChange(m.value)}
                  >
                    {m.label}
                  </button>
                ))}
              </div>
            )}
            {active && !ptt && (
              <button className="btn voice-btn" onClick={toggleMute} disabled={state === "connecting"}>
                {muted ? <MicIcon /> : <MicOffIcon />}
                {muted ? "Unmute" : "Mute"}
              </button>
            )}
            <button className="btn voice-btn" onClick={stop}>
              <CloseIcon />
              {state === "error" ? "Close" : "End"}
            </button>
          </div>
        </div>
        {active && ptt && (
          <HoldToTalk held={held} disabled={state === "connecting"} onPress={pressTalk} onRelease={releaseTalk} />
        )}
        {error && <p className="voice-message">{error}</p>}
        {(caption.user || caption.assistant) && (
          <div className="voice-caption" aria-live="polite">
            {caption.user && (
              <p>
                <span>You</span>
                {caption.user}
              </p>
            )}
            {caption.assistant && (
              <p>
                <span>Assistant</span>
                {caption.assistant}
              </p>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
