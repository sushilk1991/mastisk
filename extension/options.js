// ============================================================
// Mastisk Clipper — Options
// ============================================================

const DEFAULT_URL = MastiskServer.LOCAL_SERVER_URL;
const { PUBLIC_URL_KEY } = MastiskServer;

const urlInput = document.getElementById('server-url');
const saveBtn = document.getElementById('save');
const testBtn = document.getElementById('test');
const msg = document.getElementById('msg');
const publicUrlNote = document.getElementById('public-url');

function setMsg(text, kind) {
  msg.textContent = text;
  msg.className = kind || '';
}

function normalize(url) {
  return String(url || '').trim().replace(/\/+$/, '');
}

// Ensure we hold host permission for the server's host.
// Returns true if granted (or already held), false if denied.
// The manifest asks for <all_urls> at install, so this normally passes without
// a prompt. Chrome only asks when site access was narrowed in chrome://extensions.
async function ensurePermission(url) {
  const pattern = MastiskServer.hostPermissionPattern(url);
  if (!pattern) return false;

  const has = await chrome.permissions.contains({ origins: [pattern] });
  if (has) return true;
  try {
    return await chrome.permissions.request({ origins: [pattern] });
  } catch {
    return false;
  }
}

// The address the daemon reports for other devices. Read-only here: it is set
// in the daemon's config ([server] public_url) and synced between browsers.
function showPublicUrl(publicUrl) {
  publicUrlNote.textContent = publicUrl
    ? `Links open at ${publicUrl}, the public address reported by your Mastisk server. `
      + 'Your other Chrome browsers pick it up through Chrome Sync.'
    : 'No public address yet. Set [server] public_url in the Mastisk config and links '
      + 'will open from your other devices.';
}

// --- Load ---
document.addEventListener('DOMContentLoaded', async () => {
  const stored = await chrome.storage.sync.get(['settings', PUBLIC_URL_KEY]);
  urlInput.value = stored.settings?.serverUrl || DEFAULT_URL;
  showPublicUrl(MastiskServer.normalizeServerUrl(stored[PUBLIC_URL_KEY]));
});

chrome.storage.onChanged.addListener((changes, area) => {
  if (area === 'sync' && changes[PUBLIC_URL_KEY]) {
    showPublicUrl(MastiskServer.normalizeServerUrl(changes[PUBLIC_URL_KEY].newValue));
  }
});

// --- Save ---
saveBtn.addEventListener('click', async () => {
  const url = normalize(urlInput.value) || DEFAULT_URL;
  if (!/^https?:\/\//.test(url)) {
    setMsg('URL must start with http:// or https://', 'err');
    return;
  }
  const granted = await ensurePermission(url);
  if (!granted) {
    setMsg('Permission for that host was denied', 'err');
    return;
  }
  const { settings } = await chrome.storage.sync.get('settings');
  await chrome.storage.sync.set({ settings: { ...(settings || {}), serverUrl: url } });
  urlInput.value = url;
  setMsg('Saved', 'ok');
});

// --- Test connection ---
testBtn.addEventListener('click', async () => {
  const url = normalize(urlInput.value) || DEFAULT_URL;
  if (!/^https?:\/\//.test(url)) {
    setMsg('URL must start with http:// or https://', 'err');
    return;
  }
  const granted = await ensurePermission(url);
  if (!granted) {
    setMsg('Permission for that host was denied', 'err');
    return;
  }
  setMsg('Testing…', '');
  try {
    const resp = await chrome.runtime.sendMessage({ action: 'health', serverUrl: url });
    if (resp?.ok) setMsg('Connected ✓', 'ok');
    else setMsg('No response from daemon', 'err');
  } catch (err) {
    setMsg('Failed: ' + err.message, 'err');
  }
});
