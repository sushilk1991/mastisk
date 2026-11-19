// URL resolution rules in lib/server.js. Run: node --test extension/tests/
//
// All hostnames are placeholders. This repository is public.

const test = require('node:test');
const assert = require('node:assert/strict');

const {
  LOCAL_SERVER_URL,
  normalizeServerUrl,
  candidateServerUrls,
  parseHealth,
  resolveServer,
  linkBase,
  articleUrl,
  hostPermissionPattern,
  serverLabel,
} = require('../lib/server.js');

const PUBLIC = 'https://mastisk-host.example-tailnet.ts.net:8443';
const SAVED = 'https://saved-host.example-tailnet.ts.net:8443';

// A probe over a fixed map of url -> result; records what was asked.
function probeFrom(results, asked = []) {
  return async (url) => {
    asked.push(url);
    const result = results[url];
    if (result instanceof Error) throw result;
    return result || { ok: false };
  };
}

test('normalizeServerUrl trims and drops trailing slashes, queries and fragments', () => {
  assert.equal(normalizeServerUrl(`  ${PUBLIC}/  `), PUBLIC);
  assert.equal(normalizeServerUrl(`${PUBLIC}///`), PUBLIC);
  assert.equal(normalizeServerUrl(`${PUBLIC}/?x=1#top`), PUBLIC);
  assert.equal(normalizeServerUrl('http://localhost:5555/'), 'http://localhost:5555');
  assert.equal(normalizeServerUrl('https://example.test/mastisk/'), 'https://example.test/mastisk');
});

test('normalizeServerUrl rejects anything that is not an http(s) URL', () => {
  for (const bad of ['', '   ', null, undefined, 42, 'localhost:5555', 'ftp://example.test',
    'javascript:alert(1)', 'chrome-extension://abc/x', 'https://']) {
    assert.equal(normalizeServerUrl(bad), '', String(bad));
  }
});

test('candidates: saved URL first, then this machine, then the synced public URL', () => {
  assert.deepEqual(candidateServerUrls({ savedUrl: SAVED, publicUrl: PUBLIC }),
    [SAVED, LOCAL_SERVER_URL, PUBLIC]);
});

test('candidates: nothing saved and nothing synced leaves only this machine', () => {
  assert.deepEqual(candidateServerUrls({}), [LOCAL_SERVER_URL]);
  assert.deepEqual(candidateServerUrls(), [LOCAL_SERVER_URL]);
});

test('candidates: a fresh machine with a synced public URL tries it after localhost', () => {
  assert.deepEqual(candidateServerUrls({ publicUrl: PUBLIC }), [LOCAL_SERVER_URL, PUBLIC]);
});

test('candidates: duplicates and junk are dropped', () => {
  assert.deepEqual(candidateServerUrls({ savedUrl: 'http://localhost:5555/', publicUrl: PUBLIC }),
    [LOCAL_SERVER_URL, PUBLIC]);
  assert.deepEqual(candidateServerUrls({ savedUrl: `${PUBLIC}/`, publicUrl: PUBLIC }),
    [PUBLIC, LOCAL_SERVER_URL]);
  assert.deepEqual(candidateServerUrls({ savedUrl: 'not a url', publicUrl: 'ftp://x' }),
    [LOCAL_SERVER_URL]);
});

test('parseHealth reads public_url and treats a missing one as none', () => {
  assert.deepEqual(parseHealth({ ok: true, public_url: `${PUBLIC}/` }), { ok: true, publicUrl: PUBLIC });
  assert.deepEqual(parseHealth({ ok: true, public_url: null }), { ok: true, publicUrl: '' });
  // Daemons older than the setting answer with only { ok: true }.
  assert.deepEqual(parseHealth({ ok: true }), { ok: true, publicUrl: '' });
});

test('parseHealth does not trust a body that is not a healthy Mastisk answer', () => {
  assert.deepEqual(parseHealth({ ok: false, public_url: PUBLIC }), { ok: false, publicUrl: '' });
  assert.deepEqual(parseHealth({ ok: 'yes', public_url: PUBLIC }), { ok: false, publicUrl: '' });
  assert.deepEqual(parseHealth(null), { ok: false, publicUrl: '' });
  assert.deepEqual(parseHealth({ ok: true, public_url: 'javascript:alert(1)' }), { ok: true, publicUrl: '' });
});

test('resolveServer uses the saved URL when it answers', async () => {
  const server = await resolveServer([SAVED, LOCAL_SERVER_URL, PUBLIC], probeFrom({
    [SAVED]: { ok: true, publicUrl: PUBLIC },
    [LOCAL_SERVER_URL]: { ok: true, publicUrl: '' },
  }));
  assert.deepEqual(server, { serverUrl: SAVED, publicUrl: PUBLIC, reachable: true, needsPermission: [] });
});

test('resolveServer falls back to localhost when the saved URL is down', async () => {
  const server = await resolveServer([SAVED, LOCAL_SERVER_URL], probeFrom({
    [SAVED]: new TypeError('Failed to fetch'),
    [LOCAL_SERVER_URL]: { ok: true, publicUrl: PUBLIC },
  }));
  assert.equal(server.serverUrl, LOCAL_SERVER_URL);
  assert.equal(server.publicUrl, PUBLIC);
  assert.equal(server.reachable, true);
});

test('resolveServer reaches the synced public URL on a machine with no daemon', async () => {
  const server = await resolveServer([LOCAL_SERVER_URL, PUBLIC], probeFrom({
    [PUBLIC]: { ok: true, publicUrl: PUBLIC },
  }));
  assert.deepEqual(server, { serverUrl: PUBLIC, publicUrl: PUBLIC, reachable: true, needsPermission: [] });
});

test('resolveServer keeps preference order even when a later candidate answers first', async () => {
  const probe = (url) => new Promise((resolve) => {
    setTimeout(() => resolve({ ok: true, publicUrl: '' }), url === SAVED ? 30 : 1);
  });
  const server = await resolveServer([SAVED, LOCAL_SERVER_URL], probe);
  assert.equal(server.serverUrl, SAVED);
});

test('resolveServer probes candidates together, not one timeout after another', async () => {
  const started = [];
  const probe = (url) => new Promise((resolve) => {
    started.push(url);
    setTimeout(() => resolve({ ok: url === PUBLIC, publicUrl: '' }), 40);
  });
  const begin = Date.now();
  const server = await resolveServer([SAVED, LOCAL_SERVER_URL, PUBLIC], probe);
  assert.equal(server.serverUrl, PUBLIC);
  assert.deepEqual(started, [SAVED, LOCAL_SERVER_URL, PUBLIC]);
  assert.ok(Date.now() - begin < 110, 'three 40 ms probes ran in sequence');
});

test('resolveServer reports the first candidate, unreachable, when nothing answers', async () => {
  const server = await resolveServer([SAVED, LOCAL_SERVER_URL], probeFrom({}));
  assert.deepEqual(server, { serverUrl: SAVED, publicUrl: '', reachable: false, needsPermission: [] });
});

test('resolveServer lists candidates blocked by a missing host permission', async () => {
  const server = await resolveServer([LOCAL_SERVER_URL, PUBLIC], probeFrom({
    [PUBLIC]: { ok: false, needsPermission: true },
  }));
  assert.equal(server.reachable, false);
  assert.deepEqual(server.needsPermission, [PUBLIC]);
});

test('resolveServer with no candidates still names localhost', async () => {
  const server = await resolveServer([], probeFrom({}));
  assert.equal(server.serverUrl, LOCAL_SERVER_URL);
  assert.equal(server.reachable, false);
});

test('linkBase prefers the public URL and falls back to the server in use', () => {
  assert.equal(linkBase({ serverUrl: LOCAL_SERVER_URL, publicUrl: PUBLIC }), PUBLIC);
  assert.equal(linkBase({ serverUrl: LOCAL_SERVER_URL, publicUrl: '' }), LOCAL_SERVER_URL);
  assert.equal(linkBase({ serverUrl: SAVED }), SAVED);
  assert.equal(linkBase(undefined), LOCAL_SERVER_URL);
});

test('articleUrl joins base and id without doubling slashes', () => {
  assert.equal(articleUrl(PUBLIC, 'note-000001-example'), `${PUBLIC}/a/note-000001-example`);
  assert.equal(articleUrl(`${PUBLIC}/`, 'note-000001-example'), `${PUBLIC}/a/note-000001-example`);
  assert.equal(articleUrl(PUBLIC, 'a b/c'), `${PUBLIC}/a/a%20b%2Fc`);
});

test('hostPermissionPattern covers the host on any port', () => {
  assert.equal(hostPermissionPattern(PUBLIC), 'https://mastisk-host.example-tailnet.ts.net/*');
  assert.equal(hostPermissionPattern('http://localhost:5555'), 'http://localhost/*');
  assert.equal(hostPermissionPattern('nope'), '');
});

test('serverLabel shows host and port only', () => {
  assert.equal(serverLabel(PUBLIC), 'mastisk-host.example-tailnet.ts.net:8443');
  assert.equal(serverLabel('http://localhost:5555'), 'localhost:5555');
  assert.equal(serverLabel(''), '');
});
