// background.js — service worker. Makes the toolbar icon open the side panel,
// and walks first-time installers through setup (Groq API key).

chrome.runtime.onInstalled.addListener((details) => {
  chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true }).catch(() => {});
  if (details.reason === "install") {
    chrome.runtime.openOptionsPage();
  }
});
