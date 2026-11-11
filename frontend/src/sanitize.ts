import DOMPurify from 'dompurify';

// Article, lesson and chat bodies are model-written HTML (or markdown-it
// output). The allowlist covers what those bodies actually contain: inline
// formatting, lists, code, tables, links, and the wiki-link markup
// `<span class="link" data-target="slug">`. Everything else is dropped —
// including images (remote fetches), inline styles and event handlers.
const ALLOWED_TAGS = [
  'p', 'br', 'hr', 'span', 'div',
  'em', 'strong', 'b', 'i', 'u', 's', 'del', 'ins', 'mark', 'small', 'sub', 'sup',
  'code', 'pre', 'kbd', 'tt', 'q', 'abbr', 'cite', 'blockquote',
  'ul', 'ol', 'li', 'dl', 'dt', 'dd',
  'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
  'table', 'thead', 'tbody', 'tfoot', 'tr', 'th', 'td', 'caption',
  'details', 'summary',
  'a',
];

const ALLOWED_ATTR = ['class', 'data-target', 'href', 'title', 'colspan', 'rowspan', 'start'];

// http(s), mailto, or a scheme-less (relative / fragment) reference.
const SAFE_HREF = /^(?:(?:https?|mailto):|[^a-z]|[a-z+.-]+(?:[^a-z+.\-:]|$))/i;

export function sanitizeHtml(html: string | null | undefined): string {
  if (!html) return '';
  return DOMPurify.sanitize(html, {
    ALLOWED_TAGS,
    ALLOWED_ATTR,
    ALLOW_DATA_ATTR: false,
    ALLOW_ARIA_ATTR: false,
    ALLOWED_URI_REGEXP: SAFE_HREF,
    // Slugs are identifiers, not URLs; don't run them through the href check.
    ADD_URI_SAFE_ATTR: ['data-target'],
  });
}

/** Plain text of an HTML fragment, parsed inertly (no scripts, no loads). */
export function htmlToText(html: string): string {
  const doc = new DOMParser().parseFromString(html, 'text/html');
  return doc.body.textContent?.trim() ?? '';
}
