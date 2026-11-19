// Loads background.js into a vm context with a fake `chrome` and a fake
// network, so the service worker's server choice and link building can be
// exercised under Node without a browser.
//
// All hostnames here are placeholders. This repository is public.

const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const EXTENSION_DIR = path.resolve(__dirname, '..');

const LOCAL = 'http://localhost:5555';
const PUBLIC = 'https://mastisk-host.example-tailnet.ts.net:8443';
const SAVED = 'https://saved-host.example-tailnet.ts.net:8443';

function jsonResponse(status, body) {
  return { ok: status >= 200 && status < 300, status, async json() { return body; } };
}

// `servers` maps a base URL to a daemon description:
//   { publicUrl, article, routes: { 'POST /api/ingest/web': (body) => [status, json] } }
// A base URL that is absent refuses the connection, as a stopped daemon does.
// The returned `down` Set refuses connections for servers that exist; the
// `hung` Set accepts them and never answers (every request times out).
// `withheld` lists host-permission patterns Chrome has not granted.
function loadBackground({ servers = {}, sync = {}, withheld = [] } = {}) {
  const fetchCalls = [];
  const notifications = [];
  const syncWrites = [];
  const sessionStore = {};
  const syncStore = { ...sync };
  const down = new Set();
  const hung = new Set();
  const listeners = { message: null, storageChanged: [], notificationClicked: null };
  const openedTabs = [];
  const event = { addListener() {}, removeListener() {} };

  async function fetch(url, options = {}) {
    const method = options.method || 'GET';
    const base = new URL(url).origin;
    const route = url.slice(base.length);
    fetchCalls.push({ method, url, base, path: route });
    if (!servers[base] || down.has(base)) throw new TypeError('Failed to fetch');
    if (hung.has(base)) {
      const error = new Error('The operation timed out.');
      error.name = 'TimeoutError';
      throw error;
    }

    const server = servers[base];
    if (method === 'GET' && route === '/api/health') {
      const body = { ok: true };
      // An old daemon has no public_url key at all.
      if ('publicUrl' in server) body.public_url = server.publicUrl;
      return jsonResponse(200, body);
    }
    const handler = server.routes?.[`${method} ${route}`];
    if (!handler) return jsonResponse(404, { detail: 'Not Found' });
    const [status, body] = handler(options.body ? JSON.parse(options.body) : null);
    return jsonResponse(status, body);
  }

  function pick(store, keys) {
    if (keys == null) return { ...store };
    const wanted = Array.isArray(keys) ? keys : [keys];
    return Object.fromEntries(wanted.filter((key) => key in store).map((key) => [key, store[key]]));
  }

  const chrome = {
    runtime: {
      id: 'mastisk-test',
      lastError: null,
      onInstalled: event,
      onStartup: event,
      onMessage: { addListener(fn) { listeners.message = fn; } },
    },
    contextMenus: { onClicked: event, async removeAll() {}, create(_p, cb) { cb?.(); } },
    notifications: {
      onClicked: { addListener(fn) { listeners.notificationClicked = fn; } },
      create(properties, callback) {
        const id = `notification-${notifications.length + 1}`;
        notifications.push({ id, ...properties });
        callback?.(id);
      },
      clear() {},
    },
    alarms: { onAlarm: event, async create() {}, async clear() {} },
    action: { setBadgeBackgroundColor() {}, setBadgeText() {} },
    permissions: {
      async contains({ origins }) {
        return !origins.some((pattern) => withheld.includes(pattern));
      },
    },
    storage: {
      onChanged: { addListener(fn) { listeners.storageChanged.push(fn); } },
      sync: {
        async get(keys) { return pick(syncStore, keys); },
        async set(values) {
          syncWrites.push({ ...values });
          const changes = {};
          for (const [key, newValue] of Object.entries(values)) {
            changes[key] = { oldValue: syncStore[key], newValue };
            syncStore[key] = newValue;
          }
          for (const fn of listeners.storageChanged) fn(changes, 'sync');
        },
      },
      session: {
        async get(keys) { return pick(sessionStore, keys); },
        async set(values) { Object.assign(sessionStore, values); },
        async remove(key) { delete sessionStore[key]; },
      },
    },
    // Toasts cannot be injected here, so feedback falls through to notifications.
    scripting: { async executeScript() { throw new Error('no page to script'); } },
    tabs: {
      onUpdated: event,
      async create(spec) { openedTabs.push(spec); return { id: 99, ...spec }; },
      async remove() {},
      async get() { throw new Error('tab closed'); },
      async query() { return []; },
    },
  };

  // Polling waits 4 s between status checks and gives up after 12 minutes.
  // Waits are collapsed to 2 ms and the worker's clock is moved forward by the
  // time skipped, so a poll that never finishes times out in well under a
  // second instead of hanging the run.
  let skippedMs = 0;
  class SkippingDate extends Date {
    static now() { return Date.now() + skippedMs; }
  }
  const context = vm.createContext({
    AbortSignal,
    Date: SkippingDate,
    URL,
    chrome,
    console,
    fetch,
    setTimeout: (fn, delay = 0, ...args) => setTimeout(() => {
      skippedMs += Math.max(0, delay - 2);
      fn(...args);
    }, Math.min(delay, 2)),
    clearTimeout,
    queueMicrotask,
    encodeURIComponent,
  });
  context.importScripts = (...files) => {
    for (const file of files) {
      vm.runInContext(fs.readFileSync(path.join(EXTENSION_DIR, file), 'utf8'), context, { filename: file });
    }
  };
  context.importScripts('background.js');

  // Send a runtime message the way the popup and side panel do.
  function send(message) {
    return new Promise((resolve, reject) => {
      const keptOpen = listeners.message(message, { id: 'mastisk-test' }, resolve);
      if (keptOpen !== true) reject(new Error(`no handler for ${message.action}`));
    });
  }

  // Give fire-and-forget work (notification bookkeeping) a turn to finish.
  const settle = () => new Promise((resolve) => setTimeout(resolve, 10));

  return {
    context,
    send,
    settle,
    down,
    hung,
    fetchCalls,
    notifications,
    syncWrites,
    syncStore,
    sessionStore,
    openedTabs,
    clickNotification: (id) => listeners.notificationClicked(id),
    healthChecks: () => fetchCalls.filter((call) => call.path === '/api/health').map((call) => call.base),
  };
}

module.exports = { loadBackground, LOCAL, PUBLIC, SAVED };
