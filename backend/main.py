import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from backend.config import get_settings
from backend.ingest import ingest
from backend.live import LiveBridge
from backend.rag import RAGChain
from backend.transcribe import MAX_SECONDS, RATE, transcribe

# Uvicorn only configures its own loggers; this shows the app's (routing, languages, voice sessions)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
for noisy in ("httpx", "google_genai"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

class ChatTurn(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class ChatRequest(BaseModel):
    question: str
    history: list[ChatTurn] = []
    # The conversation's language (BCP-47), used when the question's own language is unclear
    language: str | None = None


class Source(BaseModel):
    source: str | None
    page: int | None
    content: str


class ChatResponse(BaseModel):
    answer: str
    sources: list[Source]
    # Language of the question (BCP-47), or the conversation's language, or "und"
    language: str


state: dict = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    state["rag"] = RAGChain()
    yield
    state.clear()


app = FastAPI(title="RAG Chatbot API", lifespan=lifespan)

origins = [o.strip() for o in get_settings().cors_origins.split(",") if o.strip()]
if origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type"],
    )


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest):
    if not req.question.strip():
        raise HTTPException(status_code=400, detail="Question is empty")
    try:
        return await run_in_threadpool(
            state["rag"].answer, req.question, [t.model_dump() for t in req.history], req.language
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/transcribe")
async def transcribe_clip(request: Request):
    """Body: raw 16 kHz mono PCM16 (one dictated clip). Returns {"text": ...}."""
    pcm = await request.body()
    if not pcm:
        raise HTTPException(status_code=400, detail="No audio")
    if len(pcm) > RATE * 2 * MAX_SECONDS:
        raise HTTPException(status_code=413, detail=f"Clip longer than {MAX_SECONDS} s")
    if not get_settings().gemini_api_key:
        raise HTTPException(status_code=500, detail="GEMINI_API_KEY is not set on the server")
    try:
        return {"text": await transcribe(pcm[: len(pcm) // 2 * 2])}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Transcription failed: {e}")


def origin_allowed(ws: WebSocket) -> bool:
    """CORS doesn't cover WebSockets: only accept same-host pages, configured origins and non-browser clients."""
    origin = ws.headers.get("origin")
    if not origin:
        return True
    return urlparse(origin).netloc == ws.headers.get("host") or origin in origins


@app.websocket("/live")
async def live(ws: WebSocket, answer: str | None = None, talk: str | None = None):
    settings = get_settings()
    answer = answer or settings.live_answer_mode
    talk = talk or settings.live_talk_mode
    if not origin_allowed(ws):
        await ws.close(code=1008, reason="Origin not allowed")
        return
    await ws.accept()
    if answer not in ("agent", "rag") or talk not in ("vad", "ptt"):
        await ws.close(code=1008, reason="answer must be agent|rag and talk must be vad|ptt")
        return
    if not settings.gemini_api_key:
        await ws.close(code=1011, reason="GEMINI_API_KEY is not set on the server")
        return
    await LiveBridge(ws, state["rag"], answer, talk).run()


@app.post("/ingest")
async def ingest_docs():
    try:
        result = await run_in_threadpool(ingest)
        state["rag"].refresh()
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# Serve the built React app (frontend/dist) when present. Mounted last so the
# API routes above take precedence over static files.
FRONTEND_DIST = Path(__file__).resolve().parent.parent / "frontend" / "dist"
if FRONTEND_DIST.is_dir():
    app.mount("/", StaticFiles(directory=FRONTEND_DIST, html=True), name="frontend")
