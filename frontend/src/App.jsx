import { useCallback, useEffect, useRef, useState } from "react";
import { checkHealth, ingestDocs, sendChat } from "./api.js";
import ChatMessage from "./components/ChatMessage.jsx";
import Composer from "./components/Composer.jsx";
import EmptyState from "./components/EmptyState.jsx";
import Sidebar from "./components/Sidebar.jsx";
import VoiceBar from "./components/VoiceBar.jsx";
import { MenuIcon } from "./components/icons.jsx";
import { isKnown, loadLastLanguage, saveLastLanguage } from "./language.js";
import useVoiceSession from "./voice/useVoiceSession.js";

const STORAGE_KEY = "docs-assistant-messages";
const VOICE_KEY = "docs-assistant-voice";
const HEALTH_INTERVAL_MS = 15000;

let nextId = 0;
const newId = () => `${Date.now()}-${nextId++}`;

function loadMessages() {
  try {
    return JSON.parse(localStorage.getItem(STORAGE_KEY)) || [];
  } catch {
    return [];
  }
}

// Voice settings: answer mode (agent: Gemini answers | rag: Gemini reads the RAG answer)
function loadVoiceSettings() {
  try {
    const saved = JSON.parse(localStorage.getItem(VOICE_KEY)) || {};
    return { answer: saved.answer === "rag" ? "rag" : "agent" };
  } catch {
    return { answer: "agent" };
  }
}

// History sent to the backend: skip failed replies and the question that caused them.
function toHistory(messages) {
  return messages
    .filter((m, i) => !m.error && !messages[i + 1]?.error)
    .map(({ role, content }) => ({ role, content }));
}

export default function App() {
  const [messages, setMessages] = useState(loadMessages);
  const [pending, setPending] = useState(false);
  const [backend, setBackend] = useState("checking");
  const [ingest, setIngest] = useState({ status: "idle" });
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const bottomRef = useRef(null);
  const [voiceSettings, setVoiceSettings] = useState(loadVoiceSettings);
  // The conversation's language: the last clear one detected (chat, dictation or voice). It is the hint for
  // dictation, voice and short messages, and New chat keeps it.
  const [language, setLanguage] = useState(loadLastLanguage);

  const noteLanguage = useCallback((lang) => {
    if (!isKnown(lang)) return;
    setLanguage(lang);
    saveLastLanguage(lang);
  }, []);

  // A finished voice turn becomes ordinary chat messages (marked as voice)
  const addVoiceTurn = useCallback(
    ({ user, assistant, sources, interrupted, language: lang }) => {
      const turn = [];
      const tag = isKnown(lang) ? { lang } : {};
      if (user) turn.push({ id: newId(), role: "user", content: user, voice: true, ...tag });
      if (assistant) {
        turn.push({ id: newId(), role: "assistant", content: assistant, sources, interrupted, voice: true, ...tag });
      }
      if (turn.length) setMessages((prev) => [...prev, ...turn]);
      noteLanguage(lang);
    },
    [noteLanguage]
  );

  const voice = useVoiceSession({ onTurn: addVoiceTurn });
  const { start: startVoice, stop: stopVoice, switchAnswer } = voice;
  const handsFreeActive = voice.withMic && voice.state !== "idle";

  const changeVoiceSettings = useCallback(
    (changes) => {
      const next = { ...voiceSettings, ...changes };
      setVoiceSettings(next);
      try {
        localStorage.setItem(VOICE_KEY, JSON.stringify(next));
      } catch {
        // Storage unavailable: the choice just isn't remembered
      }
      switchAnswer(next.answer, toHistory(messages), language); // reconnects a hands-free conversation
    },
    [voiceSettings, switchAnswer, messages, language]
  );

  useEffect(() => {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(messages));
    } catch {
      // Storage unavailable (private mode etc.): chat still works, just isn't kept
    }
  }, [messages]);

  useEffect(() => {
    let active = true;
    const check = () =>
      checkHealth()
        .then(() => active && setBackend("ok"))
        .catch(() => active && setBackend("down"));
    check();
    const timer = setInterval(check, HEALTH_INTERVAL_MS);
    return () => {
      active = false;
      clearInterval(timer);
    };
  }, []);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages, pending, voice.caption]);

  const ask = useCallback(
    async (question, { dictated = false, language: dictatedLanguage = null } = {}) => {
      const history = toHistory(messages);
      if (dictated) {
        // Dictated question: Gemini answers out loud (in the chosen answer mode); the answer
        // arrives as a voice turn through addVoiceTurn
        const lang = isKnown(dictatedLanguage) ? dictatedLanguage : language;
        setMessages((prev) => [
          ...prev,
          { id: newId(), role: "user", content: question, voice: true, ...(isKnown(lang) ? { lang } : {}) },
        ]);
        startVoice({ answer: voiceSettings.answer, history, mic: false, text: question, lang });
        return;
      }
      const questionId = newId();
      setMessages((prev) => [...prev, { id: questionId, role: "user", content: question }]);
      setPending(true);
      try {
        const { answer, sources, language: detected } = await sendChat(question, history, language);
        const tag = isKnown(detected) ? { lang: detected } : {};
        setMessages((prev) => [
          ...prev.map((m) => (m.id === questionId ? { ...m, ...tag } : m)),
          { id: newId(), role: "assistant", content: answer, sources, ...tag },
        ]);
        noteLanguage(detected);
      } catch (e) {
        setMessages((prev) => [
          ...prev,
          { id: newId(), role: "assistant", content: e.message, error: true },
        ]);
      } finally {
        setPending(false);
      }
    },
    [messages, startVoice, voiceSettings.answer, language, noteLanguage]
  );

  const reingest = useCallback(async () => {
    setIngest({ status: "running" });
    try {
      setIngest({ status: "done", result: await ingestDocs() });
    } catch (e) {
      setIngest({ status: "error", message: e.message });
    }
  }, []);

  const clearChat = useCallback(() => {
    stopVoice(); // a running voice session would still remember the old conversation
    setMessages([]);
    setSidebarOpen(false);
  }, [stopVoice]);

  const { caption } = voice;
  const hasLiveTurn = Boolean(caption.user || caption.assistant);

  return (
    <div className="app">
      <Sidebar
        open={sidebarOpen}
        onClose={() => setSidebarOpen(false)}
        backend={backend}
        ingest={ingest}
        onIngest={reingest}
        onClear={clearChat}
        canClear={messages.length > 0 && !pending}
        voiceSettings={voiceSettings}
        onVoiceSettings={changeVoiceSettings}
      />

      <main className="main">
        <header className="topbar">
          <button className="icon-btn" onClick={() => setSidebarOpen(true)} aria-label="Open menu">
            <MenuIcon />
          </button>
          <span className="topbar-title">Docs Assistant</span>
        </header>

        <div className="messages">
          <div className="messages-inner">
            {messages.length === 0 && !hasLiveTurn ? (
              <EmptyState onPick={ask} disabled={pending} />
            ) : (
              messages.map((m) => <ChatMessage key={m.id} message={m} />)
            )}
            {/* The voice turn in progress, streamed live until it completes */}
            {caption.user && <ChatMessage message={{ role: "user", content: caption.user, voice: true, live: true }} />}
            {caption.assistant && (
              <ChatMessage
                message={{ role: "assistant", content: caption.assistant, voice: true, live: true, lang: language }}
              />
            )}
            {pending && <ChatMessage message={{ role: "assistant", typing: true }} />}
            <div ref={bottomRef} />
          </div>
        </div>

        <VoiceBar voice={voice} />
        <Composer
          onSend={ask}
          disabled={pending}
          onHandsFree={() =>
            startVoice({ answer: voiceSettings.answer, history: toHistory(messages), mic: true, lang: language })
          }
          onDictateStart={() => !voice.withMic && stopVoice()}
          handsFreeActive={handsFreeActive}
          language={language}
          onLanguage={noteLanguage}
        />
      </main>
    </div>
  );
}
