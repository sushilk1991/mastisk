// ============================================================
// Mastisk Clipper — Options
// ============================================================

const DEFAULT_URL = 'http://localhost:5555';

const urlInput = document.getElementById('server-url');
const saveBtn = document.getElementById('save');
const testBtn = document.getElementById('test');
const msg = document.getElementById('msg');

function setMsg(text, kind) {
  msg.textContent = text;
  msg.className = kind || '';
}

function normalize(url) {
  return String(url || '').trim().replace(/\/+$/, '');
}

// Origins already granted at install time (see manifest host_permissions).
function isBuiltinOrigin(origin) {
  return origin === 'http://localhost:5555' || origin === 'http://127.0.0.1:5555';
}

// Ensure we hold host permission for a custom server origin.
// Returns true if granted (or already held), false if denied.
async function ensurePermission(url) {
  let origin;
  try {
    origin = new URL(url).origin;
  } catch {
    return false;
  }
  if (isBuiltinOrigin(origin)) return true;

  const pattern = origin + '/*';
  const has = await chrome.permissions.contains({ origins: [pattern] });
  if (has) return true;
  try {
    return await chrome.permissions.request({ origins: [pattern] });
  } catch {
    return false;
  }
}

// --- Load ---
document.addEventListener('DOMContentLoaded', async () => {
  const { settings } = await chrome.storage.sync.get('settings');
  urlInput.value = settings?.serverUrl || DEFAULT_URL;
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
