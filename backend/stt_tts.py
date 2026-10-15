"""Voice chat, STT + TTS pipeline: a streaming transcriber, the RAG chain, then a voice that reads the answer:
a TTS model (speaker "tts"), or a Gemini Live session that reads it word for word (speaker "live", faster).

Same browser protocol as /live (see live.py), so the frontend's voice session works with either:
the browser sends 16 kHz PCM16 mic audio and JSON control messages, and gets back 24 kHz PCM16 audio and JSON
events. Differences: `interim_transcript` (the turn's full text so far, sent while the user speaks) and
`input_transcript` (the turn's final text) replace the caption instead of adding to it.

A turn: the transcriber (automatic VAD) ends it with a final transcript followed by generation_complete
(~1.5 s after the speech). The bridge then gets the RAG answer and streams its speech. Speech while the answer
is being prepared is ignored; speech while it plays interrupts it (barge-in) and starts the next turn.
"""

import asyncio
import json
import logging
import re
import time
from typing import Literal

from fastapi import WebSocket, WebSocketDisconnect
from google.genai import types

from backend.config import get_settings
from backend.language import is_known
from backend.live import IN_RATE, RECONNECT_DELAYS_S, Reconnect, describe, receive_history, retryable
from backend.live_reader import LiveReader
from backend.rag import RAGChain
from backend.transcribe import get_client
from backend.tts import OUT_RATE, speak

logger = logging.getLogger(__name__)

Phase = Literal["listening", "preparing", "speaking"]
Speaker = Literal["tts", "live"]

# Speech during playback interrupts it only with this many words or characters (one long word, or a script
# without spaces), so a stray noise or a bit of echo from the speakers doesn't
MIN_BARGE_IN_WORDS = 2
MIN_BARGE_IN_CHARS = 6
# The browser plays the audio a little after it arrives; the turn ends this long after the estimated end
PLAYBACK_MARGIN_S = 0.3


def stt_config() -> types.LiveConnectConfig:
    return types.LiveConnectConfig(
        response_modalities=["TEXT"],
        # No language codes: the transcriber detects the language, including a mix of languages
        input_audio_transcription=types.AudioTranscriptionConfig(language_codes=[]),
    )


def enough_to_interrupt(text: str) -> bool:
    return len(re.findall(r"\w+", text)) >= MIN_BARGE_IN_WORDS or len(text.strip()) >= MIN_BARGE_IN_CHARS


class SttTtsBridge:
    def __init__(
        self, ws: WebSocket, rag: RAGChain, language: str | None = None, mic: bool = True, speaker: Speaker = "tts"
    ):
        self.ws = ws
        self.rag = rag
        # The conversation's language (BCP-47), updated from each answered turn
        self.language = language if is_known(language) else None
        # False for a spoken reply to a dictated question: no transcriber, only text in
        self.mic = mic
        self.reader = LiveReader() if speaker == "live" else None
        self.history: list[dict] = []
        self._send_lock = asyncio.Lock()
        self.stt = None  # the transcriber session; None while (re)connecting
        self.phase: Phase = "listening"
        self._final = ""  # final transcript of the turn in progress
        self._user_turn = False  # the user is mid-sentence (transcripts came, the turn hasn't ended)
        self._answer_task: asyncio.Task | None = None
        self._failures = 0  # consecutive failed transcriber connections
        self._go_away = False  # the transcriber will close: reconnect once the user isn't mid-sentence

    # --- sending to the browser ---

    async def send_event(self, type_: str, **data):
        async with self._send_lock:
            await self.ws.send_text(json.dumps({"type": type_, **data}))

    async def send_audio(self, data: bytes):
        async with self._send_lock:
            await self.ws.send_bytes(data)

    # --- lifecycle ---

    async def run(self):
        try:
            self.history = await receive_history(self.ws)
        except WebSocketDisconnect:
            return
        except (asyncio.TimeoutError, ValueError, KeyError) as e:
            await self.send_event("error", message=f"Expected a history message first: {e}")
            await self.ws.close(code=1008)
            return

        settings = get_settings()
        logger.info(
            "STT+TTS session started (stt=%s voice=%s mic=%s language=%s, %d history turns)",
            settings.stt_model,
            f"{settings.live_model}/{settings.live_voice}" if self.reader else f"{settings.tts_model}/{settings.tts_voice}",
            self.mic, self.language, len(self.history),
        )
        if self.reader:
            self.reader.warm_up()  # connects while the user speaks
        browser = asyncio.create_task(self.browser_loop())
        try:
            if self.mic:
                await self.transcriber_loop(browser)
            else:
                await self.send_event("ready", answer="rag", talk="vad")
                await browser
        except WebSocketDisconnect:
            pass
        except Exception as e:
            logger.exception("STT+TTS session failed")
            try:
                await self.send_event("error", message=f"The voice service is having trouble ({describe(e)}).")
            except Exception:
                pass  # browser already gone
        finally:
            browser.cancel()
            if self._answer_task:
                self._answer_task.cancel()
            if self.reader:
                await self.reader.close()
            logger.info("STT+TTS session ended")
            try:
                await self.ws.close()
            except Exception:
                pass

    async def transcriber_loop(self, browser: asyncio.Task):
        """Keeps a transcriber connection open until the browser ends the session, reconnecting as needed.
        The transcriber holds no conversation state, so a new connection simply carries on."""
        settings = get_settings()
        connected_once = False
        while not browser.done():
            try:
                async with get_client().aio.live.connect(model=settings.stt_model, config=stt_config()) as session:
                    self.stt = session
                    self._go_away = False
                    await self.send_event("resumed" if connected_once else "ready", answer="rag", talk="vad")
                    connected_once = True
                    receiver = asyncio.create_task(self.receive_transcripts(session))
                    try:
                        done, _ = await asyncio.wait({browser, receiver}, return_when=asyncio.FIRST_COMPLETED)
                    finally:
                        self.stt = None
                        receiver.cancel()
                        await asyncio.gather(receiver, return_exceptions=True)
                    for task in (browser, receiver):
                        if task in done and task.exception():
                            raise task.exception()
            except WebSocketDisconnect:
                return
            except Exception as e:
                if browser.done():
                    return
                planned = isinstance(e, Reconnect) and e.planned
                delay = 0
                if not planned:
                    if not retryable(e) or self._failures >= len(RECONNECT_DELAYS_S):
                        raise
                    delay = RECONNECT_DELAYS_S[self._failures]
                    self._failures += 1
                logger.warning("Transcriber connection ended (%s); reconnecting in %ss", describe(e), delay)
                self._final = ""  # a sentence cut off by the drop is lost
                self._user_turn = False
                await self.send_event("reconnecting", planned=planned)
                await asyncio.sleep(delay)

    # --- browser -> transcriber ---

    async def browser_loop(self):
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
            if kind == "text" and msg.get("text", "").strip() and self.phase == "listening":
                # A dictated (or typed) question that should get a spoken answer
                self.start_answer(msg["text"].strip())
            elif kind == "audio_stream_end":
                # Mic muted: without trailing silence the transcriber would never end the turn
                await self.forward(audio_stream_end=True)

    async def forward(self, **realtime_input):
        session = self.stt
        if not session:
            return  # (re)connecting: input is dropped
        try:
            await session.send_realtime_input(**realtime_input)
        except Exception as e:
            logger.debug("Dropped input on a closing transcriber connection: %s", e)

    # --- transcriber -> turns ---

    async def receive_transcripts(self, session):
        while True:
            got_any = False
            async for msg in session.receive():
                got_any = True
                if msg.go_away:
                    logger.info("Transcriber will close the connection in %s", msg.go_away.time_left)
                    self._go_away = True
                sc = msg.server_content
                if sc:
                    if sc.interim_input_transcription and (sc.interim_input_transcription.text or "").strip():
                        await self.on_transcript(sc.interim_input_transcription.text.strip(), final=False)
                    if sc.input_transcription and (sc.input_transcription.text or "").strip():
                        await self.on_transcript(sc.input_transcription.text.strip(), final=True)
                    if sc.generation_complete:
                        await self.on_turn_end()
                if self._go_away and not self._user_turn:
                    # An answer being prepared or played carries on: it doesn't use the transcriber
                    raise Reconnect("go_away", planned=True)
            if not got_any:
                raise Reconnect("transcriber closed the connection")

    async def on_transcript(self, text: str, final: bool):
        """text: the turn's full transcript so far (interim) or its final text."""
        if self.phase == "preparing":
            return  # ignored until the answer starts playing
        if self.phase == "speaking":
            if not enough_to_interrupt(text):
                return
            await self.barge_in()
        self._user_turn = True
        if final:
            self._final = text
        await self.send_event("input_transcript" if final else "interim_transcript", text=text)

    async def on_turn_end(self):
        question, self._final = self._final, ""
        self._user_turn = False
        if self.phase == "listening" and question:
            self._failures = 0
            self.start_answer(question)

    # --- answering ---

    def start_answer(self, question: str):
        self.phase = "preparing"
        self._answer_task = asyncio.create_task(self.answer(question))

    async def answer(self, question: str):
        await self.send_event("tool_call", name="rag")  # the browser shows "Preparing the answer…"
        started = time.monotonic()
        try:
            result = await asyncio.to_thread(self.rag.answer, question, list(self.history), self.language)
        except Exception as e:
            logger.exception("RAG answer failed")
            await self.send_event("error", message=f"Couldn't get an answer: {e}")
            await self.finish_turn()
            return
        rag_s = time.monotonic() - started
        await self.set_language(result["language"])
        self.history += [
            {"role": "user", "content": question},
            {"role": "assistant", "content": result["answer"]},
        ]
        # The text goes to the chat before the speech starts
        await self.send_event("rag_answer", text=result["answer"])
        if result["sources"]:
            await self.send_event("sources", sources=result["sources"])

        first_at = None
        audio_s = 0.0
        try:
            async for chunk in (self.reader.speak if self.reader else speak)(result["answer"]):
                if first_at is None:
                    first_at = time.monotonic()
                    self.phase = "speaking"  # barge-in possible from here on
                await self.send_audio(chunk)
                audio_s += len(chunk) / 2 / OUT_RATE
        except Exception as e:
            logger.exception("TTS failed")
            await self.send_event("error", message=f"Couldn't speak the answer (it is in the chat): {describe(e)}")
        logger.info(
            "Turn: RAG %.2fs, first audio %s after the answer, %.1fs of audio for %d chars",
            rag_s, f"{first_at - started - rag_s:.2f}s" if first_at else "never", audio_s, len(result["answer"]),
        )
        if first_at:
            # Still the speaking phase (barge-in possible) until the browser has played it all
            await asyncio.sleep(max(0.0, first_at + audio_s + PLAYBACK_MARGIN_S - time.monotonic()))
        await self.finish_turn()

    async def finish_turn(self):
        self._answer_task = None
        self.phase = "listening"
        await self.send_event("turn_complete")

    async def barge_in(self):
        """The user talked over the answer: stop it and listen to them."""
        task, self._answer_task = self._answer_task, None
        if task:
            task.cancel()
        self.phase = "listening"
        logger.info("Barge-in: answer interrupted")
        await self.send_event("interrupted")
        await self.send_event("turn_complete")

    async def set_language(self, language: str | None):
        if not is_known(language):
            return
        self.language = language
        await self.send_event("language", language=language)
