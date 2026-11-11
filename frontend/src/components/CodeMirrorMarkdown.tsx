// The CodeMirror editor surface. Loaded lazily through MarkdownEditor.tsx —
// import `CodeMirrorMarkdown` from there, not from this file.
import { useCallback, useEffect, useRef } from 'react';
import { defaultKeymap, history, historyKeymap, indentWithTab } from '@codemirror/commands';
import { markdown } from '@codemirror/lang-markdown';
import { EditorState } from '@codemirror/state';
import { EditorView, keymap, placeholder } from '@codemirror/view';
import { api } from '../api';

export interface CodeMirrorMarkdownProps {
  value: string;
  onChange: (value: string) => void;
  onError: (message: string | null) => void;
  onModEnter?: () => void;
}

export function CodeMirrorMarkdown({ value, onChange, onError, onModEnter }: CodeMirrorMarkdownProps) {
  const hostRef = useRef<HTMLDivElement | null>(null);
  const viewRef = useRef<EditorView | null>(null);
  const initialValueRef = useRef(value);
  const onChangeRef = useRef(onChange);
  const onErrorRef = useRef(onError);
  const onModEnterRef = useRef(onModEnter);

  useEffect(() => {
    onChangeRef.current = onChange;
    onErrorRef.current = onError;
    onModEnterRef.current = onModEnter;
  }, [onChange, onError, onModEnter]);

  useEffect(() => {
    const view = viewRef.current;
    if (!view) {
      initialValueRef.current = value;
      return;
    }
    const current = view.state.doc.toString();
    if (current !== value) {
      view.dispatch({ changes: { from: 0, to: current.length, insert: value } });
    }
  }, [value]);

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
          keymap.of([
            {
              key: 'Mod-Enter',
              run: () => {
                onModEnterRef.current?.();
                return true;
              },
            },
            indentWithTab,
            ...defaultKeymap,
            ...historyKeymap,
          ]),
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
