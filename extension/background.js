// ============================================================
// Mastisk Clipper — Background Service Worker
// ============================================================
// Clips pages / selections / links to the local Mastisk daemon
// and tracks indexing to completion.
//
// Service-worker restart policy: in-flight indexing jobs are
// persisted to chrome.storage.session (keyed by source_id) and
// resumed best-effort on startup. Notification -> article-URL
// mappings live there too, so a click still opens the article
// after a restart. A restart may drop a poll mid-flight, but the
// badge is always reconciled from live session state on startup,
// so it can never get stuck.

const DEFAULT_SETTINGS = {
  serverUrl: 'http://localhost:5555',
};

const POLL_INTERVAL_MS = 4000;
// The daemon's compiler agent ticks every 5 minutes, so a freshly queued clip
// can wait a full tick before compilation even starts. 12 minutes covers a
// worst-case tick plus a slow LLM compile.
const POLL_TIMEOUT_MS = 12 * 60 * 1000;
const LINK_LOAD_TIMEOUT_MS = 20000;
const JOBS_KEY = 'mastisk-jobs';        // session: { [sourceId]: {startedAt, title} }
const NOTIF_KEY = 'mastisk-notif-urls'; // session: { [notificationId]: articleUrl }

// Backend validation caps. We truncate to these client-side rather than let the
// daemon reject the whole clip with a 422. URL is not truncated (a sliced URL is
// meaningless) — an over-long URL skips the clip instead.
const FIELD_LIMITS = {
  title: 500,
  author: 200,
  url: 2000,
  hero_image_url: 2000,
  content: 400000,
  selection: 400000,
};

function clampField(value, max) {
  return typeof value === 'string' && value.length > max ? value.slice(0, max) : value;
}

// --- Settings ---

async function getSettings() {
  const { settings } = await chrome.storage.sync.get('settings');
  return { ...DEFAULT_SETTINGS, ...(settings || {}) };
}

async function getServerUrl() {
  const s = await getSettings();
  return String(s.serverUrl || DEFAULT_SETTINGS.serverUrl).replace(/\/+$/, '');
}

// --- Small helpers ---

function notify(title, message, articleUrl) {
  chrome.notifications.create({
    type: 'basic',
    iconUrl: 'icons/icon128.png',
    title: `Mastisk: ${title}`,
    message: message || '',
  }, (notificationId) => {
    if (articleUrl && notificationId) rememberNotifUrl(notificationId, articleUrl);
  });
}

async function rememberNotifUrl(notificationId, url) {
  await withStore(async () => {
    const store = await chrome.storage.session.get(NOTIF_KEY);
    const map = store[NOTIF_KEY] || {};
    map[notificationId] = url;
    await chrome.storage.session.set({ [NOTIF_KEY]: map });
  });
}

chrome.notifications.onClicked.addListener((notificationId) => {
  withStore(async () => {
    const store = await chrome.storage.session.get(NOTIF_KEY);
    const map = store[NOTIF_KEY] || {};
    const url = map[notificationId];
    if (url) {
      chrome.tabs.create({ url });
      delete map[notificationId];
      await chrome.storage.session.set({ [NOTIF_KEY]: map });
    }
  }).finally(() => chrome.notifications.clear(notificationId));
});

// --- Storage mutex ---
// All read-modify-write access to JOBS_KEY / NOTIF_KEY is serialized through
// one promise chain so concurrent clips (or a poll racing the alarm sweep)
// can't interleave and clobber each other's entries.
let _storeChain = Promise.resolve();
function withStore(fn) {
  const run = _storeChain.then(fn, fn);
  _storeChain = run.catch(() => {}); // keep the chain alive across failures
  return run;
}

// --- Badge ---

function setBadgeForCount(n) {
  if (n > 0) {
    chrome.action.setBadgeBackgroundColor({ color: '#6366f1' });
    chrome.action.setBadgeText({ text: '…' });
  } else {
    chrome.action.setBadgeText({ text: '' });
  }
}

async function refreshBadge() {
  return withStore(async () => {
    const store = await chrome.storage.session.get(JOBS_KEY);
    setBadgeForCount(Object.keys(store[JOBS_KEY] || {}).length);
  });
}

async function flashSuccessBadge() {
  chrome.action.setBadgeBackgroundColor({ color: '#059669' });
  chrome.action.setBadgeText({ text: '✓' });
  // Clear the checkmark shortly after, then reconcile against any other jobs.
  setTimeout(() => { refreshBadge(); }, 4000);
}

// --- Job tracking (session-persisted) ---
// storage.session is wiped on browser restart — an in-flight clip that
// spans a full browser restart is simply forgotten (no stuck badge, no
// duplicate notification). That is an accepted, documented tradeoff.

// Mutate the job map atomically and reconcile the badge from the result.
async function commitJobs(mutator) {
  return withStore(async () => {
    const store = await chrome.storage.session.get(JOBS_KEY);
    const jobs = store[JOBS_KEY] || {};
    const ret = mutator(jobs);
    await chrome.storage.session.set({ [JOBS_KEY]: jobs });
    setBadgeForCount(Object.keys(jobs).length);
    return ret;
  });
}

async function addJob(sourceId, meta) {
  await commitJobs((jobs) => { jobs[sourceId] = { startedAt: Date.now(), ...meta }; });
  await ensurePollAlarm();
}

// Atomically remove a job. Returns its meta if it was still present, else null.
// This is the concurrency guard that makes completion idempotent: whichever of
// the in-SW loop or the alarm sweep claims the job first is the one that notifies.
async function claimJob(sourceId) {
  return commitJobs((jobs) => {
    if (!(sourceId in jobs)) return null;
    const meta = jobs[sourceId];
    delete jobs[sourceId];
    return meta;
  });
}

async function jobExists(sourceId) {
  const store = await chrome.storage.session.get(JOBS_KEY);
  return !!(store[JOBS_KEY] || {})[sourceId];
}

// --- Mastisk API ---

// POST a clip to the daemon. Returns { source_id, status, article? } or throws.
async function ingestWeb(payload) {
  const server = await getServerUrl();
  let resp;
  try {
    resp = await fetch(`${server}/api/ingest/web`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
      signal: AbortSignal.timeout(30000),
    });
  } catch (err) {
    throw new Error(`Mastisk daemon unreachable at ${server}`);
  }
  if (!resp.ok && resp.status !== 202 && resp.status !== 200) {
    let detail = `HTTP ${resp.status}`;
    try {
      const j = await resp.json();
      if (j?.detail) detail = typeof j.detail === 'string' ? j.detail : JSON.stringify(j.detail);
    } catch { /* non-JSON error body */ }
    throw new Error(detail);
  }
  const data = await resp.json();
  return { httpStatus: resp.status, ...data };
}

async function getIngestStatus(sourceId) {
  const server = await getServerUrl();
  const resp = await fetch(`${server}/api/ingest/web/${encodeURIComponent(sourceId)}`, {
    signal: AbortSignal.timeout(15000),
  });
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
  return resp.json();
}

function articleUrl(server, id) {
  return `${server}/a/${id}`;
}

// --- Completion (shared by the in-SW loop and the alarm sweep) ---

const POLL_ALARM = 'mastisk-poll-sweep';

async function ensurePollAlarm() {
  // Backstop that survives service-worker termination: the alarm wakes the SW
  // even after setTimeout-based loops are gone, so completion is never dropped.
  await chrome.alarms.create(POLL_ALARM, { periodInMinutes: 0.5 });
}

async function clearAlarmIfIdle() {
  const store = await chrome.storage.session.get(JOBS_KEY);
  if (Object.keys(store[JOBS_KEY] || {}).length === 0) {
    await chrome.alarms.clear(POLL_ALARM);
  }
}

// Act on a fetched status idempotently. Returns true if the job reached a
// terminal state (done/failed) and was handled, false if still in progress.
// claimJob guarantees only the first caller notifies.
async function handleTerminalStatus(sourceId, status, fallbackTitle) {
  if (status.status === 'done') {
    const meta = await claimJob(sourceId);
    if (!meta) return true; // already handled elsewhere
    const server = await getServerUrl();
    const title = meta.title || fallbackTitle;
    if (status.article) {
      await flashSuccessBadge();
      notify('Indexed', status.article.title || title || 'Saved to your wiki',
        articleUrl(server, status.article.id));
    } else {
      // Compiler skipped it — saved as a source but not turned into an article.
      notify('Saved to Mastisk', 'Saved but not compiled into an article.');
    }
    return true;
  }
  if (status.status === 'failed') {
    const meta = await claimJob(sourceId);
    if (!meta) return true;
    notify('Indexing failed', status.error || 'The daemon could not index this clip.');
    return true;
  }
  return false; // queued | running | pending
}

async function expireJob(sourceId, fallbackTitle) {
  const meta = await claimJob(sourceId);
  if (!meta) return;
  notify('Still indexing', `"${meta.title || fallbackTitle || 'Your clip'}" is taking a while. Check Mastisk later.`);
}

// --- Poll a queued job to completion (fast in-SW loop for snappy UX) ---

async function pollJob(sourceId, title) {
  const startedAt = Date.now();

  while (Date.now() - startedAt < POLL_TIMEOUT_MS) {
    await sleep(POLL_INTERVAL_MS);
    // Bail if the alarm sweep already finished this job.
    if (!(await jobExists(sourceId))) return;
    let status;
    try {
      status = await getIngestStatus(sourceId);
    } catch (err) {
      // Transient error (daemon busy / restarting) — keep trying until timeout.
      continue;
    }
    if (await handleTerminalStatus(sourceId, status, title)) {
      await clearAlarmIfIdle();
      return;
    }
  }

  await expireJob(sourceId, title);
  await clearAlarmIfIdle();
}

// --- Alarm sweep: the restart-proof path ---

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name !== POLL_ALARM) return;
  sweepJobs().catch((err) => console.warn('[Mastisk] Poll sweep failed:', err.message));
});

async function sweepJobs() {
  const store = await chrome.storage.session.get(JOBS_KEY);
  const jobs = store[JOBS_KEY] || {};
  const ids = Object.keys(jobs);
  if (ids.length === 0) {
    await chrome.alarms.clear(POLL_ALARM);
    return;
  }
  for (const sourceId of ids) {
    const meta = jobs[sourceId];
    if (Date.now() - (meta.startedAt || 0) > POLL_TIMEOUT_MS) {
      await expireJob(sourceId, meta.title);
      continue;
    }
    let status;
    try {
      status = await getIngestStatus(sourceId);
    } catch {
      continue; // transient — try again next sweep
    }
    await handleTerminalStatus(sourceId, status, meta.title);
  }
  await clearAlarmIfIdle();
  await refreshBadge();
}

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

// --- Clip orchestration ---

// Kick off ingest + tracking for a payload. `title` is used for
// notification copy before the compiled article title is known.
async function ingestAndTrack(payload) {
  const server = await getServerUrl();

  // Guard/clamp to backend limits before POSTing.
  if (typeof payload.url === 'string' && payload.url.length > FIELD_LIMITS.url) {
    notify("Can't clip", 'The page URL is too long to save.');
    return;
  }
  payload.title = clampField(payload.title, FIELD_LIMITS.title);
  payload.author = clampField(payload.author, FIELD_LIMITS.author);
  payload.hero_image_url = clampField(payload.hero_image_url, FIELD_LIMITS.hero_image_url);
  payload.content = clampField(payload.content, FIELD_LIMITS.content);
  payload.selection = clampField(payload.selection, FIELD_LIMITS.selection);

  let res;
  try {
    res = await ingestWeb(payload);
  } catch (err) {
    notify('Clip failed', err.message);
    return;
  }

  const title = payload.title || 'Your clip';

  // Already compiled — the daemon hands back the existing article.
  if (res.article) {
    notify('Already in your wiki', res.article.title || title,
      articleUrl(server, res.article.id));
    return;
  }

  if (!res.source_id) {
    notify('Clip failed', 'Daemon did not return a source id.');
    return;
  }

  // Terminal states with no article.
  if (res.status === 'done') {
    notify('Saved to Mastisk', 'Saved but not compiled into an article.');
    return;
  }
  if (res.status === 'failed') {
    notify('Indexing failed', res.error || 'The daemon could not index this clip.');
    return;
  }

  // queued | running | pending | (unset) -> track to completion.
  notify('Sent to Mastisk', 'Indexing…');
  await addJob(res.source_id, { title });
  await pollJob(res.source_id, title);
}

function mapKind(type) {
  if (type === 'youtube') return 'youtube';
  if (type === 'twitter') return 'twitter';
  return 'web';
}

// A page we cannot script into (chrome://, Web Store, PDF viewer, etc.)
function isRestrictedUrl(url) {
  if (!url) return true;
  return /^(chrome|chrome-extension|edge|about|devtools|view-source):/i.test(url)
    || url.startsWith('https://chrome.google.com/webstore')
    || url.startsWith('https://chromewebstore.google.com');
}

// Inject Defuddle (MAIN world, UMD-unwrapped) then run chat-content.js.
async function extractFromTab(tabId) {
  try {
    const check = await chrome.scripting.executeScript({
      target: { tabId },
      world: 'MAIN',
      func: () => typeof Defuddle === 'function',
    });
    if (!check?.[0]?.result) {
      await chrome.scripting.executeScript({
        target: { tabId },
        world: 'MAIN',
        files: ['lib/defuddle.js'],
      });
      // UMD bundle assigns an exports object to window.Defuddle — unwrap the class.
      await chrome.scripting.executeScript({
        target: { tabId },
        world: 'MAIN',
        func: () => {
          if (typeof Defuddle !== 'undefined' && typeof Defuddle !== 'function' && Defuddle.Defuddle) {
            window.Defuddle = Defuddle.Defuddle;
          }
        },
      });
    }
  } catch (err) {
    console.warn('[Mastisk] Could not inject Defuddle:', err.message);
  }

  try {
    const results = await chrome.scripting.executeScript({
      target: { tabId },
      world: 'MAIN',
      files: ['chat-content.js'],
    });
    return results?.[0]?.result || null;
  } catch (err) {
    console.warn('[Mastisk] Content extraction failed:', err.message);
    return null;
  }
}

// Send the whole current page.
async function sendPage(tab) {
  if (!tab?.id || isRestrictedUrl(tab.url)) {
    notify("Can't clip this page", 'This browser page cannot be read by extensions.');
    return;
  }
  const data = await extractFromTab(tab.id);
  if (!data || !data.content) {
    notify("Can't clip this page", 'Could not extract readable content from this page.');
    return;
  }
  await ingestAndTrack({
    url: data.url || tab.url,
    title: data.title || tab.title || '',
    content: data.content,
    author: data.author || undefined,
    hero_image_url: data.hero_image_url || undefined,
    kind: mapKind(data.type),
  });
}

// Send just the selected text (plus cheap page context).
async function sendSelection(info, tab) {
  const selection = (info.selectionText || '').trim();
  if (!selection) {
    notify("Can't clip", 'No text was selected.');
    return;
  }
  // Try to grab full page content too, but a selection is enough on its own.
  let content = '';
  let title = tab?.title || '';
  let author;
  let hero;
  let kind = 'web';
  if (tab?.id && !isRestrictedUrl(tab.url)) {
    const data = await extractFromTab(tab.id);
    if (data) {
      content = data.content || '';
      title = data.title || title;
      author = data.author || undefined;
      hero = data.hero_image_url || undefined;
      kind = mapKind(data.type);
    }
  }
  await ingestAndTrack({
    url: tab?.url || info.pageUrl || '',
    title,
    content: content || selection,
    selection,
    author,
    hero_image_url: hero,
    kind,
  });
}

// Send a link target: open it in a background tab, extract, then close.
async function sendLink(linkUrl) {
  if (!linkUrl || isRestrictedUrl(linkUrl)) {
    notify("Can't clip this link", 'This link cannot be opened for clipping.');
    return;
  }

  let bgTab;
  try {
    bgTab = await chrome.tabs.create({ url: linkUrl, active: false });
  } catch (err) {
    notify("Can't clip this link", err.message);
    return;
  }

  try {
    await waitForTabComplete(bgTab.id, LINK_LOAD_TIMEOUT_MS);
    // Small settle delay for client-rendered pages.
    await sleep(1200);
    const data = await extractFromTab(bgTab.id);
    if (!data || !data.content) {
      notify("Can't clip this link", 'Could not extract readable content from the link.');
      return;
    }
    await ingestAndTrack({
      url: data.url || linkUrl,
      title: data.title || '',
      content: data.content,
      author: data.author || undefined,
      hero_image_url: data.hero_image_url || undefined,
      kind: mapKind(data.type),
    });
  } catch (err) {
    notify("Can't clip this link", err.message);
  } finally {
    try { await chrome.tabs.remove(bgTab.id); } catch { /* already gone */ }
  }
}

function waitForTabComplete(tabId, timeoutMs) {
  return new Promise((resolve, reject) => {
    const timeout = setTimeout(() => {
      chrome.tabs.onUpdated.removeListener(listener);
      reject(new Error('Link page timed out while loading'));
    }, timeoutMs);

    function listener(id, changeInfo) {
      if (id === tabId && changeInfo.status === 'complete') {
        clearTimeout(timeout);
        chrome.tabs.onUpdated.removeListener(listener);
        resolve();
      }
    }
    chrome.tabs.onUpdated.addListener(listener);

    // In case the tab already finished before we attached the listener.
    chrome.tabs.get(tabId, (t) => {
      if (!chrome.runtime.lastError && t?.status === 'complete') {
        clearTimeout(timeout);
        chrome.tabs.onUpdated.removeListener(listener);
        resolve();
      }
    });
  });
}

// --- Context menus ---

const MENU = {
  page: 'mastisk-page',
  selection: 'mastisk-selection',
  link: 'mastisk-link',
};

async function setupContextMenus() {
  await chrome.contextMenus.removeAll();
  chrome.contextMenus.create({ id: MENU.page, title: 'Send page to Mastisk', contexts: ['page'] });
  chrome.contextMenus.create({ id: MENU.selection, title: 'Send selection to Mastisk', contexts: ['selection'] });
  chrome.contextMenus.create({ id: MENU.link, title: 'Send link to Mastisk', contexts: ['link'] });
}

chrome.contextMenus.onClicked.addListener((info, tab) => {
  (async () => {
    switch (info.menuItemId) {
      case MENU.page: await sendPage(tab); break;
      case MENU.selection: await sendSelection(info, tab); break;
      case MENU.link: await sendLink(info.linkUrl); break;
    }
  })().catch((err) => {
    console.error('[Mastisk] Clip error:', err);
    notify('Clip failed', err.message || 'Unknown error');
  });
});

// --- Lifecycle ---

chrome.runtime.onInstalled.addListener(async () => {
  await setupContextMenus();
  await resumeJobs();
});

chrome.runtime.onStartup.addListener(async () => {
  await setupContextMenus();
  await resumeJobs();
});

// Reconcile the badge, re-arm the alarm, and best-effort resume the fast
// polls for any jobs that were in flight when the service worker was suspended.
async function resumeJobs() {
  await refreshBadge();
  const store = await chrome.storage.session.get(JOBS_KEY);
  const jobs = store[JOBS_KEY] || {};
  const ids = Object.keys(jobs);
  if (ids.length === 0) return;
  await ensurePollAlarm();
  for (const sourceId of ids) {
    const meta = jobs[sourceId];
    if (Date.now() - (meta.startedAt || 0) > POLL_TIMEOUT_MS) {
      await expireJob(sourceId, meta.title);
      continue;
    }
    pollJob(sourceId, meta.title).catch(() => {});
  }
}

// --- Messages (popup + side panel) ---

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (sender.id !== chrome.runtime.id) return;

  if (msg.action === 'send-page') {
    (async () => {
      const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
      if (tab) await sendPage(tab);
      sendResponse({ ok: true });
    })().catch((err) => sendResponse({ ok: false, error: err.message }));
    return true;
  }

  if (msg.action === 'get-server-settings') {
    (async () => {
      sendResponse({ ok: true, serverUrl: await getServerUrl() });
    })().catch((err) => sendResponse({ ok: false, error: err.message }));
    return true;
  }

  if (msg.action === 'health') {
    (async () => {
      const server = msg.serverUrl ? String(msg.serverUrl).replace(/\/+$/, '') : await getServerUrl();
      try {
        const resp = await fetch(`${server}/api/health`, { signal: AbortSignal.timeout(5000) });
        const data = await resp.json().catch(() => ({}));
        sendResponse({ ok: resp.ok && data?.ok === true });
      } catch (err) {
        sendResponse({ ok: false, error: err.message });
      }
    })();
    return true;
  }

  if (msg.action === 'extract-page-content') {
    (async () => {
      const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
      if (!tab) { sendResponse({ ok: false, error: 'No active tab' }); return; }
      if (isRestrictedUrl(tab.url)) {
        sendResponse({ ok: false, error: 'restricted', title: tab.title, url: tab.url });
        return;
      }
      const data = await extractFromTab(tab.id);
      sendResponse({ ok: !!data, data, title: tab?.title, url: tab?.url });
    })().catch((err) => sendResponse({ ok: false, error: err.message }));
    return true;
  }

  if (msg.action === 'ask') {
    (async () => {
      const server = await getServerUrl();
      const resp = await fetch(`${server}/api/ask`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          question: msg.question,
          page_url: msg.page_url || undefined,
          page_title: msg.page_title || undefined,
          page_content: msg.page_content || undefined,
        }),
        signal: AbortSignal.timeout(120000),
      });
      if (!resp.ok) throw new Error(`Mastisk returned HTTP ${resp.status}`);
      const data = await resp.json();
      sendResponse({ ok: true, ...data });
    })().catch((err) => sendResponse({ ok: false, error: err.message }));
    return true;
  }
});
