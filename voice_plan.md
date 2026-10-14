# Voice feature plan

Branch: `feature/voice_chat_gemini` · Started: 2026-10-09

## Goal

Add voice to the Docs Assistant using the Gemini Live API (`gemini-3.1-flash-live-preview`). There are two
buttons next to the message box:
- **Mic (dictation):** speak, and each pause is transcribed into the message box, where you can edit it.
  On Send, the answer is shown and spoken.
- **Waveform (hands-free):** a spoken conversation. Gemini answers when you pause, and you can talk over it.

Voice and text share one chat history and the same Sources panel. Typed questions still go to the existing
Groq text chat.

## Decisions

| Topic | Decision |
|---|---|
| Model | `gemini-3.1-flash-live-preview`, set by `LIVE_MODEL` in `.env`. Moving to `gemini-3.8-live` should only need a config change (see [Model notes](#model-notes)). |
| Where Gemini is called | Only from the backend. The browser talks to FastAPI over one WebSocket, and FastAPI talks to Gemini. The API key never reaches the browser. |
| Answer modes | Both, chosen per session: **agent** (Gemini searches the docs and writes the answer) and **relay** (Gemini reads the answer from the existing Groq RAG chain). Default: agent. |
| How you talk | Two composer buttons (changed 2026-10-09, replacing the hold-to-talk mode and the talk-mode setting): **dictation** (mic) and **hands-free** (waveform). See [Talking](#talking). |
| Dictation | Gemini (`TRANSCRIBE_MODEL`, default `gemini-3.5-flash-lite`) transcribes each clip the browser cuts at a pause. A dictated message gets a spoken answer in the chosen answer mode. |
| Text chat | Typed messages stay on Groq through `POST /chat`, text only. |
| Voice | A built-in Gemini voice, set by `LIVE_VOICE` (default `Kore`). |

## Model notes

Google's Live API guide marks `gemini-3.1-flash-live-preview` as a legacy preview and recommends `gemini-3.8-live` (stable).
Differences that affect this design:

| | 3.1 Flash Live (preview) | 3.8 Live (stable) |
|---|---|---|
| Tool calls | Sequential only: the model waits for our result | Asynchronous by default. Setting `behavior: BLOCKING` gives 3.1 behaviour. |
| Thinking | `thinking_level`, default `MINIMAL` | Not supported. The setting must be left out. |
| Server events | One event can carry several parts, such as audio and a transcript. Every part must be processed. | Same |
| Affective dialog, proactive audio | Not supported | Supported |

Limits that apply to both:
- **Input audio:** 16-bit PCM, 16 kHz, mono.
- **Output audio:** 16-bit PCM, 24 kHz, mono.
- **Connection:** about 10 minutes. Gemini sends a `GoAway` warning before it closes.
- **Session:** audio-only sessions stop at 15 minutes unless context compression is on.
- **Resuming:** a resumption handle is valid for 2 hours.

## Architecture

```
Browser (mic + speaker) ──WebSocket /live?answer=agent|rag&talk=vad|ptt──▶ FastAPI LiveBridge ──▶ Gemini Live
                                                                         │
                         agent mode: search_documents(query)  ───────────┼──▶ retriever (Milvus Lite)
                         relay mode: ask_knowledge_base(question) ───────┴──▶ RAGChain.answer() (Groq + Milvus)
```

Why the backend relays the audio instead of the browser connecting to Gemini with a short-lived token:
- **The retriever is already loaded in the backend.** Milvus Lite allows only one process to open the
  database, so search has to run there anyway.
- **The API key, system prompts and tools stay on the server.**
- **Tool calls are simpler.** With a direct connection, every search request would go from Gemini to the
  browser, then to our backend, and back.
- **The existing setup carries over.** The Vite proxy and HTTPS setup only need `ws: true` for `/live`.

## Answer modes

### Agent: Gemini answers
- **Tool:** `search_documents(query)` returns the top-k excerpts, using the same retriever and
  `format_docs()` as text chat.
- **Prompt:** the same rules as text chat. Answer only from the excerpts, say plainly when the answer isn't
  there, speak in two to four natural sentences, and search before answering any factual question.
- **Small talk:** Gemini replies directly without searching. This replaces the Groq router for voice.
- **Sources:** the backend sends a `sources` event with the retrieved excerpts.

### Relay: Gemini reads the existing RAG answer
- **Tool:** `ask_knowledge_base(question)` runs `RAGChain.answer(question, history)`, which is the same
  Groq router and answer chain as `/chat`.
- **Prompt:** always call the tool with the user's exact words, then read the returned answer word for word.
  No introduction, no summary, no follow-up.
- **History:** the backend keeps the session's conversation and passes it to `RAGChain.answer()`. The stored
  assistant message is the RAG text, not Gemini's transcript of it.
- **Accuracy caveat:** a Live model is a conversational model, not a text-to-speech engine, so word-for-word
  reading is likely but not guaranteed. The backend compares Gemini's transcript of what it said with the RAG
  text and logs a match score for every turn. If an exact reading is ever required, the fix is a dedicated
  text-to-speech model, which is outside this plan.

| | Agent | Relay |
|---|---|---|
| Who writes the answer | Gemini, from the retrieved excerpts | Groq RAG chain |
| Speed | Faster: one model step | Slower: Groq router and answer, then Gemini speaks |
| Same answer as text chat | Not always | Yes, apart from rare rewording |
| Sources panel | Yes | Yes |

## Talking

| | Dictation (mic button) | Hands-free (waveform button) |
|---|---|---|
| What happens | Recording runs until you click stop. Each stretch of speech ends at a pause (720 ms of quiet), goes to `POST /transcribe`, and is appended to the message box. | A Gemini Live session streams the mic. Gemini detects pauses and answers out loud. |
| Sending | You click Send (or Enter). Sending while still recording stops the mic and waits for the last words first. | Automatic at each pause |
| Answer | Shown in the chat and spoken: a short Live session receives the text (`{"type":"text"}`), speaks the answer, and closes itself once playback ends | Shown in the chat and spoken |
| Interrupting | Starting dictation stops a spoken answer; the voice bar's Stop button does too | Just talk over the answer (Gemini sends `interrupted`) |
| Controls | Mic button (turns into a red stop button), status line "Listening… / Transcribing…" | Voice bar: Mute, End |

The browser decides where a clip ends from the 40 ms loudness levels the capture worklet already reports.
Speech starts after 120 ms above 0.02 RMS, and the clip ends after 720 ms below 0.012. It keeps 320 ms from
before the speech started, and splits clips at 30 s. Transcription takes about 1.6 s per clip
(`gemini-3.5-flash-lite` with minimal thinking: correct on 4 of 4 test questions, and empty output for
silence). `gemini-3.1-flash-lite` was slower and returned "00:00" for silence.

The answer mode is fixed when a Gemini session starts. Changing it during a hands-free conversation reconnects,
passing the conversation so far.

The backend still supports `talk=ptt` (manual `activity_start` / `activity_end`, tested in Phase 3), but the UI
no longer uses it.

## WebSocket protocol (`/live`)

Query string: `answer=agent|rag`, `talk=vad|ptt`, and optionally `lang=<BCP-47 code>`, the conversation's
language (see [MULTILANGUAGE_PLAN.md](MULTILANGUAGE_PLAN.md)).

| Direction | Frame | Meaning |
|---|---|---|
| Browser → server | binary | Mic audio, PCM16 16 kHz mono, about 40 ms per frame |
| Browser → server | `{"type":"history","turns":[{role,content}]}` | Earlier conversation, sent first after connecting |
| Browser → server | `{"type":"text","text"}` | A dictated question to answer out loud |
| Browser → server | `{"type":"activity_start"}` / `{"type":"activity_end"}` | `talk=ptt` only (not used by the UI any more) |
| Browser → server | `{"type":"end"}` | End the session |
| Server → browser | binary | Model audio, PCM16 24 kHz mono |
| Server → browser | `{"type":"ready"}` | Gemini session is open |
| Browser → server | `{"type":"audio_stream_end"}` | Hands-free only: mic muted mid-sentence, so Gemini ends the turn without waiting for silence |
| Server → browser | `{"type":"input_transcript","text"}` | A new piece of what the user said; the browser joins the pieces |
| Server → browser | `{"type":"output_transcript","text"}` | A new piece of what the model is saying |
| Server → browser | `{"type":"tool_call","name"}` | A search is running (UI shows "Searching…") |
| Server → browser | `{"type":"sources","sources":[...]}` | Excerpts for the current answer |
| Server → browser | `{"type":"rag_answer","text"}` | Relay mode only: the RAG answer Gemini is reading. The chat stores this text. |
| Server → browser | `{"type":"language","language"}` | The current turn's detected language (BCP-47); the browser makes it the conversation's language |
| Server → browser | `{"type":"interrupted"}` | Clear queued audio now |
| Server → browser | `{"type":"turn_complete"}` | Model finished its turn |
| Server → browser | `{"type":"reconnecting","planned"}` | The Gemini connection ended (`planned`: Gemini announced it with `go_away`). The browser clears the partial answer; an unanswered question is asked again |
| Server → browser | `{"type":"resumed"}` | The new Gemini connection is open (the browser connection stayed open throughout) |
| Server → browser | `{"type":"error","message"}` | Something failed |

## UI

- **States:** idle, connecting, listening, thinking (tool running), speaking, error.
- **Composer:** the mic (dictation) and waveform (hands-free) buttons, beside Send.
- **Voice bar while a Live session is active:** the current state, a mic level meter, and Mute and End in
  hands-free mode, or Stop for a spoken answer to a dictated question.
- **Sidebar, "Voice answers":** answer mode only (agent or relay), saved in `localStorage`.
- **Chat list:** transcripts show up as ordinary user and assistant messages and are saved with the chat.

## Configuration (`.env`)

| Variable | Default |
|---|---|
| `GEMINI_API_KEY` | – (required for voice) |
| `LIVE_MODEL` | `gemini-3.1-flash-live-preview` |
| `LIVE_VOICE` | `Kore` |
| `LIVE_ANSWER_MODE` | `agent` |
| `LIVE_TALK_MODE` | `vad` (only for clients that don't pass `talk`) |
| `TRANSCRIBE_MODEL` | `gemini-3.5-flash-lite` |

## Phases

### Phase 0: Test what the model can do (backend only, no UI)
Script: [scripts/live_spike.py](scripts/live_spike.py). It runs against a copy of the Milvus database, so the
backend can keep running.
- [x] Install the `google-genai` SDK (2.29.0)
- [x] Write the test script with these checks:
  - `basic`: spoken reply and response time
  - `speech`: Gemini reads the test questions aloud, giving realistic input audio
  - `history`: a new session remembers conversation passed into it
  - `vad`: hands-free agent mode
  - `ptt`: push-to-talk
  - `relay`: read-aloud accuracy in relay mode
- [x] Run it and review `data/live_spike/report.json` and the WAV files

**Done when:** every check passes, or each failure has a workaround we've agreed on, and we know relay-mode
accuracy and response times.

**Results (2026-10-09, `gemini-3.1-flash-live-preview`, voice `Kore`):**

| Check | Result | Time to first audio (median) |
|---|---|---|
| basic | Spoken reply with transcript | 0.6 s after sending text |
| history | Earlier turns passed in with `send_client_content(turn_complete=False)` were remembered | – |
| vad (agent) | Searched on 10 of 10 document questions; small talk was answered without searching | 2.1 s after end of speech, including Gemini's silence detection |
| ptt (agent) | Manual `activity_start` / `activity_end` works; searched on 3 of 3 | 1.6 s after release |
| relay | Tool called on 11 of 11 turns (small talk included); spoken transcript matched the RAG text word for word on 11 of 11 | 3.5 s after end of speech (the Groq RAG step takes 0.6 to 1.9 s) |

What we learned:
- **The Live model doesn't reliably read text aloud as an instruction.** When asked to read a question aloud,
  it answered 2 of 11 questions instead. Relay mode wasn't affected: the tool-result framing kept it word for
  word on 11 of 11. The match score comes from Gemini's own transcript, so we'll also confirm by listening to
  `data/live_spike/relay_answer*.wav`.
- **The first search in the script takes about 15 s.** That's the embedding model loading on first use. The
  backend loads it at startup, so Phase 1 must keep it that way.
- **Every audio event had one part on 3.1.** The bridge still handles several parts per event.

### Phase 1: Backend bridge
- [x] `backend/config.py`, `.env.example`, `requirements.txt`: Gemini settings and `google-genai`
- [x] `backend/rag.py`: add a shared `retrieve(query)` and `to_sources(docs)`. `answer()` behaves as before.
- [x] `backend/live.py`, a `LiveBridge` class that:
  - builds the session settings for each answer and talk mode
  - forwards audio both ways in two loops
  - runs the tools as tasks, with the blocking work in a worker thread
  - keeps the session's conversation history
  - translates Gemini events into the protocol above
- [x] `backend/main.py`: add the `@app.websocket("/live")` route. It checks the `Origin` header, because CORS
  doesn't cover WebSockets: same-host pages, `CORS_ORIGINS` and non-browser clients are allowed.
- [x] Test script [scripts/live_ws_test.py](scripts/live_ws_test.py) plays WAV files over `/live`, with or
  without seeded history

**Done when:** all four combinations of answer mode and talk mode work from the test script.

**Results (2026-10-09):** all four combinations passed. Each run had 2 questions and 2 seeded history turns,
and every turn returned audio, transcripts, a tool call and 4 sources. Audio started 1.8 to 4.2 s after the
user stopped speaking. Tools took 0.03 to 0.09 s for a search and 1.2 to 2.3 s for the RAG chain.

What we learned:
- **Seeding history needs `history_config`.** Sending earlier turns with `send_client_content(turn_complete=False)`
  left an open user turn, and Gemini then took 8 to 31 s to respond (push-to-talk sometimes never responded).
  The fix is `history_config.initial_history_in_client_content=True` with history sent as
  `turn_complete=True`. It's set only when there is history, because otherwise the server waits for it.
- **Gemini's preview service is sometimes unstable.** For about 5 minutes, sessions failed with
  `1011 Internal error` or never answered a turn. The same code passed before and after. Phase 5 must handle
  this.
- **Problem in the existing text chat (not voice), now fixed:** when the history named the book ("Atomic Habits
  by James Clear"), the Groq router added that to the search query (for example, "What is the habit loop in
  Atomic Habits by James Clear?"). Retrieval then returned front-matter pages (26, 6) instead of the relevant
  ones (45, 50, 62), and the answer got worse. Relay mode inherited this. The fix: the router prompt now uses
  history only to resolve references such as "it" or "the second one", copies standalone questions unchanged,
  and never adds titles or authors. Checked on 3 standalone questions and 3 follow-ups: the standalone
  questions match their no-history retrieval, and the follow-ups still resolve correctly.

### Phase 2: Browser audio with hands-free
- [x] Mic capture: [capture-worklet.js](frontend/src/voice/capture-worklet.js) records with echo
  cancellation, noise suppression and auto gain on, downsamples to 16 kHz PCM16 by averaging windows, and
  sends 40 ms frames with a loudness level
- [x] Playback: [playback-worklet.js](frontend/src/voice/playback-worklet.js) queues 24 kHz audio, resamples
  it to the context rate by linear interpolation, clears the queue on `interrupted`, and reports when it
  starts and stops playing
- [x] [useVoiceSession.js](frontend/src/voice/useVoiceSession.js) manages the mic, the audio context and the
  WebSocket, and works out the UI state. It sends the text chat as history, so voice continues the
  conversation.
- [x] Mic button in the Composer, plus [VoiceBar.jsx](frontend/src/components/VoiceBar.jsx) with level meter,
  state label, live captions, Mute and End
- [x] `vite.config.js`: proxy `/live` with `ws: true`. The worklets are never inlined as `data:` URLs in builds.

**Done when:** a hands-free conversation works in the browser and talking over the model stops it.

**Results so far (2026-10-09):** a headless Chrome test (fake mic playing recorded questions, dev server
proxying to a test backend) held a 4-turn hands-free conversation. The state went Listening, then Searching,
then Speaking, then back to Listening each turn, with live captions and End closing cleanly. The audio the
browser sends was checked: 40 ms frames, clean 16 kHz speech, 0.03% clipping.

Interruption, tested twice: the test asked a question, then spoke a second one 2 s after "Speaking…"
appeared. Gemini sent `interrupted`, playback stopped **0.3 to 0.4 s** after the user started talking, the
bar went back to "Listening…", and the second question was searched and answered. Audio started about 1 s
after the end of the first question.

Gemini outage: from 18:57 to about 19:05 every audio session failed with `1011 Internal error`, while text
sessions kept working. This confirms Phase 5 needs reconnect and retry. Speaker echo in hands-free mode
couldn't be tested headless and needs a manual check on real hardware.

Captions stay in the voice bar for now. Phase 4 moves them into the chat list.

### Phase 3: Push-to-talk (replaced in Phase 4b)

The hold-to-talk UI below was built and tested, then replaced by dictation in Phase 4b. The backend's
`talk=ptt` support remains.

- [x] Hold-to-talk button that works with mouse and touch (pointer capture, no long-press menu), plus holding
  Space. The Space shortcut is off while typing in a text field, and losing window focus releases it.
- [x] Press sends `activity_start`. Release keeps streaming for 300 ms, so the last word isn't cut off, then
  sends `activity_end`. Pressing while the model is speaking clears playback at once and drops the rest of
  that answer.
- [x] A Hands-free / Push to talk toggle in the voice bar, remembered in `localStorage`. Switching reconnects,
  passing the conversation so far: the seeded text history plus the session's finished voice turns, which
  the hook keeps.

**Done when:** push-to-talk works with mouse, touch and keyboard, and switching modes keeps the conversation.

**Results (2026-10-09), headless Chrome:**
- Holding Space while asking, then releasing, got an answer. Audio started about 1.5 s after release.
- Pressing the on-screen button during the answer stopped playback within 100 ms, and Gemini confirmed with
  `interrupted` about 0.1 s after `activity_start`. The second question was answered.
- Switching to hands-free reconnected with 4 history turns and answered a follow-up question.
- At 390 px phone width, the controls wrap onto two rows with no horizontal scroll.

### Phase 4: Chat integration
- [x] The turn in progress streams into the chat list as faded "Live" messages. When it completes, it becomes
  ordinary messages with a "Voice" marker, saved with the chat. The voice bar no longer shows captions.
- [x] `sources` events attach to the voice answer
- [x] The current conversation is sent as history on connect, so voice continues the text chat. Typed
  questions send the voice turns to `/chat` as history.
- [x] Sidebar Voice section with answer mode ("Gemini answers" / "Read RAG answer") and talk mode, saved in
  `localStorage` (`docs-assistant-voice`). Changing either during a session reconnects with the history.
- [x] In relay mode, the stored assistant message is the RAG text (from the new `rag_answer` event). If the
  user interrupted, it stores what was actually said, marked "Voice · interrupted…".
- [x] New chat also ends the voice session, which would otherwise still remember the old conversation

**Done when:** you can switch between typing and talking in one conversation without losing context.

**Results (2026-10-09), headless Chrome:** all six checks passed:
1. The Sidebar shows the saved settings.
2. A relay turn shows live messages, then a saved voice message whose text equals the `rag_answer` exactly,
   with sources.
3. Switching to agent mode in the Sidebar reconnects with the history and answers.
4. The typed follow-up "Can you give me an example of the second thing I asked about?" sent all 6 earlier
   turns (seeded text plus voice) to `/chat`, and was correctly answered about habit stacking.
5. The 4 voice messages survive a reload.
6. New chat closes the voice session and clears the messages.

**Known limitation:** a question typed during a voice session goes through text chat, as decided, so the
running Gemini session doesn't see that exchange. It's included the next time the session connects, for
example after a mode switch or restart.

### Phase 4b: Dictation and hands-free buttons (redesign)
- [x] `POST /transcribe` ([backend/transcribe.py](backend/transcribe.py)): raw 16 kHz PCM16 in, `{text}` out,
  up to 60 s per clip
- [x] `/live` accepts `{"type":"text"}`, so a dictated question gets a spoken answer
- [x] [useDictation.js](frontend/src/voice/useDictation.js): finds pauses in the browser, sends clips and
  applies the transcripts in order; `stop()` waits for the last one
- [x] [mic.js](frontend/src/voice/mic.js): mic setup shared by dictation and hands-free
- [x] `useVoiceSession`: hands-free sessions with the mic, and spoken-reply sessions without it, which send
  the text, play the answer and close themselves. The hold-to-talk code is removed.
- [x] Composer: mic (dictate / stop) and waveform (hands-free) buttons, plus a status line. Sidebar: the
  talk-mode setting is removed.

**Results (2026-10-09), headless Chrome:**
- Dictation filled the box with "What is the habit loop?", then appended "What is habit stacking?" after
  the second pause.
- Send added a "Voice" user message, then a spoken answer covering both questions with 4 sources, without
  calling `/chat`. The voice bar closed itself after playback.
- Sending mid-dictation waited for the last clip ("What is the 2-minute rule?") before sending.

### Phase 5: Robustness
- [x] Session limits (built 2026-10-14, live test pending): session resumption and sliding-window context
  compression are on. On `go_away` the bridge reconnects with the latest resumption handle, right away
  between turns, or once the current turn completes. The browser's WebSocket stays open; only the Gemini
  connection is replaced.
- [x] Gemini errors (built 2026-10-14, live test pending):
  - a retryable close (`1001`, `1006`, `1011`, `1012`, `1013`, `1014`, 5xx, network errors) or a question
    with no audio for 15 s (`REPLY_TIMEOUT_S`) reconnects after 1 s, then 3 s; a completed turn resets the
    count. After that the session ends with "The voice service is having trouble… try again in a minute".
  - a question not yet answered (typed text, or the input transcript) is asked again as text on the new
    connection; in relay mode its RAG answer is first removed from the history
  - a refused resumption handle falls back to a fresh session seeded with the conversation. Agent mode now
    keeps its own history (input + output transcripts) for this.
  - the UI shows "Reconnecting…" and drops the partial answer
  - not covered: in hands-free mode a question Gemini never transcribes (it sends the transcript together
    with the reply), so a silent turn there still waits for Gemini's own error
  - checked offline with a fake Gemini client (1011 then success, silent turn, repeated failures,
    `go_away` mid-answer)
  - checked with real `gemini-3.1-flash-live-preview` (2026-10-14, agent mode, in-process bridge): the
    resumption and compression settings are accepted; a handle arrives after the first turn; a failure
    between turns resumed with the handle in ~2 s and the model remembered the earlier question; a failure
    mid-answer resumed, asked the question again, and answered it in full. A real `go_away` (after
    ~10 min) and a real `1011` haven't been seen yet.
- [ ] Clear errors in the UI: mic permission denied, no microphone, backend down, Gemini errors
- [ ] Close the WebSocket when the tab closes or the component unmounts
- [ ] Log for each turn:
  - time from end of speech to first audio
  - time spent in tools
  - in relay mode, how closely the spoken words matched the RAG text

**Done when:** a 20-minute session survives the reconnects, and every error case shows a clear message.

### Phase 6: Docs and model switch
- [ ] README: setup, the `/live` API, both answer modes and both talk modes
- [ ] Test `LIVE_MODEL=gemini-3.8-live`: remove the thinking level and set the tools to `BLOCKING`

## Risks

| Risk | Mitigation |
|---|---|
| Agent mode answers from Gemini's own knowledge without searching | Strict prompt; Phase 0 counts how many document questions trigger a search |
| Relay mode rewords the RAG answer | Strict prompt; match score logged every turn; text-to-speech model as a fallback |
| Relay mode is slow (two Groq calls before Gemini speaks) | Show a "thinking" state; Phase 0 measures the delay |
| Speaker echo makes hands-free mode interrupt itself | Browser echo cancellation; recommend headphones; offer push-to-talk |
| 3.1 is a legacy preview and may be retired | The model is one `.env` value; Phase 6 tests 3.8 |
| Only one process can open Milvus Lite | Voice runs inside the backend process; test scripts use a copy of the database |

## References

- [Gemini 3.1 Flash Live Preview](https://ai.google.dev/gemini-api/docs/models/gemini-3.1-flash-live-preview)
- [Gemini 3.8 Live](https://ai.google.dev/gemini-api/docs/models/gemini-3.8-live)
- [Live API capabilities guide](https://ai.google.dev/gemini-api/docs/live-guide)
- [Live API session management](https://ai.google.dev/gemini-api/docs/live-session)
- [Ephemeral tokens](https://ai.google.dev/gemini-api/docs/ephemeral-tokens)
