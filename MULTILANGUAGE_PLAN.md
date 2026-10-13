# Multilingual chat plan

Goal: users can type or speak in their own language, and everything they read in the chat (their question and
the answer) is in that language. The documents stay in English and are not re-indexed.

Scope: **every language the voice model supports**. Gemini Live 3.1 Flash covers 99 languages, detects the
spoken language itself, and can't be given a language code. The app detects the language and adapts. It
doesn't keep a fixed list. Not every language can be tested by hand, so languages are grouped into
[coverage tiers](#language-coverage-tiers).

Precheck results (M0 and M0b) are in [MULTILANGUAGE_PRECHECK_RESULTS.md](MULTILANGUAGE_PRECHECK_RESULTS.md).

---

## Contents

1. [Design](#design)
2. [Language coverage tiers](#language-coverage-tiers)
3. [Phases](#phases)
4. [Testing](#testing)
5. [Risks](#risks)
6. [Open decisions](#open-decisions)

---

## Design

```
User types or speaks Telugu ──▶ shown in the chat in Telugu
            │
            ▼
An LLM writes the search query in ENGLISH ──▶ Milvus (English documents, MiniLM, unchanged)
            │
            ▼
English excerpts ──▶ the LLM answers IN TELUGU ──▶ shown, and spoken, in Telugu
```

- **Search stays in English.** In every path an LLM already writes the search query: the Groq router for
  typed chat and relay mode, and Gemini's `search_documents` call in agent mode. A prompt rule makes that query
  English, so there is no extra call and no re-indexing.
- **The answer follows the user's language.** The answer prompts say: reply in the language of the user's
  latest message, in its usual script. If the message is too short to tell, use the language of the
  conversation so far.
- **Language detection is automatic, and the conversation remembers it.**
  - The router already reads every typed message. It also returns the message's language as a BCP-47 code,
    e.g. `te` or `pt-BR`.
  - Voice turns are detected from their transcript.
  - The conversation's current language is the language of the last turn whose language was clear. It is used
    as the dictation hint, as the reply language for short messages like "ok", and for the chat display
    (`lang`, and right-to-left layout for Arabic, Urdu, Persian or Hebrew).
  - **The last language is remembered across chats**, so a new conversation starts with a hint too. Only a
    user's very first dictated message has no hint. If that message is misdetected, the user can correct it in
    the message box before sending.
- **No language picker for now.** Users never choose a language. A sidebar picker is held back as a fallback,
  and is added only if M5 shows that detection without a hint fails too often (see
  [Language picker: only if needed](#language-picker-only-if-needed)).
- **Sources stay in English**, because they quote the documents.

---

## Language coverage tiers

Every supported language works through the same code. The tiers only decide how much testing each language
gets.

| Tier | Languages | Testing |
|---|---|---|
| **1: core** | The languages your users mostly speak (to be named, e.g. English, Hindi, Telugu) | Real recordings from native speakers. A native speaker rates the answers. All paths are tested. |
| **2: broad check** | One or two languages from each major script and region: Spanish, French, German, Portuguese, Russian, Arabic (right-to-left), Urdu (right-to-left), Bengali, Tamil, Kannada, Marathi, Gujarati, Chinese, Japanese, Korean, Thai, Indonesian, Vietnamese, Turkish, Swahili | Automated: synthetic speech, a language-ID check on every output, and retrieval overlap with the English question |
| **3: best effort** | The rest of the voice model's languages | Covered by the design. Fixed if someone reports a problem. |

---

## Phases

This file is updated to mark progress. Results go in
[MULTILANGUAGE_PRECHECK_RESULTS.md](MULTILANGUAGE_PRECHECK_RESULTS.md) or a results file for that phase.

**Testing is deferred** (decided 2026-10-13). The **Test** items below are run only when a test is mandatory,
after asking first, and kept small, so they don't use up the API quotas the running app needs.

### Phase M0: Prechecks with Hindi and Telugu ✅
- [x] Precheck script covering typed chat, speech generation, dictation and hands-free
- [x] Results recorded in [MULTILANGUAGE_PRECHECK_RESULTS.md](MULTILANGUAGE_PRECHECK_RESULTS.md#m0-hindi-and-telugu)

### Phase M0b: Broad precheck across Tier 2 languages ✅ (2026-10-13)
- [x] Translate the test questions, the small talk and the not-in-documents question into the 20 Tier 2
  languages
- [x] Language-ID check with `lingua`, plus a script check, and a manual review of the failures
- [x] Text check for both answer writers: query language, page overlap, answer language, tokens
- [x] Speech, transcribe and live checks for Arabic, Chinese, Japanese, Tamil, Spanish and Russian
- [x] **Outcome:**
  - the approach works across all 20 languages
  - Gemini Flash Lite is the recommended answer writer
  - the languages that need special handling are listed in the
    [results](MULTILANGUAGE_PRECHECK_RESULTS.md#languages-that-need-special-handling)
  - the follow-up items are added to M1 to M3 below

### Phase M1: Backend, typed chat (built, test pending)
- [x] Router (`Route` in `rag.py`):
  - new field `language` (BCP-47 code of the latest message, or `und` when it's too short to tell)
  - `search_query` always in English
  - `reply` in the user's language
- [x] Answer prompt:
  - reply in the language of the latest message, in its usual script
  - when the message's language is `und`, use the conversation's language
  - the not-found message follows the same rule, described rather than quoted. In M0b, an English example
    sentence was copied word for word in 7 of 80 answers.
  - no English glosses in parentheses
  - with no language known, the instruction is neutral. The first wording answered English questions in
    Spanish or Russian. That was fixed and rechecked: 21/21 answers came back in the question's own language.
- [x] Book-specific terms: the router may add an `alt_query`, a second English wording for an ambiguous or
  well-known term. Both queries are searched, and the best matches are merged by score.
- [x] `POST /chat`:
  - accepts an optional `language` (the conversation's language)
  - returns the detected `language`, so the frontend can track the conversation language
- [x] Answer writer setting: `ANSWER_PROVIDER=groq|gemini` and `ANSWER_MODEL`. The default is Gemini Flash
  Lite, as decided on 2026-10-13. Groq is the fallback if Gemini fails.
- [x] Log each question's language, its English query and its alternative query (`main.py` now configures
  app logging at INFO)
- [ ] **Test** ([scripts/multilang_chat_test.py](scripts/multilang_chat_test.py)):
  - the M0b text check against the real `/chat`
  - targets: answer in the right language for at least 95% of Tier 2 questions, page overlap at least 0.8
  - English answers unchanged
  - blocked by API quotas: the first run used up Groq's daily token limit partway through (see Risks)

### Phase M2: Language tracking and dictation
- [ ] Frontend conversation language:
  - updated from `/chat`'s `language`, and from the language of voice turns (M3), when it's clear
  - saved with the chat in `localStorage`
  - also saved as the **last used language** (`docs-assistant-last-language`), which New chat keeps, so the
    next conversation starts with a hint
- [ ] `POST /transcribe?lang=<code>`:
  - the prompt keeps the spoken language and script, and never translates
  - adds "The speaker is speaking <language>" when a language is known (the conversation language, or else the
    last used language)
- [ ] First dictation with no language known at all (the user's very first message):
  - transcribe without a hint, then detect the language
  - if the text looks romanized or mixed-script, re-transcribe once with the detected language as the hint
  - log these cases, so M5 can measure how often they go wrong
- [ ] Transcription accuracy: in M0b, Flash Lite misheard Tamil and Chinese words with or without a hint, but
  Gemini Live heard the same Tamil audio correctly. Compare a stronger transcription model, or more thinking,
  on those clips.
- [ ] **Test:**
  - the transcribe check against `/transcribe` for Tier 1 and the M0b subset
  - a headless browser run of dictation in two Tier 1 languages, including a New chat that keeps the hint
  - target: the right language and script for at least 90% of clips with a hint

### Phase M3: Voice (Gemini Live)
- [ ] `live.py` agent prompt:
  - always call `search_documents` with an English query
  - answer in the language the user is speaking, in its usual script
- [ ] `/live?lang=<code>`:
  - when a language is known, the system prompt adds "The user usually speaks <language>; answer in the
    language they speak"
  - relay mode passes it to `RAGChain.answer()`
- [ ] Detect each finished voice turn's language from its transcript and send a `language` event, so the
  frontend updates the conversation language
- [ ] Fix `similarity()` to match words in any script (`\w+`)
- [ ] Remove the spaces Live puts between Chinese and Japanese characters in transcripts, before showing or
  saving them (M0b)
- [ ] Guard in `search_documents`: if Gemini sends a non-English query, translate it before searching. In M0b,
  one Tamil query wasn't translated, and retrieval failed.
- [ ] Hands-free question text (behind the `LIVE_RETRANSCRIBE` flag, depends on Open decision 3):
  - the model sometimes returns the user's words romanized
  - the backend keeps each user turn's audio and re-transcribes it with the language hint
  - the chat replaces the live transcript when the turn completes
  - costs about 2 s and one extra call per turn
- [ ] Voice check: the `Kore` voice in each Tier 1 language, judged by a native speaker. The voice could later
  be chosen per language if one sounds clearly better.
- [ ] **Test:**
  - `live_ws_test.py` with `--lang` and the per-language WAVs, in agent and relay modes, with and without
    history
  - targets: English query 100%, answer in the right language at least 95%

### Phase M4: Chat display
- [ ] Set `lang` on each message from its detected language, so the browser picks fonts and line breaking
  correctly
- [ ] Set `dir="auto"` on message text, so Arabic, Urdu, Persian and Hebrew lay out right to left. Mixed
  messages also need checking: English sources under an Arabic answer.
- [ ] Fonts:
  - fall back to system fonts first
  - add the Noto family (Devanagari, Telugu, Tamil, Bengali, Arabic, CJK…) through Google Fonts with
    `unicode-range`, so only the needed fonts load
  - check line height for Indic and Thai scripts
- [ ] Label the sources list "Sources (English)" when the answer isn't in English
- [ ] **Test:** screenshots in Hindi, Telugu, Arabic, Japanese and Thai, in light and dark themes, at phone
  width

### Phase M5: Evaluation and model choice
- [ ] Tier 1:
  - record **real** questions from native speakers: 8 per language, including code-mixed speech, a noisy
    room and a fast speaker
  - a native speaker rates each answer for correctness and fluency, 1 to 5
- [ ] Tier 2: run the automated M0b suite through the real endpoints and fill in the table
- [ ] Decide on hands-free re-transcription based on the real recordings
- [ ] **Measure first messages without a hint:** dictate each Tier 1 recording with no hint, as a brand-new
  user would. If fewer than 9 in 10 come out in the right language and script after the retry, add the
  language picker (below).
- [ ] Add the suite to the repo, so a model change (e.g. `gemini-3.8-live`) can be re-checked in one command

| Metric | Tier 1 (per language) | Tier 2 (aggregate) |
|---|---|---|
| Right pages retrieved (overlap with the English question) | | |
| Answer in the right language | | |
| Answer correctness, 1 to 5 | | – |
| Dictation word accuracy | | |
| Hands-free question shown in the right script | | |
| End of speech to first audio (median) | | |

### Language picker: only if needed
Not built unless M5 shows that first messages without a hint fail too often. If it is needed:
- [ ] Sidebar **Language** setting:
  - "Auto" (the default), plus every supported language
  - names in English with the native name beside them ("Telugu – తెలుగు"), searchable
- [ ] When set, it replaces the detected language as the hint for dictation and voice, and as the reply
  language
- [ ] Estimated effort: about half a day. Nothing else in the design depends on it.

### Interface labels: not planned
Decided on 2026-10-13: buttons, statuses, the sidebar and error messages stay **in English**. Only the chat
content follows the user's language: their question and the answer.

### Phase M6: Docs
- [ ] Update README.md, VOICE_FEATURES.md and voice_plan.md: language detection and the remembered language, new parameters
  and events, the tiers, and known limits

---

## Testing

| Tool | Use |
|---|---|
| `python -m scripts.multilang_precheck` | M0 direct model checks, Hindi and Telugu |
| `PYTHONPATH=<lingua dir> python -m scripts.multilang_broad` | M0b direct model checks, 20 languages. Use `--checks text,speech,transcribe,live` and `--langs ...`. Needs `lingua-language-detector`, installed outside the project venv. |
| `python -m scripts.live_ws_test --url ws://localhost:8003/live` | `/live` end to end. Extended in M3 with `--lang`. |
| Headless Chrome (Playwright, outside the repo) | Dictation, hands-free and display, with a simulated mic, as in earlier phases |

Always test against a copy of the database on port 8003, and leave the backend on port 8002 running.

---

## Risks

| Risk | Impact | Mitigation |
|---|---|---|
| Uneven quality across 99 languages | Low-resource languages may get weaker translations, transcripts or answers | Tiers. Automated Tier 2 checks. Gemini as the answer writer if M0b confirms it covers more languages well. |
| Groq limits: 8,000 tokens per minute and **200,000 tokens per day** | The router uses about 1,000 tokens per question, so the app can answer about 200 questions a day. Once the daily limit is reached, the router fails and typed chat answers worse. This happened during the M1 test. | Move the router to Gemini, or a paid Groq tier |
| Gemini free tier: **15 requests per minute** for Flash Lite | Answers and dictation share the limit, which a few active users would exceed | A paid Gemini tier before real use |
| Gemini Flash Lite quota (429) | Dictation and Gemini answers fail during bursts | Check the quota tier before rollout. Show a clear error. |
| Misdetected or romanized first message | Question shown in the wrong script, or wrong words | Conversation language remembered across chats, and a re-transcribe retry (M2). Re-transcription in hands-free (M3). The language picker if M5 shows it's needed. |
| Lost meaning in query translation | Weaker retrieval (the environment question scored 0.5) | Monitor in M5. If needed, search with both the English query and the original. |
| English terms inside answers | Voice reads "(obvious)" aloud | The prompt forbids English glosses in parentheses |
| Code-mixed speech (Hinglish, Spanglish…) | Hardest case for transcription and language choice | Real recordings in M5. Reply in the dominant language. |
| Right-to-left and complex scripts | Broken layout or missing fonts | `dir="auto"`, Noto fonts, screenshots (M4) |
| One voice for all languages | `Kore` may sound weaker in some languages | Native-speaker check for Tier 1 (M3) |
| Synthetic test audio | Precheck results may be better than real use | Real recordings in M5 |

---

## Open decisions

1. **Tier 1 languages:** which languages do your users mostly speak? They get native-speaker testing.
2. **Hands-free question text:** accept occasional romanized text, or re-transcribe each turn (about 2 s more
   before the question shows correctly)? Decide in M5, using real recordings.

### Decided
- **Interface labels stay in English** (2026-10-13). Only the chat content is multilingual.
- **No language picker for now** (2026-10-13). The language is detected and remembered. The picker is added
  only if M5 shows that first messages without a hint fail too often.
- **Gemini Flash Lite writes the answers in every language** (2026-10-13), based on M0b. Groq is the fallback
  if Gemini fails. The router stays on Groq.
