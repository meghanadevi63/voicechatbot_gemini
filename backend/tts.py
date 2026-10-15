"""Speech for the STT + TTS voice pipeline: streams a Gemini TTS model's audio for a text.

Time to the first audio grows with the text's length (~4.4 s for one sentence, ~9.6 s for three; see
STT_TTS_SPIKE_RESULTS.md), so the first sentence and the rest are synthesized in two parallel requests: the
first sentence plays while the rest is still being made.
"""

import asyncio
import base64
import logging
import re
import time
from collections.abc import AsyncIterator

from backend.config import get_settings
from backend.transcribe import get_client

logger = logging.getLogger(__name__)

OUT_RATE = 24000  # streamed TTS audio: raw PCM16, 24 kHz, mono
MIN_FIRST_CHARS = 40  # a shorter first sentence ("Sure.") is joined with the next one
ATTEMPTS = 2
# The TTS service sometimes doesn't answer at all: give up on the speech after this long without audio
FIRST_AUDIO_TIMEOUT_S = 20
CHUNK_TIMEOUT_S = 15

# End of a sentence: . ! ? and their Devanagari, Arabic and CJK forms, followed by a space (or the CJK mark itself)
SENTENCE_END = re.compile(r"(?<=[.!?।؟])\s+|(?<=[。！？])")


def split_first(text: str) -> list[str]:
    """[first sentence(s), rest], or [text] when it is one sentence."""
    text = text.strip()
    first = ""
    for match in SENTENCE_END.finditer(text):
        first = text[: match.start()].strip()
        if len(first) >= MIN_FIRST_CHARS:
            rest = text[match.end() :].strip()
            return [first, rest] if rest else [first]
    return [text]


async def synthesize(text: str, out: asyncio.Queue):
    """Puts the audio chunks of one TTS request on out, then None. Retries once if no audio came yet."""
    settings = get_settings()
    for attempt in range(1, ATTEMPTS + 1):
        sent = False
        try:
            stream = await get_client().aio.interactions.create(
                model=settings.tts_model,
                input=text,
                response_format={"type": "audio"},
                generation_config={"speech_config": [{"voice": settings.tts_voice}]},
                stream=True,
            )
            async for event in stream:
                delta = getattr(event, "delta", None)
                if event.event_type == "step.delta" and getattr(delta, "type", None) == "audio":
                    data = base64.b64decode(delta.data) if isinstance(delta.data, str) else delta.data
                    await out.put(data)
                    sent = True
            break
        except asyncio.CancelledError:
            raise
        except Exception as e:
            if sent or attempt == ATTEMPTS:
                await out.put(e)
                break
            logger.warning("TTS failed (%s); retrying", e)
    await out.put(None)


async def speak(text: str) -> AsyncIterator[bytes]:
    """Yields the speech for text as 24 kHz PCM16 chunks. Raises if TTS fails."""
    parts = split_first(text)
    queues = [asyncio.Queue() for _ in parts]
    tasks = [asyncio.create_task(synthesize(p, q)) for p, q in zip(parts, queues)]
    started = time.monotonic()
    first = True
    try:
        for queue in queues:
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), FIRST_AUDIO_TIMEOUT_S if first else CHUNK_TIMEOUT_S)
                except asyncio.TimeoutError:
                    raise TimeoutError(f"no audio from {get_settings().tts_model} in time") from None
                if item is None:
                    break
                if isinstance(item, Exception):
                    raise item
                if first:
                    first = False
                    logger.info("TTS first audio after %.2fs (%d part(s))", time.monotonic() - started, len(parts))
                yield item
    finally:
        for task in tasks:
            task.cancel()
