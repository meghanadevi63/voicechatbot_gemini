# STT + TTS spike results (phase 0)

Run 2026-10-15, `python -m scripts.stt_tts_spike`, one run (2 TTS calls, 1 transcription session).
Raw log: `data/stt_tts_spike/results.json`; TTS audio: `data/stt_tts_spike/tts_en.wav`, `tts_hi.wav`.

## Transcribe Live (`gemini-3.5-transcribe-live`)

| Question | Result |
|---|---|
| Works with our key? | Yes |
| Interim text | **Full hypothesis of the whole turn so far**, not a delta: replace the caption, don't append. About every 0.5 s, first word ~0.75 s after speech starts. |
| Interim quality | Words appear while speaking; punctuation and casing are fixed up once the speech ends ("Habit Loop?" → "habit loop?"). |
| End of turn | Final `input_transcription` **~1.5 s after the end of speech**, immediately followed by `server_content.generation_complete: true`. That is a clear end-of-turn signal. |
| 1.0 s mid-sentence pause | **Not split**: q00 + 1 s pause + q01 came back as one final. The grace period (decision 2) isn't needed. |
| Language field | None. `language_code` and `finished` exist in the SDK type but weren't sent. `RAGChain.answer()` detects the language, as planned. |
| Hindi | Interim text in Devanagari, correct ("आदत बनाने के चार नियम क्या"). |
| Audio stopping without silence | When the audio ended with no trailing silence, **no final came** (the last Hindi word and the final never arrived within 4 s). The server VAD needs silence frames to end the turn. In the app the mic keeps streaming; when muted, the bridge must send `audio_stream_end`. |
| 40 ms chunks | Accepted (the docs recommend 100 ms; no batching needed). |

## Flash TTS (`gemini-3.8-flash-tts`, voice Sulafat)

| Text | First audio chunk | Total | Audio length |
|---|---|---|---|
| English answer, 3 sentences (213 chars) | **9.6 s** | 15.2 s | 15.9 s |
| Hindi question, 1 short sentence | **4.4 s** | 6.9 s | 2.9 s |

- Streaming works: `aio.interactions.create(..., stream=True)`, `step.delta` events, `audio/l16` = raw PCM16
  24 kHz mono (plays in our existing worklet as is).
- Once audio starts, it arrives faster than real time (no playback gaps expected).
- **Time to first audio grows with text length.** With the full answer sent at once, a typical turn would be
  ~1.5 s (STT final) + ~1.5–2 s (RAG) + ~9 s (TTS) ≈ **12 s of silence**. Too slow for a demo.

## Design changes from these results

1. **End of turn = `generation_complete`** after a final transcript; drop the 600 ms grace period
   (`stt_end_of_turn_ms`).
2. **Caption = latest interim text** (replace, not append).
3. **Sentence-by-sentence TTS moves from phase 5 into phase 2** (required, not optional): synthesize sentence 1
   and start playing it while the next sentences are synthesized in order. Expected first audio: ~4 s after the
   RAG answer instead of ~9 s.
4. **Muting sends `audio_stream_end`** so the turn still ends.
5. Optional: `TTS_MODEL=gemini-3.8-flash-lite-tts` as a lower-latency fallback (not tested yet; one call to try).
