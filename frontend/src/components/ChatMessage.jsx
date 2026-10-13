import Sources from "./Sources.jsx";
import { isEnglish } from "../language.js";
import { MicIcon } from "./icons.jsx";

// Small marker on messages that were spoken rather than typed
const VoiceTag = ({ live, interrupted }) => (
  <span className="voice-tag">
    <MicIcon />
    {live ? "Live" : interrupted ? "Voice · interrupted" : "Voice"}
  </span>
);

export default function ChatMessage({ message }) {
  const { role, content, sources, error, typing, voice, live, interrupted, lang } = message;
  const classes = ["msg", `msg-${role}`, live && "msg-live", error && "msg-error"].filter(Boolean).join(" ");

  if (role === "user") {
    return (
      <div className={classes}>
        <div className="bubble" lang={lang} dir="auto">
          {content}
        </div>
        {voice && <VoiceTag live={live} />}
      </div>
    );
  }

  return (
    <div className={classes}>
      {typing ? (
        <div className="typing" aria-label="Assistant is thinking">
          <span />
          <span />
          <span />
        </div>
      ) : (
        <>
          <div className="answer" lang={error ? undefined : lang} dir="auto">
            {error ? `Something went wrong: ${content}` : content}
            {interrupted && "…"}
          </div>
          {voice && <VoiceTag live={live} interrupted={interrupted} />}
          {sources?.length > 0 && <Sources sources={sources} translated={Boolean(lang) && !isEnglish(lang)} />}
        </>
      )}
    </div>
  );
}
