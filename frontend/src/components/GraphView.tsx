import { useEffect, useLayoutEffect, useRef, useState, useCallback } from 'react';
import { api } from '../api';
import type { GraphData, View } from '../types';

interface Props {
  onNavigate: (view: View, id?: string) => void;
}

interface SimNode {
  id: string;
  title: string;
  kind: string;
  color: string;
  size: number;
  degree: number;
  x: number;
  y: number;
  fx?: number | null;
  fy?: number | null;
}

type SimLink = { source: SimNode; target: SimNode; weight: number };

const CLUSTER_CENTERS: Record<string, [number, number]> = {
  Concept:   [0.32, 0.40],
  Synthesis: [0.68, 0.38],
  Entity:    [0.72, 0.74],
  Source:    [0.30, 0.76],
};

const WORLD_W = 2400;
const WORLD_H = 1600;
const MIN_ZOOM = 0.2;
const MAX_ZOOM = 5;
const MINI_W = 180;
const MINI_H = 120;
// CSS-pixel tolerance for "near enough" edge hit-testing. With 1px strokes the
// SVG version was effectively unhittable; explicit tolerance is friendlier.
const EDGE_HIT_TOLERANCE = 5;
const EDGE_TOOLTIP_MIN_ZOOM = 0.65;
const MAX_FIT_LABELS = 36;
const MAX_MID_LABELS = 72;
const MAX_CLOSE_LABELS = 140;
const GOLDEN_ANGLE = Math.PI * (3 - Math.sqrt(5));
const GRAPH_CACHE_KEY = 'mastisk:graph:v1';
const GRAPH_CACHE_MAX_AGE_MS = 10 * 60 * 1000;

interface LayoutBounds {
  x: number;
  y: number;
  w: number;
  h: number;
}

interface GraphPerf {
  fetchMs?: number;
  layoutMs?: number;
  readyMs?: number;
  lastDrawMs?: number;
  maxDrawMs?: number;
  draws?: number[];
  nodes?: number;
  edges?: number;
  cacheHit?: boolean;
  cacheAgeMs?: number;
  fitZoom?: number;
  fitPan?: { x: number; y: number };
  fitViewport?: { w: number; h: number };
  fitBounds?: LayoutBounds;
}

function graphPerf(): GraphPerf | null {
  if (typeof window === 'undefined') return null;
  const w = window as Window & { __mastiskGraphPerf?: GraphPerf };
  w.__mastiskGraphPerf ??= {};
  return w.__mastiskGraphPerf;
}

function hash(s: string): number {
  let h = 2166136261;
  for (let i = 0; i < s.length; i++) h = Math.imul(h ^ s.charCodeAt(i), 16777619);
  return h >>> 0;
}

function clamp(v: number, lo: number, hi: number) {
  return Math.max(lo, Math.min(hi, v));
}

function canFitViewport(viewport: { w: number; h: number }): boolean {
  if (!viewport.w || !viewport.h) return false;
  const minWidth = typeof window !== 'undefined' && window.innerWidth >= 900 ? 480 : 240;
  return viewport.w >= minWidth && viewport.h >= 320;
}

function readCachedGraph(): { data: GraphData; ageMs: number } | null {
  if (typeof window === 'undefined') return null;
  try {
    const raw = window.localStorage.getItem(GRAPH_CACHE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as { savedAt?: number; data?: GraphData };
    if (!parsed.savedAt || !parsed.data || !Array.isArray(parsed.data.nodes)) return null;
    const ageMs = Date.now() - parsed.savedAt;
    if (ageMs < 0 || ageMs > GRAPH_CACHE_MAX_AGE_MS) return null;
    return { data: parsed.data, ageMs };
  } catch {
    return null;
  }
}

function writeCachedGraph(data: GraphData) {
  if (typeof window === 'undefined') return;
  try {
    window.localStorage.setItem(GRAPH_CACHE_KEY, JSON.stringify({ savedAt: Date.now(), data }));
  } catch {
    // LocalStorage can be disabled or full. The graph still works from network.
  }
}

function layoutBounds(nodes: SimNode[]): LayoutBounds {
  if (!nodes.length) return { x: 0, y: 0, w: WORLD_W, h: WORLD_H };
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
  for (const n of nodes) {
    const x = n.x ?? 0, y = n.y ?? 0;
    if (x < minX) minX = x;
    if (y < minY) minY = y;
    if (x > maxX) maxX = x;
    if (y > maxY) maxY = y;
  }
  if (!isFinite(minX)) return { x: 0, y: 0, w: WORLD_W, h: WORLD_H };
  const pad = 60;
  return {
    x: minX - pad,
    y: minY - pad,
    w: maxX - minX + pad * 2,
    h: maxY - minY + pad * 2,
  };
}

function clusteredLayout(data: GraphData): { nodes: SimNode[]; links: SimLink[] } {
  const byKind = new Map<string, SimNode[]>();
  const nodes: SimNode[] = data.nodes.map((n) => {
    const node = {
      id: n.id,
      title: n.title,
      kind: n.kind,
      color: n.color,
      size: n.size,
      degree: n.degree,
      x: 0,
      y: 0,
    };
    const arr = byKind.get(n.kind) ?? [];
    arr.push(node);
    byKind.set(n.kind, arr);
    return node;
  });

  for (const [kind, arr] of byKind) {
    const c = CLUSTER_CENTERS[kind] ?? [0.5, 0.5];
    const cx = c[0] * WORLD_W;
    const cy = c[1] * WORLD_H;
    const sorted = arr.sort((a, b) => {
      if (b.degree !== a.degree) return b.degree - a.degree;
      return a.title.localeCompare(b.title);
    });
    const maxRadius = kind === 'Source' || kind === 'Entity' ? 520 : 360;
    const step = Math.max(11, maxRadius / Math.sqrt(Math.max(1, sorted.length)));
    const phase = (hash(kind) % 628) / 100;
    sorted.forEach((node, i) => {
      if (i === 0) {
        node.x = cx;
        node.y = cy;
        return;
      }
      const radius = Math.min(maxRadius, step * Math.sqrt(i));
      const angle = phase + i * GOLDEN_ANGLE;
      const jitter = ((hash(node.id) % 100) - 50) / 50;
      node.x = cx + Math.cos(angle) * (radius + jitter * 4);
      node.y = cy + Math.sin(angle) * (radius + jitter * 4);
    });
  }

  const nodeById = new Map(nodes.map((n) => [n.id, n]));
  const links: SimLink[] = [];
  for (const e of data.edges) {
    const source = nodeById.get(e.from_article);
    const target = nodeById.get(e.to_article);
    if (source && target) links.push({ source, target, weight: e.weight });
  }
  return { nodes, links };
}

function ellipsizeLabel(ctx: CanvasRenderingContext2D, text: string, maxWidth: number): string {
  if (ctx.measureText(text).width <= maxWidth) return text;
  let lo = 0;
  let hi = text.length;
  while (lo < hi) {
    const mid = Math.ceil((lo + hi) / 2);
    if (ctx.measureText(`${text.slice(0, mid)}...`).width <= maxWidth) lo = mid;
    else hi = mid - 1;
  }
  return `${text.slice(0, Math.max(1, lo))}...`;
}

function intersectsAny(
  rect: { x: number; y: number; w: number; h: number },
  rects: Array<{ x: number; y: number; w: number; h: number }>,
): boolean {
  return rects.some(
    (r) =>
      rect.x < r.x + r.w &&
      rect.x + rect.w > r.x &&
      rect.y < r.y + r.h &&
      rect.y + rect.h > r.y,
  );
}

function roundedRectPath(
  ctx: CanvasRenderingContext2D,
  x: number,
  y: number,
  w: number,
  h: number,
  r: number,
) {
  if (typeof ctx.roundRect === 'function') {
    ctx.roundRect(x, y, w, h, r);
  } else {
    ctx.rect(x, y, w, h);
  }
}

// Resolve a CSS custom property to its computed string. Falls back if the doc
// hasn't booted yet (SSR / very-first-paint guards).
function readCssVar(name: string, fallback: string): string {
  if (typeof window === 'undefined') return fallback;
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return v || fallback;
}

// Resolve a `var(--name)` reference to its computed value. The graph API ships
// node colours as `"var(--kind-concept)"` etc. — fine for SVG/CSS, but the
// Canvas 2D context silently rejects unresolved var() strings (it keeps the
// previous fillStyle/strokeStyle), which is what made the canvas version look
// washed-out grey in the first cut. Plain colour values pass through.
function resolveCssColor(value: string): string {
  if (value.startsWith('var(') && value.endsWith(')')) {
    const name = value.slice(4, -1).trim();
    const resolved = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return resolved || value;
  }
  return value;
}

export function GraphView({ onNavigate }: Props) {
  const [data, setData] = useState<GraphData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const mountTsRef = useRef(typeof performance !== 'undefined' ? performance.now() : 0);

  const containerRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const minimapCanvasRef = useRef<HTMLCanvasElement>(null);
  const [viewport, setViewport] = useState({ w: 800, h: 600 });

  const [zoom, setZoom] = useState(1);
  const [pan, setPan] = useState({ x: 0, y: 0 });

  const [hoverNode, setHoverNode] = useState<string | null>(null);
  const [hoverEdge, setHoverEdge] = useState<number | null>(null);
  const [hiddenKinds, setHiddenKinds] = useState<Set<string>>(new Set());
  const [search, setSearch] = useState('');

  const nodesRef = useRef<SimNode[]>([]);
  const rankedNodesRef = useRef<SimNode[]>([]);
  const linksRef = useRef<SimLink[]>([]);
  const layoutBoundsRef = useRef<LayoutBounds>({ x: 0, y: 0, w: WORLD_W, h: WORLD_H });
  const minimapCacheRef = useRef<{ key: string; canvas: HTMLCanvasElement } | null>(null);
  const [prewarmed, setPrewarmed] = useState(false);
  const [layoutReady, setLayoutReady] = useState(false);

  const rafRef = useRef<number | null>(null);

  useEffect(() => {
    const cached = readCachedGraph();
    const usedCache = cached !== null;
    if (cached) {
      const perf = graphPerf();
      if (perf) {
        perf.cacheHit = true;
        perf.cacheAgeMs = cached.ageMs;
        perf.nodes = cached.data.nodes.length;
        perf.edges = cached.data.edges.length;
      }
      setData(cached.data);
    }

    const start = performance.now();
    api.graph()
      .then((graph) => {
        writeCachedGraph(graph);
        const perf = graphPerf();
        if (perf) {
          perf.fetchMs = performance.now() - start;
          perf.nodes = graph.nodes.length;
          perf.edges = graph.edges.length;
        }
        setData((current) => {
          if (usedCache && current) return current;
          return graph;
        });
      })
      .catch((e) => setError(String(e)));
  }, []);

  // Compute a deterministic clustered layout synchronously. The old d3-force
  // pre-warm loop did produce attractive organic clusters, but it also blocked
  // the main thread for more than a second on the current 3k-node graph. This
  // keeps first paint predictable while preserving the user's expected clusters.
  useLayoutEffect(() => {
    if (!data) return;
    const start = performance.now();
    const { nodes, links } = clusteredLayout(data);
    const layoutMs = performance.now() - start;
    const perf = graphPerf();
    if (perf) perf.layoutMs = layoutMs;

    nodesRef.current = nodes;
    rankedNodesRef.current = [...nodes].sort((a, b) => b.degree - a.degree);
    linksRef.current = links;
    layoutBoundsRef.current = layoutBounds(nodes);
    minimapCacheRef.current = null;
    setPrewarmed(true);
    return () => {
      nodesRef.current = [];
      rankedNodesRef.current = [];
      linksRef.current = [];
      minimapCacheRef.current = null;
      setPrewarmed(false);
      setLayoutReady(false);
    };
  }, [data]);

  // Re-runs when the canvas first mounts (after data arrives) so we measure
  // against the real DOM synchronously, before the first paint of the graph.
  useLayoutEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const measure = () => {
      const r = el.getBoundingClientRect();
      setViewport((prev) => {
        const next = { w: r.width, h: r.height };
        if (Math.round(prev.w) === Math.round(next.w) && Math.round(prev.h) === Math.round(next.h)) {
          return prev;
        }
        return next;
      });
    };
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, [data]);

  const fitToViewport = useCallback(() => {
    const nodes = nodesRef.current;
    if (!nodes.length || !viewport.w || !viewport.h) return;
    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    for (const n of nodes) {
      if (hiddenKinds.has(n.kind)) continue;
      const x = n.x ?? 0, y = n.y ?? 0, r = n.size / 2;
      if (x - r < minX) minX = x - r;
      if (y - r < minY) minY = y - r;
      if (x + r > maxX) maxX = x + r;
      if (y + r > maxY) maxY = y + r;
    }
    if (!isFinite(minX)) return;
    const pad = 48;
    const w = maxX - minX + pad * 2;
    const h = maxY - minY + pad * 2;
    const z = clamp(Math.min(viewport.w / w, viewport.h / h), MIN_ZOOM, 1.5);
    const cx = (minX + maxX) / 2;
    const cy = (minY + maxY) / 2;
    const nextPan = { x: viewport.w / 2 - cx * z, y: viewport.h / 2 - cy * z };
    const perf = graphPerf();
    if (perf) {
      perf.fitZoom = z;
      perf.fitPan = nextPan;
      perf.fitViewport = { w: viewport.w, h: viewport.h };
      perf.fitBounds = { x: minX, y: minY, w, h };
    }
    setZoom(z);
    setPan(nextPan);
  }, [viewport.w, viewport.h, hiddenKinds]);

  // One-shot synchronous fit after pre-warm + viewport measurement. No setTimeout,
  // no observable "snap" — fit lands in the same paint as the first node render.
  // Window resizes afterwards do NOT auto-refit (would yank the user's view).
  const firstFitRef = useRef(false);
  useLayoutEffect(() => {
    if (firstFitRef.current) return;
    if (!prewarmed || !nodesRef.current.length || !canFitViewport(viewport)) return;
    firstFitRef.current = true;
    fitToViewport();
    const perf = graphPerf();
    if (perf) perf.readyMs = performance.now() - mountTsRef.current;
    setLayoutReady(true);
  }, [prewarmed, viewport.w, viewport.h, fitToViewport]);

  // ── Canvas rendering ─────────────────────────────────────────────────────
  // The main graph layer (nodes + edges) is drawn imperatively to a <canvas>
  // rather than rendered as React-managed SVG. Reconciling thousands of SVG
  // primitives on every simulation tick or hover change was the dominant cost
  // (526 nodes + ~2.3k edges = ~5k DOM elements each tick). Canvas redraws on
  // demand via requestAnimationFrame so a tick + hover + pan in the same frame
  // coalesce into a single paint.

  // drawStateRef captures the latest visual inputs for the draw function. The
  // draw function is invoked from the simulation tick handler (outside React)
  // and from React effects, so it must read up-to-date values from a ref.
  const drawStateRef = useRef({
    pan,
    zoom,
    hoverNode,
    searchLC: '',
    hiddenKinds,
    layoutReady,
    viewport,
  });

  const draw = useCallback(() => {
    const drawStart = performance.now();
    const finishDraw = () => {
      const perf = graphPerf();
      if (!perf) return;
      const drawMs = performance.now() - drawStart;
      perf.lastDrawMs = drawMs;
      perf.maxDrawMs = Math.max(perf.maxDrawMs ?? 0, drawMs);
      const draws = perf.draws ?? [];
      draws.push(drawMs);
      if (draws.length > 120) draws.shift();
      perf.draws = draws;
    };
    const canvas = canvasRef.current;
    if (!canvas) return;
    const { pan, zoom, hoverNode, searchLC, hiddenKinds, layoutReady, viewport } =
      drawStateRef.current;
    const dpr = window.devicePixelRatio || 1;
    const cw = Math.max(1, Math.round(viewport.w * dpr));
    const ch = Math.max(1, Math.round(viewport.h * dpr));
    if (canvas.width !== cw) canvas.width = cw;
    if (canvas.height !== ch) canvas.height = ch;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, viewport.w, viewport.h);

    if (!layoutReady) {
      finishDraw();
      return;
    }

    const nodes = nodesRef.current;
    const links = linksRef.current;
    // Re-read CSS vars each frame so theme changes (light↔dark) are picked up
    // automatically. getComputedStyle on documentElement is ~µs.
    const lineColor = resolveCssColor(readCssVar('--line', '#999'));
    const accentColor = resolveCssColor(readCssVar('--accent', '#0066ff'));
    const bgCardColor = resolveCssColor(readCssVar('--bg-card', '#fff'));
    const bgElevColor = resolveCssColor(readCssVar('--bg-elev', '#fff'));
    const fgColor = resolveCssColor(readCssVar('--fg', '#111'));
    const fgMuteColor = resolveCssColor(readCssVar('--fg-mute', '#666'));
    const lineSoftColor = resolveCssColor(readCssVar('--line-soft', '#ddd'));
    const monoFont = readCssVar('--mono', 'ui-monospace, SFMono-Regular, Menlo, monospace');
    // Node `color` arrives from the API as `"var(--kind-X)"` — Canvas 2D
    // doesn't resolve var(), so resolve to the underlying oklch value once per
    // distinct kind per frame.
    const kindColorCache: Record<string, string> = {};
    const kindColor = (raw: string): string => {
      const cached = kindColorCache[raw];
      if (cached) return cached;
      const resolved = resolveCssColor(raw);
      kindColorCache[raw] = resolved;
      return resolved;
    };
    const hasFocus = !!hoverNode || !!searchLC;

    const isVisible = (n: SimNode, pad = 80): boolean => {
      const sx = (n.x ?? 0) * zoom + pan.x;
      const sy = (n.y ?? 0) * zoom + pan.y;
      return sx >= -pad && sx <= viewport.w + pad && sy >= -pad && sy <= viewport.h + pad;
    };

    const drawLabel = (
      node: SimNode,
      active: boolean,
      matches: boolean,
      occupied: Array<{ x: number; y: number; w: number; h: number }>,
    ): boolean => {
      const sx = (node.x ?? 0) * zoom + pan.x;
      const sy = (node.y ?? 0) * zoom + pan.y + (node.size / 2) * zoom + 6;
      if (sx < -80 || sx > viewport.w + 80 || sy < -30 || sy > viewport.h + 80) return false;
      ctx.font = `10px ${monoFont}`;
      ctx.textBaseline = 'top';
      const text = ellipsizeLabel(ctx, node.title, active || matches ? 260 : 210);
      const metrics = ctx.measureText(text);
      const tw = metrics.width;
      const th = 14;
      const rect = { x: sx - tw / 2 - 6, y: sy - 2, w: tw + 12, h: th + 4 };
      if (!active && intersectsAny(rect, occupied)) return false;
      occupied.push(rect);

      if (active || matches) {
        ctx.fillStyle = matches ? accentColor : bgElevColor;
        ctx.globalAlpha = matches ? 0.14 : 0.96;
        ctx.beginPath();
        roundedRectPath(ctx, rect.x, rect.y, rect.w, rect.h, 4);
        ctx.fill();
        ctx.globalAlpha = 1;
        ctx.strokeStyle = matches ? accentColor : lineSoftColor;
        ctx.lineWidth = 1;
        ctx.stroke();
      }
      ctx.fillStyle = active || matches ? fgColor : fgMuteColor;
      ctx.fillText(text, sx - tw / 2, sy);
      return true;
    };

    const drawLabels = () => {
      const occupied: Array<{ x: number; y: number; w: number; h: number }> = [];
      const budget = searchLC ? MAX_CLOSE_LABELS : zoom < 0.35 ? MAX_FIT_LABELS : zoom < 0.75 ? MAX_MID_LABELS : MAX_CLOSE_LABELS;
      let drawn = 0;

      if (hoverNode) {
        const activeNode = nodes.find((n) => n.id === hoverNode);
        if (activeNode && !hiddenKinds.has(activeNode.kind) && drawLabel(activeNode, true, false, occupied)) {
          drawn += 1;
        }
      }

      for (const n of rankedNodesRef.current) {
        if (drawn >= budget) break;
        if (hiddenKinds.has(n.kind) || !isVisible(n)) continue;
        const matches = !!searchLC && n.title.toLowerCase().includes(searchLC);
        if (!matches && n.id === hoverNode) continue;
        const importantAtFit = zoom < 0.35 && n.degree >= 28;
        const importantMid = zoom >= 0.35 && zoom < 0.75 && n.degree >= 16;
        const importantClose = zoom >= 0.75 && (n.degree >= 8 || n.size * zoom >= 18);
        if (!(matches || importantAtFit || importantMid || importantClose)) continue;
        if (drawLabel(n, false, matches, occupied)) drawn += 1;
      }
    };

    const drawMinimap = () => {
      const mini = minimapCanvasRef.current;
      if (!mini || !layoutReady) return;
      const mdpr = window.devicePixelRatio || 1;
      const mw = Math.round(MINI_W * mdpr);
      const mh = Math.round(MINI_H * mdpr);
      if (mini.width !== mw) mini.width = mw;
      if (mini.height !== mh) mini.height = mh;

      const mctx = mini.getContext('2d');
      if (!mctx) return;
      const bounds = layoutBoundsRef.current;
      const scale = Math.min(MINI_W / bounds.w, MINI_H / bounds.h);
      const ox = (MINI_W - bounds.w * scale) / 2 - bounds.x * scale;
      const oy = (MINI_H - bounds.h * scale) / 2 - bounds.y * scale;
      const hiddenKey = [...hiddenKinds].sort().join('|');
      const cacheKey = `${mdpr}:${hiddenKey}:${lineColor}:${accentColor}:${bgCardColor}`;
      let cache = minimapCacheRef.current;
      if (!cache || cache.key !== cacheKey) {
        const off = document.createElement('canvas');
        off.width = mw;
        off.height = mh;
        const offCtx = off.getContext('2d');
        if (offCtx) {
          offCtx.setTransform(mdpr, 0, 0, mdpr, 0, 0);
          offCtx.clearRect(0, 0, MINI_W, MINI_H);
          offCtx.strokeStyle = lineColor;
          offCtx.globalAlpha = 0.42;
          offCtx.lineWidth = 0.5;
          offCtx.beginPath();
          for (const e of links) {
            const a = e.source as SimNode;
            const b = e.target as SimNode;
            if (!a || !b || typeof a !== 'object' || typeof b !== 'object') continue;
            if (hiddenKinds.has(a.kind) || hiddenKinds.has(b.kind)) continue;
            offCtx.moveTo((a.x ?? 0) * scale + ox, (a.y ?? 0) * scale + oy);
            offCtx.lineTo((b.x ?? 0) * scale + ox, (b.y ?? 0) * scale + oy);
          }
          offCtx.stroke();
          offCtx.globalAlpha = 0.9;
          for (const n of nodes) {
            if (hiddenKinds.has(n.kind)) continue;
            offCtx.fillStyle = kindColor(n.color);
            offCtx.beginPath();
            offCtx.arc((n.x ?? 0) * scale + ox, (n.y ?? 0) * scale + oy, Math.max(1.2, n.size * scale / 2), 0, Math.PI * 2);
            offCtx.fill();
          }
          offCtx.globalAlpha = 1;
        }
        cache = { key: cacheKey, canvas: off };
        minimapCacheRef.current = cache;
      }

      mctx.setTransform(1, 0, 0, 1, 0, 0);
      mctx.clearRect(0, 0, mw, mh);
      mctx.drawImage(cache.canvas, 0, 0);
      mctx.setTransform(mdpr, 0, 0, mdpr, 0, 0);

      const x1 = (-pan.x) / zoom;
      const y1 = (-pan.y) / zoom;
      const w = viewport.w / zoom;
      const h = viewport.h / zoom;
      const rx = x1 * scale + ox;
      const ry = y1 * scale + oy;
      const rw = w * scale;
      const rh = h * scale;
      mctx.fillStyle = accentColor;
      mctx.globalAlpha = 0.12;
      mctx.fillRect(rx, ry, rw, rh);
      mctx.globalAlpha = 1;
      mctx.strokeStyle = accentColor;
      mctx.lineWidth = 1;
      mctx.strokeRect(rx, ry, rw, rh);
    };

    // Compute neighbour set once per frame for the hover incident check.
    let neighbors: Set<string> | null = null;
    if (hoverNode) {
      neighbors = new Set();
      for (const e of links) {
        const a = e.source as SimNode;
        const b = e.target as SimNode;
        if (!a || !b || typeof a !== 'object' || typeof b !== 'object') continue;
        if (a.id === hoverNode) neighbors.add(b.id);
        else if (b.id === hoverNode) neighbors.add(a.id);
      }
    }

    ctx.save();
    ctx.translate(pan.x, pan.y);
    ctx.scale(zoom, zoom);

    // ── Edges ──
    // Pass 1: non-incident edges. Stroke width and opacity scale with weight
    // so the topology is more legible than a flat line wash.
    ctx.lineCap = 'round';
    ctx.strokeStyle = lineColor;
    for (const e of links) {
      const a = e.source as SimNode;
      const b = e.target as SimNode;
      if (!a || !b || typeof a !== 'object' || typeof b !== 'object') continue;
      if (hiddenKinds.has(a.kind) || hiddenKinds.has(b.kind)) continue;
      const incident = hoverNode != null && (a.id === hoverNode || b.id === hoverNode);
      if (incident) continue;
      ctx.globalAlpha = hasFocus ? 0.05 : 0.18 + 0.45 * e.weight;
      ctx.lineWidth = (0.6 + 0.7 * e.weight) / zoom;
      ctx.beginPath();
      ctx.moveTo(a.x ?? 0, a.y ?? 0);
      ctx.lineTo(b.x ?? 0, b.y ?? 0);
      ctx.stroke();
    }
    // Pass 2: incident edges drawn on top in the accent colour with extra
    // weight, so the hovered node's neighbourhood reads as a connected sub-graph.
    if (hoverNode) {
      ctx.strokeStyle = accentColor;
      for (const e of links) {
        const a = e.source as SimNode;
        const b = e.target as SimNode;
        if (!a || !b || typeof a !== 'object' || typeof b !== 'object') continue;
        if (hiddenKinds.has(a.kind) || hiddenKinds.has(b.kind)) continue;
        if (a.id !== hoverNode && b.id !== hoverNode) continue;
        ctx.globalAlpha = 0.55 + 0.35 * e.weight;
        ctx.lineWidth = (1.3 + 0.6 * e.weight) / zoom;
        ctx.beginPath();
        ctx.moveTo(a.x ?? 0, a.y ?? 0);
        ctx.lineTo(b.x ?? 0, b.y ?? 0);
        ctx.stroke();
      }
    }
    ctx.globalAlpha = 1;

    // ── Nodes ──
    // Soft halo behind the hovered node — gives the focused element clear
    // visual primacy without animating, which keeps the cost flat.
    if (hoverNode) {
      const hn = nodes.find((x) => x.id === hoverNode);
      if (hn) {
        const c = kindColor(hn.color);
        ctx.beginPath();
        ctx.arc(hn.x ?? 0, hn.y ?? 0, hn.size / 2 + 10, 0, Math.PI * 2);
        ctx.fillStyle = c;
        ctx.globalAlpha = 0.18;
        ctx.fill();
        ctx.globalAlpha = 1;
      }
    }

    for (const n of nodes) {
      if (hiddenKinds.has(n.kind)) continue;
      let dim = false;
      if (hoverNode) dim = !(n.id === hoverNode || (neighbors?.has(n.id) ?? false));
      else if (searchLC) dim = !n.title.toLowerCase().includes(searchLC);
      const active = hoverNode === n.id;
      const r = n.size / 2;
      const c = kindColor(n.color);
      ctx.beginPath();
      ctx.arc(n.x ?? 0, n.y ?? 0, r, 0, Math.PI * 2);
      ctx.fillStyle = n.size >= 22 ? c : bgCardColor;
      ctx.globalAlpha = dim ? 0.2 : 1;
      ctx.fill();
      ctx.strokeStyle = c;
      ctx.lineWidth = (active ? 2.6 : 1.4) / zoom;
      ctx.stroke();
    }
    ctx.globalAlpha = 1;

    ctx.restore();
    drawLabels();
    drawMinimap();
    finishDraw();
  }, []);

  const scheduleDraw = useCallback(() => {
    if (rafRef.current !== null) return;
    rafRef.current = requestAnimationFrame(() => {
      rafRef.current = null;
      draw();
    });
  }, [draw]);

  // Keep drawStateRef in sync with React state and request a redraw. Runs every
  // render — the body is a few field assignments + an rAF, which dedupes when
  // multiple state updates land in the same tick.
  useLayoutEffect(() => {
    drawStateRef.current = {
      pan,
      zoom,
      hoverNode,
      searchLC: search.trim().toLowerCase(),
      hiddenKinds,
      layoutReady,
      viewport,
    };
    scheduleDraw();
  });

  useEffect(() => () => {
    if (rafRef.current !== null) cancelAnimationFrame(rafRef.current);
  }, []);

  const zoomAt = useCallback(
    (factor: number, cx: number, cy: number) => {
      setZoom((z) => {
        const nz = clamp(z * factor, MIN_ZOOM, MAX_ZOOM);
        setPan((p) => ({ x: cx - (cx - p.x) * (nz / z), y: cy - (cy - p.y) * (nz / z) }));
        return nz;
      });
    },
    [],
  );

  const handleWheel = (e: React.WheelEvent) => {
    if (!containerRef.current) return;
    const r = containerRef.current.getBoundingClientRect();
    const cx = e.clientX - r.left;
    const cy = e.clientY - r.top;
    zoomAt(Math.exp(-e.deltaY * 0.0015), cx, cy);
  };

  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
    };
    el.addEventListener('wheel', onWheel, { passive: false });
    return () => el.removeEventListener('wheel', onWheel);
  }, []);

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      const tag = (e.target as HTMLElement | null)?.tagName;
      if (tag === 'INPUT' || tag === 'TEXTAREA') return;
      if (e.key === '+' || e.key === '=') {
        zoomAt(1.25, viewport.w / 2, viewport.h / 2);
        e.preventDefault();
      } else if (e.key === '-' || e.key === '_') {
        zoomAt(0.8, viewport.w / 2, viewport.h / 2);
        e.preventDefault();
      } else if (e.key === '0') {
        fitToViewport();
        e.preventDefault();
      } else if (e.key === 'Escape') {
        setSearch('');
        setHoverNode(null);
      }
    }
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [zoomAt, fitToViewport, viewport.w, viewport.h]);

  const dragRef = useRef<
    | null
    | {
        mode: 'pan' | 'node-pending' | 'node' | 'minimap';
        sx: number;
        sy: number;
        origPan: { x: number; y: number };
        nodeId?: string;
        nodeStart?: { x: number; y: number };
      }
  >(null);

  // Convert a (clientX, clientY) relative to the canvas wrapper into world
  // coordinates by inverting the current pan + zoom transform.
  const clientToWorld = (clientX: number, clientY: number) => {
    const el = containerRef.current;
    if (!el) return { x: 0, y: 0 };
    const r = el.getBoundingClientRect();
    return { x: (clientX - r.left - pan.x) / zoom, y: (clientY - r.top - pan.y) / zoom };
  };

  const hitTestNode = (wx: number, wy: number): SimNode | null => {
    const nodes = nodesRef.current;
    // Scan in reverse so visually-on-top nodes (last drawn) win ties.
    for (let i = nodes.length - 1; i >= 0; i--) {
      const n = nodes[i];
      if (hiddenKinds.has(n.kind)) continue;
      const dx = (n.x ?? 0) - wx;
      const dy = (n.y ?? 0) - wy;
      const r = n.size / 2;
      if (dx * dx + dy * dy <= r * r) return n;
    }
    return null;
  };

  const hitTestEdge = (wx: number, wy: number): number | null => {
    const links = linksRef.current;
    const tol = EDGE_HIT_TOLERANCE / zoom;
    const tol2 = tol * tol;
    let bestI = -1;
    let bestD2 = Infinity;
    for (let i = 0; i < links.length; i++) {
      const e = links[i];
      const a = e.source as SimNode;
      const b = e.target as SimNode;
      if (!a || !b || typeof a !== 'object' || typeof b !== 'object') continue;
      if (hiddenKinds.has(a.kind) || hiddenKinds.has(b.kind)) continue;
      const ax = a.x ?? 0, ay = a.y ?? 0;
      const bx = b.x ?? 0, by = b.y ?? 0;
      const ex = bx - ax;
      const ey = by - ay;
      const len2 = ex * ex + ey * ey;
      if (len2 === 0) continue;
      const t = clamp(((wx - ax) * ex + (wy - ay) * ey) / len2, 0, 1);
      const px = ax + t * ex;
      const py = ay + t * ey;
      const ddx = wx - px;
      const ddy = wy - py;
      const d2 = ddx * ddx + ddy * ddy;
      if (d2 < tol2 && d2 < bestD2) {
        bestD2 = d2;
        bestI = i;
      }
    }
    return bestI >= 0 ? bestI : null;
  };

  const onCanvasPointerDown = (e: React.PointerEvent) => {
    if (e.button !== 0) return;
    const w = clientToWorld(e.clientX, e.clientY);
    const node = hitTestNode(w.x, w.y);
    if (node) {
      dragRef.current = {
        mode: 'node-pending',
        sx: e.clientX,
        sy: e.clientY,
        origPan: { ...pan },
        nodeId: node.id,
        nodeStart: { x: node.x ?? 0, y: node.y ?? 0 },
      };
    } else {
      dragRef.current = {
        mode: 'pan',
        sx: e.clientX,
        sy: e.clientY,
        origPan: { ...pan },
      };
    }
    (e.currentTarget as Element).setPointerCapture?.(e.pointerId);
  };

  const onPointerMove = (e: React.PointerEvent) => {
    const d = dragRef.current;
    if (d) {
      const dx = e.clientX - d.sx;
      const dy = e.clientY - d.sy;
      if (d.mode === 'node-pending' && Math.hypot(dx, dy) > 4 && d.nodeId) {
        d.mode = 'node';
      }
      if (d.mode === 'pan') {
        setPan({ x: d.origPan.x + dx, y: d.origPan.y + dy });
      } else if (d.mode === 'node' && d.nodeId && d.nodeStart) {
        const n = nodesRef.current.find((x) => x.id === d.nodeId);
        if (n) {
          n.x = d.nodeStart.x + dx / zoom;
          n.y = d.nodeStart.y + dy / zoom;
          layoutBoundsRef.current = layoutBounds(nodesRef.current);
          minimapCacheRef.current = null;
          scheduleDraw();
        }
      } else if (d.mode === 'minimap') {
        panToMinimap(e);
      }
      return;
    }

    // No active drag → update hover.
    const w = clientToWorld(e.clientX, e.clientY);
    const node = hitTestNode(w.x, w.y);
    if (node) {
      if (hoverNode !== node.id) setHoverNode(node.id);
      if (hoverEdge !== null) setHoverEdge(null);
      return;
    }
    if (hoverNode !== null) setHoverNode(null);
    if (zoom < EDGE_TOOLTIP_MIN_ZOOM) {
      if (hoverEdge !== null) setHoverEdge(null);
      return;
    }
    const edgeI = hitTestEdge(w.x, w.y);
    if (edgeI !== hoverEdge) setHoverEdge(edgeI);
  };

  const onPointerUp = (e: React.PointerEvent) => {
    const d = dragRef.current;
    if (!d) return;
    dragRef.current = null;
    if (d.mode === 'node-pending' && d.nodeId) {
      onNavigate('article', d.nodeId);
    } else if (d.mode === 'node' && d.nodeId) {
      minimapCacheRef.current = null;
      scheduleDraw();
    }
    (e.currentTarget as Element).releasePointerCapture?.(e.pointerId);
  };

  const onPointerLeave = () => {
    if (hoverNode !== null) setHoverNode(null);
    if (hoverEdge !== null) setHoverEdge(null);
  };

  const miniTransform = () => {
    const bounds = layoutBoundsRef.current;
    const scale = Math.min(MINI_W / bounds.w, MINI_H / bounds.h);
    return {
      scale,
      ox: (MINI_W - bounds.w * scale) / 2 - bounds.x * scale,
      oy: (MINI_H - bounds.h * scale) / 2 - bounds.y * scale,
    };
  };

  function panToMinimap(e: React.PointerEvent) {
    const tgt = e.currentTarget as Element;
    const r = tgt.getBoundingClientRect();
    const mx = e.clientX - r.left;
    const my = e.clientY - r.top;
    const { scale, ox, oy } = miniTransform();
    const worldX = (mx - ox) / scale;
    const worldY = (my - oy) / scale;
    setPan({ x: viewport.w / 2 - worldX * zoom, y: viewport.h / 2 - worldY * zoom });
  }

  const onMinimapPointerDown = (e: React.PointerEvent) => {
    if (e.button !== 0) return;
    e.stopPropagation();
    dragRef.current = {
      mode: 'minimap',
      sx: e.clientX,
      sy: e.clientY,
      origPan: { ...pan },
    };
    (e.currentTarget as Element).setPointerCapture?.(e.pointerId);
    panToMinimap(e);
  };

  const toggleKind = (k: string) => {
    setHiddenKinds((prev) => {
      const next = new Set(prev);
      if (next.has(k)) next.delete(k);
      else next.add(k);
      return next;
    });
  };

  if (error) {
    return (
      <div className="view">
        <div className="view-h">Graph</div>
        <p className="view-sub">Error: {error}</p>
      </div>
    );
  }
  if (!data) {
    return (
      <div className="view">
        <div className="view-h">Graph</div>
        <p className="view-sub">loading…</p>
      </div>
    );
  }
  if (data.stats.pages === 0) {
    return (
      <div className="view">
        <div className="view-h">System · Graph</div>
        <h1 className="view-title">Nothing to graph yet.</h1>
        <p className="view-sub">
          The knowledge graph is built from articles your agents compile. Add a feed, queue a
          video, or load the demo to see it populate. Each node is a page, each edge is a
          wiki-link between pages; clusters group by kind (Concept · Entity · Source · Synthesis).
        </p>
      </div>
    );
  }

  const links = linksRef.current;
  const hoverEdgeData = hoverEdge != null ? links[hoverEdge] : null;

  return (
    <div>
      <div className="graph-header">
        <div>
          <div className="view-h">System · Graph</div>
          <div className="graph-title">
            {data.stats.pages.toLocaleString()} {data.stats.pages === 1 ? 'page' : 'pages'} ·{' '}
            {data.stats.connections.toLocaleString()}{' '}
            {data.stats.connections === 1 ? 'connection' : 'connections'}
          </div>
        </div>
        <div className="graph-toolbar">
          <input
            className="graph-search"
            type="search"
            placeholder="Search pages…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
          <div className="graph-chips">
            {data.clusters
              .filter((c) => c.count > 0)
              .map((c) => {
                const off = hiddenKinds.has(c.kind);
                return (
                  <button
                    key={c.kind}
                    type="button"
                    className={`graph-chip${off ? ' is-off' : ''}`}
                    onClick={() => toggleKind(c.kind)}
                    aria-pressed={!off}
                    title={`${off ? 'Show' : 'Hide'} ${c.kind}`}
                  >
                    <span className="graph-chip-dot" style={{ background: c.color }} />
                    {c.kind} · {c.count}
                  </button>
                );
              })}
          </div>
        </div>
      </div>

      <div
        ref={containerRef}
        className={`graph-canvas${layoutReady ? ' is-ready' : ''}`}
        onWheel={handleWheel}
        onPointerDown={onCanvasPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerCancel={onPointerUp}
        onPointerLeave={onPointerLeave}
        style={{ cursor: dragRef.current?.mode === 'pan' ? 'grabbing' : 'grab' }}
      >
        <canvas
          ref={canvasRef}
          className="graph-svg"
          style={{ width: '100%', height: '100%' }}
        />

        {hoverEdgeData &&
          (() => {
            const a = hoverEdgeData.source as SimNode;
            const b = hoverEdgeData.target as SimNode;
            if (!a || !b || typeof a !== 'object' || typeof b !== 'object') return null;
            const mx = ((a.x ?? 0) + (b.x ?? 0)) / 2 * zoom + pan.x;
            const my = ((a.y ?? 0) + (b.y ?? 0)) / 2 * zoom + pan.y;
            return (
              <div className="graph-edge-tip" style={{ left: mx, top: my }}>
                {a.title} ↔ {b.title}
                <span className="graph-edge-tip-w">· weight {hoverEdgeData.weight.toFixed(2)}</span>
              </div>
            );
          })()}

        <div className="graph-controls" onPointerDown={(e) => e.stopPropagation()}>
          <button
            type="button"
            className="graph-ctrl"
            onClick={() => zoomAt(1.25, viewport.w / 2, viewport.h / 2)}
            title="Zoom in (+)"
            aria-label="Zoom in"
          >
            +
          </button>
          <button
            type="button"
            className="graph-ctrl"
            onClick={() => zoomAt(0.8, viewport.w / 2, viewport.h / 2)}
            title="Zoom out (−)"
            aria-label="Zoom out"
          >
            −
          </button>
          <button
            type="button"
            className="graph-ctrl"
            onClick={() => fitToViewport()}
            title="Fit (0)"
            aria-label="Fit to view"
          >
            ⌂
          </button>
          <div className="graph-zoom-pct">{Math.round(zoom * 100)}%</div>
        </div>

        <canvas
          ref={minimapCanvasRef}
          className="graph-minimap"
          width={MINI_W}
          height={MINI_H}
          style={{ width: MINI_W, height: MINI_H }}
          onPointerDown={onMinimapPointerDown}
          onPointerMove={onPointerMove}
          onPointerUp={onPointerUp}
        />

        <div className="graph-hint" aria-hidden>
          scroll = zoom · drag bg = pan · drag node = move · + − 0 = zoom/fit
        </div>
      </div>
    </div>
  );
}
