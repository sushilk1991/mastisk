// ============================================================
// Mastisk Clipper — Side Panel Chat
// ============================================================
// Chats with the whole wiki plus the current page. Each /api/ask
// call is single-turn, so we prepend the last 2 Q/A pairs into the
// question text for lightweight continuity.

(() => {
  // --- State ---
  let serverUrl = 'http://localhost:5555';
  let activeTabId = null;
  let currentUrl = '';
  let pageContent = null;       // { type, title, content, ... } for the active tab
  let isAsking = false;
  let extractionGen = 0;        // guards against stale extractions on fast tab switches

  // Per-tab conversation history: tabId -> [{ role:'user'|'assistant', content, cites?, hits? }]
  const histories = new Map();

  // --- DOM ---
  const connDot = document.getElementById('conn-dot');
  const pageTitleEl = document.getElementById('page-title');
  const includePage = document.getElementById('include-page');
  const messagesEl = document.getElementById('messages');
  const welcomeEl = document.getElementById('welcome');
  const inputEl = document.getElementById('input');
  const sendBtn = document.getElementById('send');

  // --- Helpers ---
  function historyFor(tabId) {
    if (tabId == null) return [];
    if (!histories.has(tabId)) histories.set(tabId, []);
    return histories.get(tabId);
  }

  function currentHistory() {
    return historyFor(activeTabId);
  }

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function articleUrl(id) {
    return `${serverUrl}/a/${id}`;
  }

  // Replace [[Wiki Title]] citations with links (resolved via hits by title).
  function renderAnswer(rawText, hits) {
    const byTitle = new Map();
    for (const h of hits || []) {
      if (h?.title) byTitle.set(h.title.toLowerCase().trim(), h);
    }

    const tokens = [];
    const replaced = String(rawText || '').replace(/\[\[([^\]]+)\]\]/g, (_, title) => {
      const t = title.trim();
      const idx = tokens.length;
      tokens.push({ title: t, hit: byTitle.get(t.toLowerCase()) });
      return `CITE${idx}`;
    });

    let html = typeof renderMarkdown === 'function' ? renderMarkdown(replaced) : escapeHtml(replaced);
    html = html.replace(/CITE(\d+)/g, (_, i) => {
      const tok = tokens[Number(i)];
      if (!tok) return '';
      const safe = escapeHtml(tok.title);
      // Only compiled articles have a /a/{id} page; link those. Other hits
      // (tasks, people, books) render as plain text — inline in prose we keep
      // just the title; the kind label is shown in the Sources list below.
      if (tok.hit?.is_article === true && tok.hit?.id != null) {
        return `<a class="cite-link" data-article="${escapeHtml(tok.hit.id)}" href="#">${safe}</a>`;
      }
      return `<span class="cite-plain">${safe}</span>`;
    });
    return html;
  }

  function renderCites(cites, hits) {
    if (!cites || cites.length === 0) return null;
    const byTitle = new Map();
    for (const h of hits || []) {
      if (h?.title) byTitle.set(h.title.toLowerCase().trim(), h);
    }
    const wrap = document.createElement('div');
    wrap.className = 'cites';
    const label = document.createElement('span');
    label.className = 'cites-label';
    label.textContent = 'Sources:';
    wrap.appendChild(label);

    for (const title of cites) {
      const hit = byTitle.get(String(title).toLowerCase().trim());
      if (hit?.is_article === true && hit?.id != null) {
        const a = document.createElement('a');
        a.href = '#';
        a.dataset.article = String(hit.id);
        a.textContent = title;
        wrap.appendChild(a);
      } else {
        // Non-article source (task / person / book / …): plain text + kind label.
        const span = document.createElement('span');
        span.textContent = hit?.kind ? `${title} (${hit.kind})` : title;
        span.style.marginRight = '8px';
        wrap.appendChild(span);
      }
    }
    return wrap;
  }

  // --- Rendering ---
  function clearMessages() {
    messagesEl.innerHTML = '';
    messagesEl.appendChild(welcomeEl);
    welcomeEl.classList.remove('hidden');
    welcomeEl.style.display = '';
  }

  function hideWelcome() {
    welcomeEl.style.display = 'none';
  }

  function appendUser(text) {
    hideWelcome();
    const div = document.createElement('div');
    div.className = 'message user';
    div.textContent = text;
    messagesEl.appendChild(div);
    scrollToBottom();
  }

  function appendAssistant(rawText, cites, hits, loading) {
    hideWelcome();
    const div = document.createElement('div');
    div.className = 'message assistant';
    if (loading) {
      div.innerHTML = '<div class="loading-dots"><span></span><span></span><span></span></div>';
    } else {
      div.innerHTML = renderAnswer(rawText, hits);
      const citeEl = renderCites(cites, hits);
      if (citeEl) div.appendChild(citeEl);
    }
    messagesEl.appendChild(div);
    scrollToBottom();
    return div;
  }

  function appendError(text) {
    hideWelcome();
    const div = document.createElement('div');
    div.className = 'message error';
    div.textContent = text;
    messagesEl.appendChild(div);
    scrollToBottom();
  }

  function renderHistory() {
    clearMessages();
    const history = currentHistory();
    if (history.length === 0) return;
    for (const msg of history) {
      if (msg.role === 'user') appendUser(msg.content);
      else appendAssistant(msg.content, msg.cites, msg.hits, false);
    }
  }

  function scrollToBottom() {
    messagesEl.scrollTop = messagesEl.scrollHeight;
  }

  // --- Connection ---
  async function checkConnection() {
    try {
      const resp = await chrome.runtime.sendMessage({ action: 'health' });
      const online = !!resp?.ok;
      connDot.classList.toggle('online', online);
      connDot.classList.toggle('offline', !online);
      connDot.title = online ? `Connected to ${serverUrl}` : `Mastisk unreachable at ${serverUrl}`;
    } catch {
      connDot.classList.add('offline');
      connDot.title = `Mastisk unreachable at ${serverUrl}`;
    }
  }

  // --- Page content ---
  async function loadActiveTab() {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!tab) return;
    activeTabId = tab.id;
    currentUrl = tab.url || '';
    pageTitleEl.textContent = tab.title || tab.url || '';
    renderHistory();
    await extractContent();
  }

  async function extractContent() {
    const gen = ++extractionGen;
    pageContent = null;
    let resp;
    try {
      resp = await chrome.runtime.sendMessage({ action: 'extract-page-content' });
    } catch {
      resp = null;
    }
    if (gen !== extractionGen) return; // superseded by a newer switch

    if (resp?.ok && resp.data) {
      pageContent = resp.data;
      pageTitleEl.textContent = pageContent.title || resp.url || currentUrl;
    } else {
      // Restricted page or extraction failed — chat still works against the wiki.
      pageTitleEl.textContent = (resp && resp.title) || currentUrl || 'This page can\'t be read';
    }
  }

  // --- Ask ---
  function buildMessages() {
    // The current user turn has already been appended. Send prior turns as a
    // bounded structured history so the backend can use them for continuity
    // without polluting full-text retrieval with pasted assistant prose.
    return currentHistory()
      .slice(0, -1)
      .slice(-10)
      .map((message) => ({ role: message.role, content: message.content }));
  }

  async function ask(userText) {
    const text = userText.trim();
    if (!text || isAsking) return;
    isAsking = true;
    updateSendState();

    // Bind this exchange to the tab it was asked from. A response arriving
    // after a tab switch must land in THIS tab's history — never render into
    // whatever tab happens to be active when it returns.
    const askTabId = activeTabId;
    const askHistory = historyFor(askTabId);

    appendUser(text);
    askHistory.push({ role: 'user', content: text });

    inputEl.value = '';
    autoResize();

    const loadingBubble = appendAssistant('', null, null, true);

    const payload = { action: 'ask', question: text, messages: buildMessages() };
    if (includePage.checked && pageContent?.content) {
      payload.page_url = pageContent.url || currentUrl;
      payload.page_title = pageContent.title || '';
      payload.page_content = pageContent.content;
    } else if (includePage.checked) {
      // No extracted content (restricted page) — still hand over url/title.
      payload.page_url = currentUrl;
      payload.page_title = pageTitleEl.textContent || '';
    }

    try {
      const resp = await chrome.runtime.sendMessage(payload);
      if (!resp?.ok) {
        throw new Error(resp?.error || 'Request failed');
      }
      const answer = resp.answer || '(no answer)';
      const cites = resp.cites || [];
      const hits = resp.hits || [];
      // Persist to the originating tab's history regardless of what's active.
      askHistory.push({ role: 'assistant', content: answer, cites, hits });
      // Only touch the DOM if that tab is still the one on screen.
      if (askTabId === activeTabId) {
        loadingBubble.remove();
        appendAssistant(answer, cites, hits, false);
      }
    } catch (err) {
      if (askTabId === activeTabId) {
        loadingBubble.remove();
        const msg = /Failed to fetch|unreachable|NetworkError/i.test(err.message)
          ? `Mastisk daemon unreachable at ${serverUrl}.`
          : err.message;
        appendError(msg);
      }
    } finally {
      isAsking = false;
      updateSendState();
    }
  }

  // --- Input handling ---
  function updateSendState() {
    sendBtn.disabled = isAsking || inputEl.value.trim().length === 0;
  }

  function autoResize() {
    inputEl.style.height = 'auto';
    inputEl.style.height = Math.min(inputEl.scrollHeight, 120) + 'px';
  }

  inputEl.addEventListener('input', () => { autoResize(); updateSendState(); });
  inputEl.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      ask(inputEl.value);
    }
  });
  sendBtn.addEventListener('click', () => ask(inputEl.value));

  // Suggestion chips
  welcomeEl.addEventListener('click', (e) => {
    const btn = e.target.closest('.suggestion');
    if (btn?.dataset.q) ask(btn.dataset.q);
  });

  // Citation click -> open the wiki article in a new tab
  messagesEl.addEventListener('click', (e) => {
    const link = e.target.closest('[data-article]');
    if (!link) return;
    e.preventDefault();
    chrome.tabs.create({ url: articleUrl(link.dataset.article) });
  });

  // --- Tab tracking ---
  chrome.tabs.onActivated.addListener(({ tabId }) => {
    if (tabId === activeTabId) return;
    loadActiveTab();
  });

  chrome.tabs.onUpdated.addListener((tabId, changeInfo) => {
    if (tabId !== activeTabId) return;
    if (changeInfo.url && changeInfo.url !== currentUrl) {
      // Navigated within the same tab — treat as a new page: fresh history.
      currentUrl = changeInfo.url;
      histories.set(activeTabId, []);
      clearMessages();
      extractContent();
    } else if (changeInfo.status === 'complete' && !pageContent) {
      extractContent();
    }
  });

  // React to server URL changes from the options page.
  chrome.storage.onChanged.addListener((changes, area) => {
    if (area === 'sync' && changes.settings) {
      const s = changes.settings.newValue;
      if (s?.serverUrl) serverUrl = String(s.serverUrl).replace(/\/+$/, '');
      checkConnection();
    }
  });

  // --- Init ---
  async function init() {
    try {
      const resp = await chrome.runtime.sendMessage({ action: 'get-server-settings' });
      if (resp?.ok && resp.serverUrl) serverUrl = resp.serverUrl;
    } catch { /* use default */ }
    await checkConnection();
    await loadActiveTab();
    updateSendState();
  }

  init();
})();
