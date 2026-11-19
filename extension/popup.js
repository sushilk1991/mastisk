// ============================================================
// Mastisk Clipper — Popup
// ============================================================

const dot = document.getElementById('dot');
const statusEl = document.getElementById('status');
const serverEl = document.getElementById('server');
const grantBtn = document.getElementById('grant');
const wikiLink = document.getElementById('open-wiki');

function showStatus(online, text) {
  dot.classList.toggle('online', online);
  dot.classList.toggle('offline', !online);
  statusEl.textContent = text;
}

async function checkConnection() {
  grantBtn.hidden = true;
  wikiLink.hidden = true;
  let resp;
  try {
    resp = await chrome.runtime.sendMessage({ action: 'health' });
  } catch {
    resp = null;
  }
  if (resp?.ok) {
    showStatus(true, 'connected');
    // Say which daemon answered: with fallbacks it is no longer obvious.
    serverEl.textContent = MastiskServer.serverLabel(resp.serverUrl);
    serverEl.title = resp.serverUrl;
    wikiLink.href = resp.linkBase;
    wikiLink.hidden = false;
    return;
  }
  showStatus(false, 'offline');
  serverEl.textContent = '';
  // A known server that Chrome will not let us reach (site access was
  // restricted): one click here grants it, with no trip to Settings.
  const blocked = resp?.needsPermission?.[0];
  if (blocked) {
    grantBtn.textContent = `Allow access to ${MastiskServer.serverLabel(blocked)}`;
    grantBtn.dataset.pattern = MastiskServer.hostPermissionPattern(blocked);
    grantBtn.hidden = false;
  }
}

// permissions.request must run inside the click itself.
grantBtn.addEventListener('click', () => {
  chrome.permissions.request({ origins: [grantBtn.dataset.pattern] })
    .catch(() => false)
    .then((granted) => { if (granted) checkConnection(); });
});

// Opens the wiki at the address other devices can use too.
wikiLink.addEventListener('click', (e) => {
  e.preventDefault();
  chrome.tabs.create({ url: wikiLink.href });
  window.close();
});

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
