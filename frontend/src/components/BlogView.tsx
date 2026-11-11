import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { api, ApiError } from '../api';
import { localDateTime } from '../date';
import type { BlogPostDetail, BlogPostSource, View } from '../types';

interface Props {
  blogPostId: number;
  onNavigate: (view: View, id?: string) => void;
  /**
   * Fired on every successful fetch and on unmount (with null). Lets the
   * parent surface the live draft title in breadcrumbs / titlebar without
   * duplicating the polling state.
   */
  onLoaded?: (bp: BlogPostDetail | null) => void;
}

// Matches `[source 3]` and `[source 3, source 7]` — same shape the agent
// emits and the backend persists (spec §8.4).
const CITATION_RE = /\[source\s+(\d+(?:\s*,\s*(?:source\s+)?\d+)*)\s*\]/gi;

export function BlogView({ blogPostId, onNavigate, onLoaded }: Props) {
  const [bp, setBp] = useState<BlogPostDetail | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [regenerating, setRegenerating] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [copied, setCopied] = useState(false);
  const [vaultPath, setVaultPath] = useState<string | null>(null);
  const [pollToken, setPollToken] = useState(0);
  const [copyMenuOpen, setCopyMenuOpen] = useState(false);
  const [copyLabel, setCopyLabel] = useState<string | null>(null);
  const copyMenuRef = useRef<HTMLDivElement | null>(null);
  const copyTimerRef = useRef<number | null>(null);
  const copyLabelTimerRef = useRef<number | null>(null);
  const mountedRef = useRef(true);
  const bpRef = useRef<BlogPostDetail | null>(null);

  // Keep a ref in sync with bp so the polling catch can read the latest
  // known status without rebuilding the effect closure on every state change.
  useEffect(() => { bpRef.current = bp; }, [bp]);

  useEffect(() => {
    mountedRef.current = true;
    return () => { mountedRef.current = false; };
  }, []);

  // Push loaded data up to the parent so the titlebar/breadcrumb can reflect
  // the live draft title. Clears on unmount so the crumb doesn't flash stale
  // data when the user navigates away. Ref-indirects the callback to keep the
  // effect dependency stable (otherwise parents passing an inline function
  // would re-fire cleanup on every render and emit spurious nulls).
  const onLoadedRef = useRef(onLoaded);
  useEffect(() => { onLoadedRef.current = onLoaded; }, [onLoaded]);
  useEffect(() => {
    // Only propagate positive data. The id-change reset sets bp=null, and
    // emitting onLoaded(null) there makes the Titlebar flash its "Draft"
    // fallback on /blog/41 → /blog/42 transitions. Unmount cleanup (below)
    // still fires onLoaded(null) so the crumb clears when the view goes away.
    if (bp) onLoadedRef.current?.(bp);
  }, [bp]);
  useEffect(() => {
    return () => { onLoadedRef.current?.(null); };
  }, []);

  // Reset view state whenever we switch to a different draft. Keyed on
  // blogPostId only (not pollToken) so regenerate doesn't flash a loading
  // skeleton — the polling effect will overwrite bp with fresh data first.
  // We also reset bpRef synchronously here: the `setBp(null)` above only
  // takes effect on the next render, but the polling effect's catch may read
  // bpRef.current before that — stale data would feed the retry decision.
  useEffect(() => {
    setBp(null);
    setErr(null);
    bpRef.current = null;
  }, [blogPostId]);

  // Polling loop while status is pending/running. Re-fetches in the same
  // effect that cancels on unmount and on id changes. The pollToken dep
  // lets onRegenerate re-arm this loop after a terminal status.
  useEffect(() => {
    let cancelled = false;
    let timer: number | undefined;

    const fetchOnce = async () => {
      try {
        const data = await api.blog.get(blogPostId);
        if (cancelled) return;
        setBp(data);
        setErr(null);
        if (data.status === 'pending' || data.status === 'running') {
          timer = window.setTimeout(fetchOnce, 2000);
        }
      } catch (e) {
        if (cancelled) return;
        const apiErr = e instanceof ApiError ? e : null;
        if (apiErr && apiErr.status === 404) {
          setErr('draft not found');
          return;  // terminal — don't retry
        }
        // Transient (network, 5xx). Retry with backoff only if the last
        // known status was non-terminal; otherwise surface the error.
        const lastStatus = bpRef.current?.status;
        if (!lastStatus || lastStatus === 'pending' || lastStatus === 'running') {
          timer = window.setTimeout(fetchOnce, 5000);
        } else {
          setErr(e instanceof Error ? e.message : 'failed to refresh draft');
        }
      }
    };

    fetchOnce();
    return () => {
      cancelled = true;
      if (timer) window.clearTimeout(timer);
    };
  }, [blogPostId, pollToken]);

  // Pull vault_path once for the copy-path action. Cheap + cached — matches
  // the ingest view's usage.
  useEffect(() => {
    api.vaultInfo()
      .then((info) => setVaultPath(info.vault_path))
      .catch(() => setVaultPath(null));
  }, []);

  useEffect(() => {
    return () => {
      if (copyTimerRef.current) window.clearTimeout(copyTimerRef.current);
      if (copyLabelTimerRef.current) window.clearTimeout(copyLabelTimerRef.current);
    };
  }, []);

  // Close the copy-format dropdown on outside click. Only attached while open.
  useEffect(() => {
    if (!copyMenuOpen) return;
    const onDocClick = (e: MouseEvent) => {
      if (!copyMenuRef.current?.contains(e.target as Node)) setCopyMenuOpen(false);
    };
    document.addEventListener('mousedown', onDocClick);
    return () => document.removeEventListener('mousedown', onDocClick);
  }, [copyMenuOpen]);

  const onCopyFormat = useCallback(async (label: string, text: string) => {
    try {
      await navigator.clipboard.writeText(text);
      setCopyMenuOpen(false);
      setCopyLabel(`copied (${label})`);
      if (copyLabelTimerRef.current) window.clearTimeout(copyLabelTimerRef.current);
      copyLabelTimerRef.current = window.setTimeout(() => setCopyLabel(null), 1800);
    } catch {
      setErr('clipboard unavailable');
    }
  }, []);

  const onRegenerate = useCallback(async () => {
    if (!bp) return;
    setRegenerating(true);
    setErr(null);
    try {
      await api.blog.regenerate(bp.id);
      if (!mountedRef.current) return;
      // Optimistically flip status to pending. The pollToken bump re-fires
      // the polling effect so fetchOnce picks up fresh data and continues
      // polling until the draft reaches a terminal state.
      setBp(prev => prev ? { ...prev, status: 'pending', error: null, finished_at: null } : prev);
      setPollToken(t => t + 1);
    } catch (e) {
      if (!mountedRef.current) return;
      setErr(e instanceof Error ? e.message : 'regenerate failed');
    } finally {
      if (mountedRef.current) setRegenerating(false);
    }
  }, [bp]);

  const onSaveAsNote = useCallback(async () => {
    if (!bp) return;
    setSaving(true);
    setErr(null);
    try {
      const res = await api.blog.saveAsNote(bp.id);
      if (!mountedRef.current) return;
      onNavigate('note', String(res.note_id));
    } catch (e) {
      if (!mountedRef.current) return;
      setErr(e instanceof Error ? e.message : 'save failed');
      setSaving(false);
    }
  }, [bp, onNavigate]);

  const onDelete = useCallback(async () => {
    if (!bp) return;
    const label = bp.title?.trim() || `draft #${bp.id}`;
    if (!confirm(`Delete ${label}?\n\nThe vault file will also be unlinked.`)) return;
    setDeleting(true);
    try {
      await api.blog.delete(bp.id);
      if (!mountedRef.current) return;
      onNavigate('blog');
    } catch (e) {
      if (!mountedRef.current) return;
      setErr(e instanceof Error ? e.message : 'delete failed');
      setDeleting(false);
    }
  }, [bp, onNavigate]);

  const onCopyPath = useCallback(async () => {
    if (!bp || !bp.path) return;
    // Match the spec's "vault/<path>" display, joined with the vault root
    // when we know it, so Obsidian/VS Code accepts the absolute path. Trim
    // any trailing slash from the root (iCloud vault paths sometimes have one).
    const full = vaultPath
      ? `${vaultPath.replace(/\/$/, '')}/${bp.path}`
      : `vault/${bp.path}`;
    try {
      await navigator.clipboard.writeText(full);
      setCopied(true);
      if (copyTimerRef.current) window.clearTimeout(copyTimerRef.current);
      copyTimerRef.current = window.setTimeout(() => setCopied(false), 1500);
    } catch {
      setErr('clipboard unavailable');
    }
  }, [bp, vaultPath]);

  const navigateSource = useCallback((src: BlogPostSource) => {
    if (src.resolved.deleted) return;
    if (src.kind === 'note') onNavigate('note', src.ref);
    else if (src.kind === 'article') onNavigate('article', src.ref);
    else if (src.kind === 'roundtable') onNavigate('roundtable', src.ref);
  }, [onNavigate]);

  // Memoized source lookup for citation rendering. n is 1-indexed.
  const sourceByN = useMemo(() => {
    if (!bp) return new Map<number, BlogPostSource>();
    const m = new Map<number, BlogPostSource>();
    for (const s of bp.sources) m.set(s.n, s);
    return m;
  }, [bp]);

  if (err && !bp) return <div className="view"><p style={{ color: 'var(--danger, crimson)' }}>{err}</p></div>;
  if (!bp) return <div className="view"><p style={{ color: 'var(--fg-faint)', fontFamily: 'var(--mono)', fontSize: 12 }}>loading…</p></div>;

  const title = bp.title?.trim()
    ? bp.title
    : bp.status === 'pending' || bp.status === 'running'
      ? 'Draft in progress'
      : bp.status === 'failed'
        ? 'Draft failed'
        : 'Untitled draft';
  const statusColor =
    bp.status === 'failed' ? 'var(--danger, crimson)'
    : bp.status === 'done' ? 'var(--fg)'
    : 'var(--fg-faint)';

  const isTerminal = bp.status === 'done' || bp.status === 'failed';
  const fileMissing = bp.body_md === null && bp.error === 'file missing';

  // Whether the top-toolbar Save action is offered. Same gate as the bottom
  // version had — keep them mutually exclusive so the user only sees one.
  const showTopSave = bp.status === 'done' && !bp.saved_as_note_id;

  return (
    <div className="view">
      <div className="view-h">Blog Post · #{bp.id}</div>
      <div className="bp-title-row">
        <h1 className="view-title">{title}</h1>
        {(bp.body_md || showTopSave) && (
          <div className="bp-toolbar">
            {bp.body_md && (
              <div ref={copyMenuRef} className="bp-copy-wrap">
                <button
                  className="bp-btn bp-btn-primary"
                  onClick={() => setCopyMenuOpen(o => !o)}
                  title="Copy post content formatted for sharing"
                  aria-haspopup="menu"
                  aria-expanded={copyMenuOpen}
                >
                  {copyLabel ? (
                    <span className="bp-copied-flash" style={{ color: 'inherit' }}>
                      {copyLabel}
                    </span>
                  ) : (
                    <>
                      <span>Copy</span>
                      <span className="caret">▾</span>
                    </>
                  )}
                </button>
                {copyMenuOpen && (
                  <div className="bp-menu" role="menu">
                    <CopyMenuItem
                      label="Markdown"
                      hint="Substack · Dev.to · Hashnode (footnote citations)"
                      onClick={() => onCopyFormat('markdown', formatMarkdown(bp))}
                    />
                    <CopyMenuItem
                      label="Twitter Article"
                      hint="X long-form (clean prose, links at end)"
                      onClick={() => onCopyFormat('x article', formatTwitter(bp))}
                    />
                    <div className="bp-menu-divider" />
                    <CopyMenuItem
                      label="Plain text"
                      hint="No citations, no sources"
                      onClick={() => onCopyFormat('plain', formatPlain(bp))}
                    />
                  </div>
                )}
              </div>
            )}
            {showTopSave && (
              <button
                className="bp-btn"
                onClick={onSaveAsNote}
                disabled={saving}
                title="Save this draft as a note in your wiki"
              >
                {saving ? 'saving…' : 'Save as note'}
              </button>
            )}
          </div>
        )}
      </div>

      <div style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--fg-faint)', marginBottom: 12 }}>
        {localDateTime(bp.created_at)}
        {' · '}
        <span style={{ color: statusColor }}>{bp.status}</span>
        {bp.model && <> · {bp.model}</>}
        {bp.word_count !== null && bp.word_count !== undefined && <> · {bp.word_count} words</>}
        {bp.finished_at && <> · finished {localDateTime(bp.finished_at)}</>}
        {' · '}{bp.window_days}d window
      </div>

      {bp.theme && (
        <div style={{ marginBottom: 12 }}>
          <span
            style={{
              display: 'inline-block', padding: '2px 8px',
              border: '1px solid var(--line)', borderRadius: 4,
              fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--fg-mute)',
              background: 'var(--bg-elev, transparent)',
            }}
          >
            theme: {bp.theme}
          </span>
        </div>
      )}

      {bp.tags.length > 0 && (
        <div style={{ marginBottom: 12, fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--fg-faint)' }}>
          {bp.tags.map(t => `#${t}`).join(' ')}
        </div>
      )}

      {(bp.status === 'pending' || bp.status === 'running') && (
        <p style={{ color: 'var(--fg-faint)', fontFamily: 'var(--mono)', fontSize: 12 }}>
          Drafting… this usually takes 60–120s.
        </p>
      )}

      {bp.status === 'failed' && bp.error && (
        <div
          style={{
            marginBottom: 16, padding: 10,
            border: '1px solid var(--danger, crimson)',
            borderRadius: 6,
            background: 'var(--accent-soft, transparent)',
            color: 'var(--danger, crimson)', fontSize: 13,
          }}
        >
          <strong>Draft failed:</strong> {bp.error}
        </div>
      )}

      {fileMissing && (
        <div
          style={{
            marginBottom: 16, padding: 10,
            border: '1px solid var(--line)', borderRadius: 6,
            background: 'var(--bg-elev, transparent)',
            color: 'var(--fg-mute)', fontSize: 13,
          }}
        >
          Draft file missing from vault. The row is still browsable but the body can't be rendered.
          Check <code>{bp.path}</code> in your vault, or regenerate to rewrite the file.
        </div>
      )}

      {err && (
        <div style={{ color: 'var(--danger, crimson)', marginBottom: 12, fontSize: 12 }}>
          {err}
        </div>
      )}

      {bp.body_md && (
        <div
          style={{
            marginTop: 16, marginBottom: 24,
            fontFamily: 'var(--serif)', fontSize: 16, lineHeight: 1.6,
          }}
        >
          <MarkdownBody
            body={bp.body_md}
            sourceByN={sourceByN}
            onCiteClick={navigateSource}
          />
        </div>
      )}

      {bp.sources.length > 0 && (
        <section style={{ marginTop: 32, marginBottom: 24 }}>
          <h3 style={{ fontSize: 13, color: 'var(--fg-faint)', marginBottom: 8, fontFamily: 'var(--mono)' }}>
            Sources · {bp.sources.filter(s => s.used).length} cited / {bp.sources.length} offered
          </h3>
          <ol style={{ listStyle: 'none', padding: 0, margin: 0, display: 'flex', flexDirection: 'column', gap: 6 }}>
            {bp.sources.map((s) => <SourceRow key={s.n} src={s} onClick={navigateSource} />)}
          </ol>
        </section>
      )}

      <div className="bp-actions">
        <button className="bp-btn bp-btn-ghost" onClick={() => onNavigate('blog')}>
          ← All drafts
        </button>
        {isTerminal && (
          <button
            className="bp-btn"
            onClick={onRegenerate}
            disabled={regenerating || deleting}
          >
            {regenerating ? 'regenerating…' : 'Regenerate'}
          </button>
        )}
        {bp.path && (
          <button
            className="bp-btn bp-btn-ghost"
            onClick={onCopyPath}
            title="Copy the vault file path for editing in Obsidian/VS Code"
          >
            {copied ? 'path copied' : 'Copy vault path'}
          </button>
        )}
        {bp.saved_as_note_id && (
          <button
            className="bp-btn"
            onClick={() => onNavigate('note', String(bp.saved_as_note_id))}
          >
            Open saved note →
          </button>
        )}
        <span className="bp-spacer" />
        <button
          className="bp-btn bp-btn-danger"
          onClick={onDelete}
          disabled={
            deleting || regenerating ||
            bp.status === 'pending' || bp.status === 'running'
          }
          title={
            bp.status === 'pending' || bp.status === 'running'
              ? 'Wait for the draft to finish before deleting'
              : undefined
          }
        >
          {deleting ? 'deleting…' : 'Delete'}
        </button>
      </div>
    </div>
  );
}

function SourceRow({
  src, onClick,
}: { src: BlogPostSource; onClick: (src: BlogPostSource) => void }) {
  const label = src.resolved.title ?? fallbackLabel(src);
  const rankStyle: React.CSSProperties = {
    display: 'inline-block', minWidth: 24, textAlign: 'center',
    padding: '1px 6px', marginRight: 8,
    fontFamily: 'var(--mono)', fontSize: 11,
    border: '1px solid var(--line)', borderRadius: 4,
    color: src.used ? 'var(--accent)' : 'var(--fg-faint)',
    background: src.used ? 'var(--accent-soft, transparent)' : 'transparent',
  };
  const deleted = src.resolved.deleted;

  return (
    <li
      style={{
        padding: '8px 10px',
        border: '1px solid var(--line)', borderRadius: 6,
        background: 'var(--bg-elev, transparent)',
        display: 'flex', alignItems: 'flex-start',
        opacity: deleted ? 0.55 : 1,
      }}
    >
      <span style={rankStyle}>{src.n}</span>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--fg-faint)', marginBottom: 2 }}>
          {src.kind}
          {src.origin && <> · {src.origin}</>}
          {src.used
            ? <> · <span style={{ color: 'var(--accent)' }}>cited</span></>
            : <> · offered</>}
          {deleted && <> · (deleted)</>}
        </div>
        {deleted ? (
          <span
            style={{
              fontSize: 13,
              color: 'var(--fg-faint)',
              textDecoration: 'line-through',
            }}
          >
            {label}
          </span>
        ) : (
          <a
            href="#"
            onClick={(e) => { e.preventDefault(); onClick(src); }}
            style={{ fontSize: 13, color: 'var(--fg)', textDecoration: 'none' }}
          >
            {label}
          </a>
        )}
      </div>
    </li>
  );
}

function fallbackLabel(src: BlogPostSource): string {
  if (src.kind === 'note') return `note #${src.ref}`;
  if (src.kind === 'article') return src.ref;
  if (src.kind === 'roundtable') return `roundtable #${src.ref}`;
  return src.ref;
}

// ───── Copy / share formatters ─────
//
// Goal: produce post-ready text for public platforms. The body's
// `[source N]` markers reference a mix of public articles, internal
// articles without a public origin, notes, and roundtables. Only
// article sources with a known `resolved.url` make it into the
// output — everything else (notes, roundtables, articles without a
// public origin) is silently stripped from both prose and the sources
// section, so a copied draft never carries an internal/localhost
// reference into a public post. Surviving citations are renumbered
// sequentially so the prose and the sources list line up.

interface PublicEntry { n: number; src: BlogPostSource; }

function buildPublicMap(sources: BlogPostSource[]): Map<number, PublicEntry> {
  const map = new Map<number, PublicEntry>();
  let next = 1;
  for (const s of sources) {
    if (!s.used) continue;
    if (s.kind !== 'article') continue;
    if (s.resolved.deleted) continue;
    const url = s.resolved.url;
    if (!url) continue;
    map.set(s.n, { n: next++, src: s });
  }
  return map;
}

function tidyAfterStrip(text: string): string {
  // After removing citations the prose can leave " ." or "  " artifacts.
  // Tighten punctuation spacing and collapse runs of inline whitespace; do
  // not touch newlines (paragraph structure must survive).
  return text
    .replace(/[ \t]+([.,;:!?])/g, '$1')
    .replace(/[ \t]{2,}/g, ' ');
}

function rewriteCitations(
  body: string,
  publicMap: Map<number, PublicEntry>,
  render: (newNs: number[]) => string,
): string {
  const re = new RegExp(CITATION_RE.source, 'gi');
  const out = body.replace(re, (_match, nums: string) => {
    const ns = nums
      .split(',')
      .map(p => p.replace(/source\s*/i, '').trim())
      .map(p => Number(p))
      .filter(n => Number.isFinite(n));
    const newNs: number[] = [];
    for (const n of ns) {
      const entry = publicMap.get(n);
      if (entry) newNs.push(entry.n);
    }
    if (newNs.length === 0) return '';
    return render(newNs);
  });
  return tidyAfterStrip(out);
}

const SUPERSCRIPT_DIGITS: Record<string, string> = {
  '0': '⁰', '1': '¹', '2': '²', '3': '³', '4': '⁴',
  '5': '⁵', '6': '⁶', '7': '⁷', '8': '⁸', '9': '⁹',
};

function toSuperscript(n: number): string {
  return n.toString().split('').map(d => SUPERSCRIPT_DIGITS[d] ?? d).join('');
}

function orderedPublicEntries(map: Map<number, PublicEntry>): PublicEntry[] {
  return Array.from(map.values()).sort((a, b) => a.n - b.n);
}

function titleHeader(bp: BlogPostDetail): string {
  const t = bp.title?.trim();
  return t ? `# ${t}\n\n` : '';
}

export function formatMarkdown(bp: BlogPostDetail): string {
  const map = buildPublicMap(bp.sources);
  const body = rewriteCitations(
    bp.body_md ?? '',
    map,
    (newNs) => newNs.map(n => `[^${n}]`).join(''),
  );
  if (map.size === 0) return titleHeader(bp) + body;
  const defs = orderedPublicEntries(map)
    .map(({ n, src }) => `[^${n}]: [${src.resolved.title ?? `source ${n}`}](${src.resolved.url})`)
    .join('\n');
  return `${titleHeader(bp)}${body}\n\n${defs}\n`;
}

export function formatTwitter(bp: BlogPostDetail): string {
  // X long-form: strip inline citation markers entirely so the prose reads
  // clean (no superscripts breaking sentences). Keep the public links in
  // a Sources block at the end — they're worth keeping accessible without
  // cluttering the body.
  const map = buildPublicMap(bp.sources);
  const stripped = tidyAfterStrip((bp.body_md ?? '').replace(CITATION_RE, ''));
  if (map.size === 0) return titleHeader(bp) + stripped;
  const list = orderedPublicEntries(map)
    .map(({ src }) => `- [${src.resolved.title ?? 'source'}](${src.resolved.url})`)
    .join('\n');
  return `${titleHeader(bp)}${stripped}\n\n## Sources\n\n${list}\n`;
}

export function formatPlain(bp: BlogPostDetail): string {
  const stripped = tidyAfterStrip((bp.body_md ?? '').replace(CITATION_RE, ''));
  return titleHeader(bp) + stripped;
}

interface CopyMenuItemProps {
  label: string;
  hint: string;
  onClick: () => void;
}

function CopyMenuItem({ label, hint, onClick }: CopyMenuItemProps) {
  return (
    <button className="bp-menu-item" role="menuitem" onClick={onClick}>
      <span className="label">{label}</span>
      <span className="hint">{hint}</span>
    </button>
  );
}

// ───── Markdown rendering ─────

interface MarkdownBodyProps {
  body: string;
  sourceByN: Map<number, BlogPostSource>;
  onCiteClick: (src: BlogPostSource) => void;
}

/**
 * Minimal block-level markdown renderer. Handles headers, paragraphs, fenced
 * code blocks, lists, and blockquotes — no third-party deps. After block
 * splitting, each text run is tokenized for inline emphasis + `[source N]`
 * citations so clicks route through onNavigate.
 *
 * Rough edges: doesn't render tables, images, or raw HTML. The author's
 * voice is prose-heavy (spec §8.2 constraints), so these are rare in a blog
 * draft. If they show up, the fallback is just plain-text output, which is
 * readable even if unstyled.
 */
function MarkdownBody({ body, sourceByN, onCiteClick }: MarkdownBodyProps) {
  const blocks = useMemo(() => splitBlocks(body), [body]);
  return (
    <>
      {blocks.map((b, i) => renderBlock(b, i, sourceByN, onCiteClick))}
    </>
  );
}

type Block =
  | { kind: 'h1' | 'h2' | 'h3' | 'h4'; text: string }
  | { kind: 'code'; lang: string | null; text: string }
  | { kind: 'quote'; text: string }
  | { kind: 'ul'; items: string[] }
  | { kind: 'ol'; items: string[] }
  | { kind: 'p'; text: string };

function splitBlocks(body: string): Block[] {
  const lines = body.split('\n');
  const blocks: Block[] = [];
  let i = 0;

  while (i < lines.length) {
    const line = lines[i];

    // fenced code
    if (/^```/.test(line)) {
      const lang = line.slice(3).trim() || null;
      const start = i + 1;
      let end = start;
      while (end < lines.length && !/^```\s*$/.test(lines[end])) end++;
      blocks.push({ kind: 'code', lang, text: lines.slice(start, end).join('\n') });
      i = end + 1;
      continue;
    }

    // header
    const h = /^(#{1,4})\s+(.+)$/.exec(line);
    if (h) {
      const level = h[1].length as 1 | 2 | 3 | 4;
      const kind = (`h${level}`) as 'h1' | 'h2' | 'h3' | 'h4';
      blocks.push({ kind, text: h[2] });
      i++;
      continue;
    }

    // blockquote (consume contiguous `>` lines)
    if (/^>\s?/.test(line)) {
      const buf: string[] = [];
      while (i < lines.length && /^>\s?/.test(lines[i])) {
        buf.push(lines[i].replace(/^>\s?/, ''));
        i++;
      }
      blocks.push({ kind: 'quote', text: buf.join('\n') });
      continue;
    }

    // unordered list
    if (/^\s*[-*]\s+/.test(line)) {
      const buf: string[] = [];
      while (i < lines.length && /^\s*[-*]\s+/.test(lines[i])) {
        buf.push(lines[i].replace(/^\s*[-*]\s+/, ''));
        i++;
      }
      blocks.push({ kind: 'ul', items: buf });
      continue;
    }

    // ordered list
    if (/^\s*\d+\.\s+/.test(line)) {
      const buf: string[] = [];
      while (i < lines.length && /^\s*\d+\.\s+/.test(lines[i])) {
        buf.push(lines[i].replace(/^\s*\d+\.\s+/, ''));
        i++;
      }
      blocks.push({ kind: 'ol', items: buf });
      continue;
    }

    // blank line -> skip
    if (line.trim() === '') {
      i++;
      continue;
    }

    // paragraph (gather until blank line or block-starter)
    const buf: string[] = [];
    while (i < lines.length) {
      const cur = lines[i];
      if (
        cur.trim() === '' ||
        /^```/.test(cur) ||
        /^(#{1,4})\s+/.test(cur) ||
        /^>\s?/.test(cur) ||
        /^\s*[-*]\s+/.test(cur) ||
        /^\s*\d+\.\s+/.test(cur)
      ) break;
      buf.push(cur);
      i++;
    }
    if (buf.length > 0) blocks.push({ kind: 'p', text: buf.join(' ') });
  }

  return blocks;
}

function renderBlock(
  b: Block,
  key: number,
  sourceByN: Map<number, BlogPostSource>,
  onCiteClick: (src: BlogPostSource) => void,
): React.ReactNode {
  const inline = (text: string) => renderInline(text, sourceByN, onCiteClick);
  switch (b.kind) {
    case 'h1':
      return <h1 key={key} style={{ fontFamily: 'var(--serif)', fontSize: 32, margin: '24px 0 12px' }}>{inline(b.text)}</h1>;
    case 'h2':
      return <h2 key={key} style={{ fontFamily: 'var(--serif)', fontSize: 24, margin: '22px 0 10px' }}>{inline(b.text)}</h2>;
    case 'h3':
      return <h3 key={key} style={{ fontFamily: 'var(--serif)', fontSize: 19, margin: '18px 0 8px' }}>{inline(b.text)}</h3>;
    case 'h4':
      return <h4 key={key} style={{ fontFamily: 'var(--serif)', fontSize: 16, margin: '14px 0 6px' }}>{inline(b.text)}</h4>;
    case 'code':
      return (
        <pre
          key={key}
          style={{
            background: 'var(--bg-sunk, transparent)',
            border: '1px solid var(--line)',
            borderRadius: 6,
            padding: 12,
            fontFamily: 'var(--mono)',
            fontSize: 13,
            overflow: 'auto',
            margin: '12px 0',
          }}
        >
          <code>{b.text}</code>
        </pre>
      );
    case 'quote':
      return (
        <blockquote
          key={key}
          style={{
            borderLeft: '3px solid var(--accent)',
            paddingLeft: 12,
            color: 'var(--fg-mute)',
            margin: '12px 0',
            fontStyle: 'italic',
          }}
        >
          {b.text.split('\n').map((ln, j) => <p key={j} style={{ margin: '4px 0' }}>{inline(ln)}</p>)}
        </blockquote>
      );
    case 'ul':
      return (
        <ul key={key} style={{ paddingLeft: 22, margin: '12px 0' }}>
          {b.items.map((it, j) => <li key={j} style={{ margin: '4px 0' }}>{inline(it)}</li>)}
        </ul>
      );
    case 'ol':
      return (
        <ol key={key} style={{ paddingLeft: 22, margin: '12px 0' }}>
          {b.items.map((it, j) => <li key={j} style={{ margin: '4px 0' }}>{inline(it)}</li>)}
        </ol>
      );
    case 'p':
      return <p key={key} style={{ margin: '12px 0' }}>{inline(b.text)}</p>;
  }
}

function renderInline(
  text: string,
  sourceByN: Map<number, BlogPostSource>,
  onCiteClick: (src: BlogPostSource) => void,
): React.ReactNode {
  // Pass 1: split on citation tokens. The spec's agent emits `[source N]` or
  // `[source 3, source 7]`; we preserve the matched group so each gets a
  // clickable span.
  const out: React.ReactNode[] = [];
  let lastIndex = 0;
  let keyCounter = 0;
  const re = new RegExp(CITATION_RE.source, 'gi');
  let match: RegExpExecArray | null;
  while ((match = re.exec(text)) !== null) {
    if (match.index > lastIndex) {
      out.push(
        <Fragment key={keyCounter++}>
          {renderEmphasis(text.slice(lastIndex, match.index), keyCounter++)}
        </Fragment>,
      );
    }
    out.push(renderCitation(match[1], keyCounter++, sourceByN, onCiteClick));
    lastIndex = match.index + match[0].length;
  }
  if (lastIndex < text.length) {
    out.push(
      <Fragment key={keyCounter++}>
        {renderEmphasis(text.slice(lastIndex), keyCounter++)}
      </Fragment>,
    );
  }
  return out;
}

function renderCitation(
  numsRaw: string,
  baseKey: number,
  sourceByN: Map<number, BlogPostSource>,
  onCiteClick: (src: BlogPostSource) => void,
): React.ReactNode {
  // Split "3, source 7" → ["3", "7"]
  const parts = numsRaw.split(',')
    .map(p => p.replace(/source\s*/i, '').trim())
    .filter(Boolean);
  return (
    <span key={baseKey} style={{ whiteSpace: 'nowrap' }}>
      {'['}
      {parts.map((pStr, i) => {
        const n = Number(pStr);
        const src = Number.isFinite(n) ? sourceByN.get(n) : undefined;
        const isDeleted = src?.resolved.deleted === true;
        const prefix = i > 0 ? ', ' : '';
        if (!src) {
          // Source number didn't resolve — render inert (the agent normally
          // strips these, but render defensively in case one slips through).
          return (
            <Fragment key={i}>
              {prefix}
              <span style={{ color: 'var(--fg-faint)' }}>{n}</span>
            </Fragment>
          );
        }
        if (isDeleted) {
          return (
            <Fragment key={i}>
              {prefix}
              <span
                className="citation-deleted"
                title="source deleted"
                style={{
                  color: 'var(--fg-faint)',
                  textDecoration: 'line-through',
                  cursor: 'help',
                }}
              >
                {n}
              </span>
            </Fragment>
          );
        }
        return (
          <Fragment key={i}>
            {prefix}
            <a
              href="#"
              data-cite={n}
              onClick={(e) => { e.preventDefault(); onCiteClick(src); }}
              title={src.resolved.title ?? fallbackLabel(src)}
              style={{
                color: 'var(--accent)',
                textDecoration: 'none',
                fontWeight: 500,
                cursor: 'pointer',
              }}
            >
              {n}
            </a>
          </Fragment>
        );
      })}
      {']'}
    </span>
  );
}

// Simple inline emphasis — handles `**bold**`, `*italic*`, and `` `code` ``.
// Intentionally small; blog drafts are prose-heavy so deeper formatting
// (links, images, nested emphasis) lives outside v1 scope.
function renderEmphasis(text: string, baseKey: number): React.ReactNode {
  const out: React.ReactNode[] = [];
  const re = /(\*\*([^*]+)\*\*|\*([^*]+)\*|`([^`]+)`)/g;
  let last = 0;
  let m: RegExpExecArray | null;
  let k = 0;
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) out.push(<Fragment key={`${baseKey}-t${k++}`}>{text.slice(last, m.index)}</Fragment>);
    if (m[2] !== undefined) {
      out.push(<strong key={`${baseKey}-b${k++}`}>{m[2]}</strong>);
    } else if (m[3] !== undefined) {
      out.push(<em key={`${baseKey}-i${k++}`}>{m[3]}</em>);
    } else if (m[4] !== undefined) {
      out.push(
        <code
          key={`${baseKey}-c${k++}`}
          style={{
            fontFamily: 'var(--mono)', fontSize: '0.9em',
            background: 'var(--bg-sunk, transparent)',
            padding: '1px 4px', borderRadius: 3,
          }}
        >
          {m[4]}
        </code>,
      );
    }
    last = m.index + m[0].length;
  }
  if (last < text.length) out.push(<Fragment key={`${baseKey}-t${k++}`}>{text.slice(last)}</Fragment>);
  return out;
}
