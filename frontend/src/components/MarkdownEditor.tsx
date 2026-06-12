import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { defaultKeymap, history, historyKeymap, indentWithTab } from '@codemirror/commands';
import { markdown } from '@codemirror/lang-markdown';
import { EditorState } from '@codemirror/state';
import { EditorView, keymap, placeholder } from '@codemirror/view';
import ReactMarkdown, { type Components } from 'react-markdown';
import { api } from '../api';

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
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    bodyRef.current = body;
  }, [body]);

  useEffect(() => {
    let active = true;
    api.editing.lock(path).catch((e) => {
      if (active) setErr(e instanceof Error ? e.message : 'lock failed');
    });
    const heartbeat = window.setInterval(() => {
      api.editing.heartbeat(path).catch(() => void 0);
    }, 30000);
    return () => {
      active = false;
      window.clearInterval(heartbeat);
      api.editing.unlock(path).catch(() => void 0);
    };
  }, [path]);

  const save = useCallback(async () => {
    setSaving(true);
    setErr(null);
    try {
      await api.vaultFile.write(path, `${split.frontmatter}${bodyRef.current}`, baseSha256);
      await onSaved?.();
      onClose();
    } catch (e) {
      setErr(e instanceof Error ? e.message : 'save failed');
    } finally {
      setSaving(false);
    }
  }, [baseSha256, onClose, onSaved, path, split.frontmatter]);

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
              <ReactMarkdown components={markdownPreviewComponents}>{body}</ReactMarkdown>
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

function previewAttachmentUrl(url: string | undefined): string | undefined {
  if (!url?.startsWith('attachments/')) return url;
  const name = url.slice('attachments/'.length);
  if (!/^[a-f0-9]{32}(?:[a-f0-9]{32})?\.(?:png|jpg|gif|webp|heic|mp4|mov|pdf)$/.test(name)) {
    return url;
  }
  return `/api/attachments/${encodeURIComponent(name)}`;
}

function CodeMirrorMarkdown({
  value,
  onChange,
  onError,
}: {
  value: string;
  onChange: (value: string) => void;
  onError: (message: string | null) => void;
}) {
  const hostRef = useRef<HTMLDivElement | null>(null);
  const viewRef = useRef<EditorView | null>(null);
  const initialValueRef = useRef(value);
  const onChangeRef = useRef(onChange);
  const onErrorRef = useRef(onError);

  useEffect(() => {
    onChangeRef.current = onChange;
    onErrorRef.current = onError;
  }, [onChange, onError]);

  const insertAttachmentMarkdown = useCallback(async (
    file: File,
    view: EditorView,
    at?: number,
  ) => {
    try {
      onErrorRef.current(null);
      const uploaded = await api.attachments.upload(file);
      const from = at ?? view.state.selection.main.from;
      view.dispatch({
        changes: { from, insert: uploaded.markdown },
        selection: { anchor: from + uploaded.markdown.length },
      });
      view.focus();
    } catch (e) {
      onErrorRef.current(e instanceof Error ? e.message : 'attachment upload failed');
    }
  }, []);

  useEffect(() => {
    if (!hostRef.current) return undefined;
    const view = new EditorView({
      parent: hostRef.current,
      state: EditorState.create({
        doc: initialValueRef.current,
        extensions: [
          history(),
          markdown(),
          placeholder(''),
          keymap.of([indentWithTab, ...defaultKeymap, ...historyKeymap]),
          EditorView.lineWrapping,
          EditorView.updateListener.of((update) => {
            if (update.docChanged) {
              onChangeRef.current(update.state.doc.toString());
            }
          }),
          EditorView.domEventHandlers({
            paste(event, editorView) {
              const file = firstAttachmentFile(event.clipboardData?.files);
              if (!file) return false;
              event.preventDefault();
              void insertAttachmentMarkdown(file, editorView);
              return true;
            },
            drop(event, editorView) {
              const file = firstAttachmentFile(event.dataTransfer?.files);
              if (!file) return false;
              event.preventDefault();
              const pos = editorView.posAtCoords({ x: event.clientX, y: event.clientY });
              void insertAttachmentMarkdown(file, editorView, pos ?? undefined);
              return true;
            },
          }),
        ],
      }),
    });
    viewRef.current = view;
    return () => {
      view.destroy();
      viewRef.current = null;
    };
  }, [insertAttachmentMarkdown]);

  return <div className="codemirror-host" ref={hostRef}/>;
}

function firstAttachmentFile(files: FileList | null | undefined): File | null {
  if (!files || files.length === 0) return null;
  return files[0] ?? null;
}

function splitFrontmatter(content: string): SplitContent {
  if (!content.startsWith('---\n')) {
    return { frontmatter: '', body: content };
  }
  const close = content.indexOf('\n---', 4);
  if (close < 0) {
    return { frontmatter: '', body: content };
  }
  let end = close + '\n---'.length;
  if (content[end] === '\n') end += 1;
  if (content[end] === '\n') end += 1;
  return {
    frontmatter: content.slice(0, end),
    body: content.slice(end),
  };
}
