"""
server.py — Local FastAPI backend wrapping the GitReader agent.

Run with:  uvicorn server:app --reload --port 8000

Exposes:
  POST /load          {repo, chat_model?, embedding_model?} -> {session_id, repo_path, num_files, num_chunks}
  POST /chat          {session_id, message}                 -> {reply}
  POST /chat/stream   {session_id, message}                 -> newline-delimited JSON events
                                                                 ({"type": "tool"|"token"|"error"|"done", ...})
  GET  /health -> {status}

Designed to be called from the GitReader browser extension running on
http://localhost / a chrome-extension:// origin — see extension/ for the client.
"""
import json
import uuid

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

load_dotenv()  # picks up GROQ_API_KEY from a .env file in backend/, if present

from agent import ask, ask_stream, build_agent
from loader import load_repo
from splitter import split_documents
from vectorstore import build_or_load_vectorstore

app = FastAPI(title="GitReader Backend")

# Local-only tool: a permissive CORS policy is acceptable since this server should
# only ever be bound to localhost. Tighten allow_origins if you expose it further.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# session_id -> {"repo_path": str, "vectorstore": Chroma, "agent": CompiledGraph}
SESSIONS: dict[str, dict] = {}


class LoadRequest(BaseModel):
    repo: str
    chat_model: str = "openai/gpt-oss-20b"
    embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    force_rebuild: bool = False


class LoadResponse(BaseModel):
    session_id: str
    repo_path: str
    num_files: int
    num_chunks: int


class ChatRequest(BaseModel):
    session_id: str
    message: str


class ChatResponse(BaseModel):
    reply: str


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/load", response_model=LoadResponse)
def load(req: LoadRequest):
    try:
        repo_path, docs = load_repo(req.repo)
        chunks = split_documents(docs)
        store = build_or_load_vectorstore(
            repo_path, chunks, embedding_model=req.embedding_model, force_rebuild=req.force_rebuild
        )
        agent = build_agent(repo_path, store, chat_model=req.chat_model)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    session_id = str(uuid.uuid4())
    SESSIONS[session_id] = {"repo_path": repo_path, "vectorstore": store, "agent": agent}

    return LoadResponse(
        session_id=session_id, repo_path=repo_path, num_files=len(docs), num_chunks=len(chunks)
    )


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest):
    session = SESSIONS.get(req.session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Unknown session_id. Call /load first.")
    try:
        reply = ask(session["agent"], thread_id=req.session_id, question=req.message)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return ChatResponse(reply=reply)


@app.post("/chat/stream")
def chat_stream(req: ChatRequest):
    session = SESSIONS.get(req.session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Unknown session_id. Call /load first.")

    def event_generator():
        try:
            for event in ask_stream(session["agent"], thread_id=req.session_id, question=req.message):
                yield json.dumps(event) + "\n"
        except Exception as e:
            yield json.dumps({"type": "error", "detail": str(e)}) + "\n"
        yield json.dumps({"type": "done"}) + "\n"

    return StreamingResponse(event_generator(), media_type="application/x-ndjson")


@app.delete("/session/{session_id}")
def close_session(session_id: str):
    SESSIONS.pop(session_id, None)
    return {"status": "closed"}
