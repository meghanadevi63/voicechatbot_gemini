"""Phase 0 spike: check what the Gemini Live model supports before building the voice feature.

Checks (run all by default, or pick with --checks):
  basic    text in -> spoken reply, output transcription, latency
  speech   make spoken test questions (Gemini reads each question aloud), used by the checks below
  history  earlier turns seeded with send_client_content are remembered
  vad      hands-free: real-time audio + automatic voice detection, agent mode (search_documents tool)
  ptt      push-to-talk: automatic detection off, manual activity_start / activity_end
  relay    hands-free, relay mode: ask_knowledge_base runs the existing RAG chain and Gemini
           reads the answer; reports how closely the spoken words match the RAG text

Uses a copy of the Milvus Lite database, so the backend can keep running.
Writes WAV files and report.json to data/live_spike/.

    python -m scripts.live_spike
    python -m scripts.live_spike --checks basic,history --questions 3
"""

import argparse
import asyncio
import json
import os
import re
import shutil
import time
import traceback
import wave
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path

import numpy as np
from dotenv import load_dotenv

OUT_DIR = Path("data/live_spike")
IN_RATE = 16000  # Live API input: 16-bit PCM, 16 kHz, mono
OUT_RATE = 24000  # Live API output: 16-bit PCM, 24 kHz, mono
CHUNK_MS = 40

QUESTIONS = [
    "What is the habit loop?",
    "What are the four laws of behavior change?",
    "What is habit stacking?",
    "What does the book say about identity-based habits?",
    "What is the two-minute rule?",
    "What is the plateau of latent potential?",
    "How does environment design affect our habits?",
    "What is temptation bundling?",
    "What is the Goldilocks rule?",
    "Why do small habits make such a big difference?",
]
SMALL_TALK = "Hi there, how are you doing today?"

AGENT_PROMPT = """You are a friendly voice assistant that answers questions about the user's documents.

Rules:
- For any question or request for information, call search_documents first with a short standalone search
  query (use the conversation to resolve follow-ups like "tell me more" or "what about the second one").
- Answer only from what search_documents returns. Do not use outside knowledge or guess.
- If the results don't contain the answer, say so plainly, for example: "I couldn't find that in the documents."
  If they contain part of the answer, give that part and say what is missing.
- For purely social messages (greetings, thanks, goodbyes) reply briefly and warmly without searching.
- Speak in natural sentences: no lists, markdown or URLs. Keep it to two to four sentences unless the user
  asks for more detail.
- Don't mention "excerpts", "search results" or the tool. If it helps, name the source document naturally."""

RELAY_PROMPT = """You are the voice of a document assistant. You never answer anything yourself.

For every user message, including greetings and small talk, call ask_knowledge_base with the user's message
exactly as they said it. Do not say anything before calling it.

When it returns, read the "answer" text aloud word for word. Do not add, remove, summarize or rephrase
anything, and do not add an introduction or a follow-up question."""

TTS_PROMPT = """You are a text-to-speech reader. Read the user's text aloud exactly as written, word for word,
in a natural speaking voice. Never answer it, comment on it or add anything."""

SEARCH_TOOL = {
    "name": "search_documents",
    "description": "Search the user's documents and return the most relevant excerpts.",
    "parameters": {
        "type": "OBJECT",
        "properties": {"query": {"type": "STRING", "description": "Standalone search query"}},
        "required": ["query"],
    },
}
RELAY_TOOL = {
    "name": "ask_knowledge_base",
    "description": "Get the answer to the user's message from the document assistant.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "question": {"type": "STRING", "description": "The user's message, exactly as said"}
        },
        "required": ["question"],
    },
}


# --- audio helpers ---------------------------------------------------------------------------


def save_wav(path: Path, pcm: bytes, rate: int) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)


def load_wav(path: Path) -> bytes:
    with wave.open(str(path), "rb") as w:
        return w.readframes(w.getnframes())


def resample(pcm: bytes, src: int, dst: int) -> bytes:
    x = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    n = int(len(x) * dst / src)
    y = np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x)
    return np.clip(y, -32768, 32767).astype(np.int16).tobytes()


def chunks(pcm: bytes, rate: int, ms: int = CHUNK_MS):
    size = rate * ms // 1000 * 2
    for i in range(0, len(pcm), size):
        yield pcm[i : i + size]


def seconds(pcm: bytes, rate: int) -> float:
    return len(pcm) / 2 / rate


def words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", text.lower())


def similarity(a: str, b: str) -> float:
    return round(SequenceMatcher(None, words(a), words(b)).ratio(), 3)


# --- Live session helpers --------------------------------------------------------------------


@dataclass
class Turn:
    audio: bytearray = field(default_factory=bytearray, repr=False)
    input_text: str = ""
    output_text: str = ""
    tool_calls: list = field(default_factory=list)
    tool_results: list = field(default_factory=list)
    first_audio_at: float | None = None
    interrupted: bool = False
    parts_per_event: int = 0  # max parts seen in one server event (3.1 can bundle several)


def live_config(types, model: str, voice: str, system: str, tools=None, ptt=False):
    config = dict(
        response_modalities=["AUDIO"],
        system_instruction=system,
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice)
            )
        ),
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
    )
    if "3.1" in model:
        config["thinking_config"] = types.ThinkingConfig(thinking_level="MINIMAL")
    if tools:
        config["tools"] = [types.Tool(function_declarations=tools)]
    if ptt:
        config["realtime_input_config"] = types.RealtimeInputConfig(
            automatic_activity_detection=types.AutomaticActivityDetection(disabled=True)
        )
    return types.LiveConnectConfig(**config)


async def collect_turn(session, types, tool_handler=None) -> Turn:
    """Read server events until the model finishes its turn, answering tool calls on the way."""
    turn = Turn()
    async for msg in session.receive():
        if msg.tool_call:
            responses = []
            for fc in msg.tool_call.function_calls:
                turn.tool_calls.append({"name": fc.name, "args": dict(fc.args or {})})
                result = await tool_handler(fc.name, dict(fc.args or {}))
                turn.tool_results.append(result)
                responses.append(types.FunctionResponse(id=fc.id, name=fc.name, response=result))
            await session.send_tool_response(function_responses=responses)

        sc = msg.server_content
        if not sc:
            continue
        if sc.model_turn and sc.model_turn.parts:
            turn.parts_per_event = max(turn.parts_per_event, len(sc.model_turn.parts))
            for part in sc.model_turn.parts:
                if part.inline_data and part.inline_data.data:
                    if turn.first_audio_at is None:
                        turn.first_audio_at = time.monotonic()
                    turn.audio.extend(part.inline_data.data)
        if sc.input_transcription and sc.input_transcription.text:
            turn.input_text += sc.input_transcription.text
        if sc.output_transcription and sc.output_transcription.text:
            turn.output_text += sc.output_transcription.text
        if sc.interrupted:
            turn.interrupted = True
    return turn


async def stream_audio(session, types, pcm: bytes, realtime: bool, trailing_silence_s: float = 0):
    """Send PCM in small chunks like a microphone would. Returns the time the speech ended."""
    blob = lambda data: types.Blob(data=data, mime_type=f"audio/pcm;rate={IN_RATE}")
    for chunk in chunks(pcm, IN_RATE):
        await session.send_realtime_input(audio=blob(chunk))
        if realtime:
            await asyncio.sleep(CHUNK_MS / 1000)
    speech_end = time.monotonic()
    silence = b"\x00\x00" * int(IN_RATE * trailing_silence_s)
    for chunk in chunks(silence, IN_RATE):
        await session.send_realtime_input(audio=blob(chunk))
        await asyncio.sleep(CHUNK_MS / 1000)
    return speech_end


def latency(turn: Turn, start: float) -> float | None:
    return round(turn.first_audio_at - start, 2) if turn.first_audio_at else None


# --- checks ----------------------------------------------------------------------------------


class Spike:
    def __init__(self, args):
        from google import genai
        from google.genai import types

        self.args = args
        self.types = types
        self.client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
        self.model = args.model
        self.voice = args.voice
        self.questions = QUESTIONS[: args.questions]
        self._rag = None

    def connect(self, system, tools=None, ptt=False):
        config = live_config(self.types, self.model, self.voice, system, tools, ptt)
        return self.client.aio.live.connect(model=self.model, config=config)

    @property
    def rag(self):
        if self._rag is None:
            from backend.rag import RAGChain

            self._rag = RAGChain()
        return self._rag

    async def search_tool(self, name, args):
        from backend.rag import format_docs

        docs = await asyncio.to_thread(self.rag.retriever.invoke, args.get("query", ""))
        return {"result": format_docs(docs)}

    async def basic(self):
        async with self.connect("You are a helpful voice assistant. Answer in one short sentence.") as s:
            start = time.monotonic()
            await s.send_realtime_input(text="Hello! Tell me one fun fact about honeybees.")
            turn = await asyncio.wait_for(collect_turn(s, self.types), 60)
        save_wav(OUT_DIR / "basic.wav", bytes(turn.audio), OUT_RATE)
        return {
            "ok": bool(turn.audio) and bool(turn.output_text),
            "audio_s": round(seconds(turn.audio, OUT_RATE), 2),
            "first_audio_s": latency(turn, start),
            "transcript": turn.output_text.strip(),
            "max_parts_per_event": turn.parts_per_event,
        }

    async def speech(self):
        """Have Gemini read each test question aloud; cache as 16 kHz WAV for the audio checks."""
        results = []
        for i, text in enumerate([*self.questions, SMALL_TALK]):
            path = OUT_DIR / f"q{i:02d}.wav"
            if path.exists():
                results.append({"text": text, "cached": True})
                continue
            # The Live model sometimes answers the question instead of reading it: retry until it reads it
            for attempt in range(1, 6):
                async with self.connect(TTS_PROMPT) as s:
                    await s.send_realtime_input(text=f'Read aloud: "{text}"')
                    turn = await asyncio.wait_for(collect_turn(s, self.types), 60)
                match = similarity(text, turn.output_text)
                if match > 0.9:
                    break
            save_wav(path, resample(bytes(turn.audio), OUT_RATE, IN_RATE), IN_RATE)
            results.append(
                {"text": text, "spoken": turn.output_text.strip(), "match": match, "attempts": attempt}
            )
        return {"ok": all(r.get("cached") or r["match"] > 0.8 for r in results), "items": results}

    def question_audio(self):
        audio = [load_wav(OUT_DIR / f"q{i:02d}.wav") for i in range(len(self.questions))]
        return audio, load_wav(OUT_DIR / f"q{len(self.questions):02d}.wav")

    async def history(self):
        T = self.types
        async with self.connect("You are a helpful voice assistant. Answer in one short sentence.") as s:
            await s.send_client_content(
                turns=[
                    T.Content(role="user", parts=[T.Part(text="My favourite colour is teal and my dog is called Biscuit.")]),
                    T.Content(role="model", parts=[T.Part(text="Lovely, teal and Biscuit. I'll remember that.")]),
                ],
                turn_complete=False,
            )
            await s.send_realtime_input(text="What's my dog's name and my favourite colour?")
            turn = await asyncio.wait_for(collect_turn(s, T), 60)
        text = turn.output_text.lower()
        return {"ok": "teal" in text and "biscuit" in text, "transcript": turn.output_text.strip()}

    async def vad(self):
        audio, small_talk = self.question_audio()
        items = []
        async with self.connect(AGENT_PROMPT, tools=[SEARCH_TOOL]) as s:
            for text, pcm in [(SMALL_TALK, small_talk), *zip(self.questions, audio)]:
                collector = asyncio.create_task(collect_turn(s, self.types, self.search_tool))
                speech_end = await stream_audio(s, self.types, pcm, realtime=True, trailing_silence_s=2)
                turn = await asyncio.wait_for(collector, 90)
                save_wav(OUT_DIR / f"vad_answer{len(items):02d}.wav", bytes(turn.audio), OUT_RATE)
                items.append(
                    {
                        "asked": text,
                        "heard": turn.input_text.strip(),
                        "tool_calls": turn.tool_calls,
                        "answer": turn.output_text.strip(),
                        "first_audio_s": latency(turn, speech_end),
                    }
                )
        doc_items = items[1:]
        return {
            "ok": all(i["answer"] for i in items),
            "small_talk_skipped_tool": not items[0]["tool_calls"],
            "doc_questions_searched": sum(bool(i["tool_calls"]) for i in doc_items),
            "doc_questions": len(doc_items),
            "median_first_audio_s": median([i["first_audio_s"] for i in doc_items]),
            "items": items,
        }

    async def ptt(self):
        audio, _ = self.question_audio()
        T = self.types
        items = []
        async with self.connect(AGENT_PROMPT, tools=[SEARCH_TOOL], ptt=True) as s:
            for text, pcm in list(zip(self.questions, audio))[:3]:
                collector = asyncio.create_task(collect_turn(s, T, self.search_tool))
                await s.send_realtime_input(activity_start=T.ActivityStart())
                await stream_audio(s, T, pcm, realtime=True)
                await s.send_realtime_input(activity_end=T.ActivityEnd())
                released = time.monotonic()
                turn = await asyncio.wait_for(collector, 90)
                save_wav(OUT_DIR / f"ptt_answer{len(items):02d}.wav", bytes(turn.audio), OUT_RATE)
                items.append(
                    {
                        "asked": text,
                        "heard": turn.input_text.strip(),
                        "tool_calls": turn.tool_calls,
                        "answer": turn.output_text.strip(),
                        "first_audio_s": latency(turn, released),
                    }
                )
        return {
            "ok": all(i["answer"] for i in items),
            "median_first_audio_s": median([i["first_audio_s"] for i in items]),
            "items": items,
        }

    async def relay(self):
        audio, small_talk = self.question_audio()
        history: list[dict] = []

        async def ask(name, args):
            question = args.get("question", "")
            started = time.monotonic()
            result = await asyncio.to_thread(self.rag.answer, question, list(history))
            history.extend(
                [{"role": "user", "content": question}, {"role": "assistant", "content": result["answer"]}]
            )
            return {"answer": result["answer"], "rag_s": round(time.monotonic() - started, 2)}

        items = []
        async with self.connect(RELAY_PROMPT, tools=[RELAY_TOOL]) as s:
            for text, pcm in [(SMALL_TALK, small_talk), *zip(self.questions, audio)]:
                collector = asyncio.create_task(collect_turn(s, self.types, ask))
                speech_end = await stream_audio(s, self.types, pcm, realtime=True, trailing_silence_s=2)
                turn = await asyncio.wait_for(collector, 120)
                save_wav(OUT_DIR / f"relay_answer{len(items):02d}.wav", bytes(turn.audio), OUT_RATE)
                rag_answer = " ".join(r.get("answer", "") for r in turn.tool_results)
                items.append(
                    {
                        "asked": text,
                        "heard": turn.input_text.strip(),
                        "tool_calls": turn.tool_calls,
                        "rag_answer": rag_answer,
                        "spoken": turn.output_text.strip(),
                        "fidelity": similarity(rag_answer, turn.output_text) if rag_answer else None,
                        "rag_s": sum(r.get("rag_s", 0) for r in turn.tool_results),
                        "first_audio_s": latency(turn, speech_end),
                    }
                )
        scores = [i["fidelity"] for i in items if i["fidelity"] is not None]
        return {
            "ok": len(scores) == len(items),
            "tool_called": f"{len(scores)}/{len(items)}",
            "mean_fidelity": round(sum(scores) / len(scores), 3) if scores else None,
            "min_fidelity": min(scores) if scores else None,
            "median_first_audio_s": median([i["first_audio_s"] for i in items]),
            "items": items,
        }


def median(values):
    values = sorted(v for v in values if v is not None)
    return values[len(values) // 2] if values else None


def use_db_copy():
    """Point the backend settings at a copy of the Milvus Lite database (the backend holds the original)."""
    src = Path(os.environ.get("MILVUS_DB_PATH", "./data/milvus.db"))
    dst = OUT_DIR / "milvus_copy.db"
    if dst.exists():
        shutil.rmtree(dst) if dst.is_dir() else dst.unlink()
    (shutil.copytree if src.is_dir() else shutil.copy)(src, dst)
    os.environ["MILVUS_DB_PATH"] = str(dst)


async def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--checks", default="basic,speech,history,vad,ptt,relay")
    parser.add_argument("--questions", type=int, default=len(QUESTIONS))
    parser.add_argument("--model", default=os.environ.get("LIVE_MODEL", "gemini-3.1-flash-live-preview"))
    parser.add_argument("--voice", default=os.environ.get("LIVE_VOICE", "Kore"))
    args = parser.parse_args()

    if not os.environ.get("GEMINI_API_KEY"):
        raise SystemExit("Set GEMINI_API_KEY in .env first.")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    checks = [c.strip() for c in args.checks.split(",") if c.strip()]
    if {"vad", "ptt", "relay"} & set(checks):
        use_db_copy()

    spike = Spike(args)
    report = {"model": args.model, "voice": args.voice}
    for name in checks:
        print(f"\n=== {name}", flush=True)
        started = time.monotonic()
        try:
            result = await getattr(spike, name)()
        except Exception as e:
            traceback.print_exc()
            result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        result["took_s"] = round(time.monotonic() - started, 1)
        report[name] = result
        print(json.dumps({k: v for k, v in result.items() if k != "items"}, indent=2), flush=True)

    (OUT_DIR / "report.json").write_text(json.dumps(report, indent=2))
    print(f"\nFull report: {OUT_DIR / 'report.json'}")


if __name__ == "__main__":
    load_dotenv()
    asyncio.run(main())
