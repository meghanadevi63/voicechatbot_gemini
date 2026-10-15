# Voice RAG Assistant: POC report

**Date:** 2026-10-15 · **Branch:** `feature/stt-tts-pipeline` · **Document set used in tests:** *Atomic Habits* (PDF)

## 1. Summary

The POC is a document assistant that answers questions about PDFs by **text or voice, in any language Gemini
supports**, with every answer grounded in the documents through a RAG pipeline. It now offers **three voice
pipelines**, selectable in the sidebar:

| # | Pipeline | In one line | Status |
|---|---|---|---|
| 1 | **Gemini 3.1 Flash Live** | One Live model listens, answers (or reads the RAG answer) and speaks | Working, tested in browser |
| 2 | **Transcribe 3.5 + Flash TTS 3.8** | Streaming speech-to-text, RAG answer, studio TTS | Working, **too slow to start speaking** for live conversation |
| 3 | **Transcribe 3.5 + Live 3.1 voice** | Streaming speech-to-text, RAG answer, Live model reads it word for word | Working, **recommended for the demo** |

Key results:

- **Live text while speaking** (pipelines 2 and 3): the user's words appear about every 0.5 s while they talk.
  Pipeline 1 can't do this: the Live models deliver the user's transcript in one piece after the turn.
- **Exact answers by voice:** pipelines 2 and 3 speak exactly the RAG text shown in the chat (word similarity
  1.000 on every measured turn).
- **Latency, end of speech → first audio:** pipeline 1 ~1–2 s (agent) / ~3.5 s (RAG relay); pipeline 3 ~4 s;
  pipeline 2 ~8 s or more.
- **Barge-in** (talking over the answer) works on pipelines 1 and 3.

## 2. Architecture

```
                  ┌── POST /chat ───────────────▶ RAGChain ─────────────────────────────┐
                  │                                                                   │
                  ├── POST /transcribe ─────────▶ Gemini 3.5 Flash Lite (dictation)   │
 Browser (React)  │                                                                   ▼
 mic 16 kHz PCM ──┼── WS /live ─────────────────▶ LiveBridge ──▶ Gemini 3.1 Flash Live ─ tool ─▶ RAG
 speaker 24 kHz   │                                                                   ▲
                  └── WS /stt-tts ──────────────▶ SttTtsBridge                        │
                         ?speaker=tts|live           ├─▶ Gemini 3.5 Transcribe Live (speech → text)
                                                     ├─▶ RAGChain.answer() ───────────┘
                                                     └─▶ Gemini 3.8 Flash TTS  |  Gemini Live reader

 RAGChain: Groq router (gpt-oss-20b) → Milvus Lite retrieval (MiniLM embeddings) → Gemini 3.5 Flash Lite answer
           (Groq fallback)
```

- **Backend:** FastAPI (Python). All API keys stay on the server; the browser streams raw audio to the backend,
  which relays it to Google.
- **Frontend:** React + Vite. AudioWorklets capture the mic (16 kHz PCM16, 40 ms chunks) and play replies
  (24 kHz PCM16) with instant clear for barge-in.
- **One conversation:** typed, dictated and spoken turns share one chat history; voice turns are saved with
  their sources.

## 3. The RAG pipeline (shared by all modes)

| Step | What happens | Model / component |
|---|---|---|
| Ingest | PDFs in `docs/` split into 1000-char chunks (150 overlap), embedded, stored | `all-MiniLM-L6-v2` (local CPU), Milvus Lite file DB (FLAT, cosine) |
| 1. Route | Classify the message: **small talk** (reply directly, no search) or **document query**; write a standalone **English** search query (+ an alternative wording when a term is ambiguous); detect the message's language | Groq `openai/gpt-oss-20b`, structured output |
| 2. Retrieve | Top 4 chunks for the query (and the alternative query, merged by score) | Milvus Lite |
| 3. Answer | Answer **only from the excerpts**, 2–4 spoken-style sentences, in the user's language; say plainly when the documents don't contain it | Gemini `gemini-3.5-flash-lite` (minimal thinking); Groq `gpt-oss-20b` as fallback |
| Output | Answer text, sources (file, page, excerpt), detected language | |

- **Multilingual:** documents are English; questions and answers can be in any language. Searches always run
  in English (the router translates).
- **History:** the last 6 messages are used to resolve follow-ups ("tell me more", "the second one").
- **Robustness:** if the router fails, plain retrieval on the question; if Gemini fails, Groq writes the answer.
- **Speed:** 0.6–2.3 s warm. **The first question after a backend start took 9–10 s** in today's tests.

### Two ways a voice pipeline can use RAG

| RAG mode | How | Who writes the answer | Same answer as typed chat? |
|---|---|---|---|
| **Agent** ("Gemini answers") | The Live model calls `search_documents`; the backend returns the top 4 excerpts | The Live model | Not always |
| **RAG answer** ("Read RAG answer") | The full `RAGChain.answer()` runs (router → retrieval → answer); the voice reads the result | The RAG answer writer | **Yes** |

## 4. Input modes

| Mode | How | Answer |
|---|---|---|
| **Typed** | Message box → `POST /chat` | Text with sources |
| **Dictation** | Mic button; each pause (720 ms) is transcribed into the box by Gemini 3.5 Flash Lite (~1.6 s per clip); editable before sending | Text **and** spoken, by the selected voice pipeline |
| **Hands-free** | Waveform button; just talk | Spoken and saved as text, with barge-in |

## 5. Voice pipelines in detail

### 5.1 Pipeline 1: Gemini 3.1 Flash Live (`/live`)

- **Models:** `gemini-3.1-flash-live-preview` for listening, deciding and speaking (voice `Kore`).
- **RAG modes supported:** **Agent** and **RAG answer** (relay: the model calls `ask_knowledge_base` and reads
  the returned answer).
- **Turn detection and barge-in:** built into the Live model (server-side voice activity detection). Playback
  stops 0.3–0.4 s after the user starts talking.
- **User transcript:** arrives in one piece after the user's turn, not word by word.
- **Reliability features:** reconnects with a session-resumption handle on `go_away`, errors and unanswered
  turns, and asks the unanswered question again; history is seeded into fresh sessions.
- **Strengths:** fastest (agent mode ~1–2 s to first audio); natural, built-in interruption.
- **Weaknesses:** the model is a **legacy preview**; agent answers can differ from typed chat; relay
  word-for-word reading is reliable in tests (23/23 turns) but not guaranteed by design; outages seen on
  2026-10-09 (`1011` errors for ~8 min, twice) and slow spells (20–34 s first audio).

### 5.2 Pipeline 2: Transcribe 3.5 + Flash TTS 3.8 (`/stt-tts?speaker=tts`)

- **Models:** `gemini-3.5-transcribe-live` (speech → text), RAG chain, `gemini-3.8-flash-tts` (voice `Sulafat`).
- **RAG modes supported:** **RAG answer only.**
- **Flow:** the transcriber streams interim text → final text + `generation_complete` ends the turn (~1.3–1.5 s
  after speech) → RAG answer shown in chat → TTS speaks it (sent as 2 parallel requests: first sentence, rest).
- **Drawbacks (measured):**
  - **Slow to start speaking:** 4.4 s (one short sentence) to 9.6 s (three sentences) before the first audio;
    5.0 s in the end-to-end test. The model processes the whole text before sending any audio.
  - **Unreliable today:** two requests got no response within 40 s.
  - **Quota:** 2 TTS requests per answer, on top of the RAG calls.
  - Barge-in and dictated replies are **not yet tested** on this pipeline.
- **Strength:** studio-quality, expressive voice; exact text.

### 5.3 Pipeline 3: Transcribe 3.5 + Live 3.1 voice (`/stt-tts?speaker=live`)

- **Models:** `gemini-3.5-transcribe-live` (speech → text), RAG chain, `gemini-3.1-flash-live-preview` as a
  **reader** (voice `Kore`).
- **RAG modes supported:** **RAG answer only.**
- **How it differs from pipeline 2:** a Live session is told to read each text word for word. It is connected
  in advance and reused across turns, so there's no connection delay in a turn.
- **Barge-in:** **managed by our backend**, because the reader never hears the mic. While the answer plays, the
  transcriber's interim text with ≥ 2 words (or ≥ 6 characters) cancels the answer, closes the reader connection
  (so no stale audio plays later), clears the browser's audio queue, and the user's words become the next
  question. A new reader connects in the background.
- **Results (WebSocket tests, recorded questions):** first audio **0.6–1.2 s** after the answer; reading
  fidelity **1.000** on 3/3 turns; barge-in stopped the answer ~1 s after the user started talking; the next
  answer started 0.6 s after its RAG answer.
- **Strengths:** live text while speaking, exact RAG text, fast voice, tested end to end.

### 5.4 Turn-taking in pipelines 2 and 3

| Moment | Behaviour | UI |
|---|---|---|
| User speaking | Interim transcript replaces the live caption every ~0.5 s | "Listening…" |
| Turn ended → first audio | Speech is **ignored** (by design) | "Preparing the answer…" |
| Answer playing | Speech with ≥ 2 words interrupts (barge-in) | "Speaking…" |
| Mute | Sends `audio_stream_end` so the turn still ends | "Muted" |
| Transcriber drops / 10-min limit | Reconnects; an answer in progress carries on | "Reconnecting…" |
| Voice fails | Turn ends with "Couldn't speak the answer (it is in the chat)" after 15–20 s | Error line |

## 6. Feature matrix

| Capability | P1 Live (agent) | P1 Live (RAG answer) | P2 Transcribe + Flash TTS | P3 Transcribe + Live voice |
|---|---|---|---|---|
| RAG mode | Agent (retrieval only) | Full RAG chain | Full RAG chain | Full RAG chain |
| Same answer as typed chat | Not always | Yes | Yes | Yes |
| Spoken text = chat text | Model's own words | 23/23 in tests | Exact (TTS) | 1.000 in tests |
| Live text while user speaks | No (after the turn) | No | **Yes** | **Yes** |
| Answer text before the voice | No (streams with audio) | Yes | Yes | Yes |
| End of speech → first audio | ~1–2.2 s | ~3.5 s | ~8 s or more | **~4 s** |
| Barge-in | Built in (0.3–0.4 s) | Built in | Ours (untested) | **Ours (~1 s)** |
| Small talk | Model replies | Router replies | Router replies | Router replies |
| Multilingual | Yes | Yes | Yes (130+ TTS languages) | Yes |
| Dictated message replies | Yes | Yes | Yes (untested) | Yes (untested in browser) |
| Requests per voice turn (approx.) | Live session + tool | Live session + Groq + Gemini | Transcriber + Groq + Gemini + 2 TTS | Transcriber + Groq + Gemini + Live |

## 7. Models: capabilities and limits

| Model | Role | Capabilities | Limits / caveats |
|---|---|---|---|
| `gemini-3.1-flash-live-preview` | P1 voice; P3 reader | Real-time audio in/out over one WebSocket; built-in VAD and barge-in; tool calling (sequential); input/output transcripts; ~0.6 s text → first audio; 30 prebuilt voices | **Legacy preview** (successor `gemini-3.8-live`, untested); connections ~10 min (`go_away` first), audio sessions 15 min without compression; user transcript not streamed; outages and slow spells seen |
| `gemini-3.5-transcribe-live` | P2/P3 speech → text | Streaming transcription with interim text (~0.5 s cadence, full hypothesis each time); automatic VAD, hybrid and manual modes; 85+ languages with auto-detect and code-switching; native scripts (Hindi in Devanagari confirmed); 4.0% average WER reported by Google | **Preview** (announced 2026-08-26); sessions ≤ 10 min; final ~1.5 s after speech; no language code returned (SDK field exists, not sent); turn never ends if audio stops without silence; no diarization/word timestamps in live mode (per third-party guide) |
| `gemini-3.8-flash-tts` | P2 voice | Studio-quality, expressive speech; 30 voices + extended library; 130+ languages, auto-detected; streaming 24 kHz PCM; up to 2 speakers | 4–10 s before first audio, growing with text length; no responses in 40 s at times today; listed as preview on Google Cloud's Enterprise API |
| `gemini-3.8-flash-lite-tts` | Not used (option) | Lower-cost TTS | 7.7 s to first audio in one test |
| `gemini-3.5-flash-lite` | RAG answer writer; dictation; query translation | Fast (~1–2 s with minimal thinking), multilingual, structured output | Dictation clips ≤ 60 s (browser splits at 30 s) |
| Groq `openai/gpt-oss-20b` | Router; fallback answer writer | Fast structured output; intent + English query + language | Groq limits: 8K tokens/min, 200K tokens/day |
| `all-MiniLM-L6-v2` | Embeddings | Local, CPU, 0.02–0.23 s per search | English-oriented (fine here, since queries are translated to English); changing it needs re-ingest |
| Milvus Lite | Vector store | File database, no server | One process at a time (a second backend can't open it) |

**Quotas:** Gemini free tier ~15 requests/min shared by all Gemini calls; TTS free-tier limits are low. Enough
for a demo, not for multiple users.

## 8. Measured performance

| Measure | Result | Source |
|---|---|---|
| RAG answer (router + retrieval + answer) | 0.6–2.3 s warm; 9–10 s first question after start | voice tests, 2026-10-09 / 10-15 |
| Retrieval only | 0.02–0.23 s | 2026-10-09 |
| Dictation clip → text | median 1.6 s, max 2.6 s | 2026-10-09 |
| P1 agent, end of speech → first audio | ~1 s browser, median 2.1 s scripted | 2026-10-09 |
| P1 relay, end of speech → first audio | median 3.5 s | 2026-10-09 |
| P1 barge-in | 0.3–0.4 s | 2026-10-09 |
| Transcribe Live: first interim word | ~0.75 s after speech starts | 2026-10-15 spike |
| Transcribe Live: final after speech ends | 1.3–1.5 s | 2026-10-15 |
| Flash TTS first audio | 4.4–9.6 s; sometimes none in 40 s | 2026-10-15 |
| Live reader first audio | 0.6–1.2 s | 2026-10-15 |
| P3 barge-in | ~1 s | 2026-10-15 |
| Spoken vs RAG text (relay / reader) | 23/23 turns (P1); 1.000 on 3/3 (P3) | 2026-10-09 / 10-15 |

## 9. Security and privacy

- Gemini and Groq keys never reach the browser; the backend relays all audio.
- WebSocket routes check the `Origin` header (CORS doesn't cover WebSockets).
- Microphone access needs HTTPS (the dev server uses a self-signed certificate).
- **Data sent to providers:** mic audio, transcripts, history and document excerpts go to Google (Gemini);
  router calls (questions and history) go to Groq.
- The backend stores no audio or transcripts; the browser keeps the chat in `localStorage`.
- **No user authentication** in the POC.

## 10. Known limitations

| Limitation | Affects | Impact |
|---|---|---|
| Live model is a legacy preview; transcribe and TTS models are previews | All | Names and behaviour may change; outages possible |
| Flash TTS latency and unreliability | P2 | Not usable for conversation today |
| First RAG question after a backend start is slow (9–10 s) | All RAG modes | Bad first impression in a demo |
| Speech while the answer is being prepared is dropped | P2, P3 | "…and also X" added in that window is lost (the UI says "Preparing the answer…") |
| Barge-in by our backend is ~1 s, vs 0.3–0.4 s built in | P3 | Slightly slower to stop |
| Speaker echo not tested on real hardware | All hands-free | May interrupt itself without headphones |
| New pipelines not yet tested in a real browser with a mic | P2, P3 | Tested over WebSocket with recorded audio only |
| Free-tier quotas | All | A fast conversation can hit Gemini's 15 requests/min |
| Milvus Lite is single-process | Backend | No horizontal scaling; test tools need a DB copy |
| No auth, no server-side history | All | Not multi-user ready |
| Safari autoplay and mic behaviour untested | All | Possible blocked audio on Safari |

## 11. Recommended next steps

**Before the demo (high priority)**

1. **Browser check of pipeline 3** with a real mic and speakers: turn-taking, barge-in, Hindi/Telugu,
   dictated replies, switching pipelines mid-conversation.
2. **Warm up the RAG chain at backend start** (one dummy routing and answer call) so the first real question
   isn't 9–10 s.
3. **Use headphones** for the demo, or raise the barge-in threshold if echo interrupts the answer.
4. **Update [VOICE_FEATURES.md](VOICE_FEATURES.md)**: it predates the new pipelines and the reconnect work
   (it still says the bridge doesn't reconnect and that Groq writes the answers).

**Improvements (medium priority)**

5. **Stream the RAG answer into the reader** sentence by sentence (Gemini streaming output), so P3 starts
   speaking before the whole answer is written: about 1–1.5 s faster.
6. **Live dictation with Transcribe 3.5** (manual VAD): words appear in the message box as the user speaks,
   instead of after each pause. Now possible; earlier ruled out with the Live models.
7. **Hybrid VAD** in P2/P3 (browser detects silence and sends `audio_stream_end`) to cut the ~1.5 s
   end-of-turn wait.
8. **Try `gemini-3.8-live`** (stable) for P1 and the P3 reader; the code already has the 3.8 branches.
9. **Decide on P2:** keep it only as a "studio voice" option, or retry once Flash TTS latency improves.

**Before production (needed)**

10. **Paid API tier** and rate-limit handling (Gemini 15 requests/min and Groq token limits are demo-only).
11. **Authentication**, server-side conversation storage and a privacy notice (audio goes to Google).
12. **Scalable vector store** (Milvus server or managed) instead of Milvus Lite.
13. **Monitoring:** log per-turn latency (already logged: RAG time, first audio, fidelity) to a dashboard;
    alert on provider errors.
14. **RAG evaluation set:** a fixed list of questions with expected sources, run in each language, to measure
    answer quality across changes.
15. **Cross-browser tests** (Safari, mobile) and an automated browser test for the new pipelines.

## 12. Configuration reference

| Variable | Default | Used by |
|---|---|---|
| `GEMINI_API_KEY`, `GROQ_API_KEY` | – | All |
| `ROUTER_MODEL`, `GROQ_MODEL` | `openai/gpt-oss-20b` | RAG router / fallback |
| `ANSWER_PROVIDER`, `ANSWER_MODEL` | `gemini`, `gemini-3.5-flash-lite` | RAG answer writer |
| `EMBEDDING_MODEL`, `TOP_K`, `CHUNK_SIZE`, `CHUNK_OVERLAP` | MiniLM, 4, 1000, 150 | Ingest / retrieval |
| `LIVE_MODEL`, `LIVE_VOICE` | `gemini-3.1-flash-live-preview`, `Kore` | P1; P3 reader |
| `TRANSCRIBE_MODEL` | `gemini-3.5-flash-lite` | Dictation |
| `STT_MODEL` | `gemini-3.5-transcribe-live` | P2, P3 |
| `TTS_MODEL`, `TTS_VOICE` | `gemini-3.8-flash-tts`, `Sulafat` | P2 |

The pipeline and the P1 answer mode are chosen in the sidebar and remembered in the browser.

## 13. Code map

| File | Role |
|---|---|
| [backend/rag.py](backend/rag.py) | RAG chain: router, retrieval, answer writer |
| [backend/live.py](backend/live.py) | Pipeline 1 bridge (`/live`), prompts, tools, reconnects |
| [backend/stt_tts.py](backend/stt_tts.py) | Pipelines 2 and 3 bridge (`/stt-tts`): transcriber, turns, barge-in |
| [backend/tts.py](backend/tts.py) | Pipeline 2 voice: Flash TTS streaming |
| [backend/live_reader.py](backend/live_reader.py) | Pipeline 3 voice: Live session reading the answer |
| [backend/transcribe.py](backend/transcribe.py) | Dictation transcription |
| [frontend/src/voice/useVoiceSession.js](frontend/src/voice/useVoiceSession.js) | Voice sessions for all pipelines |
| [frontend/src/components/Sidebar.jsx](frontend/src/components/Sidebar.jsx) | Pipeline and answer-mode choice |
| [scripts/stt_tts_spike.py](scripts/stt_tts_spike.py) | Model spike for pipelines 2/3 |

Related documents: [STT_TTS_PIPELINE_PLAN.md](STT_TTS_PIPELINE_PLAN.md),
[STT_TTS_SPIKE_RESULTS.md](STT_TTS_SPIKE_RESULTS.md), [VOICE_FEATURES.md](VOICE_FEATURES.md),
[README.md](README.md).

## Sources

- [Live transcription with Gemini Live API](https://ai.google.dev/gemini-api/docs/live-api/live-transcribe)
- [Gemini 3.5 Transcribe (Google blog)](https://blog.google/innovation-and-ai/models-and-research/gemini-models/gemini-3-5-transcribe/)
- [Gemini API speech generation (TTS)](https://ai.google.dev/gemini-api/docs/speech-generation)
- [Gemini-TTS on Google Cloud](https://docs.cloud.google.com/text-to-speech/docs/gemini-tts)
