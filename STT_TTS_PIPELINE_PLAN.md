# STT + TTS voice pipeline plan: Transcribe 3.5 Live + Flash TTS 3.8, RAG answers only

Branch: `feature/stt-tts-pipeline` (from `feature/multilanguage`).

## Goal

A second voice pipeline next to the current Gemini Live one, chosen in the UI:

| Pipeline (UI label) | How it works | Answer modes |
|---|---|---|
| **Gemini 3.1 Flash Live** (current, unchanged) | One Gemini Live session hears, answers (or relays) and speaks | Gemini answers / Read RAG answer |
| **Transcribe 3.5 + Flash TTS 3.8** (new) | `gemini-3.5-transcribe-live` turns speech into text → RAG chain answers → `gemini-3.8-flash-tts` speaks the answer | Read RAG answer only |

What the new pipeline shows off:

- **Live text while the user speaks.** Transcribe Live sends interim text during speech; 3.1 Flash Live and
  3.8 Live only send the user's transcript in one piece after the turn ends.
- **The spoken answer is exactly the RAG answer.** TTS reads the text it is given, so there is no relay drift
  (3.1 Live relay was word for word on 23/23 turns, but that's prompt-dependent).
- **The answer text appears before the audio.** The RAG text goes to the chat as soon as it's ready; the voice
  follows.

Scope: **hands-free only, automatic VAD only** (server detects end of speech). Dictation keeps its current
flow (`POST /transcribe`). A dictated message's spoken reply follows the selected pipeline (step 4.3).

## Pipeline

```
browser mic (16 kHz PCM16, 40 ms chunks)
   │  WebSocket /stt-tts
   ▼
SttTtsBridge (backend/stt_tts.py)
   ├─► Transcribe Live session (automatic VAD) ──► interim text ──► browser caption (live)
   │                                           └─► final text ───► end-of-turn grace ──► question
   ├─► RAGChain.answer(question, history, language)  ──► rag_answer + sources ──► browser chat
   └─► Flash TTS stream (24 kHz PCM16) ──────────────────────────────────────► browser playback
```

The browser keeps the **same event protocol** as `/live` (`ready`, `input_transcript`, `tool_call`,
`rag_answer`, `sources`, `language`, `interrupted`, `turn_complete`, `reconnecting`, `resumed`, `error`, binary
24 kHz audio), plus one new event, `interim_transcript`. `useVoiceSession.js` and the playback worklet are reused
almost as they are.

## Facts from the docs (2026-10-15)

**`gemini-3.5-transcribe-live`** ([Live transcription docs](https://ai.google.dev/gemini-api/docs/live-api/live-transcribe))

- Preview. Uses the Live API (`client.aio.live.connect`) with `response_modalities=["TEXT"]` and
  `input_audio_transcription=AudioTranscriptionConfig(language_codes=[])` (empty = auto-detect, including
  code-switching; 85+ languages).
- Input: 16 kHz mono PCM16, `audio/pcm;rate=16000`, recommended 100 ms chunks (our mic sends 40 ms).
- Automatic VAD is the default. `server_content.interim_input_transcription` = partial text while speaking;
  `server_content.input_transcription` = final text "when the speaker pauses, the turn completes, or speech is
  finalized".
- Sessions stream for up to **10 minutes**.
- Not documented: a `turn_complete`-style flag, a detected-language field, silence-duration settings, session
  resumption, whether interim text is a delta or the full hypothesis.

**`gemini-3.8-flash-tts`** ([Speech generation docs](https://ai.google.dev/gemini-api/docs/speech-generation))

- `client.interactions.create(model=..., response_format={"type": "audio"},
  generation_config={"speech_config": [{"voice": "Kore"}]}, stream=True)` (google-genai ≥ 2.25.0; we have
  2.29.0).
- Streaming: `step.delta` events with base64 audio, raw PCM16, **24 kHz mono**: the same format our playback
  worklet already plays.
- 130+ languages, language auto-detected from the text. Kore is available (same voice as Live today).
- Google Cloud lists 3.8 Flash TTS as preview on the Enterprise API; the Gemini API page has no preview tag.
  Phase 0 confirms it works with our API key.

## Decisions (proposed, open to discussion)

1. **New WebSocket route `/stt-tts`**, separate from `/live`. The Live code stays untouched, so the existing
   pipeline can't regress.
2. **End of turn = final transcript followed by `generation_complete`** (phase 0: ~1.5 s after speech ends; a
   1 s mid-sentence pause doesn't split the turn, so no grace period is needed). Interim text is the full
   hypothesis of the turn so far, so the caption is replaced, not appended. Muting sends `audio_stream_end`,
   because without trailing silence the server never ends the turn.
3. **Language:** the transcriber auto-detects (no hint), and `RAGChain.answer()` already detects the question's
   language and answers in it. The bridge sends the detected language as a `language` event, as `/live` does.
4. **Interrupting the answer (barge-in): yes.** The mic keeps streaming while the answer plays. When an
   interim transcript with real words arrives during playback, the bridge stops TTS and sends `interrupted`;
   the interrupting speech becomes the next question. Browser echo cancellation is on already; phase 0 checks
   the speaker doesn't trigger it.
5. **Speech while the answer is being prepared** (after the turn ends, before audio starts): **ignored.** The
   bridge drops transcripts in that window, so they neither change the question nor show as a caption. Barge-in
   only becomes possible once the first audio chunk has been sent. The caption shows **"Preparing the answer…"**
   in that window so the user knows to wait.
7. **Sidebar:** with the new pipeline selected, the answer-mode setting shows only "Read RAG answer". Both options
   ("Gemini answers" / "Read RAG answer") appear only for the Live pipeline. A dictated message's spoken reply
   follows the selected pipeline (RAG answer read by Flash TTS on the new one).
8. **Voice: `Sulafat`** (warm), so the demo audibly differs from Live's Kore. `TTS_VOICE` in `.env` changes it.
6. **Errors:** the answer text is in the chat before TTS starts, so a TTS failure only loses the audio: retry
   once, then show "couldn't speak the answer". A dropped or expiring transcriber connection reconnects with a
   fresh session (it keeps no conversation state, so no resumption handle is needed); the conversation history
   lives in the bridge.

## Phases

### Phase 0: spike (prove both models with our key)

`scripts/stt_tts_spike.py`: Flash TTS speaks the test questions (testing TTS), and that audio is streamed to
Transcribe Live in real time (testing STT). 3 TTS calls + 1 transcription session in total. Results go in
`STT_TTS_SPIKE_RESULTS.md`. **Stop after phase 0 and review the results before building.**

Done 2026-10-15; see [STT_TTS_SPIKE_RESULTS.md](STT_TTS_SPIKE_RESULTS.md).

- [x] 0.1 Transcribe Live connects with our key.
- [x] 0.2 Interim text is the full hypothesis of the turn (replace the caption).
- [x] 0.3 Final ~1.5 s after speech, then `generation_complete`; a 1 s pause doesn't split the turn.
- [x] 0.4 No language field; Hindi interim text in Devanagari.
- [x] 0.5 40 ms chunks work.
- [x] 0.6 Async streaming TTS works; first chunk 9.6 s for a 3-sentence answer, 4.4 s for one sentence →
      sentence-by-sentence TTS is required.
- [ ] 0.7 Rate limits: not measured (OK for the demo, per the user).

Keep the runs short: the spike shares the quota with the running app.

Phases 1–4 built 2026-10-15 (import and frontend build checked; browser check pending).

### Phase 1: backend building blocks

- [x] 1.1 [backend/config.py](backend/config.py): `stt_model = "gemini-3.5-transcribe-live"`,
      `tts_model = "gemini-3.8-flash-tts"`, `tts_voice = "Sulafat"`;
      [.env.example](.env.example) to match.
- [x] 1.2 `backend/tts.py`: `async def speak(text) -> AsyncIterator[bytes]`, which yields 24 kHz PCM16 chunks
      from the streaming call (decoded from base64). Splits the text into the first sentence and the rest, two
      parallel requests (2 TTS calls per turn, to spare the quota), so the first audio comes after one sentence's
      synthesis (~4 s), not the whole answer's (~9 s, phase 0). Logs time to first chunk.

### Phase 2: the bridge (`backend/stt_tts.py`) and route

- [x] 2.1 `SttTtsBridge(ws, rag, language)`: receives the history message first (same as `LiveBridge`).
- [x] 2.2 Opens the transcriber session (TEXT modality, auto-detect, automatic VAD) and forwards browser audio
      to it.
- [x] 2.3 Interim → `interim_transcript` event (full text so far); final → `input_transcript` event;
      `generation_complete` after a final → end of turn.
- [x] 2.4 Turn end → `tool_call` event (the browser shows "thinking") → `rag.answer()` in a thread → `language`,
      `rag_answer`, `sources` events → stream TTS audio as binary frames → `turn_complete`. Append the turn to
      the history.
- [x] 2.5 [backend/main.py](backend/main.py): `@app.websocket("/stt-tts")` with the same origin and API-key
      checks as `/live`.
- [x] 2.6 Log per turn: end of speech → final transcript, RAG time, TTS first chunk, total. These are the
      numbers to compare against the Live pipeline in the demo.

### Phase 3: frontend pipeline switch

- [x] 3.1 [Sidebar.jsx](frontend/src/components/Sidebar.jsx): a new "Voice pipeline" choice, "Gemini 3.1 Flash
      Live" (default) or "Transcribe 3.5 + Flash TTS 3.8". With the new pipeline, the answer-mode list shows
      only "Read RAG answer"; with Live, both options.
- [x] 3.4 Caption: "Preparing the answer…" from the end of the question until the first audio.
- [x] 3.2 [App.jsx](frontend/src/App.jsx): save `pipeline` with the voice settings (localStorage). Switching
      pipeline mid-conversation restarts hands-free with the history, as switching answer mode does now.
- [x] 3.3 [useVoiceSession.js](frontend/src/voice/useVoiceSession.js): `start({pipeline, ...})` picks `/live`
      or `/stt-tts`; handle `interim_transcript` (the caption shows the finalized text so far plus the current
      interim text, styled lighter).

Checkpoint (manual, in the browser): a hands-free conversation in English, then Hindi or Telugu, on the new
pipeline. Live text appears while speaking, and the spoken answer matches the chat text.

### Phase 4: interruption, reconnects, dictation replies

- [x] 4.1 Barge-in (decision 4): cancel TTS, `interrupted`, the browser clears playback. The chat keeps the
      full RAG text, marked interrupted, the same as relay mode now.
- [x] 4.2 Speech during "thinking" (decision 5): drop transcripts until the first audio chunk is sent.
- [x] 4.3 Spoken reply to a dictated message: `{type: "text"}` with no mic skips the transcriber and runs RAG +
      TTS, so dictation follows the chosen pipeline too.
- [x] 4.4 Reconnect the transcriber on close, error or before the 10-minute cap (`reconnecting`/`resumed`
      events, with the delays `RECONNECT_DELAYS_S` already uses); keep the finals of an unfinished turn.

### Phase 5: latency polish and docs

- [ ] 5.1 If first audio is still too slow with sentence splitting: try `TTS_MODEL=gemini-3.8-flash-lite-tts`.
- [ ] 5.2 [VOICE_FEATURES.md](VOICE_FEATURES.md) and [README.md](README.md): the new pipeline, its settings,
      the measured latencies next to the Live pipeline, and its limits.
- [ ] 5.3 Manual demo checklist (below).

### Added 2026-10-15: third pipeline, Transcribe 3.5 + Live 3.1 voice (`stt-live`)

Flash TTS was too slow to start speaking in practice (5 s first audio; at times no answer in 40 s), so a third
sidebar option keeps Transcribe 3.5 for the live text and has a Gemini Live session read the RAG answer
([backend/live_reader.py](backend/live_reader.py); `/stt-tts?speaker=live`). The reader session is connected
ahead of time and reused; an interrupted answer replaces it.

Measured over WebSocket (3 sessions, recorded questions):

| | Flash TTS (`stt-tts`) | Live reader (`stt-live`) |
|---|---|---|
| First audio after the RAG answer | 5.0 s | 0.6 to 1.2 s |
| Reading matches the RAG text | exact (it's TTS) | 1.000 word similarity, 3/3 turns |
| Barge-in | not yet tested | answer stopped ~1 s after the user started talking; next question answered |

RAG itself: 2.0 to 2.3 s when warm, 9 to 10 s on the first question after a backend start.

## Demo checklist (manual)

- [ ] Same question on both pipelines: compare the live caption, time to first audio, and spoken vs chat text.
- [ ] A question with a mid-sentence pause: sent as one question.
- [ ] Interrupt a long answer by speaking.
- [ ] Hindi and Telugu questions: live text in the native script, answer spoken in the same language.
- [ ] Switch pipeline mid-conversation: the history carries over.
- [ ] Dictated message with each pipeline selected.

## Risks

- **Both models are new and may be preview:** behaviour and names can change. The pipeline is opt-in, and the
  Live pipeline stays the default.
- **Latency:** three steps in a row (transcribe finalization + RAG ~1–2 s + TTS first chunk) may be slower than
  Live relay. Phase 2.6 measures it; 5.1 is the fallback.
- **Quota:** each turn uses a Groq routing call, a Gemini answer and a TTS request, plus the transcriber
  session. Gemini free tier is 15 req/min, so a fast demo conversation can hit it.
- **Echo self-interruption:** if speakers are loud, the TTS audio may be transcribed as user speech. Mitigations:
  ignore interim text in the first ~300 ms of playback, require at least two words; use headphones for the demo.
- **Interim text format unknown** until phase 0 (delta vs full hypothesis).

## Testing

Token budget is tight: development comes first, testing is kept to the minimum. One spike run in phase 0,
an import/build check after each phase, and manual browser checks by the user at each checkpoint. No automated
tests unless one becomes necessary (ask first, and keep it small: it shares the live app's quota).
