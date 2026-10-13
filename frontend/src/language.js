// Conversation language (BCP-47 codes like "te" or "pt-BR"). The backend detects it from each message;
// the last clear one is the hint for dictation, voice and short replies, and is kept across chats.

const LAST_LANGUAGE_KEY = "docs-assistant-last-language";

export const isKnown = (lang) => Boolean(lang) && !["und", "auto"].includes(lang.toLowerCase());

export const isEnglish = (lang) => isKnown(lang) && lang.toLowerCase().split("-")[0] === "en";

export function loadLastLanguage() {
  try {
    const saved = localStorage.getItem(LAST_LANGUAGE_KEY);
    return isKnown(saved) ? saved : null;
  } catch {
    return null;
  }
}

export function saveLastLanguage(lang) {
  try {
    localStorage.setItem(LAST_LANGUAGE_KEY, lang);
  } catch {
    // Storage unavailable: the next chat just starts without a hint
  }
}

// Gemini Live puts spaces between Chinese and Japanese characters in its transcripts
const CJK = "\\p{Script=Han}\\p{Script=Hiragana}\\p{Script=Katakana}\\u3000-\\u303f\\uff00-\\uffef";
const CJK_GAP = new RegExp(`(?<=[${CJK}])\\s+(?=[${CJK}])`, "gu");

export const tidyTranscript = (text) => text.replace(CJK_GAP, "");
