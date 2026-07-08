// sidepanel.js — chat UI logic. Talks to the local GitReader FastAPI backend.

const BACKEND_URL = "http://localhost:8000";
const STORAGE_KEY = "gitreader_session";

const repoInput = document.getElementById("repo-input");
const loadBtn = document.getElementById("load-btn");
const modelSelect = document.getElementById("model-select");
const clearBtn = document.getElementById("clear-btn");
const statusBar = document.getElementById("status-bar");
const chatLog = document.getElementById("chat-log");
const emptyState = document.getElementById("empty-state");
const chatForm = document.getElementById("chat-form");
const chatInput = document.getElementById("chat-input");
const sendBtn = document.getElementById("send-btn");

let sessionId = null;
let messages = []; // {role: 'user'|'assistant'|'error', text: string}
let isBusy = false;

// ---------- Status bar ----------

function setStatus(text, kind) {
  statusBar.textContent = "";
  statusBar.className = kind ? kind : "";
  if (kind === "loading") {
    const spinner = document.createElement("span");
    spinner.className = "spinner";
    statusBar.appendChild(spinner);
  }
  statusBar.appendChild(document.createTextNode(text));
}

// ---------- Persistence ----------

function persistSession() {
  chrome.storage.local.set({
    [STORAGE_KEY]: {
      repo: repoInput.value,
      sessionId,
      model: modelSelect.value,
      messages,
    },
  });
}

function restoreSession() {
  chrome.storage.local.get(STORAGE_KEY, (data) => {
    const saved = data[STORAGE_KEY];
    if (!saved) return;
    if (saved.repo) repoInput.value = saved.repo;
    if (saved.model) modelSelect.value = saved.model;
    if (saved.sessionId && Array.isArray(saved.messages)) {
      sessionId = saved.sessionId;
      messages = saved.messages;
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
    bubble.innerHTML = renderMarkdown(text);
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
  sendBtn.disabled = !enabled || isBusy;
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

loadBtn.addEventListener("click", async () => {
  const repo = repoInput.value.trim();
  if (!repo) {
    setStatus("Enter a repo path or URL first.", "error");
    return;
  }

  loadBtn.disabled = true;
  setStatus("Cloning + indexing repo... this can take a while on first load.", "loading");
  chatLog.querySelectorAll(".msg-row").forEach((el) => el.remove());
  messages = [];
  updateEmptyState();
  setChatEnabled(false);
  sessionId = null;

  try {
    const res = await fetch(`${BACKEND_URL}/load`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ repo, chat_model: modelSelect.value }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `Backend returned ${res.status}`);
    }
    const data = await res.json();
    sessionId = data.session_id;
    setStatus(`Loaded ${data.repo_path} — ${data.num_files} files, ${data.num_chunks} chunks.`, "success");
    setChatEnabled(true);
    chatInput.focus();
    persistSession();
  } catch (e) {
    setStatus("Failed to load repo. Is the GitReader backend running on localhost:8000?", "error");
    renderMessage("error", e.message);
  } finally {
    loadBtn.disabled = false;
  }
});

clearBtn.addEventListener("click", () => {
  if (sessionId) {
    fetch(`${BACKEND_URL}/session/${sessionId}`, { method: "DELETE" }).catch(() => {});
  }
  sessionId = null;
  messages = [];
  chatLog.querySelectorAll(".msg-row").forEach((el) => el.remove());
  updateEmptyState();
  setChatEnabled(false);
  setStatus("Cleared. Load a repo to start a new session.");
  chrome.storage.local.remove(STORAGE_KEY);
});

modelSelect.addEventListener("change", persistSession);

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
  const message = chatInput.value.trim();
  if (!message || !sessionId || isBusy) return;

  renderMessage("user", message);
  messages.push({ role: "user", text: message });
  chatInput.value = "";
  autoResizeInput();
  isBusy = true;
  setChatEnabled(true); // keep input focus-able, but send button disabled via isBusy
  setStatus("Thinking...", "loading");

  const pinnedToBottom = true;
  const row = document.createElement("div");
  row.className = "msg-row assistant";
  const meta = document.createElement("div");
  meta.className = "msg-meta";
  meta.textContent = "GitReader";
  const bubble = document.createElement("div");
  bubble.className = "bubble cursor-blink";
  row.appendChild(meta);
  row.appendChild(bubble);
  chatLog.appendChild(row);
  updateEmptyState();
  scrollToBottom();

  let answerText = "";
  const seenTools = new Set();

  try {
    const res = await fetch(`${BACKEND_URL}/chat/stream`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: sessionId, message }),
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
            const pill = document.createElement("div");
            pill.className = "tool-pill";
            pill.textContent = TOOL_LABELS[event.tool] || `🔧 ${event.tool}`;
            row.insertBefore(pill, bubble);
          }
        } else if (event.type === "token") {
          answerText += event.text;
          bubble.innerHTML = renderMarkdown(answerText) || "&nbsp;";
          bubble.classList.add("cursor-blink");
          if (pinnedToBottom && isScrolledToBottom()) scrollToBottom();
        } else if (event.type === "error") {
          streamError = event.detail;
        }
      }
    }

    bubble.classList.remove("cursor-blink");

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
    bubble.classList.remove("cursor-blink");
    row.remove();
    renderMessage("error", err.message);
    messages.push({ role: "error", text: err.message });
    setStatus("Error — see message above.", "error");
  } finally {
    isBusy = false;
    setChatEnabled(true);
    chatInput.focus();
  }
});

restoreSession();
