import { useEffect, useRef, useState } from 'react';

type MermaidTheme = 'dark' | 'neutral';

// Module-level render counter. Mermaid is loaded lazily so the ~500KB
// library only ships to readers who actually open an article with a
// diagram section.
let renderSeq = 0;
let initializedTheme: MermaidTheme | null = null;

function currentTheme(): MermaidTheme {
  const t = document.documentElement.getAttribute('data-theme');
  const dark = t ? t === 'dark' : (window.matchMedia?.('(prefers-color-scheme: dark)').matches ?? false);
  return dark ? 'dark' : 'neutral';
}

interface Props {
  source: string;
}

/** Renders one Mermaid diagram from raw source, with a code fallback on parse errors. */
export function MermaidBlock({ source }: Props) {
  const ref = useRef<HTMLDivElement | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [theme, setTheme] = useState<MermaidTheme>(currentTheme);

  // Re-render diagrams when the app theme toggles (App.tsx stamps data-theme
  // on <html>).
  useEffect(() => {
    const obs = new MutationObserver(() => setTheme(currentTheme()));
    obs.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
    return () => obs.disconnect();
  }, []);

  useEffect(() => {
    let cancelled = false;
    // Capture the id up front: renderSeq is module-global and another
    // MermaidBlock may increment it before our catch runs.
    const id = `mmd-${++renderSeq}`;
    (async () => {
      try {
        const mermaid = (await import('mermaid')).default;
        if (initializedTheme !== theme) {
          // No fontFamily override: mermaid measures node boxes with the font
          // it's configured for, so inheriting the app font clips labels.
          mermaid.initialize({
            startOnLoad: false,
            securityLevel: 'strict',
            theme,
          });
          initializedTheme = theme;
        }
        const { svg } = await mermaid.render(id, source.trim());
        if (!cancelled && ref.current) {
          ref.current.innerHTML = svg;
          setError(null);
        }
      } catch (e) {
        // mermaid.render leaves an orphaned error element in <body> on parse
        // failure; clean it up so broken diagrams don't litter the page.
        document.getElementById(`d${id}`)?.remove();
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      }
    })();
    return () => { cancelled = true; };
  }, [source, theme]);

  // The render container stays mounted even in the error state — a later
  // successful render (theme change, source fix) needs ref.current to exist.
  return (
    <>
      <div className="mermaid-block" ref={ref} style={{ overflowX: 'auto', display: error ? 'none' : undefined }} />
      {error && (
        <pre className="mermaid-error" title={error} style={{
          overflowX: 'auto', fontSize: 12, padding: 12,
          border: '1px solid var(--border)', borderRadius: 6, opacity: 0.75,
        }}>{source}</pre>
      )}
    </>
  );
}
