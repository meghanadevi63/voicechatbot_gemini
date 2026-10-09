import { useEffect, useRef } from "react";
import { CloseIcon, MicIcon, MicOffIcon } from "./icons.jsx";

const LABELS = {
  connecting: "Connecting…",
  listening: "Listening…",
  thinking: "Searching the documents…",
  speaking: "Speaking…",
  error: "Voice chat stopped",
};

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

export default function VoiceBar({ voice }) {
  const { state, error, muted, caption, levelRef, stop, toggleMute } = voice;
  if (state === "idle") return null;

  const label = state === "listening" && muted ? "Muted" : LABELS[state];

  return (
    <div className="voice-wrap">
      <div className={`voice-bar voice-${state}`}>
        <div className="voice-row">
          <LevelMeter levelRef={levelRef} active={state === "listening" && !muted} />
          <span className="voice-label" role="status">
            {label}
          </span>
          <div className="voice-actions">
            {state !== "error" && (
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
