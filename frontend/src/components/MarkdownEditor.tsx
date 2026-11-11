import { Suspense, lazy, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import ReactMarkdown, { type Components } from 'react-markdown';
import { api } from '../api';
import type { CodeMirrorMarkdownProps } from './CodeMirrorMarkdown';

const LazyCodeMirrorMarkdown = lazy(() =>
  import('./CodeMirrorMarkdown').then((m) => ({ default: m.CodeMirrorMarkdown })));

export interface VaultEditorTarget {
  path: string;
  title: string;
}

interface VaultMarkdownEditorProps {
  target: VaultEditorTarget;
  onClose: () => void;
  onSaved?: () => void | Promise<void>;
}

interface MarkdownEditorProps {
  path: string;
  title: string;
  initialContent: string;
  baseSha256: string;
  onClose: () => void;
  onSaved?: () => void | Promise<void>;
}

interface LoadedVaultFile {
  content: string;
  content_sha256: string;
}

interface SplitContent {
  frontmatter: string;
  body: string;
}

export function VaultMarkdownEditor({ target, onClose, onSaved }: VaultMarkdownEditorProps) {
  const [file, setFile] = useState<LoadedVaultFile | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setFile(null);
    setErr(null);
    api.vaultFile.read(target.path)
      .then((loaded) => {
        if (!cancelled) setFile(loaded);
      })
      .catch((e) => {
        if (!cancelled) setErr(e instanceof Error ? e.message : 'failed');
      });
    return () => { cancelled = true; };
  }, [target.path]);

  if (err) {
    return (
      <div className="markdown-editor-shell">
        <div className="markdown-editor">
          <div className="markdown-editor-bar">
            <div>
              <span className="dash-muted">{target.path}</span>
              <h2>{target.title}</h2>
            </div>
            <button className="chip muted" type="button" onClick={onClose}>close</button>
          </div>
          <p className="dash-error">{err}</p>
        </div>
      </div>
    );
  }

  if (file === null) {
    return (
      <div className="markdown-editor-shell">
        <div className="markdown-editor">
          <p className="dash-empty">loading...</p>
        </div>
      </div>
    );
  }

  return (
    <MarkdownEditor
      key={target.path}
      path={target.path}
      title={target.title}
      initialContent={file.content}
      baseSha256={file.content_sha256}
      onClose={onClose}
      onSaved={onSaved}
    />
  );
}

export function MarkdownEditor({
  path,
  title,
  initialContent,
  baseSha256,
  onClose,
  onSaved,
}: MarkdownEditorProps) {
  const split = useMemo(() => splitFrontmatter(initialContent), [initialContent]);
  const [body, setBody] = useState(split.body);
  const bodyRef = useRef(split.body);
  const editingTokenRef = useRef<string | null>(null);
  const [currentBaseSha256, setCurrentBaseSha256] = useState(baseSha256);
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [rescanWarning, setRescanWarning] = useState<string | null>(null);

  useEffect(() => {
    bodyRef.current = body;
  }, [body]);

  useEffect(() => {
    setCurrentBaseSha256(baseSha256);
  }, [baseSha256]);

  useEffect(() => {
    let active = true;
    editingTokenRef.current = null;
    api.editing.lock(path)
      .then((locked) => {
        if (active) editingTokenRef.current = locked.token;
      })
      .catch((e) => {
        if (active) setErr(e instanceof Error ? e.message : 'lock failed');
      });
    const heartbeat = window.setInterval(() => {
      const token = editingTokenRef.current;
      if (token) api.editing.heartbeat(path, token).catch(() => void 0);
    }, 30000);
    return () => {
      active = false;
      window.clearInterval(heartbeat);
      const token = editingTokenRef.current;
      editingTokenRef.current = null;
      if (token) api.editing.unlock(path, token).catch(() => void 0);
    };
  }, [path]);

  const save = useCallback(async () => {
    setSaving(true);
    setErr(null);
    setRescanWarning(null);
    try {
      const saved = await api.vaultFile.write(
        path,
        joinFrontmatterAndBody(split.frontmatter, bodyRef.current),
        currentBaseSha256,
      );
      setCurrentBaseSha256(saved.content_sha256);
      if (saved.rescan_failed) {
        setRescanWarning('Saved, but the derived views did not refresh. Try again after closing the editor.');
        return;
      }
      await onSaved?.();
      onClose();
    } catch (e) {
      setErr(e instanceof Error ? e.message : 'save failed');
    } finally {
      setSaving(false);
    }
  }, [currentBaseSha256, onClose, onSaved, path, split.frontmatter]);

  return (
    <div className="markdown-editor-shell">
      <div className="markdown-editor">
        <div className="markdown-editor-bar">
          <div>
            <span className="dash-muted">{path}</span>
            <h2>{title}</h2>
          </div>
          <div className="dash-actions">
            <button className="chip muted" type="button" onClick={onClose}>close</button>
            <button className="chip" type="button" disabled={saving} onClick={save}>
              {saving ? 'saving' : 'save'}
            </button>
          </div>
        </div>
        {err && <p className="dash-error">{err}</p>}
        {rescanWarning && <p className="dash-warning">{rescanWarning}</p>}
        {split.frontmatter && (
          <details className="frontmatter-readonly">
            <summary>Frontmatter</summary>
            <pre>{split.frontmatter.trimEnd()}</pre>
          </details>
        )}
        <div className="markdown-editor-grid">
          <section className="markdown-editor-pane">
            <div className="dash-mini-h">Markdown</div>
            <CodeMirrorMarkdown value={body} onChange={setBody} onError={setErr}/>
          </section>
          <section className="markdown-editor-pane preview">
            <div className="dash-mini-h">Preview</div>
            <div className="markdown-preview">
              <MarkdownBlock source={body}/>
            </div>
          </section>
        </div>
      </div>
    </div>
  );
}

const markdownPreviewComponents: Components = {
  a({ href, node: _node, ...props }) {
    return <a {...props} href={previewAttachmentUrl(href)} />;
  },
  img({ src, node: _node, ...props }) {
    return <img {...props} src={previewAttachmentUrl(src)} />;
  },
};

export function MarkdownBlock({ source }: { source?: string }) {
  return <ReactMarkdown components={markdownPreviewComponents}>{source ?? ''}</ReactMarkdown>;
}

function previewAttachmentUrl(url: string | undefined): string | undefined {
  if (!url?.startsWith('attachments/')) return url;
  const name = url.slice('attachments/'.length);
  if (!/^[a-f0-9]{32}(?:[a-f0-9]{32})?\.(?:png|jpg|gif|webp|heic|mp4|mov|pdf)$/.test(name)) {
    return url;
  }
  return `/api/attachments/${encodeURIComponent(name)}`;
}

// CodeMirror is ~300 kB, and this module is imported by read-only views for
// MarkdownBlock. The editor itself lives in its own chunk and loads when an
// editor is first opened; the fallback keeps the host box so the pane does not
// jump when it arrives.
export function CodeMirrorMarkdown(props: CodeMirrorMarkdownProps) {
  return (
    <Suspense fallback={<div className="codemirror-host" aria-busy="true"/>}>
      <LazyCodeMirrorMarkdown {...props}/>
    </Suspense>
  );
}

function joinFrontmatterAndBody(frontmatter: string, body: string): string {
  if (!frontmatter || !body) return `${frontmatter}${body}`;
  if (frontmatter.endsWith('\n\n')) return `${frontmatter}${body}`;
  if (frontmatter.endsWith('\n')) return `${frontmatter}\n${body}`;
  return `${frontmatter}\n\n${body}`;
}

function splitFrontmatter(content: string): SplitContent {
  if (!content.startsWith('---\n')) {
    return { frontmatter: '', body: content };
  }
  const close = /^---[ \t]*\r?$/m.exec(content.slice(4));
  if (!close) {
    return { frontmatter: '', body: content };
  }
  let end = 4 + close.index + close[0].length;
  if (content[end] === '\r') end += 1;
  if (content[end] === '\n') end += 1;
  if (content[end] === '\n') end += 1;
  return {
    frontmatter: content.slice(0, end),
    body: content.slice(end),
  };
}
