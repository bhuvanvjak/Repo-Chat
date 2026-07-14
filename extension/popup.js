// popup.js — chat UI logic. Talks to the local GitReader FastAPI backend.

const BACKEND_URL = "http://localhost:8000";
const STORAGE_KEY = "gitreader_session";
const GROQ_KEY_STORAGE = "gitreader_groq_api_key";

const repoInput = document.getElementById("repo-input");
const loadBtn = document.getElementById("load-btn");
const modelSeg = document.getElementById("model-select");
const clearBtn = document.getElementById("clear-btn");
const settingsBtn = document.getElementById("settings-btn");
const closeBtn = document.getElementById("close-btn");
const popoutBtn = document.getElementById("popout-btn");
const statusBar = document.getElementById("status-bar");
const statusText = document.getElementById("status-text");
const statusTimer = document.getElementById("status-timer");
const statusCancel = document.getElementById("status-cancel");
const progressTrack = document.getElementById("progress-track");
const progressFill = document.getElementById("progress-fill");
const chatLog = document.getElementById("chat-log");
const emptyState = document.getElementById("empty-state");
const chatForm = document.getElementById("chat-form");
const chatInput = document.getElementById("chat-input");
const sendBtn = document.getElementById("send-btn");

let sessionId = null;
let messages = []; // {role: 'user'|'assistant'|'error', text: string}
let isBusy = false;
let githubRepo = null; // "owner/repo", or null if the loaded repo isn't on github.com
let defaultBranch = null;
let loadAbortController = null;
let chatAbortController = null;

// ---------- Model segmented control ----------
// Replaces the old <select>; behaves the same from the rest of the code's POV.

function getModel() {
  const active = modelSeg.querySelector(".seg-btn.active");
  return active ? active.dataset.value : "openai/gpt-oss-20b";
}

function setModel(value) {
  let matched = false;
  modelSeg.querySelectorAll(".seg-btn").forEach((btn) => {
    const on = btn.dataset.value === value;
    btn.classList.toggle("active", on);
    if (on) matched = true;
  });
  // Fall back to the first option if a persisted value no longer exists.
  if (!matched) modelSeg.querySelector(".seg-btn")?.classList.add("active");
}

modelSeg.addEventListener("click", (e) => {
  const btn = e.target.closest(".seg-btn");
  if (!btn) return;
  setModel(btn.dataset.value);
  persistSession();
});

// ---------- Window vs side-panel mode ----------
// The same page backs both the docked side panel (default) and the detached
// popup window (opened via the pop-out button, which appends ?mode=window).
// Show the relevant header buttons for each: pop-out only makes sense from the
// panel; the ✕ close only works in the standalone window.
const isWindowMode = new URLSearchParams(location.search).get("mode") === "window";
if (isWindowMode) {
  popoutBtn.classList.add("hidden");
  closeBtn.classList.remove("hidden");
} else {
  popoutBtn.classList.remove("hidden");
  closeBtn.classList.add("hidden");
}

// Close button — the detached window stays open until this is pressed.
closeBtn.addEventListener("click", () => window.close());

// Pop-out button — ask the service worker to (re)open the UI as a floating window.
popoutBtn.addEventListener("click", () => {
  chrome.runtime.sendMessage({ action: "popout" });
});

// ---------- "Thinking" status cycle ----------
// Rotates friendly status phrases while waiting on the first tool call or token,
// so a long model round-trip reads as active progress rather than a stalled UI.
// Once a real tool call comes back, we switch to reporting that instead of guessing.
const THINKING_PHRASES = [
  "Thinking...",
  "Reading the code...",
  "Tracing logic...",
  "Cross-referencing files...",
  "Piecing it together...",
  "Almost there...",
];
let thinkingTimer = null;

function startThinkingCycle() {
  let idx = 0;
  setStatus(THINKING_PHRASES[idx], "loading");
  thinkingTimer = setInterval(() => {
    idx = (idx + 1) % THINKING_PHRASES.length;
    setStatus(THINKING_PHRASES[idx], "loading");
  }, 2200);
}

function stopThinkingCycle() {
  if (thinkingTimer) {
    clearInterval(thinkingTimer);
    thinkingTimer = null;
  }
}

// ---------- Elapsed-time badge ----------
// A live "· 7s" counter next to the status text. Independent of whatever the
// status text says, so the user always has proof the extension is still alive
// even during silent stretches (cloning, multi-tool-call agent loops, etc).
let elapsedStart = null;
let elapsedInterval = null;
let longWaitNudged = false;

function startElapsedTimer(onLongWait) {
  elapsedStart = Date.now();
  longWaitNudged = false;
  statusTimer.textContent = "· 0s";
  elapsedInterval = setInterval(() => {
    const secs = Math.floor((Date.now() - elapsedStart) / 1000);
    statusTimer.textContent = `· ${secs}s`;
    if (onLongWait && !longWaitNudged && secs >= 12) {
      longWaitNudged = true;
      onLongWait();
    }
  }, 1000);
}

function stopElapsedTimer() {
  if (elapsedInterval) {
    clearInterval(elapsedInterval);
    elapsedInterval = null;
  }
  statusTimer.textContent = "";
}

// Turns a `repo://relative/path.py#L12-L34` citation into a real github.com blob
// URL for the currently loaded repo. Returns null (render as plain text) when the
// loaded repo isn't a github.com repo, since there's nowhere sensible to link to.
function resolveRepoLink(url) {
  if (!githubRepo || !defaultBranch) return null;
  const rest = url.slice("repo://".length); // "path/to/file.py#L12-L34" or "path/to/file.py"
  const [path, hash] = rest.split("#");
  const cleanPath = path.split("/").map(encodeURIComponent).join("/");
  return `https://github.com/${githubRepo}/blob/${encodeURIComponent(defaultBranch)}/${cleanPath}${hash ? "#" + hash : ""}`;
}

function getGroqKey() {
  return new Promise((resolve) => {
    chrome.storage.local.get(GROQ_KEY_STORAGE, (data) => resolve(data[GROQ_KEY_STORAGE] || null));
  });
}

settingsBtn.addEventListener("click", () => chrome.runtime.openOptionsPage());

function setProgress(fraction) {
  if (fraction == null) {
    progressTrack.classList.remove("active", "indeterminate");
    return;
  }
  progressTrack.classList.add("active");
  progressTrack.classList.remove("indeterminate");
  progressFill.style.width = `${Math.round(fraction * 100)}%`;
}

// Shown before the backend has anything measurable to report (e.g. still
// cloning the repo) — a moving segment reads as "working", not "stuck at 0%".
function setProgressIndeterminate() {
  progressTrack.classList.add("active", "indeterminate");
}

// ---------- Status bar ----------

let statusSpinner = null;

function setStatus(text, kind) {
  statusBar.className = kind ? kind : "";
  statusText.textContent = text;
  if (kind === "loading") {
    if (!statusSpinner) {
      statusSpinner = document.createElement("span");
      statusSpinner.className = "spinner";
      statusBar.insertBefore(statusSpinner, statusText);
    }
  } else if (statusSpinner) {
    statusSpinner.remove();
    statusSpinner = null;
  }
}

// Cancel button in the status bar aborts whichever cancellable request is in flight.
statusCancel.addEventListener("click", () => {
  if (loadAbortController) loadAbortController.abort();
  if (chatAbortController) chatAbortController.abort();
});

// ---------- Persistence ----------

function persistSession() {
  chrome.storage.local.set({
    [STORAGE_KEY]: {
      repo: repoInput.value,
      sessionId,
      model: getModel(),
      messages,
      githubRepo,
      defaultBranch,
    },
  });
}

function restoreSession() {
  chrome.storage.local.get(STORAGE_KEY, (data) => {
    const saved = data[STORAGE_KEY];
    if (!saved) return;
    if (saved.repo) repoInput.value = saved.repo;
    if (saved.model) setModel(saved.model);
    if (saved.sessionId && Array.isArray(saved.messages)) {
      sessionId = saved.sessionId;
      messages = saved.messages;
      githubRepo = saved.githubRepo || null;
      defaultBranch = saved.defaultBranch || null;
      messages.forEach((m) => renderMessage(m.role, m.text, false));
      setChatEnabled(true);
      setStatus(`Restored session for ${saved.repo}`, "success");
    }
  });
}

// ---------- Rendering ----------

function updateEmptyState() {
  emptyState.style.display = messages.length === 0 ? "flex" : "none";
}

function isScrolledToBottom() {
  return chatLog.scrollHeight - chatLog.scrollTop - chatLog.clientHeight < 40;
}

function scrollToBottom() {
  chatLog.scrollTop = chatLog.scrollHeight;
}

function renderMessage(role, text, animate) {
  updateEmptyState();
  const row = document.createElement("div");
  row.className = `msg-row ${role}`;
  const meta = document.createElement("div");
  meta.className = "msg-meta";
  meta.textContent = { user: "You", assistant: "GitReader", error: "Error" }[role] || role;
  const bubble = document.createElement("div");
  bubble.className = "bubble";
  if (role === "assistant") {
    bubble.innerHTML = renderMarkdown(text, resolveRepoLink);
  } else {
    bubble.textContent = text;
  }
  row.appendChild(meta);
  row.appendChild(bubble);
  chatLog.appendChild(row);
  if (animate !== false) scrollToBottom();
  return bubble;
}

function setChatEnabled(enabled) {
  chatInput.disabled = !enabled;
  // Stays enabled while busy: at that point it's acting as the Stop button, not Send.
  sendBtn.disabled = !enabled;
}

function setSendButtonBusy(busy) {
  sendBtn.textContent = busy ? "■" : "➤";
  sendBtn.title = busy ? "Stop generating" : "Send";
  sendBtn.classList.toggle("stop-mode", busy);
}

function autoResizeInput() {
  chatInput.style.height = "auto";
  chatInput.style.height = Math.min(chatInput.scrollHeight, 120) + "px";
}

// Copy-to-clipboard on code blocks (event delegation, since bubbles are re-created).
chatLog.addEventListener("click", (e) => {
  const btn = e.target.closest(".copy-btn");
  if (!btn) return;
  const code = btn.parentElement.querySelector("code");
  navigator.clipboard.writeText(code.textContent).then(() => {
    const original = btn.textContent;
    btn.textContent = "Copied!";
    setTimeout(() => (btn.textContent = original), 1200);
  });
});

// ---------- Repo detection ----------

chrome.storage.local.get("gitreader_detected_repo", (data) => {
  if (data.gitreader_detected_repo && !repoInput.value) {
    repoInput.value = data.gitreader_detected_repo;
  }
});

chrome.storage.onChanged.addListener((changes, area) => {
  if (area === "local" && changes.gitreader_detected_repo && !sessionId) {
    repoInput.value = changes.gitreader_detected_repo.newValue;
  }
});

// ---------- Load ----------

function setLoadButtonBusy(busy) {
  loadBtn.textContent = busy ? "Stop" : "Load";
  loadBtn.classList.toggle("stop-mode", busy);
  loadBtn.title = busy ? "Cancel loading" : "";
}

loadBtn.addEventListener("click", async () => {
  if (loadAbortController) {
    // Already loading — this click means "stop", not "start another load".
    loadAbortController.abort();
    return;
  }

  const repo = repoInput.value.trim();
  if (!repo) {
    setStatus("Enter a repo path or URL first.", "error");
    return;
  }

  const groqApiKey = await getGroqKey();
  if (!groqApiKey) {
    setStatus("No Groq API key set yet — opening setup...", "error");
    chrome.runtime.openOptionsPage();
    return;
  }

  loadAbortController = new AbortController();
  setLoadButtonBusy(true);
  setStatus("Starting...", "loading");
  setProgressIndeterminate();
  startElapsedTimer(() => setStatus("Still working — first-time indexing of larger repos can take a minute or two.", "loading"));
  chatLog.querySelectorAll(".msg-row").forEach((el) => el.remove());
  messages = [];
  githubRepo = null;
  defaultBranch = null;
  updateEmptyState();
  setChatEnabled(false);
  sessionId = null;

  try {
    const res = await fetch(`${BACKEND_URL}/load/stream`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ repo, chat_model: getModel(), groq_api_key: groqApiKey }),
      signal: loadAbortController.signal,
    });
    if (!res.ok || !res.body) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `Backend returned ${res.status}`);
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let done_ = false;
    let doneData = null;
    let loadError = null;

    while (!done_) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      buffer = lines.pop();

      for (const line of lines) {
        if (!line.trim()) continue;
        const event = JSON.parse(line);
        if (event.type === "status") {
          setStatus(event.message, "loading");
        } else if (event.type === "embedding_progress") {
          setStatus(`Embedding chunks... ${event.done}/${event.total}`, "loading");
          setProgress(event.total ? event.done / event.total : 1);
        } else if (event.type === "done") {
          doneData = event;
          done_ = true;
        } else if (event.type === "error") {
          loadError = event.detail;
          done_ = true;
        }
      }
    }

    if (loadError) throw new Error(loadError);
    if (!doneData) throw new Error("Stream ended without completing.");

    sessionId = doneData.session_id;
    githubRepo = doneData.github_repo || null;
    defaultBranch = doneData.default_branch || null;
    setStatus(`Loaded ${doneData.repo_path} — ${doneData.num_files} files, ${doneData.num_chunks} chunks.`, "success");
    setProgress(null);
    setChatEnabled(true);
    chatInput.focus();
    persistSession();
  } catch (e) {
    if (e.name === "AbortError") {
      setStatus("Load cancelled.", "");
    } else {
      setStatus("Failed to load repo. Is the GitReader backend running on localhost:8000?", "error");
      renderMessage("error", e.message);
    }
    setProgress(null);
  } finally {
    stopElapsedTimer();
    loadAbortController = null;
    setLoadButtonBusy(false);
  }
});

clearBtn.addEventListener("click", () => {
  if (sessionId) {
    fetch(`${BACKEND_URL}/session/${sessionId}`, { method: "DELETE" }).catch(() => {});
  }
  sessionId = null;
  messages = [];
  githubRepo = null;
  defaultBranch = null;
  chatLog.querySelectorAll(".msg-row").forEach((el) => el.remove());
  updateEmptyState();
  setChatEnabled(false);
  setStatus("Cleared. Load a repo to start a new session.");
  chrome.storage.local.remove(STORAGE_KEY);
});

// ---------- Chat (streaming) ----------

chatInput.addEventListener("input", autoResizeInput);
chatInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    chatForm.requestSubmit();
  }
});

const TOOL_LABELS = {
  semantic_search: "🔎 Searching code",
  read_file: "📄 Reading file",
  list_directory: "📂 Listing directory",
  grep_code: "🔍 Grepping",
  git_log: "🕒 Checking history",
  git_blame: "🕵️ Checking blame",
};

chatForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  if (isBusy) {
    // Send button doubles as Stop while a response is in flight.
    chatAbortController?.abort();
    return;
  }
  const message = chatInput.value.trim();
  if (!message || !sessionId) return;

  renderMessage("user", message);
  messages.push({ role: "user", text: message });
  chatInput.value = "";
  autoResizeInput();
  isBusy = true;
  setChatEnabled(true); // keep input focus-able; sendBtn stays enabled to act as Stop
  setSendButtonBusy(true);
  startThinkingCycle();
  startElapsedTimer(() => setStatus("Still working — chaining a few tool calls can take a bit longer.", "loading"));
  chatAbortController = new AbortController();

  const pinnedToBottom = true;
  const row = document.createElement("div");
  row.className = "msg-row assistant";
  const meta = document.createElement("div");
  meta.className = "msg-meta";
  meta.textContent = "GitReader";
  const bubble = document.createElement("div");
  bubble.className = "bubble skeleton";
  bubble.innerHTML = '<div class="skeleton-line"></div><div class="skeleton-line"></div><div class="skeleton-line"></div>';
  row.appendChild(meta);
  row.appendChild(bubble);
  chatLog.appendChild(row);
  updateEmptyState();
  scrollToBottom();

  let answerText = "";
  const seenTools = new Set();
  let activePill = null;

  try {
    const res = await fetch(`${BACKEND_URL}/chat/stream`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: sessionId, message }),
      signal: chatAbortController.signal,
    });
    if (!res.ok || !res.body) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `Backend returned ${res.status}`);
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let streamError = null;

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      buffer = lines.pop(); // last (possibly incomplete) line stays in the buffer

      for (const line of lines) {
        if (!line.trim()) continue;
        const event = JSON.parse(line);
        if (event.type === "tool") {
          if (!seenTools.has(event.tool)) {
            seenTools.add(event.tool);
            if (activePill) activePill.classList.add("done");
            const pill = document.createElement("div");
            pill.className = "tool-pill";
            const label = TOOL_LABELS[event.tool] || `🔧 ${event.tool}`;
            pill.innerHTML = `<span class="pill-icon"><span class="spinner"></span><span class="check">✓</span></span><span class="pill-label">${label}</span>`;
            row.insertBefore(pill, bubble);
            activePill = pill;
            // Real tool activity is a better signal than the generic rotating
            // phrases — stop guessing and report what's actually happening.
            stopThinkingCycle();
            setStatus(`${label}...`, "loading");
          }
        } else if (event.type === "token") {
          if (!answerText) {
            stopThinkingCycle();
            setStatus("Writing response...", "loading");
            bubble.classList.remove("skeleton");
            bubble.innerHTML = "";
          }
          answerText += event.text;
          bubble.innerHTML = renderMarkdown(answerText, resolveRepoLink) || "&nbsp;";
          bubble.classList.add("cursor-blink");
          if (pinnedToBottom && isScrolledToBottom()) scrollToBottom();
        } else if (event.type === "error") {
          streamError = event.detail;
        }
      }
    }

    stopThinkingCycle();
    bubble.classList.remove("cursor-blink", "skeleton");
    if (activePill) activePill.classList.add("done");
    if (!answerText) bubble.innerHTML = "&nbsp;";

    if (streamError && !answerText) {
      throw new Error(streamError);
    }
    if (streamError) {
      const note = document.createElement("div");
      note.className = "msg-meta";
      note.textContent = `(stream ended early: ${streamError})`;
      row.appendChild(note);
    }

    messages.push({ role: "assistant", text: answerText });
    setStatus("Ready.", "success");
    persistSession();
  } catch (err) {
    stopThinkingCycle();
    bubble.classList.remove("cursor-blink", "skeleton");
    if (activePill) activePill.classList.add("done");
    if (err.name === "AbortError") {
      if (answerText) {
        // Keep whatever was streamed so far rather than throwing it away.
        const note = document.createElement("div");
        note.className = "msg-meta";
        note.textContent = "(stopped)";
        row.appendChild(note);
        messages.push({ role: "assistant", text: answerText });
      } else {
        row.remove();
      }
      setStatus("Stopped.", "");
    } else {
      row.remove();
      renderMessage("error", err.message);
      messages.push({ role: "error", text: err.message });
      setStatus("Error — see message above.", "error");
    }
  } finally {
    stopElapsedTimer();
    isBusy = false;
    chatAbortController = null;
    setSendButtonBusy(false);
    setChatEnabled(true);
    chatInput.focus();
  }
});

restoreSession();
