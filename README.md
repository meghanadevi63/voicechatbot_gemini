# RAG Chatbot (React + FastAPI + LangChain + Milvus Lite + Gemini Live voice)

Ask questions about the PDFs in `docs/`, by typing or by voice. Typed answers come from Groq, grounded in chunks retrieved from a local Milvus Lite database (`data/milvus.db`, no Docker or server needed). Voice uses the Gemini Live API (`gemini-3.1-flash-live-preview`): answers are spoken and also shown in the chat with their sources.

```
React (frontend/) ──HTTP /chat──────▶ FastAPI (backend/main.py) ──▶ RAG chain (backend/rag.py) ──▶ Groq LLM
       │                                   │                               │
       ├──HTTP /transcribe (dictation)───▶ backend/transcribe.py ──▶ Gemini Flash Lite     ▼
       │                                                                   Milvus Lite file ◀── backend/ingest.py ◀── docs/*.pdf
       └──WebSocket /live (voice)───────▶ backend/live.py ──▶ Gemini Live          (HuggingFace embeddings: all-MiniLM-L6-v2)
                                             tools: search_documents → retriever, ask_knowledge_base → RAG chain
```

The design, protocol and test results for voice are in [voice_plan.md](voice_plan.md).

## Setup

The system `python3.11` on this machine is 3.11.0rc1, which breaks torch. Use a uv-managed Python 3.11 instead:

```bash
uv python install 3.11
uv venv --python 3.11 .venv
source .venv/bin/activate
uv pip install -r requirements.txt --index-strategy unsafe-best-match

cp .env.example .env    # then set GROQ_API_KEY, and GEMINI_API_KEY for voice
```

Frontend (Node 20+):

```bash
cd frontend
npm install
```

## Run

```bash
# 1. Backend (port 8002)
uvicorn backend.main:app --host 0.0.0.0 --port 8002

# 2. Index the docs (first run, or after adding PDFs) — or use "Re-ingest documents" in the UI sidebar
curl -X POST localhost:8002/ingest

# 3. Frontend, development (hot reload): https://localhost:5174
cd frontend && npm run dev
```

The Vite dev server forwards `/health`, `/chat`, `/ingest`, `/transcribe` and the `/live` WebSocket to the backend, so no CORS setup is needed. To point it at a backend on another host or port, start it with `BACKEND_URL=http://host:port npm run dev`.

The dev server listens on all network interfaces. Other machines on the same network can open the `Network:` URL that `npm run dev` prints (`https://<this-machine-ip>:5174`). If they can't connect, allow the port through the firewall (`sudo ufw allow 5174/tcp`).

The dev server uses HTTPS with a self-signed certificate (`@vitejs/plugin-basic-ssl`), because browsers only allow microphone access on secure pages. On first visit the browser shows a "not private" warning: click **Advanced → Proceed** once per browser.

If the browser calls the backend directly instead of going through the Vite proxy (`VITE_BACKEND_URL` set, or the frontend hosted elsewhere), add the frontend's origin to `CORS_ORIGINS` in `.env`. The same list decides which pages may open the voice WebSocket (`/live`), because CORS doesn't cover WebSockets. Same-host pages are always allowed.

For a single-server setup, build the frontend once and FastAPI serves it at http://localhost:8002 (or `http://<this-machine-ip>:8002` from other machines):

```bash
cd frontend && npm run build    # writes frontend/dist/, picked up on backend start
```

## Voice

Two buttons next to the message box:

- **Mic: dictation.** Click it and speak. Each time you pause, your words are transcribed into the message box, and further speech is appended. Edit if you like, then press Send. The answer appears in the chat and is spoken aloud. Click the mic again (it turns into a red stop button) to stop recording. Sending while still recording waits for your last words.
- **Waveform: hands-free conversation.** Click it and talk. Gemini answers out loud when you pause, and you can interrupt by talking over it. The voice bar above the message box shows the state and has Mute and End.

Voice turns are saved in the chat like typed ones (marked "Voice"), so you can switch between typing and talking in one conversation.

The sidebar's **Voice answers** setting picks who writes spoken answers:

| Setting | What happens |
|---|---|
| Gemini answers (default) | Gemini searches the documents (`search_documents` tool) and answers. Fastest. |
| Read RAG answer | Gemini calls the same Groq RAG chain as typed chat (`ask_knowledge_base` tool) and reads its answer. The chat stores the RAG text. About a second slower. |

Notes:

- The browser needs a secure page for the microphone: use the HTTPS dev server, or `localhost`.
- In hands-free mode on speakers, echo cancellation relies on the browser. Headphones avoid the model hearing itself.
- `gemini-3.1-flash-live-preview` is a preview model. It occasionally fails with `1011 Internal error`, or is slow, for a few minutes at a time. The voice bar then shows the error. Start again once it recovers.
- `scripts/live_spike.py` and `scripts/live_ws_test.py` test the Live model and the `/live` endpoint from Python (see their docstrings).

Milvus Lite allows only one process to open the database. While the backend is running, ingest through `POST /ingest`. Use `python -m backend.ingest` only when the backend is stopped.

## API

| Method | Path      | Body                                  | Returns                         |
|--------|-----------|---------------------------------------|---------------------------------|
| GET    | `/health` | –                                     | `{"status": "ok"}`              |
| POST   | `/chat`   | `{"question": "...", "history": [...]}` | `{"answer": "...", "sources": [...]}` |
| POST   | `/ingest` | –                                     | `{"files", "pages", "chunks"}`  |
| POST   | `/transcribe` | raw 16 kHz mono PCM16 audio (≤ 60 s) | `{"text": "..."}`             |
| WS     | `/live?answer=agent\|rag` | see [voice_plan.md](voice_plan.md#websocket-protocol-live) | audio + JSON events |

## Configuration (`.env`)

| Variable          | Default                                  |
|-------------------|------------------------------------------|
| `GROQ_API_KEY`    | – (required)                             |
| `GROQ_MODEL`      | `openai/gpt-oss-20b`                     |
| `ROUTER_MODEL`    | `openai/gpt-oss-20b`                     |
| `EMBEDDING_MODEL` | `sentence-transformers/all-MiniLM-L6-v2` |
| `MILVUS_DB_PATH`  | `./data/milvus.db`                       |
| `COLLECTION_NAME` | `docs_rag`                               |
| `DOCS_DIR`        | `./docs`                                 |
| `CHUNK_SIZE` / `CHUNK_OVERLAP` / `TOP_K` | `1000` / `150` / `4` |
| `CORS_ORIGINS`    | `https://localhost:5174,https://127.0.0.1:5174` |
| `GEMINI_API_KEY`  | – (required for voice)                   |
| `LIVE_MODEL`      | `gemini-3.1-flash-live-preview`          |
| `LIVE_VOICE`      | `Kore`                                   |
| `LIVE_ANSWER_MODE` | `agent` (only for clients that don't pass `answer`) |
| `TRANSCRIBE_MODEL` | `gemini-3.5-flash-lite`                 |

Don't name a variable `MILVUS_URI`. pymilvus reads it from `.env` itself and expects a server URL.

If you change `EMBEDDING_MODEL`, re-run ingestion. Vectors from different models aren't compatible.
