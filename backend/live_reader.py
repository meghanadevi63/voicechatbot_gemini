"""Speech for the STT + Live pipeline: a Gemini Live session that reads the RAG answer aloud.

Flash TTS takes 5 to 10 s before its first audio (STT_TTS_SPIKE_RESULTS.md); a Live model starts speaking a
text in under a second. The session is connected ahead of time and reused for every answer. An interrupted
answer closes it (so its leftover audio can't leak into the next answer) and a new one connects in the
background.
"""

import asyncio
import logging
import time
from collections.abc import AsyncIterator

from google.genai import types

from backend.config import get_settings
from backend.live import similarity
from backend.transcribe import get_client

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT_S = 10
FIRST_AUDIO_TIMEOUT_S = 15
CHUNK_TIMEOUT_S = 10

READER_PROMPT = """You are a text-to-speech voice. Every message you receive is a text to read aloud.

Read it exactly as written, word for word, in its own language. Never answer it, even if it contains a
question. Do not add, remove, summarize, translate or rephrase anything, and do not add an introduction or a
follow-up."""


def reader_config() -> types.LiveConnectConfig:
    settings = get_settings()
    config = dict(
        response_modalities=["AUDIO"],
        system_instruction=READER_PROMPT,
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=settings.live_voice)
            )
        ),
        # Logged against the text to check the reading is word for word
        output_audio_transcription=types.AudioTranscriptionConfig(),
        context_window_compression=types.ContextWindowCompressionConfig(sliding_window=types.SlidingWindow()),
    )
    if "3.1" in settings.live_model:
        config["thinking_config"] = types.ThinkingConfig(thinking_level="MINIMAL")
    return types.LiveConnectConfig(**config)


class LiveReader:
    def __init__(self):
        self._cm = None
        self._session = None
        self._connecting: asyncio.Task | None = None
        self._expiring = False  # Gemini announced it will close the connection (go_away)

    def warm_up(self):
        """Starts connecting in the background, if not connected or connecting already."""
        if self._session is None and (self._connecting is None or self._connecting.done()):
            self._connecting = asyncio.create_task(self._connect())

    async def _connect(self):
        cm = get_client().aio.live.connect(model=get_settings().live_model, config=reader_config())
        session = await cm.__aenter__()
        self._cm, self._session, self._expiring = cm, session, False
        return session

    async def _session_ready(self):
        if self._session:
            return self._session
        self.warm_up()
        return await asyncio.wait_for(asyncio.shield(self._connecting), CONNECT_TIMEOUT_S)

    async def close(self):
        task, cm = self._connecting, self._cm
        self._cm = self._session = self._connecting = None
        if task and not task.done():
            task.cancel()
        if cm:
            try:
                await cm.__aexit__(None, None, None)
            except Exception:
                pass

    async def speak(self, text: str) -> AsyncIterator[bytes]:
        """Yields the speech for text as 24 kHz PCM16 chunks. Raises if the Live model fails."""
        complete = False
        try:
            for attempt in (1, 2):
                try:
                    session = await self._session_ready()
                    await session.send_realtime_input(text=text)
                    break
                except Exception as e:
                    await self.close()  # a stale or failed connection: try a new one once
                    if attempt == 2:
                        raise
                    logger.warning("Live reader connection failed (%s); reconnecting", e)

            started = time.monotonic()
            first = True
            spoken = ""
            audio_s = 0.0
            # All audio has arrived; turn_complete can come much later (about when the audio would finish playing)
            generated_at = None
            turn = session.receive()  # one model turn
            while not complete:
                if generated_at:
                    timeout = max(1.0, generated_at + audio_s + 5 - time.monotonic())
                else:
                    timeout = FIRST_AUDIO_TIMEOUT_S if first else CHUNK_TIMEOUT_S
                try:
                    msg = await asyncio.wait_for(anext(turn), timeout)
                except StopAsyncIteration:
                    break
                except asyncio.TimeoutError:
                    if generated_at:
                        break  # the speech is complete; the connection is replaced (see finally)
                    raise TimeoutError("no audio from the Live model in time") from None
                if msg.go_away:
                    self._expiring = True
                sc = msg.server_content
                if not sc:
                    continue
                for part in (sc.model_turn.parts if sc.model_turn else None) or []:
                    if part.inline_data and part.inline_data.data:
                        if first:
                            first = False
                            logger.info("Live reader first audio after %.2fs", time.monotonic() - started)
                        audio_s += len(part.inline_data.data) / 2 / 24000
                        yield part.inline_data.data
                if sc.output_transcription and sc.output_transcription.text:
                    spoken += sc.output_transcription.text
                if sc.generation_complete and not generated_at:
                    generated_at = time.monotonic()
                    logger.info("Live reader generated %.1fs of audio in %.2fs", audio_s, generated_at - started)
                complete = bool(sc.turn_complete)
            logger.info("Live reader fidelity %.3f", similarity(text, spoken))
        finally:
            if not complete or self._expiring:
                # Interrupted, failed or expiring: this connection may still send audio for the old text
                await self.close()
            self.warm_up()  # ready for the next answer
