"""Phase 1 test: drive the backend's /live WebSocket like the browser will, using recorded questions.

Needs the spoken questions from the Phase 0 spike (data/live_spike/q*.wav) and a running backend.

    python -m scripts.live_ws_test --url ws://localhost:8002/live            # all four mode combinations
    python -m scripts.live_ws_test --answer rag --talk ptt --questions 2
"""

import argparse
import asyncio
import json
import time
import wave
from pathlib import Path

from websockets.asyncio.client import connect

SPIKE_DIR = Path("data/live_spike")
OUT_DIR = Path("data/live_ws_test")
IN_RATE, OUT_RATE, CHUNK_MS = 16000, 24000, 40

# Seeded as earlier text chat, to check the bridge accepts history
HISTORY = [
    {"role": "user", "content": "Which book are these documents from?"},
    {"role": "assistant", "content": "They're from Atomic Habits by James Clear."},
]


def load_wav(path: Path) -> bytes:
    with wave.open(str(path), "rb") as w:
        return w.readframes(w.getnframes())


def save_wav(path: Path, pcm: bytes) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(OUT_RATE)
        w.writeframes(pcm)


async def collect_turn(ws) -> dict:
    turn = {"heard": "", "said": "", "tools": [], "sources": 0, "audio": bytearray(), "events": []}
    while True:
        frame = await ws.recv()
        if isinstance(frame, bytes):
            if not turn["audio"]:
                turn["first_audio_at"] = time.monotonic()
            turn["audio"].extend(frame)
            continue
        event = json.loads(frame)
        turn["events"].append(event["type"])
        if event["type"] == "input_transcript":
            turn["heard"] += event["text"]
        elif event["type"] == "output_transcript":
            turn["said"] += event["text"]
        elif event["type"] == "tool_call":
            turn["tools"].append(event["name"])
        elif event["type"] == "sources":
            turn["sources"] = len(event["sources"])
        elif event["type"] == "error":
            raise RuntimeError(event["message"])
        elif event["type"] == "turn_complete":
            return turn


async def send_audio(ws, pcm: bytes, silence_s: float = 0) -> None:
    size = IN_RATE * CHUNK_MS // 1000 * 2
    padded = pcm + b"\x00\x00" * int(IN_RATE * silence_s)
    for i in range(0, len(padded), size):
        await ws.send(padded[i : i + size])
        await asyncio.sleep(CHUNK_MS / 1000)  # real time, like a microphone


async def run_combo(url: str, answer: str, talk: str, n: int, history: list[dict]) -> list[dict]:
    results = []
    async with connect(f"{url}?answer={answer}&talk={talk}", max_size=None) as ws:
        await ws.send(json.dumps({"type": "history", "turns": history}))
        ready = json.loads(await asyncio.wait_for(ws.recv(), 30))
        assert ready["type"] == "ready", ready
        for i in range(n):
            pcm = load_wav(SPIKE_DIR / f"q{i:02d}.wav")
            collector = asyncio.create_task(collect_turn(ws))
            if talk == "ptt":
                await ws.send(json.dumps({"type": "activity_start"}))
                await send_audio(ws, pcm)
                await ws.send(json.dumps({"type": "activity_end"}))
            else:
                await send_audio(ws, pcm, silence_s=2)
            speech_end = time.monotonic() - (2 if talk == "vad" else 0)
            turn = await asyncio.wait_for(collector, 120)
            save_wav(OUT_DIR / f"{answer}_{talk}_{i:02d}.wav", bytes(turn["audio"]))
            first = turn.get("first_audio_at")
            results.append(
                {
                    "heard": turn["heard"].strip(),
                    "said": turn["said"].strip(),
                    "tools": turn["tools"],
                    "sources": turn["sources"],
                    "audio_s": round(len(turn["audio"]) / 2 / OUT_RATE, 1),
                    "first_audio_s": round(first - speech_end, 2) if first else None,
                    "events": sorted(set(turn["events"])),
                }
            )
        await ws.send(json.dumps({"type": "end"}))
    return results


async def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default="ws://localhost:8002/live")
    parser.add_argument("--answer", choices=["agent", "rag"])
    parser.add_argument("--talk", choices=["vad", "ptt"])
    parser.add_argument("--questions", type=int, default=2)
    parser.add_argument("--no-history", action="store_true", help="start without earlier conversation")
    args = parser.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    combos = [
        (a, t)
        for a in ([args.answer] if args.answer else ["agent", "rag"])
        for t in ([args.talk] if args.talk else ["vad", "ptt"])
    ]
    failed = False
    for answer, talk in combos:
        print(f"\n=== answer={answer} talk={talk}", flush=True)
        try:
            for r in await run_combo(args.url, answer, talk, args.questions, [] if args.no_history else HISTORY):
                ok = r["said"] and r["audio_s"] > 0 and r["tools"] and r["sources"]
                failed |= not ok
                print(json.dumps({"ok": bool(ok), **r}, indent=2), flush=True)
        except Exception as e:
            failed = True
            print(f"FAILED: {type(e).__name__}: {e}", flush=True)
    print(f"\nAudio saved to {OUT_DIR}/")
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    asyncio.run(main())
