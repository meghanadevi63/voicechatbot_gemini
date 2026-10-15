"""Voice chat: bridge a browser WebSocket to a Gemini Live session.

The browser sends 16 kHz PCM16 mic audio as binary frames and small JSON control messages.
The bridge forwards audio to Gemini, answers Gemini's tool calls from the RAG index,
and sends back 24 kHz PCM16 audio plus JSON events (transcripts, sources, turn state).
The browser connection outlives the Gemini connection: when Gemini announces a close (go_away), drops the
connection or stops answering, the bridge reconnects with the session-resumption handle and asks the
unanswered question again. See voice_plan.md for the protocol.
"""

import asyncio
import json
import logging
import re
import time
from difflib import SequenceMatcher
from typing import Literal

import websockets
from fastapi import WebSocket, WebSocketDisconnect
from google import genai
from google.genai import errors, types

from backend.config import get_settings
from backend.language import is_known
from backend.rag import RAGChain, format_docs, to_english_query, to_sources

logger = logging.getLogger(__name__)

AnswerMode = Literal["agent", "rag"]
TalkMode = Literal["vad", "ptt"]

IN_RATE = 16000
HISTORY_TIMEOUT_S = 10
MAX_SEED_TURNS = 20
# Waits before each reconnect after a Gemini failure; when they run out, the session ends with an error.
# A completed turn resets the count.
RECONNECT_DELAYS_S = (1, 3)
# A question (or tool result) with no audio from Gemini for this long counts as a failed turn
REPLY_TIMEOUT_S = 15
# Gemini close codes worth reconnecting after: going away, abnormal closure, internal error, service
# restart, try again later, bad gateway
RETRYABLE_CODES = {1001, 1006, 1011, 1012, 1013, 1014}

AGENT_PROMPT = """You are a friendly voice assistant that answers questions about the user's documents.

Rules:
- For any question or request for information, call search_documents first with a short standalone search
  query IN ENGLISH, whatever language the user speaks (the documents are in English). Use the conversation to
  resolve follow-ups like "tell me more" or "what about the second one".
- Answer only from what search_documents returns. Do not use outside knowledge or guess.
- If the results don't contain the answer, say plainly that you couldn't find it in the documents.
  If they contain part of the answer, give that part and say what is missing.
- For purely social messages (greetings, thanks, goodbyes) reply briefly and warmly without searching.
- Speak in natural sentences: no lists, markdown or URLs. Keep it to two to four sentences unless the user
  asks for more detail.
- Don't mention "excerpts", "search results" or the tool. If it helps, name the source document naturally.
- Answer in the language the user is speaking, in that language's usual script, even though the documents
  are in English. Don't add English translations of terms in parentheses."""

# Added to the agent prompt when the conversation's language is known
LANGUAGE_HINT = """

The user usually speaks the language with BCP-47 code {language}; answer in the language they speak."""

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
        "properties": {
            "query": {"type": "STRING", "description": "Standalone search query, in English"},
            "language": {
                "type": "STRING",
                "description": 'BCP-47 code of the language the user is speaking ("en", "hi", "te", ...)',
            },
        },
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
    words = lambda s: re.findall(r"[\w']+", s.lower())
    return round(SequenceMatcher(None, words(a), words(b)).ratio(), 3)


class Reconnect(Exception):
    """Ends the current Gemini connection so the bridge opens a new one.

    planned: Gemini announced the close (go_away), so it doesn't count as a failure.
    """

    def __init__(self, reason: str, planned: bool = False):
        super().__init__(reason)
        self.planned = planned


def retryable(e: BaseException) -> bool:
    """A Gemini failure that a new connection may fix."""
    if isinstance(e, Reconnect):
        return True
    if isinstance(e, errors.APIError):
        return e.code in RETRYABLE_CODES or 500 <= e.code < 600
    # Network trouble, or sending on a connection Gemini just closed
    return isinstance(e, (OSError, asyncio.TimeoutError, websockets.ConnectionClosed))


def describe(e: BaseException) -> str:
    if isinstance(e, errors.APIError):
        return f"Gemini error {e.code}" + (f": {e.details}" if e.details else "")
    return str(e) or type(e).__name__


async def receive_history(ws: WebSocket) -> list[dict]:
    """The first browser message is the conversation so far: {"type":"history","turns":[...]}."""
    raw = await asyncio.wait_for(ws.receive_text(), HISTORY_TIMEOUT_S)
    msg = json.loads(raw)
    if msg.get("type") != "history":
        raise ValueError(f"got {msg.get('type')!r}")
    turns = []
    for t in msg.get("turns") or []:
        role, content = t.get("role"), (t.get("content") or "").strip()
        if role in ("user", "assistant") and content:
            turns.append({"role": role, "content": content})
    return turns[-MAX_SEED_TURNS:]


def live_config(
    answer_mode: AnswerMode,
    talk_mode: TalkMode,
    has_history: bool,
    language: str | None = None,
    resume_handle: str | None = None,
) -> types.LiveConnectConfig:
    """has_history: the conversation will be seeded (only on a fresh session, never when resuming)."""
    settings = get_settings()
    is_31 = "3.1" in settings.live_model
    tool = dict(RELAY_TOOL if answer_mode == "rag" else SEARCH_TOOL)
    if not is_31:
        # Newer Live models run tools asynchronously by default; the voice flow expects the
        # model to wait for the result, as 3.1 always does.
        tool["behavior"] = "BLOCKING"

    config = dict(
        response_modalities=["AUDIO"],
        # Relay mode reads RAGChain's answer, which gets the language itself
        system_instruction=RELAY_PROMPT
        if answer_mode == "rag"
        else AGENT_PROMPT + (LANGUAGE_HINT.format(language=language) if is_known(language) else ""),
        tools=[types.Tool(function_declarations=[tool])],
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=settings.live_voice)
            )
        ),
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
        # Gemini sends a handle for continuing this session on a new connection (connections last ~10 min)
        session_resumption=types.SessionResumptionConfig(handle=resume_handle),
        # Audio sessions otherwise stop at 15 minutes; the sliding window drops the oldest turns instead
        context_window_compression=types.ContextWindowCompressionConfig(sliding_window=types.SlidingWindow()),
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
    def __init__(
        self, ws: WebSocket, rag: RAGChain, answer_mode: AnswerMode, talk_mode: TalkMode, language: str | None = None
    ):
        self.ws = ws
        # The conversation's language (BCP-47), updated from each voice turn whose language is clear
        self.language = language if is_known(language) else None
        self.rag = rag
        self.answer_mode = answer_mode
        self.talk_mode = talk_mode
        # Conversation so far, as {role, content}: seeds a fresh Gemini session, and relay mode passes it
        # to RAGChain.answer()
        self.history: list[dict] = []
        self._send_lock = asyncio.Lock()
        self._tool_tasks: dict[str, asyncio.Task] = {}
        # The Gemini connection; None while (re)connecting, when browser input is dropped
        self.session = None
        self.resume_handle: str | None = None
        self._connected_once = False
        self._failures = 0  # consecutive failed connections
        self._go_away = False  # Gemini announced it will close this connection
        # Monotonic time by which Gemini should send audio; None when no reply is expected
        self._reply_deadline: float | None = None
        # Per-turn bookkeeping
        self._turn_active = False
        self._turn_question = ""  # the user's words, asked again if the connection drops before turn_complete
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
        """Serve one voice session until the browser ends it, reconnecting to Gemini as needed."""
        try:
            self.history = await self.receive_history()
        except WebSocketDisconnect:
            return
        except (asyncio.TimeoutError, ValueError, KeyError) as e:
            await self.send_event("error", message=f"Expected a history message first: {e}")
            await self.ws.close(code=1008)
            return

        client = genai.Client(api_key=get_settings().gemini_api_key)
        browser = asyncio.create_task(self.browser_to_gemini())
        try:
            while not browser.done():
                try:
                    await self.serve_connection(client, browser)
                except WebSocketDisconnect:
                    break
                except Exception as e:
                    if browser.done():
                        break
                    await self.prepare_reconnect(e)
        except Exception as e:
            logger.exception("Live session failed")
            try:
                await self.send_event("error", message=self.error_message(e))
            except Exception:
                pass  # browser already gone
        finally:
            browser.cancel()
            for task in self._tool_tasks.values():
                task.cancel()
            logger.info("Live session ended")
            try:
                await self.ws.close()
            except Exception:
                pass

    async def serve_connection(self, client: genai.Client, browser: asyncio.Task):
        """One Gemini connection. Returns when the browser ends the session; raises when Gemini's side ends."""
        settings = get_settings()
        config = live_config(
            self.answer_mode,
            self.talk_mode,
            has_history=bool(self.history) and not self.resume_handle,
            language=self.language,
            resume_handle=self.resume_handle,
        )
        async with client.aio.live.connect(model=settings.live_model, config=config) as session:
            if not self.resume_handle:
                await self.seed_history(session)
            self.session = session
            self._go_away = False
            self._reply_deadline = None
            event = "resumed" if self._connected_once else "ready"
            self._connected_once = True
            await self.send_event(event, answer=self.answer_mode, talk=self.talk_mode)
            logger.info(
                "Live session %s (model=%s answer=%s talk=%s language=%s, %s)",
                "resumed" if event == "resumed" else "started",
                settings.live_model, self.answer_mode, self.talk_mode, self.language,
                "resumption handle" if self.resume_handle else f"{len(self.history)} history turns",
            )
            if self._turn_question:
                # The connection dropped before the last question was answered: ask it again
                logger.info("Asking again: %r", self._turn_question)
                await self.ask(self._turn_question)

            downstream = asyncio.create_task(self.gemini_to_browser(session))
            watchdog = asyncio.create_task(self.watch_reply())
            try:
                done, _ = await asyncio.wait({browser, downstream, watchdog}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                self.session = None
                for task in (downstream, watchdog):
                    task.cancel()
                await asyncio.gather(downstream, watchdog, return_exceptions=True)
            for task in (browser, downstream, watchdog):
                if task in done and task.exception():
                    raise task.exception()

    async def prepare_reconnect(self, e: Exception):
        """Decide whether to reconnect after the Gemini connection ended with e (raises to give up)."""
        planned = (isinstance(e, Reconnect) and e.planned) or self._go_away
        if not planned:
            if not retryable(e):
                if not self.resume_handle:
                    raise e
                # Maybe the handle was refused: start a fresh session seeded with the conversation
                logger.warning("Dropping the resumption handle after: %s", describe(e))
                self.resume_handle = None
            if self._failures >= len(RECONNECT_DELAYS_S):
                raise e
            delay = RECONNECT_DELAYS_S[self._failures]
            self._failures += 1
        for task in self._tool_tasks.values():
            task.cancel()
        if self.answer_mode == "rag" and self._turn_relay_answer:
            # The question is asked again, so its RAG answer will be added to the history again
            del self.history[-2:]
        self._turn_output = ""
        self._turn_relay_answer = ""
        self._turn_active = bool(self._turn_question)
        logger.warning(
            "Gemini connection ended (%s); reconnecting%s",
            describe(e), "" if planned else f" in {delay}s (attempt {self._failures})",
        )
        await self.send_event("reconnecting", planned=planned)
        if not planned:
            await asyncio.sleep(delay)

    def error_message(self, e: Exception) -> str:
        if isinstance(e, Reconnect):
            return "The voice service stopped answering. Please try again in a minute."
        if retryable(e):
            return f"The voice service is having trouble ({describe(e)}). Please try again in a minute."
        return describe(e)

    async def receive_history(self) -> list[dict]:
        return await receive_history(self.ws)

    async def seed_history(self, session):
        if not self.history:
            return
        await session.send_client_content(
            turns=[
                types.Content(
                    role="user" if t["role"] == "user" else "model",
                    parts=[types.Part(text=t["content"])],
                )
                for t in self.history[-MAX_SEED_TURNS:]
            ],
            turn_complete=True,  # ends the initial history (see history_config in live_config)
        )

    # --- browser -> Gemini ---

    async def browser_to_gemini(self):
        """Reads the browser until it ends the session. Input that arrives while reconnecting is dropped,
        apart from a typed question, which is asked once connected."""
        while True:
            message = await self.ws.receive()
            if message["type"] == "websocket.disconnect":
                return
            if message.get("bytes"):
                await self.forward(audio=types.Blob(data=message["bytes"], mime_type=f"audio/pcm;rate={IN_RATE}"))
                continue
            if not message.get("text"):
                continue
            msg = json.loads(message["text"])
            kind = msg.get("type")
            if kind == "end":
                return
            if kind == "text" and msg.get("text", "").strip():
                # A dictated (or typed) question that should get a spoken answer
                self._turn_question = msg["text"].strip()
                self._turn_active = True
                if self.session:
                    await self.ask(self._turn_question)
                continue
            if kind == "activity_start" and self.talk_mode == "ptt":
                await self.forward(activity_start=types.ActivityStart())
            elif kind == "activity_end" and self.talk_mode == "ptt":
                await self.forward(activity_end=types.ActivityEnd())
            elif kind == "audio_stream_end" and self.talk_mode == "vad":
                # Mic muted mid-sentence: lets Gemini finish the turn without waiting for silence
                await self.forward(audio_stream_end=True)

    async def forward(self, **realtime_input):
        session = self.session
        if not session:
            return
        try:
            await session.send_realtime_input(**realtime_input)
        except Exception as e:
            # The connection is going down; gemini_to_browser sees it and the bridge reconnects
            logger.debug("Dropped input on a closing connection: %s", e)

    async def ask(self, text: str):
        await self.forward(text=text)
        self.expect_reply()

    # --- Gemini -> browser ---

    async def gemini_to_browser(self, session):
        while True:
            # receive() yields one model turn, then stops; loop for the next turn
            async for msg in session.receive():
                await self.handle_server_message(session, msg)

    def expect_reply(self):
        self._reply_deadline = time.monotonic() + REPLY_TIMEOUT_S

    async def watch_reply(self):
        """Raises Reconnect when Gemini leaves a question unanswered (it sometimes goes silent)."""
        while True:
            await asyncio.sleep(1)
            if self._reply_deadline and time.monotonic() > self._reply_deadline:
                raise Reconnect(f"no reply within {REPLY_TIMEOUT_S}s")

    async def handle_server_message(self, session, msg: types.LiveServerMessage):
        if msg.session_resumption_update:
            update = msg.session_resumption_update
            if update.resumable and update.new_handle:
                self.resume_handle = update.new_handle

        if msg.go_away:
            logger.info("Gemini will close the connection in %s", msg.go_away.time_left)
            self._go_away = True
            if not self._turn_active:
                raise Reconnect("go_away", planned=True)
            # Otherwise reconnect once this turn completes (or when Gemini closes, if that comes first)

        if msg.tool_call:
            self._turn_active = True
            self._reply_deadline = None  # our tool is working; the deadline restarts with its result
            for fc in msg.tool_call.function_calls or []:
                task = asyncio.create_task(self.run_tool(session, fc))
                self._tool_tasks[fc.id] = task
                task.add_done_callback(lambda _, id_=fc.id: self._tool_tasks.pop(id_, None))

        if msg.tool_call_cancellation:
            for id_ in msg.tool_call_cancellation.ids or []:
                if task := self._tool_tasks.get(id_):
                    task.cancel()

        sc = msg.server_content
        if not sc:
            return
        # One event can carry several parts (3.1 Live): handle every one
        for part in (sc.model_turn.parts if sc.model_turn else None) or []:
            if part.inline_data and part.inline_data.data:
                self._turn_active = True
                self._reply_deadline = None
                await self.send_audio(part.inline_data.data)
        if sc.input_transcription and sc.input_transcription.text:
            self._turn_active = True
            self._turn_question += sc.input_transcription.text
            await self.send_event("input_transcript", text=sc.input_transcription.text)
        if sc.output_transcription and sc.output_transcription.text:
            self._turn_output += sc.output_transcription.text
            await self.send_event("output_transcript", text=sc.output_transcription.text)
        if sc.interrupted:
            self._reply_deadline = None
            await self.send_event("interrupted")
        if sc.turn_complete:
            self.finish_turn(interrupted=bool(sc.interrupted))
            await self.send_event("turn_complete")
            if self._go_away:
                raise Reconnect("go_away", planned=True)

    async def run_tool(self, session, fc: types.FunctionCall):
        args = dict(fc.args or {})
        await self.send_event("tool_call", name=fc.name)
        started = time.monotonic()
        try:
            if fc.name == "search_documents":
                await self.set_language(args.get("language"))
                # Gemini sometimes leaves the query in the user's language; the documents are English
                query = await asyncio.to_thread(to_english_query, args.get("query", ""))
                docs = await asyncio.to_thread(self.rag.retrieve, query)
                response = {"result": format_docs(docs) or "No matching excerpts were found."}
                sources = to_sources(docs)
            elif fc.name == "ask_knowledge_base":
                question = args.get("question", "")
                result = await asyncio.to_thread(self.rag.answer, question, list(self.history), self.language)
                await self.set_language(result["language"])
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
        self.expect_reply()
        if fc.name == "ask_knowledge_base" and "answer" in response:
            # The chat stores the RAG text itself, not Gemini's transcript of reading it
            await self.send_event("rag_answer", text=response["answer"])
        if sources:
            await self.send_event("sources", sources=sources)

    async def set_language(self, language: str | None):
        """A voice turn's detected language: becomes the session's language and is sent to the browser."""
        if not is_known(language):
            return
        self.language = language
        await self.send_event("language", language=language)

    def finish_turn(self, interrupted: bool):
        if self._turn_relay_answer:
            logger.info(
                "Relay fidelity %.3f%s",
                similarity(self._turn_relay_answer, self._turn_output),
                " (interrupted)" if interrupted else "",
            )
        elif self.answer_mode == "agent" and self._turn_question.strip() and self._turn_output.strip():
            # Kept so that a fresh session (no resumption handle) can be seeded with the conversation
            self.history += [
                {"role": "user", "content": self._turn_question.strip()},
                {"role": "assistant", "content": self._turn_output.strip()},
            ]
        self._failures = 0
        self._reply_deadline = None
        self._turn_active = False
        self._turn_question = ""
        self._turn_output = ""
        self._turn_relay_answer = ""
