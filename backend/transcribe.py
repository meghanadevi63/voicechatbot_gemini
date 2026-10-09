"""Dictation: transcribe one spoken clip (sent by the browser after each pause) with a Gemini Flash model."""

import io
import wave
from functools import lru_cache

from google import genai
from google.genai import types

from backend.config import get_settings

RATE = 16000  # the browser sends 16 kHz mono PCM16, same as the voice chat mic
MAX_SECONDS = 60

PROMPT = (
    "Transcribe the speech in this audio exactly as spoken. Reply with only the transcript. "
    "If there is no speech, reply with nothing."
)


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


async def transcribe(pcm: bytes) -> str:
    response = await get_client().aio.models.generate_content(
        model=get_settings().transcribe_model,
        contents=[types.Part.from_bytes(data=to_wav(pcm), mime_type="audio/wav"), PROMPT],
        # Minimal thinking keeps a short clip at ~1.5 s instead of ~2.5 s
        config=types.GenerateContentConfig(
            temperature=0, thinking_config=types.ThinkingConfig(thinking_level="MINIMAL")
        ),
    )
    return (response.text or "").strip()
