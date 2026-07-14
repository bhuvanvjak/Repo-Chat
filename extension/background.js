// background.js — service worker.
//
// Default: clicking the toolbar icon opens GitReader in the side panel (a
// resizable, persistent panel docked to the side of the window). From inside
// the panel, the "pop out" button asks us here to re-open the same UI as a
// detached popup window for users who prefer a floating window.

const POPUP = { width: 460, height: 760 };
let popupWindowId = null;

chrome.runtime.onInstalled.addListener((details) => {
  chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true }).catch(() => {});
  if (details.reason === "install") {
    chrome.runtime.openOptionsPage();
  }
});

// The side panel sends this when its "pop out" button is clicked.
chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg?.action === "popout") {
    openPopoutWindow().then(() => sendResponse({ ok: true }));
    return true; // keep the message channel open for the async response
  }
});

async function openPopoutWindow() {
  // Already open? Just focus it instead of spawning a duplicate.
  if (popupWindowId !== null) {
    try {
      await chrome.windows.get(popupWindowId);
      await chrome.windows.update(popupWindowId, { focused: true });
      return;
    } catch {
      popupWindowId = null; // stale id (window was closed) — fall through and recreate
    }
  }

  const win = await chrome.windows.create({
    url: chrome.runtime.getURL("popup.html?mode=window"),
    type: "popup",
    width: POPUP.width,
    height: POPUP.height,
    focused: true,
  });
  popupWindowId = win.id;
}

chrome.windows.onRemoved.addListener((closedId) => {
  if (closedId === popupWindowId) popupWindowId = null;
});
