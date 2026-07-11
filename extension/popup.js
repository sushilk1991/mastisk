// ============================================================
// Mastisk Clipper — Popup
// ============================================================

const dot = document.getElementById('dot');
const statusEl = document.getElementById('status');

async function checkConnection() {
  try {
    const resp = await chrome.runtime.sendMessage({ action: 'health' });
    const online = !!resp?.ok;
    dot.classList.toggle('online', online);
    dot.classList.toggle('offline', !online);
    statusEl.textContent = online ? 'connected' : 'offline';
  } catch {
    dot.classList.add('offline');
    statusEl.textContent = 'offline';
  }
}

// Send the current page (background does the extraction + POST + tracking).
document.getElementById('send-page').addEventListener('click', () => {
  chrome.runtime.sendMessage({ action: 'send-page' });
  window.close();
});

// Open the side panel for the current tab. chrome.sidePanel.open() must be
// invoked while the user gesture is still live, so it must not sit behind an
// `await`. We use the callback form of tabs.query and call open() as the first
// statement in that callback (no awaited work precedes it).
document.getElementById('open-chat').addEventListener('click', () => {
  chrome.tabs.query({ active: true, currentWindow: true }, ([tab]) => {
    if (tab?.id != null) {
      chrome.sidePanel.open({ tabId: tab.id }).catch(() => {}).finally(() => window.close());
    } else {
      window.close();
    }
  });
});

document.getElementById('open-options').addEventListener('click', (e) => {
  e.preventDefault();
  chrome.runtime.openOptionsPage();
  window.close();
});

checkConnection();
