// ============================================================
// Mastisk Clipper — Page Content Extractor
// ============================================================
// Injected on-demand by background.js into the page's MAIN world.
// Uses Defuddle for general pages; hand-rolled extraction for
// YouTube / Twitter / Reddit / Hacker News. Returns a normalized
// shape: { type, title, content, author, hero_image_url, url }.

(async () => {
  const MAX_CHARS = 400000; // Mastisk API caps content at 400k chars

  // Detect page type from hostname
  function detectPageType() {
    const hostname = window.location.hostname;
    if (['www.youtube.com', 'youtube.com', 'm.youtube.com'].includes(hostname)) {
      return 'youtube';
    }
    if (['x.com', 'www.x.com', 'twitter.com', 'www.twitter.com', 'mobile.twitter.com'].includes(hostname)) {
      return 'twitter';
    }
    if (['www.reddit.com', 'reddit.com', 'old.reddit.com', 'new.reddit.com'].includes(hostname)) {
      return 'reddit';
    }
    if (['news.ycombinator.com'].includes(hostname)) {
      return 'hackernews';
    }
    return 'page';
  }

  // --- YouTube extraction via Defuddle (async — transcript via InnerTube) ---
  async function extractYouTube() {
    if (typeof Defuddle === 'undefined') return null;
    try {
      const result = await new Defuddle(document, { url: window.location.href }).parseAsync();

      let description = '';
      try {
        const player = document.querySelector('#movie_player');
        if (player && typeof player.getPlayerResponse === 'function') {
          description = player.getPlayerResponse()?.videoDetails?.shortDescription || '';
        }
      } catch (e) { /* not available */ }
      if (!description) {
        try {
          description = window.ytInitialPlayerResponse?.videoDetails?.shortDescription || '';
        } catch (e) { /* not available */ }
      }

      const transcript = result.variables?.transcript || '';
      const parts = [];
      if (description) parts.push(description);
      if (transcript) parts.push('## Transcript\n\n' + transcript);
      const content = parts.join('\n\n');

      return {
        type: 'youtube',
        title: result.title || document.title,
        author: result.variables?.author || result.author || '',
        content,
        hero_image_url: ogImage(),
        url: window.location.href,
      };
    } catch (err) {
      console.warn('[Mastisk] Defuddle YouTube extraction failed:', err.message);
      return null;
    }
  }

  // --- Twitter extraction (thread text from DOM) ---
  function extractTwitter() {
    const tweetEls = document.querySelectorAll('[data-testid="tweet"]');
    const lines = [];
    let author = '';

    for (const el of tweetEls) {
      const nameEl = el.querySelector('[data-testid="User-Name"]');
      const textEl = el.querySelector('[data-testid="tweetText"]');
      const timeEl = el.querySelector('time');

      let text = '';
      if (textEl) {
        for (const node of textEl.childNodes) {
          if (node.nodeType === Node.TEXT_NODE) text += node.textContent;
          else if (node.tagName === 'IMG') text += node.alt || '';
          else if (node.tagName === 'BR') text += '\n';
          else text += node.textContent || '';
        }
      }
      const who = nameEl?.textContent?.trim() || '';
      if (!author && who) author = who;
      const time = timeEl?.getAttribute('datetime') || '';
      if (text.trim()) {
        lines.push(`${who}${time ? ` (${time})` : ''}:\n${text.trim()}`);
      }
    }

    return {
      type: 'twitter',
      title: document.title,
      author,
      content: lines.join('\n\n---\n\n'),
      hero_image_url: ogImage(),
      url: window.location.href,
    };
  }

  // --- Reddit extraction ---
  function extractReddit() {
    const postTitle = document.querySelector('[slot="title"]')?.textContent
      || document.querySelector('.Post h1, .Post h3')?.textContent
      || document.title;
    const postBody = document.querySelector('[slot="text-body"]')?.innerText
      || document.querySelector('.Post .RichTextJSON-root, .Post [data-click-id="text"]')?.innerText
      || '';

    const comments = [];
    const commentEls = document.querySelectorAll('shreddit-comment, .Comment');
    for (const el of commentEls) {
      const author = el.getAttribute('author')
        || el.querySelector('.Comment__author, [data-testid="comment_author_link"]')?.textContent?.trim();
      const body = el.querySelector('[slot="comment"]')?.innerText
        || el.querySelector('.Comment__body, .RichTextJSON-root')?.innerText;
      if (body) comments.push(`u/${author || 'anonymous'}:\n${body.trim().substring(0, 2000)}`);
    }

    const content = [postBody.trim(), comments.slice(0, 50).join('\n\n')].filter(Boolean).join('\n\n---\n\n');
    return {
      type: 'page',
      title: (postTitle || document.title).trim(),
      author: '',
      content,
      hero_image_url: ogImage(),
      url: window.location.href,
    };
  }

  // --- Hacker News extraction ---
  function extractHackerNews() {
    const titleEl = document.querySelector('.titleline > a, .storylink');
    const title = titleEl?.textContent || document.title;

    const comments = [];
    const commentEls = document.querySelectorAll('.comtr');
    for (const el of commentEls) {
      const author = el.querySelector('.hnuser')?.textContent;
      const body = el.querySelector('.commtext')?.innerText;
      if (body) comments.push(`${author || 'anonymous'}:\n${body.trim().substring(0, 2000)}`);
    }

    return {
      type: 'page',
      title: title.trim(),
      author: '',
      content: comments.slice(0, 50).join('\n\n'),
      hero_image_url: '',
      url: window.location.href,
    };
  }

  // --- General page extraction via Defuddle ---
  function extractGeneral() {
    try {
      if (typeof Defuddle !== 'undefined') {
        const result = new Defuddle(document).parse();
        return {
          type: 'page',
          title: result.title || document.title,
          author: result.author || '',
          content: result.content || document.body.innerText,
          hero_image_url: httpImage(result.image) || ogImage(),
          url: window.location.href,
        };
      }
    } catch (err) {
      console.warn('[Mastisk] Defuddle extraction failed:', err.message);
    }
    // Fallback: raw text extraction
    return {
      type: 'page',
      title: document.title,
      author: '',
      content: (document.body?.innerText || '').substring(0, MAX_CHARS),
      hero_image_url: ogImage(),
      url: window.location.href,
    };
  }

  // Accept only absolute http(s) image URLs; the backend rejects anything else.
  function httpImage(url) {
    return typeof url === 'string' && /^https?:\/\//.test(url) ? url : '';
  }

  // Best-effort hero image from Open Graph / Twitter card meta tags
  function ogImage() {
    const sel = 'meta[property="og:image"], meta[name="og:image"], meta[name="twitter:image"], meta[name="twitter:image:src"]';
    const el = document.querySelector(sel);
    return httpImage(el?.getAttribute('content') || '');
  }

  // --- Main extraction logic ---
  const pageType = detectPageType();
  let result;

  if (pageType === 'youtube') {
    result = await extractYouTube();
    if (!result || !result.content) result = extractGeneral();
  } else if (pageType === 'twitter') {
    result = extractTwitter();
    if (!result.content) result = extractGeneral();
  } else if (pageType === 'reddit') {
    result = extractReddit();
    if (!result.content) result = extractGeneral();
  } else if (pageType === 'hackernews') {
    result = extractHackerNews();
    if (!result.content) result = extractGeneral();
  } else {
    result = extractGeneral();
  }

  // Enforce Mastisk's content cap
  if (result.content && result.content.length > MAX_CHARS) {
    result.content = result.content.substring(0, MAX_CHARS);
  }

  return result;
})();
