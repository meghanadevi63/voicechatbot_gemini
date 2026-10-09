"""Voice chat: bridge a browser WebSocket to a Gemini Live session.

The browser sends 16 kHz PCM16 mic audio as binary frames and small JSON control messages.
The bridge forwards audio to Gemini, answers Gemini's tool calls from the RAG index,
and sends back 24 kHz PCM16 audio plus JSON events (transcripts, sources, turn state).
See voice_plan.md for the protocol.
"""

import asyncio
import json
import logging
import re
import time
from difflib import SequenceMatcher
from typing import Literal

from fastapi import WebSocket, WebSocketDisconnect
from google import genai
from google.genai import types

from backend.config import get_settings
from backend.rag import RAGChain, format_docs, to_sources

logger = logging.getLogger(__name__)

AnswerMode = Literal["agent", "rag"]
TalkMode = Literal["vad", "ptt"]

IN_RATE = 16000
HISTORY_TIMEOUT_S = 10
MAX_SEED_TURNS = 20

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


def similarity(a: str, b: str) -> float:
    """Word-level match between two texts, 0 to 1 (used to log how faithfully relay mode reads)."""
    words = lambda s: re.findall(r"[a-z0-9']+", s.lower())
    return round(SequenceMatcher(None, words(a), words(b)).ratio(), 3)


def live_config(answer_mode: AnswerMode, talk_mode: TalkMode, has_history: bool) -> types.LiveConnectConfig:
    settings = get_settings()
    is_31 = "3.1" in settings.live_model
    tool = dict(RELAY_TOOL if answer_mode == "rag" else SEARCH_TOOL)
    if not is_31:
        # Newer Live models run tools asynchronously by default; the voice flow expects the
        # model to wait for the result, as 3.1 always does.
        tool["behavior"] = "BLOCKING"

    config = dict(
        response_modalities=["AUDIO"],
        system_instruction=RELAY_PROMPT if answer_mode == "rag" else AGENT_PROMPT,
        tools=[types.Tool(function_declarations=[tool])],
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=settings.live_voice)
            )
        ),
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
    )
    if is_31:
        config["thinking_config"] = types.ThinkingConfig(thinking_level="MINIMAL")
    if talk_mode == "ptt":
        config["realtime_input_config"] = types.RealtimeInputConfig(
            automatic_activity_detection=types.AutomaticActivityDetection(disabled=True)
        )
    if has_history:
        # The server then waits for the seeded history (ended by turn_complete) before realtime input,
        # and doesn't reply to it. Seeding without this leaves an open user turn that stalls turn-taking.
        config["history_config"] = types.HistoryConfig(initial_history_in_client_content=True)
    return types.LiveConnectConfig(**config)


class LiveBridge:
    def __init__(self, ws: WebSocket, rag: RAGChain, answer_mode: AnswerMode, talk_mode: TalkMode):
        self.ws = ws
        self.rag = rag
        self.answer_mode = answer_mode
        self.talk_mode = talk_mode
        # Conversation so far, as {role, content}; relay mode passes it to RAGChain.answer()
        self.history: list[dict] = []
        self._send_lock = asyncio.Lock()
        self._tool_tasks: dict[str, asyncio.Task] = {}
        # Per-turn bookkeeping for logs
        self._turn_output = ""
        self._turn_relay_answer = ""

    # --- sending to the browser ---

    async def send_event(self, type_: str, **data):
        async with self._send_lock:
            await self.ws.send_text(json.dumps({"type": type_, **data}))

    async def send_audio(self, data: bytes):
        async with self._send_lock:
            await self.ws.send_bytes(data)

    # --- lifecycle ---

    async def run(self):
        """Serve one voice session until the browser or Gemini ends it."""
        try:
            self.history = await self.receive_history()
        except WebSocketDisconnect:
            return
        except (asyncio.TimeoutError, ValueError, KeyError) as e:
            await self.send_event("error", message=f"Expected a history message first: {e}")
            await self.ws.close(code=1008)
            return

        settings = get_settings()
        client = genai.Client(api_key=settings.gemini_api_key)
        try:
            async with client.aio.live.connect(
                model=settings.live_model,
                config=live_config(self.answer_mode, self.talk_mode, has_history=bool(self.history)),
            ) as session:
                await self.seed_history(session)
                await self.send_event("ready", answer=self.answer_mode, talk=self.talk_mode)
                logger.info(
                    "Live session started (model=%s answer=%s talk=%s, %d history turns)",
                    settings.live_model, self.answer_mode, self.talk_mode, len(self.history),
                )
                upstream = asyncio.create_task(self.browser_to_gemini(session))
                downstream = asyncio.create_task(self.gemini_to_browser(session))
                done, pending = await asyncio.wait(
                    {upstream, downstream}, return_when=asyncio.FIRST_COMPLETED
                )
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                for task in done:
                    if task.exception() and not isinstance(task.exception(), WebSocketDisconnect):
                        raise task.exception()
        except WebSocketDisconnect:
            pass
        except Exception as e:
            logger.exception("Live session failed")
            try:
                await self.send_event("error", message=str(e) or type(e).__name__)
            except Exception:
                pass  # browser already gone
        finally:
            for task in self._tool_tasks.values():
                task.cancel()
            logger.info("Live session ended")
            try:
                await self.ws.close()
            except Exception:
                pass

    async def receive_history(self) -> list[dict]:
        """The first browser message is the conversation so far: {"type":"history","turns":[...]}."""
        raw = await asyncio.wait_for(self.ws.receive_text(), HISTORY_TIMEOUT_S)
        msg = json.loads(raw)
        if msg.get("type") != "history":
            raise ValueError(f"got {msg.get('type')!r}")
        turns = []
        for t in msg.get("turns") or []:
            role, content = t.get("role"), (t.get("content") or "").strip()
            if role in ("user", "assistant") and content:
                turns.append({"role": role, "content": content})
        return turns[-MAX_SEED_TURNS:]

    async def seed_history(self, session):
        if not self.history:
            return
        await session.send_client_content(
            turns=[
                types.Content(
                    role="user" if t["role"] == "user" else "model",
                    parts=[types.Part(text=t["content"])],
                )
                for t in self.history
            ],
            turn_complete=True,  # ends the initial history (see history_config in live_config)
        )

    # --- browser -> Gemini ---

    async def browser_to_gemini(self, session):
        while True:
            message = await self.ws.receive()
            if message["type"] == "websocket.disconnect":
                return
            if message.get("bytes"):
                await session.send_realtime_input(
                    audio=types.Blob(data=message["bytes"], mime_type=f"audio/pcm;rate={IN_RATE}")
                )
                continue
            if not message.get("text"):
                continue
            kind = json.loads(message["text"]).get("type")
            if kind == "end":
                return
            if kind == "activity_start" and self.talk_mode == "ptt":
                await session.send_realtime_input(activity_start=types.ActivityStart())
            elif kind == "activity_end" and self.talk_mode == "ptt":
                await session.send_realtime_input(activity_end=types.ActivityEnd())
            elif kind == "audio_stream_end" and self.talk_mode == "vad":
                # Mic muted mid-sentence: lets Gemini finish the turn without waiting for silence
                await session.send_realtime_input(audio_stream_end=True)

    # --- Gemini -> browser ---

    async def gemini_to_browser(self, session):
        while True:
            # receive() yields one model turn, then stops; loop for the next turn
            async for msg in session.receive():
                await self.handle_server_message(session, msg)

    async def handle_server_message(self, session, msg: types.LiveServerMessage):
        if msg.tool_call:
            for fc in msg.tool_call.function_calls or []:
                task = asyncio.create_task(self.run_tool(session, fc))
                self._tool_tasks[fc.id] = task
                task.add_done_callback(lambda _, id_=fc.id: self._tool_tasks.pop(id_, None))

        if msg.tool_call_cancellation:
            for id_ in msg.tool_call_cancellation.ids or []:
                if task := self._tool_tasks.get(id_):
                    task.cancel()

        if msg.go_away:
            logger.warning("Gemini will close the connection in %s", msg.go_away.time_left)

        sc = msg.server_content
        if not sc:
            return
        # One event can carry several parts (3.1 Live): handle every one
        for part in (sc.model_turn.parts if sc.model_turn else None) or []:
            if part.inline_data and part.inline_data.data:
                await self.send_audio(part.inline_data.data)
        if sc.input_transcription and sc.input_transcription.text:
            await self.send_event("input_transcript", text=sc.input_transcription.text)
        if sc.output_transcription and sc.output_transcription.text:
            self._turn_output += sc.output_transcription.text
            await self.send_event("output_transcript", text=sc.output_transcription.text)
        if sc.interrupted:
            await self.send_event("interrupted")
        if sc.turn_complete:
            self.log_turn(interrupted=bool(sc.interrupted))
            await self.send_event("turn_complete")

    async def run_tool(self, session, fc: types.FunctionCall):
        args = dict(fc.args or {})
        await self.send_event("tool_call", name=fc.name)
        started = time.monotonic()
        try:
            if fc.name == "search_documents":
                docs = await asyncio.to_thread(self.rag.retrieve, args.get("query", ""))
                response = {"result": format_docs(docs) or "No matching excerpts were found."}
                sources = to_sources(docs)
            elif fc.name == "ask_knowledge_base":
                question = args.get("question", "")
                result = await asyncio.to_thread(self.rag.answer, question, list(self.history))
                self.history += [
                    {"role": "user", "content": question},
                    {"role": "assistant", "content": result["answer"]},
                ]
                self._turn_relay_answer = result["answer"]
                response = {"answer": result["answer"]}
                sources = result["sources"]
            else:
                response = {"error": f"Unknown tool {fc.name}"}
                sources = []
        except Exception as e:
            logger.exception("Tool %s failed", fc.name)
            response = {"error": f"The document search failed: {e}"}
            sources = []
        logger.info("Tool %s(%s) took %.2fs", fc.name, args, time.monotonic() - started)

        await session.send_tool_response(
            function_responses=[types.FunctionResponse(id=fc.id, name=fc.name, response=response)]
        )
        if sources:
            await self.send_event("sources", sources=sources)

    def log_turn(self, interrupted: bool):
        if self._turn_relay_answer:
            logger.info(
                "Relay fidelity %.3f%s",
                similarity(self._turn_relay_answer, self._turn_output),
                " (interrupted)" if interrupted else "",
            )
        self._turn_output = ""
        self._turn_relay_answer = ""
