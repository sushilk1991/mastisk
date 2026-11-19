// ============================================================
// Mastisk Clipper — Server resolution
// ============================================================
// Decides which Mastisk daemon to talk to and which address to put in links.
// Pure logic: no chrome.* here, so the same file loads in the service worker
// (importScripts), in the extension pages (<script>) and under Node for tests.
//
// Two addresses matter and they are often different:
//   serverUrl  — where this browser sends API requests (often localhost).
//   publicUrl  — what the daemon says other devices should use, from the
//                `public_url` field of GET /api/health. Links use it so they
//                open from a phone or another computer.

(function (root) {
  const LOCAL_SERVER_URL = 'http://localhost:5555';
  // chrome.storage.sync key holding the public address a daemon reported.
  const PUBLIC_URL_KEY = 'publicUrl';

  // '' for anything that is not a usable http(s) base URL. Query strings and
  // fragments are dropped: a base URL is joined with a path.
  function normalizeServerUrl(value) {
    if (typeof value !== 'string') return '';
    const trimmed = value.trim();
    if (!trimmed) return '';
    let parsed;
    try {
      parsed = new URL(trimmed);
    } catch {
      return '';
    }
    if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') return '';
    if (!parsed.hostname) return '';
    return parsed.origin + parsed.pathname.replace(/\/+$/, '');
  }

  // Servers to try, most preferred first:
  //   1. the URL saved in Settings — an explicit choice always wins;
  //   2. a daemon on this machine;
  //   3. the public address a daemon reported earlier, synced from any of the
  //      owner's browsers — what makes a machine with no daemon work untouched.
  function candidateServerUrls({ savedUrl, publicUrl } = {}) {
    const ordered = [normalizeServerUrl(savedUrl), LOCAL_SERVER_URL, normalizeServerUrl(publicUrl)];
    return ordered.filter((url, index) => url && ordered.indexOf(url) === index);
  }

  // Read a GET /api/health body. Daemons older than `public_url` return only
  // { ok: true }; a missing field means "none", the same as null.
  function parseHealth(body) {
    const ok = !!body && body.ok === true;
    return { ok, publicUrl: ok ? normalizeServerUrl(body.public_url) : '' };
  }

  // Pick the first candidate, in order, whose probe answers.
  //
  // `probe(url)` resolves to { ok, publicUrl, needsPermission }. Probes start
  // together and are read in preference order, so a later candidate never
  // overrides an earlier one that answered, and an unreachable address costs
  // one timeout in total rather than one per candidate.
  //
  // When nothing answers, the first candidate is returned with
  // reachable: false so callers still have an address to name in the error.
  async function resolveServer(candidates, probe) {
    const urls = Array.isArray(candidates) && candidates.length ? candidates : [LOCAL_SERVER_URL];
    const probes = urls.map((url) =>
      Promise.resolve()
        .then(() => probe(url))
        .catch(() => null));

    const needsPermission = [];
    for (let i = 0; i < urls.length; i += 1) {
      const result = await probes[i];
      if (result?.ok) {
        return {
          serverUrl: urls[i],
          publicUrl: normalizeServerUrl(result.publicUrl),
          reachable: true,
          needsPermission,
        };
      }
      if (result?.needsPermission) needsPermission.push(urls[i]);
    }
    return { serverUrl: urls[0], publicUrl: '', reachable: false, needsPermission };
  }

  // The base for any link a person might open or share.
  function linkBase(server) {
    return normalizeServerUrl(server?.publicUrl) || normalizeServerUrl(server?.serverUrl) || LOCAL_SERVER_URL;
  }

  function articleUrl(base, id) {
    return `${String(base || '').replace(/\/+$/, '')}/a/${encodeURIComponent(String(id))}`;
  }

  // Host-permission match pattern for a server URL. The port is left out on
  // purpose: a pattern without one matches every port on that host.
  function hostPermissionPattern(url) {
    const normalized = normalizeServerUrl(url);
    if (!normalized) return '';
    const parsed = new URL(normalized);
    return `${parsed.protocol}//${parsed.hostname}/*`;
  }

  // Short label for the status line: host and port, no scheme.
  function serverLabel(url) {
    const normalized = normalizeServerUrl(url);
    return normalized ? new URL(normalized).host : '';
  }

  const api = {
    LOCAL_SERVER_URL,
    PUBLIC_URL_KEY,
    normalizeServerUrl,
    candidateServerUrls,
    parseHealth,
    resolveServer,
    linkBase,
    articleUrl,
    hostPermissionPattern,
    serverLabel,
  };

  root.MastiskServer = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof globalThis !== 'undefined' ? globalThis : this);
