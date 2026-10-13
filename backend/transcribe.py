"""Dictation: transcribe one spoken clip (sent by the browser after each pause) with a Gemini Flash model."""

import io
import logging
import wave
from functools import lru_cache

from google import genai
from google.genai import types
from pydantic import BaseModel

from backend.config import get_settings
from backend.language import is_known, looks_romanized

logger = logging.getLogger(__name__)

RATE = 16000  # the browser sends 16 kHz mono PCM16, same as the voice chat mic
MAX_SECONDS = 60

PROMPT = """Transcribe the speech in this audio exactly as spoken, in the language spoken. Never translate.
Write each language in its usual script (for example Hindi in Devanagari, Telugu in Telugu script, Arabic in
Arabic script), never romanized. English words the speaker uses stay in English.{hint}

Return `text`: the transcript, empty if there is no speech, and `language`: the BCP-47 code of the speech
("en", "hi", "te", "pt-BR", ...), or "und" if there is no speech or you can't tell."""

HINT = "\nThe speaker is speaking the language with BCP-47 code {language}."


class Transcript(BaseModel):
    text: str = ""
    language: str = "und"


@lru_cache
def get_client() -> genai.Client:
    return genai.Client(api_key=get_settings().gemini_api_key)


def to_wav(pcm: bytes) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm)
    return buf.getvalue()


async def transcribe_once(wav: bytes, language: str | None) -> Transcript:
    hint = HINT.format(language=language) if is_known(language) else ""
    response = await get_client().aio.models.generate_content(
        model=get_settings().transcribe_model,
        contents=[types.Part.from_bytes(data=wav, mime_type="audio/wav"), PROMPT.format(hint=hint)],
        # Minimal thinking keeps a short clip at ~1.5 s instead of ~2.5 s
        config=types.GenerateContentConfig(
            temperature=0,
            thinking_config=types.ThinkingConfig(thinking_level="MINIMAL"),
            response_mime_type="application/json",
            response_schema=Transcript,
        ),
    )
    result = response.parsed or Transcript()
    return Transcript(text=result.text.strip(), language=result.language.strip() or "und")


async def transcribe(pcm: bytes, language: str | None = None) -> dict:
    """language: a hint (the conversation's language). Returns {"text", "language"}."""
    wav = to_wav(pcm)
    result = await transcribe_once(wav, language)
    if not is_known(language):
        # A first dictation with no language known: logged so the no-hint success rate can be measured
        logger.info("Dictation without a hint: language=%s text=%r", result.language, result.text)
        if looks_romanized(result.text, result.language):
            # Came back romanized: retry once with the detected language as the hint
            result = await transcribe_once(wav, result.language)
            logger.info("Retried with hint %s: %r", result.language, result.text)
    return result.model_dump()
