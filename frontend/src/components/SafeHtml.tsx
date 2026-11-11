import { useMemo, type CSSProperties } from 'react';
import { sanitizeHtml } from '../sanitize';

interface Props {
  html: string | null | undefined;
  as?: 'div' | 'p';
  className?: string;
  style?: CSSProperties;
}

/** Renders model-written HTML through the shared allowlist sanitizer. */
export function SafeHtml({ html, as: Tag = 'div', className, style }: Props) {
  // Sanitizing parses the fragment; redo it only when the source changes.
  const clean = useMemo(() => sanitizeHtml(html), [html]);
  return <Tag className={className} style={style} dangerouslySetInnerHTML={{ __html: clean }}/>;
}
