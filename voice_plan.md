# Voice feature plan

Branch: `feature/voice_chat_gemini` · Started: 2026-10-09

## Goal

Add spoken conversation to the Docs Assistant using the Gemini Live API (`gemini-3.1-flash-live-preview`).
You can talk to it hands-free or with push-to-talk. It answers out loud. Voice and text share one chat
history and the same Sources panel. The existing text chat on Groq doesn't change.

## Decisions

| Topic | Decision |
|---|---|
| Model | `gemini-3.1-flash-live-preview`, set by `LIVE_MODEL` in `.env`. Moving to `gemini-3.8-live` should only need a config change (see [Model notes](#model-notes)). |
| Where Gemini is called | Only from the backend. The browser talks to FastAPI over one WebSocket, and FastAPI talks to Gemini. The API key never reaches the browser. |
| Answer modes | Both, chosen per session: **agent** (Gemini searches the docs and writes the answer) and **relay** (Gemini reads the answer from the existing Groq RAG chain). Default: agent. |
| Talk modes | Both, chosen per session: **hands-free** (Gemini detects when you stop talking) and **push-to-talk** (hold a button or Space). |
| Text chat | Stays on Groq through `POST /chat`. Text typed during a voice session goes through normal text chat. |
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

## Talk modes

| | Hands-free | Push-to-talk |
|---|---|---|
| How a turn ends | Gemini detects end of speech | Releasing the button or Space |
| Gemini setting | Automatic voice detection on | Automatic voice detection off; the browser sends `activity_start` and `activity_end` |
| Interrupting the model | Start talking; Gemini sends `interrupted` | Press the button; local playback is cleared and a new turn starts |
| Echo from speakers | Handled by the browser's echo cancellation; headphones help | Not a problem: the mic only streams while held |
| Controls | Mute, End | Hold to talk (button or Space), End |

Answer mode and talk mode are fixed when a Gemini session starts. Changing either one starts a new session and
passes the conversation so far into it.

## WebSocket protocol (`/live`)

Query string: `answer=agent|rag`, `talk=vad|ptt`.

| Direction | Frame | Meaning |
|---|---|---|
| Browser → server | binary | Mic audio, PCM16 16 kHz mono, about 40 ms per frame |
| Browser → server | `{"type":"history","turns":[{role,content}]}` | Earlier conversation, sent first after connecting |
| Browser → server | `{"type":"activity_start"}` / `{"type":"activity_end"}` | Push-to-talk button pressed / released |
| Browser → server | `{"type":"end"}` | End the session |
| Server → browser | binary | Model audio, PCM16 24 kHz mono |
| Server → browser | `{"type":"ready"}` | Gemini session is open |
| Server → browser | `{"type":"input_transcript","text","final"}` | What the user said |
| Server → browser | `{"type":"output_transcript","text"}` | What the model is saying |
| Server → browser | `{"type":"tool_call","name"}` | A search is running (UI shows "Searching…") |
| Server → browser | `{"type":"sources","sources":[...]}` | Excerpts for the current answer |
| Server → browser | `{"type":"interrupted"}` | Clear queued audio now |
| Server → browser | `{"type":"turn_complete"}` | Model finished its turn |
| Server → browser | `{"type":"reconnecting"}` | Reconnecting with a resumption handle |
| Server → browser | `{"type":"error","message"}` | Something failed |

## UI

- **States:** idle, connecting, listening, thinking (tool running), speaking, error.
- **Composer:** a mic button starts a voice session.
- **Voice bar while active:**
  - a mic level meter and the current state
  - Mute and End in hands-free mode; Hold to talk and End in push-to-talk mode
  - a control to switch between hands-free and push-to-talk
- **Sidebar, "Voice" section:** answer mode (agent or relay) and talk mode, saved in `localStorage`.
- **Chat list:** transcripts show up as ordinary user and assistant messages and are saved with the chat.

## Configuration (`.env`)

| Variable | Default |
|---|---|
| `GEMINI_API_KEY` | – (required for voice) |
| `LIVE_MODEL` | `gemini-3.1-flash-live-preview` |
| `LIVE_VOICE` | `Kore` |
| `LIVE_ANSWER_MODE` | `agent` |
| `LIVE_TALK_MODE` | `vad` |

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
- [ ] `backend/config.py`, `.env.example`, `requirements.txt`: Gemini settings and `google-genai`
- [ ] `backend/rag.py`: move retrieval into a shared `retrieve(query)` function. `answer()` stays unchanged.
- [ ] `backend/live.py`, a `LiveBridge` class that:
  - builds the session settings for each answer and talk mode
  - forwards audio both ways in two loops
  - runs the tools in a worker thread
  - keeps the session's conversation history
  - translates Gemini events into the protocol above
- [ ] `backend/main.py`: add the `@app.websocket("/live")` route
- [ ] A test script that plays WAV files over `/live`

**Done when:** all four combinations of answer mode and talk mode work from the test script.

### Phase 2: Browser audio with hands-free
- [ ] Mic capture: an AudioWorklet that records with echo cancellation and noise suppression on, converts to
  16 kHz PCM16, and sends about 40 ms per frame
- [ ] Playback: an AudioWorklet that plays 24 kHz audio from a queue and can clear it instantly
- [ ] `useVoiceSession` hook that manages the WebSocket and the UI states
- [ ] Mic button in the Composer, plus the voice bar with level meter, Mute and End
- [ ] `vite.config.js`: proxy `/live` with `ws: true`

**Done when:** a hands-free conversation works in the browser and talking over the model stops it.

### Phase 3: Push-to-talk
- [ ] Hold-to-talk button that works with mouse and touch, plus holding the Space bar. The Space shortcut is
  off while the text box has focus.
- [ ] Press sends `activity_start` and release sends `activity_end`. Pressing while the model is speaking
  interrupts it.
- [ ] Switching between hands-free and push-to-talk reconnects and keeps the conversation

**Done when:** push-to-talk works with mouse, touch and keyboard, and switching modes keeps the conversation.

### Phase 4: Chat integration
- [ ] Transcripts become chat messages, so voice and text share one conversation
- [ ] `sources` events attach to the voice answer
- [ ] The current conversation is sent as history on connect, so voice continues the text chat
- [ ] Sidebar Voice section with answer mode and talk mode, saved in `localStorage`
- [ ] In relay mode, the stored assistant message is the RAG text

**Done when:** you can switch between typing and talking in one conversation without losing context.

### Phase 5: Robustness
- [ ] Session limits: on `GoAway`, reconnect with the resumption handle; turn on context compression
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
