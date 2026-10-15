from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    groq_api_key: str = ""
    groq_model: str = "openai/gpt-oss-20b"
    # Small, fast model used only to classify intent before retrieval
    router_model: str = "openai/gpt-oss-20b"
    # Who writes RAG answers: gemini (better across languages) or groq. Groq is also the fallback if Gemini fails.
    answer_provider: str = "gemini"
    answer_model: str = "gemini-3.5-flash-lite"

    embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2"

    milvus_db_path: str = "./data/milvus.db"
    collection_name: str = "docs_rag"
    docs_dir: str = "./docs"

    chunk_size: int = 1000
    chunk_overlap: int = 150
    top_k: int = 4

    # Voice (Gemini Live API)
    gemini_api_key: str = ""
    live_model: str = "gemini-3.1-flash-live-preview"
    live_voice: str = "Kore"
    # agent: Gemini searches the docs and answers; rag: Gemini reads the answer from the RAG chain
    live_answer_mode: str = "agent"
    # vad: hands-free (Gemini detects end of speech); ptt: push-to-talk
    live_talk_mode: str = "vad"
    # Dictation: transcribes each spoken clip into the message box
    transcribe_model: str = "gemini-3.5-flash-lite"
    # STT + TTS voice pipeline (/stt-tts): streaming transcription, the RAG answer, then speech
    stt_model: str = "gemini-3.5-transcribe-live"
    tts_model: str = "gemini-3.8-flash-tts"
    tts_voice: str = "Sulafat"

    # Comma-separated origins allowed to call the API directly from a browser.
    # Only needed when the frontend is served from a different origin.
    cors_origins: str = "https://localhost:5174,https://127.0.0.1:5174"


@lru_cache
def get_settings() -> Settings:
    return Settings()
