import { useEffect, useRef, useState } from 'react';
import type { Mounted } from 'pele';

// App-owned limits bound generated or captured source before inserting SVG.
const SOURCE_LIMIT = 50_000;
const SVG_OUTPUT_LIMIT = 1_000_000;
const EDGE_LIMIT = 500;

interface Props {
  source: string;
}

/** Stored Mermaid source -> Pele SVG -> sanitizer -> responsive local diagram. */
export function MermaidBlock({ source }: Props) {
  const ref = useRef<HTMLDivElement | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const container = ref.current;
    let cancelled = false;
    let diagram: Mounted | undefined;
    setError(null);
    setLoading(true);

    // Bundle locally and load only for readers opening a diagram or new chart.
    void (async () => {
      try {
        const [{ mount }, { default: DOMPurify }] = await Promise.all([
          import('pele'), import('dompurify'),
        ]);
        if (cancelled || !container) {
          return;
        }

        // Source cannot enable navigation, external images, or raw icon markup.
        diagram = mount(container, source.trim(), {
          links: false,
          images: false,
          limit: SOURCE_LIMIT,
          outputLimit: SVG_OUTPUT_LIMIT,
          maxEdges: EDGE_LIMIT,
          zoom: 'auto',
          sanitize: (svg) => DOMPurify.sanitize(svg, {
            USE_PROFILES: { svg: true, svgFilters: true },
            FORBID_TAGS: ['foreignObject', 'image', 'a'],
            RETURN_DOM_FRAGMENT: true,
          }),
          onRender: () => {
            if (cancelled) {
              return;
            }
            setError(null);
            setLoading(false);
          },
          onError: (reason) => {
            if (cancelled) {
              return;
            }
            setError(reason instanceof Error ? reason.message : String(reason));
            setLoading(false);
          },
        });
      } catch (reason) {
        if (cancelled) {
          return;
        }
        setError(reason instanceof Error ? reason.message : String(reason));
        setLoading(false);
      }
    })();

    // Stop resize/font observers and zoom handlers on source changes or unmount.
    return () => {
      cancelled = true;
      diagram?.destroy();
      container?.replaceChildren();
    };
  }, [source]);

  // Keep the container mounted so corrected source can recover from parse errors.
  return (
    <>
      <div className="mermaid-block" ref={ref} aria-busy={loading} style={{ display: error ? 'none' : undefined }} />
      {loading && <span role="status">Rendering diagram…</span>}
      {error && <pre className="mermaid-error" title={error}>{source}</pre>}
    </>
  );
}
