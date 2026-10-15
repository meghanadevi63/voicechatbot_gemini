"""Spike (STT_TTS_PIPELINE_PLAN.md phase 0): Transcribe 3.5 Live + Flash TTS 3.8 with our API key.

TTS: speaks an English answer (time to first chunk, total time, chunk format) and a Hindi sentence.
STT: one Transcribe Live session (automatic VAD) gets, in real time and 40 ms chunks:
  q00, 1.0 s silence, q01   (does a short pause split one turn into two finals?)
  3 s silence, the Hindi TTS audio, 3 s silence
Every interim/final message is printed with its time relative to the end of the speech segment it follows.

3 API uses in total (2 TTS calls, 1 transcription session). Outputs in data/stt_tts_spike/.

    python -m scripts.stt_tts_spike
"""

import argparse
import asyncio
import base64
import json
import os
import time
import wave
from pathlib import Path

from dotenv import load_dotenv

from scripts.live_spike import CHUNK_MS, IN_RATE, OUT_DIR as SPIKE_DIR, chunks, load_wav, resample

OUT_DIR = Path("data/stt_tts_spike")
TTS_RATE = 24000
EN_ANSWER = (
    "The habit loop has four steps: cue, craving, response and reward. The cue triggers a craving, which "
    "motivates a response, and the reward satisfies the craving. Over time this loop turns behaviour into habit."
)
HI_QUESTION = "आदत बनाने के चार नियम क्या हैं?"  # "What are the four rules for building a habit?"


def save_wav(path: Path, pcm: bytes, rate: int):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)


async def tts(client, model: str, voice: str, text: str, label: str) -> dict:
    """Streams one TTS request; returns timings and the PCM."""
    started = time.monotonic()
    stream = await client.aio.interactions.create(
        model=model,
        input=text,
        response_format={"type": "audio"},
        generation_config={"speech_config": [{"voice": voice}]},
        stream=True,
    )
    first = None
    pcm = b""
    event_types = {}
    sizes = []
    async for event in stream:
        kind = getattr(event, "event_type", type(event).__name__)
        event_types[kind] = event_types.get(kind, 0) + 1
        delta = getattr(event, "delta", None)
        if kind == "step.delta" and getattr(delta, "type", None) == "audio":
            data = base64.b64decode(delta.data) if isinstance(delta.data, str) else delta.data
            if first is None:
                first = time.monotonic() - started
                print(f"  [{label}] first audio chunk after {first:.2f}s (mime={getattr(delta, 'mime_type', None)})")
            pcm += data
            sizes.append(len(data))
    total = time.monotonic() - started
    audio_s = len(pcm) / 2 / TTS_RATE
    print(f"  [{label}] done in {total:.2f}s: {audio_s:.1f}s of audio in {len(sizes)} chunks; events {event_types}")
    save_wav(OUT_DIR / f"tts_{label}.wav", pcm, TTS_RATE)
    return {"label": label, "first_chunk_s": first, "total_s": total, "audio_s": audio_s,
            "chunks": len(sizes), "events": event_types, "pcm": pcm}


async def stt(client, model: str, segments: list[tuple[str, bytes]], tail_s: float) -> list[dict]:
    """segments: (label, 16 kHz PCM; label "" = silence). Streams them in real time to one session."""
    from google.genai import types

    config = types.LiveConnectConfig(
        response_modalities=["TEXT"],
        input_audio_transcription=types.AudioTranscriptionConfig(language_codes=[]),
    )
    log = []
    marks = {}  # label -> monotonic time its speech ended
    last_speech = [None, None]  # (label, end time)

    async with client.aio.live.connect(model=model, config=config) as session:
        t0 = time.monotonic()

        async def send():
            for label, pcm in segments:
                for chunk in chunks(pcm, IN_RATE, CHUNK_MS):
                    await session.send_realtime_input(
                        audio=types.Blob(data=chunk, mime_type=f"audio/pcm;rate={IN_RATE}")
                    )
                    await asyncio.sleep(CHUNK_MS / 1000)
                if label:
                    marks[label] = time.monotonic()
                    last_speech[:] = [label, marks[label]]
                    print(f"  {time.monotonic() - t0:6.2f}s  -- end of speech: {label}")
            await asyncio.sleep(tail_s)

        async def receive():
            while True:
                async for msg in session.receive():
                    sc = msg.server_content
                    now = time.monotonic()
                    rel = f"{last_speech[0]}+{now - last_speech[1]:.2f}s" if last_speech[1] else "during 1st"
                    if sc is None:
                        other = msg.model_dump(exclude_none=True)
                        print(f"  {now - t0:6.2f}s  other: {json.dumps(other, default=str)[:200]}")
                        continue
                    for kind in ("interim_input_transcription", "input_transcription"):
                        tr = getattr(sc, kind)
                        if tr:
                            entry = {"t": round(now - t0, 2), "after": rel, "kind": kind.split("_")[0],
                                     **tr.model_dump(exclude_none=True)}
                            log.append(entry)
                            print(f"  {now - t0:6.2f}s  [{rel}] {json.dumps(entry, ensure_ascii=False)}")
                    rest = sc.model_dump(exclude_none=True,
                                         exclude={"interim_input_transcription", "input_transcription"})
                    if rest:
                        print(f"  {now - t0:6.2f}s  server_content: {json.dumps(rest, default=str)[:200]}")

        receiver = asyncio.create_task(receive())
        try:
            await send()
        finally:
            receiver.cancel()
            await asyncio.gather(receiver, return_exceptions=True)
    return log


async def main(args):
    from google import genai

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

    print(f"TTS ({args.tts_model}, voice {args.voice})")
    en = await tts(client, args.tts_model, args.voice, EN_ANSWER, "en")
    hi = await tts(client, args.tts_model, args.voice, HI_QUESTION, "hi")

    silence = lambda s: b"\0\0" * int(IN_RATE * s)
    segments = [
        ("q00", load_wav(SPIKE_DIR / "q00.wav")),
        ("", silence(1.0)),
        ("q01", load_wav(SPIKE_DIR / "q01.wav")),
        ("", silence(3.0)),
        ("hi", resample(hi["pcm"], TTS_RATE, IN_RATE)),
    ]
    print(f"\nSTT ({args.stt_model}): q00 + 1.0s pause + q01, 3s silence, Hindi")
    log = await stt(client, args.stt_model, segments, tail_s=4.0)

    results = {
        "tts": [{k: v for k, v in r.items() if k != "pcm"} for r in (en, hi)],
        "stt": log,
    }
    (OUT_DIR / "results.json").write_text(json.dumps(results, indent=2, ensure_ascii=False))
    print(f"\nSaved {OUT_DIR / 'results.json'} and the TTS audio in {OUT_DIR}/")


if __name__ == "__main__":
    load_dotenv()
    parser = argparse.ArgumentParser()
    parser.add_argument("--stt-model", default=os.environ.get("STT_MODEL", "gemini-3.5-transcribe-live"))
    parser.add_argument("--tts-model", default=os.environ.get("TTS_MODEL", "gemini-3.8-flash-tts"))
    parser.add_argument("--voice", default=os.environ.get("TTS_VOICE", "Sulafat"))
    args = parser.parse_args()
    if not os.environ.get("GEMINI_API_KEY"):
        raise SystemExit("Set GEMINI_API_KEY in .env first.")
    asyncio.run(main(args))
