# Plan: move voice from Gemini 3.1 Flash Live to Gemini 3.8 Live

Branch: `feature/voice_gemini_3.8_live`, created from `feature/voice_chat_gemini` (`53412aa`).
Status: **plan only, nothing changed yet.**

The multilanguage work on `feature/multilanguage` is **out of scope** here. This branch keeps the
English-only behaviour of `53412aa`. See [Multilanguage](#multilanguage-support-in-38-live) at the end.

Rule for this plan: **test first, change second.** Every code change has a check that runs before it
(baseline on 3.1, or a direct model check on 3.8) and a check after it.

---

## 1. Why

- Google marks `gemini-3.1-flash-live-preview` as a legacy preview. `gemini-3.8-live` is the stable,
  recommended Live model.
- 3.1 had two outages of about 8 minutes on 2026-10-09 (`1011 Internal error` on every audio session) and
  slow spells with 20 to 34 s to first audio.
- We also want to re-check whether 3.8 streams the user's transcript while they speak (live dictation text).
  On 3.1 it doesn't. See `data/live_dictation_spike/report.json` (2026-10-12).

## 2. What 3.8 changes (from Google's docs, checked 2026-10-14)

| Area | 3.1 Flash Live (now) | 3.8 Live | Effect on us |
|---|---|---|---|
| Thinking | `thinking_level`, we set `MINIMAL` | Interleaved reasoning; `thinking_level` **not supported** (omit it) | `live_config()` already omits it for non-3.1 models |
| Tool calls | Sequential: the model waits for the result | **Asynchronous (`NON_BLOCKING`) by default**; `BLOCKING` available; scheduling `SILENT` / `WHEN_IDLE` / `INTERRUPTED` | `live_config()` already sets `BLOCKING`. **The spike script does not** (see step 1.1) |
| Proactive audio | Not available | **Always on; setting `proactive_audio: false` is an error** | The model may decide **not to reply** to some input. Risk for relay mode, the dictated-question reply, and small talk |
| Affective dialog | Not available | **Removed from the API** (don't set `enable_affective_dialog`) | Nothing to do. (Earlier notes said it was supported. That was wrong.) |
| `send_client_content` | Initial history only, with `history_config` | Works **for the whole session** with `user` / `model` roles; **`turn_complete=true` interrupts any active generation** | `seed_history()` sends `turn_complete=True`. Fine at session start, but test that `history_config` is still accepted and needed |
| Turn coverage default | Audio activity | `TURN_INCLUDES_AUDIO_ACTIVITY_AND_ALL_VIDEO` | We send no video. Check that VAD and turn-taking timing don't change |
| Response modality | Audio | Audio only; use output transcription for text | Same as now |
| Events | Several parts per event possible | Same | The bridge already handles every part |
| Extended Thinking variant | – | `NON_BLOCKING` tools only; **no blocking, no scheduling** | **Not usable** with our design. Don't point `LIVE_MODEL` at it |
| Session limits | ~10 min connection, 15 min audio session without compression, resumption handle valid 2 h | Same (session guide samples use 3.8) | Phase 5 work applies to both models |
| Input transcription streaming | One piece, **after** the turn ends (our 2026-10-12 test) | **Not documented** | Must be tested (step 2.3) |
| Languages | 99, auto-detected | 99, auto-detected, no explicit language code | See [Multilanguage](#multilanguage-support-in-38-live) |

Sources: [Gemini 3.8 Live model page](https://ai.google.dev/gemini-api/docs/models/gemini-3.8-live),
[Live API guide](https://ai.google.dev/gemini-api/docs/live-guide),
[Session management](https://ai.google.dev/gemini-api/docs/live-session).

## 3. Risks to check

| # | Risk | Where it bites | How we check |
|---|---|---|---|
| R1 | Proactive audio: the model stays silent on some turns | Relay prompt ("Do not say anything before calling it"), spoken reply to a dictated question (`{"type":"text"}`), greetings | Count turns with no audio and no tool call (steps 2.1, 3.2) |
| R2 | Tools run without waiting (`NON_BLOCKING`) if `BLOCKING` is missing | The model speaks before the search result arrives, so the answer isn't grounded | Fix the spike first (1.1); check "tool call before first audio" on every turn |
| R3 | `history_config.initial_history_in_client_content` behaves differently or is rejected | Sessions with earlier conversation (every session after the first message) | `history` check in the spike, and `live_ws_test` with history (2.1, 3.2) |
| R4 | `turn_complete=true` interrupts generation | Only if we ever send client content mid-session (we don't today) | Note for Phase 5 (typed messages during hands-free could now be sent into the session) |
| R5 | Different latency or VAD timing | Hands-free feel, interruption speed | Compare with the 3.1 baseline (step 0) |
| R6 | Relay mode stops reading word for word | "Read RAG answer" mode | Relay match score in the spike and backend log |
| R7 | Different voices (`Kore` missing) | Session setup fails | `basic` check |
| R8 | Quota: tests share the live app's Gemini quota (free tier ~15 requests/min) | Your running backend on port 8002 | Keep runs small, run checks one at a time |

## 4. Steps

### Step 0: Baseline on 3.1 (no code change)

Why: 3.1 numbers are from 2026-10-09, and the service varies day to day. Run the same small set **on the same
day** as the 3.8 runs so the comparison is fair.

- [ ] Set up the test backend on a copy of the database (don't touch port 8002):
  ```bash
  cp -r data/milvus.db data/live_spike/milvus_copy.db
  MILVUS_DB_PATH=./data/live_spike/milvus_copy.db uvicorn backend.main:app --port 8003
  ```
- [ ] `python -m scripts.live_spike --checks basic,vad,relay --questions 3`
- [ ] `python -m scripts.live_ws_test --url ws://localhost:8003/live --questions 2`
- [ ] Save the reports as `data/live_spike/report_31_<date>.json` (or copy them aside).

Record: first-audio latency (agent and relay), searches on document questions, relay match score, failures.

### Step 1: Prepare the test tooling (scripts only, app code unchanged)

- [ ] **1.1** `scripts/live_spike.py` `live_config()`: for non-3.1 models, add `behavior: "BLOCKING"` to each
  tool declaration, as `backend/live.py` already does. Without it, 3.8 results aren't comparable (R2).
- [ ] **1.2** Spike and `live_ws_test`: add a per-turn flag "no reply" (no audio and no tool call before
  `turn_complete` or a timeout) to measure R1.
- [ ] **1.3** Confirm `scripts/live_dictation_spike.py` takes `--model` (it does) and uses the fixed
  `live_config()` from 1.1.

Test: rerun `--checks basic` on 3.1 to make sure the script changes didn't break anything.

### Step 2: Direct model checks on 3.8 (no app change)

- [ ] **2.1** `python -m scripts.live_spike --model gemini-3.8-live --checks basic,history,vad,ptt,relay --questions 3`

  | Check | Pass when |
  |---|---|
  | basic | Session opens with `Kore`, spoken reply plus transcript |
  | history | Seeded turns remembered; no stall after seeding (first audio within ~3 s) |
  | vad (agent) | Searches on every document question, **before** speaking; small talk answered |
  | ptt | `activity_start` / `activity_end` still work (backend keeps `talk=ptt`) |
  | relay | Tool called every turn; match score ≥ 0.95; no silent turns |

- [ ] **2.2** Try the `{"type":"text"}` path: send a question as realtime text and confirm a spoken answer
  (this is how dictated questions get their spoken reply). Watch for silent turns (R1).
- [ ] **2.3** Live dictation question:
  `python -m scripts.live_dictation_spike --model gemini-3.8-live --cases ptt_open,ptt_end,vad`

  | Result | Meaning |
  |---|---|
  | `inputs_before_speech_end > 0` in `ptt_open` or `vad` | 3.8 streams the user transcript. A Live-based dictation preview is possible |
  | `0`, like 3.1 | Not possible. Keep `/transcribe`; consider the Web Speech API for a preview |

**Decision gate A:** if 2.1 or 2.2 fails in a way a prompt or config change can't fix, stop and stay on 3.1.

### Step 3: Switch the app to 3.8

Code changes (small, expected):

- [ ] **3.1a** `.env`: `LIVE_MODEL=gemini-3.8-live` (your own copy; the test backend can take it as an env var)
- [ ] **3.1b** `backend/config.py:26` and `.env.example:21`: default to `gemini-3.8-live`
- [ ] **3.1c** `backend/live.py`: keep the `is_31` branches, so switching back is one env var. Update the
  comment at line 278 ("3.1 Live") to say it applies to both.
- [ ] **3.1d** Only if step 2 showed silent turns (R1): tighten `RELAY_PROMPT` and `AGENT_PROMPT` to say
  "always respond to every user message". Re-run 2.1 after any prompt change.
- [ ] **3.1e** Only if step 2 showed a `history_config` problem (R3): adjust `live_config()` / `seed_history()`.

Tests:

- [ ] **3.2** Test backend with 3.8:
  `LIVE_MODEL=gemini-3.8-live MILVUS_DB_PATH=./data/live_spike/milvus_copy.db uvicorn backend.main:app --port 8003`,
  then run `python -m scripts.live_ws_test --url ws://localhost:8003/live` for each answer and talk mode,
  with and without history (`--no-history`). Pass: every turn returns audio, transcripts, a tool call and sources.
- [ ] **3.3** Browser checks (headless Chrome with a fake mic, as in `voice_plan.md`), against
  `BACKEND_URL=http://localhost:8003 npx vite --port 5175`:
  1. Dictation fills the box, Send gives a spoken answer, the voice bar closes itself
  2. Send while dictating waits for the last words
  3. Hands-free 3-turn conversation: Listening → Searching → Speaking → Listening
  4. Interruption: playback stops in ≤ 0.5 s, second question answered
  5. Switching the answer mode mid-conversation reconnects with history
  6. Relay mode: saved message equals `rag_answer`
  7. New chat ends the session
- [ ] **3.4** Manual check on real hardware: hands-free with speakers (echo), and a short interruption test.

**Decision gate B:** compare with the step 0 baseline. Switch the default only if 3.8 is at least as
reliable, and no slower than about +0.5 s to first audio.

### Step 4: Docs

- [ ] README: model name, the known-issues line about 3.1 outages
- [ ] `voice_plan.md`: tick Phase 6, add results
- [ ] `GEMINI_LIVE_MODEL.md`: retitle to 3.8, fix section 13 (affective dialog removed, proactive audio
  always on, `send_client_content` behaviour)
- [ ] `VOICE_FEATURES.md`: remove the multilanguage parts that don't exist on this branch, update the model
  and numbers
- [ ] `RESUME_PROMPT.txt`: new state

### Step 5 (optional, after the switch): live dictation preview

Only if step 2.3 shows 3.8 streams input transcription. Otherwise use the Web Speech API (Chrome and Edge
only) for a word-by-word preview, with the `/transcribe` text replacing it at each pause. Needs its own
small plan.

## 5. Rollback

One setting: `LIVE_MODEL=gemini-3.1-flash-live-preview`. The `is_31` branches in `live_config()` stay, so
both models keep working.

## 6. Test cost

Roughly 30 to 40 Live sessions in total (steps 0 to 3), plus a few `/transcribe` calls. Run them in small
batches, not all at once, because they share quota with your backend on port 8002.

---

## Multilanguage support in 3.8 Live

What Google documents (Live API guide, applies to the native-audio Live models including 3.8):

- **99 languages**, listed with BCP-47 codes (for example `en`, `hi`, `te`, `pt-BR`, `zh-Hans`).
- **Detected automatically.** The model "can switch between languages naturally during conversation".
- **You can't set a language code** on native-audio models. To limit languages, say so in the system
  instruction (for example "Always reply in English").
- **Transcripts:** the output transcript's language "is inferred from the model's response". Input
  transcription follows the spoken language.
- The 3.8 model page adds nothing language-specific beyond this.

What it means for this branch (English documents, English prompts):

| Mode | Likely behaviour if a user speaks, say, Hindi | Gap |
|---|---|---|
| Agent | Gemini probably answers in Hindi. The search query may be Hindi, and the English MiniLM index retrieves poorly | Needs "search in English" in the prompt |
| Relay | Groq writes an English answer and Gemini reads it in English | Language mismatch |
| Relay match score | `similarity()` in `live.py` only counts `[a-z0-9']` words | Meaningless for non-Latin scripts |
| Dictation | `/transcribe` (Flash Lite) is a separate model, not affected by the switch | – |

`feature/multilanguage` already solves these on top of 3.1: English search queries, answers in the user's
language, and language hints for `/transcribe` and `/live`. Recommended order: finish this switch on the
English-only base, then merge or rebase `feature/multilanguage` onto it and rerun its multilanguage tests
on 3.8. Expect conflicts in `backend/live.py` and `frontend/src/voice/useVoiceSession.js`.
