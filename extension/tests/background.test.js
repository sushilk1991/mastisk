// The service worker's server choice, public-URL sync and link building,
// against a fake chrome and a fake network (see harness.js).
// Run: node --test "extension/tests/*.test.js"

const test = require('node:test');
const assert = require('node:assert/strict');

const { loadBackground, LOCAL, PUBLIC, SAVED } = require('./harness.js');

const ARTICLE = { id: 'note-000001-example', title: 'An Essay' };
const CLIP = { url: 'https://example.com/essay', title: 'An Essay', content: '# An Essay', kind: 'web' };

// A daemon that already holds the article for any clip.
const hasArticle = { 'POST /api/ingest/web': () => [200, { article: ARTICLE }] };

// A daemon that queues the clip and reports it done on the first status poll.
const queuesThenDone = {
  'POST /api/ingest/web': () => [202, { source_id: 'src-1', status: 'queued' }],
  'GET /api/ingest/web/src-1': () => [200, { status: 'done', article: ARTICLE }],
};

test('main machine: uses localhost, syncs the public URL once, links with it', async () => {
  const bg = loadBackground({ servers: { [LOCAL]: { publicUrl: PUBLIC, routes: hasArticle } } });

  const health = await bg.send({ action: 'health' });
  assert.deepEqual(JSON.parse(JSON.stringify(health)), {
    ok: true, serverUrl: LOCAL, publicUrl: PUBLIC, linkBase: PUBLIC, needsPermission: [],
  });
  assert.deepEqual(bg.syncWrites, [{ publicUrl: PUBLIC }]);

  // Asking again finds the same value: no second write (storage.sync is rate-limited).
  await bg.send({ action: 'health' });
  assert.equal(bg.syncWrites.length, 1);

  await bg.context.ingestAndTrack({ ...CLIP }, null);
  await bg.settle();
  const posts = bg.fetchCalls.filter((call) => call.method === 'POST');
  assert.deepEqual(posts.map((call) => call.url), [`${LOCAL}/api/ingest/web`]);

  const notice = bg.notifications.find((n) => n.title === 'Mastisk: Already in your wiki');
  bg.clickNotification(notice.id);
  await bg.settle();
  assert.deepEqual(openedUrls(bg), [`${PUBLIC}/a/${ARTICLE.id}`]);
});

test('other machine: no daemon, synced public URL, nothing typed', async () => {
  const bg = loadBackground({
    servers: { [PUBLIC]: { publicUrl: PUBLIC, routes: hasArticle } },
    sync: { publicUrl: PUBLIC }, // arrived through Chrome Sync; no saved settings
  });

  await bg.context.ingestAndTrack({ ...CLIP }, null);
  await bg.settle();

  const posts = bg.fetchCalls.filter((call) => call.method === 'POST');
  assert.deepEqual(posts.map((call) => call.url), [`${PUBLIC}/api/ingest/web`]);
  bg.clickNotification(bg.notifications.at(-1).id);
  await bg.settle();
  assert.deepEqual(openedUrls(bg), [`${PUBLIC}/a/${ARTICLE.id}`]);
  // The value it already had is not written back.
  assert.deepEqual(bg.syncWrites, []);
});

test('saved URL wins over a local daemon when it answers', async () => {
  const bg = loadBackground({
    servers: { [SAVED]: { publicUrl: null }, [LOCAL]: { publicUrl: PUBLIC } },
    sync: { settings: { serverUrl: `${SAVED}/` } },
  });

  const health = await bg.send({ action: 'health' });

  assert.equal(health.serverUrl, SAVED);
  // No public_url reported: links fall back to the server in use.
  assert.equal(health.linkBase, SAVED);
  assert.deepEqual(bg.syncWrites, []);
});

test('saved URL down: falls back to the daemon on this machine', async () => {
  const bg = loadBackground({
    servers: { [LOCAL]: { publicUrl: PUBLIC } },
    sync: { settings: { serverUrl: SAVED } },
  });

  const health = await bg.send({ action: 'health' });

  assert.equal(health.ok, true);
  assert.equal(health.serverUrl, LOCAL);
  assert.equal(health.linkBase, PUBLIC);
});

test('a daemon without public_url does not erase the remembered one', async () => {
  const bg = loadBackground({
    servers: { [LOCAL]: {} }, // old daemon: health is just { ok: true }
    sync: { publicUrl: PUBLIC },
  });

  const health = await bg.send({ action: 'health' });

  assert.equal(health.serverUrl, LOCAL);
  assert.equal(health.linkBase, LOCAL);
  assert.deepEqual(bg.syncWrites, []);
  assert.equal(bg.syncStore.publicUrl, PUBLIC);
});

test('nothing answers: reports offline and names the first candidate', async () => {
  const bg = loadBackground({ sync: { publicUrl: PUBLIC } });

  const health = await bg.send({ action: 'health' });
  assert.equal(health.ok, false);
  assert.equal(health.serverUrl, LOCAL);

  await bg.context.ingestAndTrack({ ...CLIP }, null);
  await bg.settle();
  const failure = bg.notifications.at(-1);
  assert.equal(failure.title, 'Mastisk: Clip failed');
  assert.equal(failure.message, `Mastisk daemon unreachable at ${LOCAL}`);
});

test('withheld host permission: no request is sent and the popup is told which host', async () => {
  const bg = loadBackground({
    servers: { [PUBLIC]: { publicUrl: PUBLIC } },
    sync: { publicUrl: PUBLIC },
    withheld: ['https://mastisk-host.example-tailnet.ts.net/*'],
  });

  const health = await bg.send({ action: 'health' });

  assert.equal(health.ok, false);
  assert.deepEqual(Array.from(health.needsPermission), [PUBLIC]);
  assert.deepEqual(bg.healthChecks(), [LOCAL]);
});

test('queued clip: status is polled on the accepting server and the notification links publicly', async () => {
  const bg = loadBackground({ servers: { [LOCAL]: { publicUrl: PUBLIC, routes: queuesThenDone } } });

  await bg.context.ingestAndTrack({ ...CLIP }, null);
  await bg.settle();

  const polls = bg.fetchCalls.filter((call) => call.path === '/api/ingest/web/src-1');
  assert.deepEqual(polls.map((call) => call.base), [LOCAL]);
  const indexed = bg.notifications.find((n) => n.title === 'Mastisk: Indexed');
  assert.equal(indexed.message, ARTICLE.title);
  bg.clickNotification(indexed.id);
  await bg.settle();
  assert.deepEqual(openedUrls(bg), [`${PUBLIC}/a/${ARTICLE.id}`]);
});

test('a job stays with the server that accepted it when resolution later changes', async () => {
  let localPolls = 0;
  let bg = null;
  bg = loadBackground({
    servers: {
      [LOCAL]: {
        publicUrl: null,
        routes: {
          // Accepted by the local daemon. Right then a saved URL appears
          // (synced from another browser) and would now win resolution.
          'POST /api/ingest/web': () => {
            chromeSyncSet(bg, { settings: { serverUrl: SAVED } });
            return [202, { source_id: 'src-1', status: 'queued' }];
          },
          'GET /api/ingest/web/src-1': () => {
            localPolls += 1;
            return [200, { status: 'done', article: ARTICLE }];
          },
        },
      },
      [SAVED]: { publicUrl: PUBLIC },
    },
  });

  await bg.context.ingestAndTrack({ ...CLIP }, null);
  await bg.settle();

  assert.equal(localPolls, 1);
  assert.equal((await bg.send({ action: 'health' })).serverUrl, SAVED); // resolution did move
  assert.equal(bg.fetchCalls.some((call) => call.base === SAVED && call.path.startsWith('/api/ingest')), false);
  const indexed = bg.notifications.find((n) => n.title === 'Mastisk: Indexed');
  bg.clickNotification(indexed.id);
  await bg.settle();
  // The local daemon reported no public URL, so its own address is the link.
  assert.deepEqual(openedUrls(bg), [`${LOCAL}/a/${ARTICLE.id}`]);
});

test('stale choice: a refused connection re-resolves and retries on another server', async () => {
  const bg = loadBackground({
    servers: {
      [LOCAL]: { publicUrl: PUBLIC, routes: hasArticle },
      [PUBLIC]: { publicUrl: PUBLIC, routes: hasArticle },
    },
    sync: { publicUrl: PUBLIC },
  });
  await bg.send({ action: 'health' }); // caches localhost
  bg.down.add(LOCAL);                  // the local daemon stops

  await bg.context.ingestAndTrack({ ...CLIP }, null);
  await bg.settle();

  const posts = bg.fetchCalls.filter((call) => call.method === 'POST');
  assert.deepEqual(posts.map((call) => call.base), [LOCAL, PUBLIC]);
  assert.equal(bg.notifications.at(-1).title, 'Mastisk: Already in your wiki');
});

test('timeout: the POST is not repeated on another server', async () => {
  const bg = loadBackground({
    servers: {
      [LOCAL]: { publicUrl: PUBLIC, routes: hasArticle },
      [PUBLIC]: { publicUrl: PUBLIC, routes: hasArticle },
    },
    sync: { publicUrl: PUBLIC },
  });
  await bg.send({ action: 'health' }); // caches localhost
  bg.hung.add(LOCAL);                  // it now accepts requests and never answers

  await bg.context.ingestAndTrack({ ...CLIP }, null);
  await bg.settle();

  // The clip may have landed on the hung daemon. Sending it to the public
  // address as well could queue it twice, so the failure is reported instead.
  const posts = bg.fetchCalls.filter((call) => call.method === 'POST');
  assert.deepEqual(posts.map((call) => call.base), [LOCAL]);
  assert.equal(bg.notifications.at(-1).message, `Mastisk daemon unreachable at ${LOCAL}`);
});

test('a resolution that began before the saved URL changed is not cached', async () => {
  const bg = loadBackground({
    servers: { [LOCAL]: { publicUrl: null, routes: hasArticle }, [SAVED]: { publicUrl: null, routes: hasArticle } },
  });

  const inFlight = bg.send({ action: 'health' });          // starts probing [localhost]
  await chromeSyncSet(bg, { settings: { serverUrl: SAVED } }); // saved URL lands mid-probe
  assert.equal((await inFlight).serverUrl, LOCAL);

  await bg.context.ingestAndTrack({ ...CLIP }, null);
  const posts = bg.fetchCalls.filter((call) => call.method === 'POST');
  assert.deepEqual(posts.map((call) => call.base), [SAVED]);
});

test('the choice is cached between requests and dropped when the saved URL changes', async () => {
  const bg = loadBackground({
    servers: { [LOCAL]: { publicUrl: null, routes: hasArticle }, [SAVED]: { publicUrl: null, routes: hasArticle } },
  });

  await bg.context.ingestAndTrack({ ...CLIP }, null);
  await bg.context.ingestAndTrack({ ...CLIP }, null);
  assert.deepEqual(bg.healthChecks(), [LOCAL]); // one probe for two clips

  await chromeSyncSet(bg, { settings: { serverUrl: SAVED } });
  await bg.context.ingestAndTrack({ ...CLIP }, null);
  const posts = bg.fetchCalls.filter((call) => call.method === 'POST');
  assert.deepEqual(posts.map((call) => call.base), [LOCAL, LOCAL, SAVED]);
});

test('Test connection asks only the address typed in Settings', async () => {
  const bg = loadBackground({ servers: { [LOCAL]: { publicUrl: PUBLIC }, [SAVED]: { publicUrl: null } } });

  const typed = await bg.send({ action: 'health', serverUrl: `${SAVED}/` });
  const dead = await bg.send({ action: 'health', serverUrl: 'https://nobody.example-tailnet.ts.net' });
  const junk = await bg.send({ action: 'health', serverUrl: 'not a url' });

  assert.equal(typed.ok, true);
  assert.equal(dead.ok, false);
  assert.equal(junk.ok, false);
  assert.deepEqual(bg.healthChecks(), [SAVED, 'https://nobody.example-tailnet.ts.net']);
  // Testing an address is not the same as adopting what it reports.
  assert.deepEqual(bg.syncWrites, []);
});

test('ask: answers carry the link base of the server that answered', async () => {
  const bg = loadBackground({
    servers: {
      [LOCAL]: {
        publicUrl: PUBLIC,
        routes: { 'POST /api/ask': () => [200, { answer: 'grounded', cites: [], hits: [] }] },
      },
    },
  });

  const response = await bg.send({ action: 'ask', question: 'What matters here?' });

  assert.equal(response.ok, true);
  assert.equal(response.answer, 'grounded');
  assert.equal(response.linkBase, PUBLIC);
});

// Tab objects are created inside the vm context; compare plain strings.
function openedUrls(bg) {
  return bg.openedTabs.map((tab) => tab.url);
}

// Write to the fake chrome.storage.sync from inside the worker's context, the
// way the options page (or Chrome Sync) would.
function chromeSyncSet(bg, values) {
  return bg.context.chrome.storage.sync.set(values);
}
