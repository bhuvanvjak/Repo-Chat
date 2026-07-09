// options.js — GitReader setup page: save the user's own Groq API key locally.

const STORAGE_KEY = "gitreader_groq_api_key";

const keyInput = document.getElementById("key-input");
const toggleBtn = document.getElementById("toggle-btn");
const saveBtn = document.getElementById("save-btn");
const statusEl = document.getElementById("status");

chrome.storage.local.get(STORAGE_KEY, (data) => {
  if (data[STORAGE_KEY]) {
    keyInput.value = data[STORAGE_KEY];
    statusEl.textContent = "A key is already saved. Paste a new one to replace it.";
  }
});

toggleBtn.addEventListener("click", () => {
  keyInput.type = keyInput.type === "password" ? "text" : "password";
});

saveBtn.addEventListener("click", () => {
  const key = keyInput.value.trim();
  if (!key) {
    statusEl.className = "error";
    statusEl.textContent = "Enter a key first — get one at console.groq.com/keys.";
    return;
  }
  if (!key.startsWith("gsk_")) {
    statusEl.className = "error";
    statusEl.textContent = 'That doesn\'t look like a Groq key (they start with "gsk_"). Saved anyway — double check if chat fails.';
  } else {
    statusEl.className = "success";
    statusEl.textContent = "Saved. You can close this tab and start using GitReader.";
  }
  chrome.storage.local.set({ [STORAGE_KEY]: key });
});

keyInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter") saveBtn.click();
});
