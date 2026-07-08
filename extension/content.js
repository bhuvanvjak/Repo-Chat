// content.js — runs on github.com pages, detects the repo currently being viewed
// and stores it so the side panel can auto-load it.

const NON_REPO_SEGMENTS = new Set([
  "settings", "notifications", "marketplace", "explore", "topics",
  "trending", "collections", "sponsors", "issues", "pulls", "codespaces",
  "dashboard", "orgs", "new", "login", "join", "about", "features", "search",
]);

function detectRepoFromLocation() {
  const parts = window.location.pathname.split("/").filter(Boolean);
  if (parts.length < 2) return null;
  const [owner, repo] = parts;
  if (NON_REPO_SEGMENTS.has(owner.toLowerCase())) return null;
  return `https://github.com/${owner}/${repo}`;
}

function reportDetectedRepo() {
  const repoUrl = detectRepoFromLocation();
  if (repoUrl) {
    chrome.storage.local.set({ gitreader_detected_repo: repoUrl });
  }
}

// Initial detection.
reportDetectedRepo();

// GitHub is a Turbo/PJAX SPA — the URL can change without a full page reload.
let lastHref = window.location.href;
setInterval(() => {
  if (window.location.href !== lastHref) {
    lastHref = window.location.href;
    reportDetectedRepo();
  }
}, 1000);
