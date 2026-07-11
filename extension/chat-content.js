// ============================================================
// Mastisk Clipper — Page Content Extractor
// ============================================================
// Injected on-demand by background.js into the page's MAIN world.
// Extraction is delegated entirely to Defuddle 0.19 (full build),
// which ships site-specific extractors for YouTube (description +
// transcript), Twitter/X threads, Reddit, Hacker News, Medium,
// Substack, Wikipedia, GitHub, LinkedIn, Bluesky, Mastodon,
// Threads, and ChatGPT/Claude/Gemini/Grok conversations — plus
// markdown output, math/code standardization, and footnotes.
// Returns a normalized shape:
//   { type, title, content, author, hero_image_url, url }

(async () => {
  const MAX_CHARS = 400000; // Mastisk API caps content at 400k chars

  // Kind detection for Mastisk's source tagging. Hostname-based because the
  // minified Defuddle bundle can mangle extractorType (constructor names).
  function detectPageType() {
    const h = window.location.hostname.replace(/^(www|m|mobile)\./, '');
    if (h === 'youtube.com' || h === 'youtu.be') return 'youtube';
    if (h === 'x.com' || h === 'twitter.com') return 'twitter';
    return 'page';
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

  const pageType = detectPageType();
  let result = null;

  try {
    if (typeof Defuddle === 'function') {
      // parseAsync lets async extractors run (e.g. YouTube transcript via
      // InnerTube). markdown:true converts the cleaned HTML to markdown,
      // which is what Mastisk's compiler wants to read.
      const r = await new Defuddle(document, {
        url: window.location.href,
        markdown: true,
      }).parseAsync();
      if (r?.content?.trim()) {
        result = {
          type: pageType,
          title: r.title || document.title,
          author: r.author || '',
          content: r.content,
          hero_image_url: httpImage(r.image) || ogImage(),
          url: window.location.href,
        };
      }
    }
  } catch (err) {
    console.warn('[Mastisk] Defuddle extraction failed:', err.message);
  }

  // Fallback: raw text extraction
  if (!result) {
    result = {
      type: pageType,
      title: document.title,
      author: '',
      content: (document.body?.innerText || '').substring(0, MAX_CHARS),
      hero_image_url: ogImage(),
      url: window.location.href,
    };
  }

  // Enforce Mastisk's content cap
  if (result.content && result.content.length > MAX_CHARS) {
    result.content = result.content.substring(0, MAX_CHARS);
  }

  return result;
})();
