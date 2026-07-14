# Repo Chat

A "chat with any git repo" agent: a LangGraph ReAct agent (chat reasoning via the
free [Groq](https://console.groq.com) API, embeddings run locally via
sentence-transformers) with tools for semantic code search, file reading, grep,
and git history — exposed through a small backend API and used from a Chrome extension
that auto-detects the repo you're viewing on github.com and answers your questions about that repo.

Only the chat completions call goes to Groq's API; everything else (cloning, indexing,
embeddings, vector search) runs on your machine.

## Layout

```
gitreader/
  backend/
    loader.py
    splitter.py
    vectorstore.py
    tools.py
    agent.py
    server.py
    requirements.txt
    .env.example
  extension/
    manifest.json
    background.js
    content.js
    popup.html
    popup.js
```

## 1. Prerequisites

- Python 3.11+
- A free [Groq API key](https://console.groq.com/keys) (chat/agent reasoning only —
  embeddings run locally, no key needed for those)
- Copy `backend/.env.example` to `backend/.env` and fill in `GROQ_API_KEY`
- Default models (both overridable via the `/load` request body, see `server.py`):
  - chat: `openai/gpt-oss-20b` — picked deliberately over Meta's Llama models on
    Groq: `llama-3.3-70b-versatile` reliably emits a malformed, non-JSON tool
    call (`<function=name{...}</function>`, Llama's native "pythonic" tool-call
    syntax) once several custom tools are bound, which Groq's API rejects with
    a 400 `tool_use_failed`. The `openai/gpt-oss-*` models use standard
    JSON tool calling and don't hit this. `openai/gpt-oss-120b` is a stronger
    (slower) alternative if `-20b`'s answers feel shallow.
  - embeddings: `sentence-transformers/all-MiniLM-L6-v2` (downloaded automatically
    on first run and cached locally)

## 2. Run the backend

```bash
cd backend
python -m venv venv && source venv/bin/activate   # optional but recommended
pip install -r requirements.txt
uvicorn server:app --reload --port 8000
```

Sanity check: `curl http://localhost:8000/health` should return `{"status":"ok"}`.

You can also drive the agent from the command line without the extension:

```bash
python agent.py /path/to/local/repo
# or
python agent.py https://github.com/some-owner/some-repo
```

## 3. Load the Chrome extension

1. Open `chrome://extensions`, enable **Developer mode** (top right).
2. Click **Load unpacked**, select the `extension/` folder.
3. Pin the GitReader icon to your toolbar.
4. Visit any repo on github.com, click the GitReader icon — it opens a compact
   popup window with the repo URL pre-filled (auto-detected from the page). The
   window stays open until you close it with the ✕ button, so a load or a
   streaming answer won't get interrupted if you click back onto the page.
5. Click **Load** (first load clones + indexes the repo, can take a bit depending on
   repo size and your machine), then start chatting.

You can also paste in a local path or any git URL manually, not just what's
auto-detected — the field is just pre-filled as a convenience.

## How it works

- `loader.py` clones (or reads a local path) and walks the repo, keeping only
  text/code files under a size cap.
- `splitter.py` chunks each file with a language-aware splitter (respects
  function/class boundaries per language) so retrieval doesn't return
  arbitrarily-cut code.
- `vectorstore.py` embeds chunks locally with a sentence-transformers model into a
  per-repo persistent Chroma collection (cached under `~/.gitreader/chroma`,
  keyed by a hash of the repo path — re-loading the same repo won't re-embed
  unless you pass `force_rebuild`).
- `tools.py` gives the agent six tools: `semantic_search`, `read_file`,
  `list_directory`, `grep_code`, `git_log`, `git_blame` — combining RAG with
  live repo exploration, since some questions ("who last touched this line")
  aren't answerable from embeddings alone.
- `agent.py` builds a LangGraph `create_react_agent` around `ChatGroq`, with
  `MemorySaver` checkpointing so each browser-side session keeps conversational
  context across turns.
- `server.py` is a thin FastAPI wrapper (`/load`, `/chat`) so the browser
  extension — which can't clone repos or run git itself — can drive the agent
  over HTTP on localhost.
- The extension's `content.js` detects the `owner/repo` from the current
  github.com URL (re-checking on SPA navigation) and stores it via
  `chrome.storage`; `popup.js` reads that, calls the backend, and renders
  the chat. `background.js` opens the UI as a persistent detached popup window
  (`chrome.windows.create`) rather than a Chrome action popup, so it doesn't
  force-close on blur.

## Known limitations / next steps

- The FastAPI server has no auth and permissive CORS — fine for `localhost`-only
  local use, don't expose it on a network.
- Sessions are in-memory; restarting `uvicorn` loses active chat threads (the
  Chroma index itself persists on disk, so reloading a repo is fast the 2nd time).
- Large monorepos will take a while to embed on first load — there's no
  incremental/background indexing yet.
- Groq's free tier has rate limits (requests/tokens per minute); heavy use can
  hit `429`s. Swapping `ChatGroq` for `ChatOpenAI`/`ChatAnthropic` in `agent.py`
  is a small change if you need a different provider.
