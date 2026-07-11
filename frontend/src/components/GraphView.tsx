import { useEffect, useLayoutEffect, useRef, useState, useCallback } from 'react';
import { quadtree, type Quadtree, type QuadtreeLeaf } from 'd3-quadtree';
import { api } from '../api';
import type { GraphData, View } from '../types';

interface Props {
  onNavigate: (view: View, id?: string) => void;
}

interface SimNode {
  id: string;
  title: string;
  titleLC: string;
  kind: string;
  color: string;
  size: number;
  degree: number;
  index: number;
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
const GRAPH_CACHE_KEY = 'mastisk:graph:v1';
const GRAPH_CACHE_MAX_AGE_MS = 10 * 60 * 1000;
const ORGANIC_RELAX_TICKS = 16;

// ── Motion time-constants (ms). Eased with 1 - exp(-dt/τ) so motion is
// frame-rate independent and always converges in finite time. ─────────────
const TAU_VIEW = 100;    // wheel/keyboard zoom + pan ease toward target
const TAU_FOCUS = 100;   // hover/search dim fade-in / fade-out
const TAU_POS = 60;      // interpolation between simulation snapshots
const TAU_INERTIA = 90;  // pan-release velocity decay
const VIEW_EPS = 0.0005;
const PAN_EPS = 0.05;
const INERTIA_SPEED_EPS = 0.004; // px/ms below which inertia stops
const EDGE_BUCKETS = 4;

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
  // Simulation + animation instrumentation (the evidence for the perf gate).
  simTickMs?: number;    // last worker-reported force-tick cost
  simAlpha?: number;     // last reported simulation alpha
  snapshotHz?: number;   // measured position-snapshot rate
  fps?: number;          // interpolated display frame rate (EMA)
  workerActive?: boolean;
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

function hashUnit(s: string): number {
  return (hash(s) % 1_000_000) / 1_000_000;
}

function clamp(v: number, lo: number, hi: number) {
  return Math.max(lo, Math.min(hi, v));
}

function prefersReducedMotion(): boolean {
  if (typeof window === 'undefined' || !window.matchMedia) return false;
  return window.matchMedia('(prefers-reduced-motion: reduce)').matches;
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

function organicLayout(data: GraphData): { nodes: SimNode[]; links: SimLink[] } {
  const byKind = new Map<string, SimNode[]>();
  const nodes: SimNode[] = data.nodes.map((n, i) => {
    const node = {
      id: n.id,
      title: n.title,
      titleLC: n.title.toLowerCase(),
      kind: n.kind,
      color: n.color,
      size: n.size,
      degree: n.degree,
      index: i,
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
    const maxRadius =
      kind === 'Source' ? 560 :
      kind === 'Entity' ? 470 :
      kind === 'Synthesis' ? 360 :
      260;
    const count = Math.max(1, sorted.length - 1);
    sorted.forEach((node, i) => {
      const rank = i / count;
      const angle = hashUnit(`${node.id}:angle`) * Math.PI * 2;
      const randomRadius = Math.sqrt(hashUnit(`${node.id}:radius`)) * maxRadius;
      const degreeBias = 0.38 + 0.62 * Math.sqrt(rank);
      const radius = randomRadius * degreeBias;
      const aspect = 0.72 + hashUnit(`${node.id}:aspect`) * 0.24;
      node.x = cx + Math.cos(angle) * radius;
      node.y = cy + Math.sin(angle) * radius * aspect;
    });
  }

  const nodeById = new Map(nodes.map((n) => [n.id, n]));
  const links: SimLink[] = [];
  for (const e of data.edges) {
    const source = nodeById.get(e.from_article);
    const target = nodeById.get(e.to_article);
    if (source && target) links.push({ source, target, weight: e.weight });
  }

  const centerFor = (kind: string): [number, number] => {
    const c = CLUSTER_CENTERS[kind] ?? [0.5, 0.5];
    return [c[0] * WORLD_W, c[1] * WORLD_H];
  };
  for (let tick = 0; tick < ORGANIC_RELAX_TICKS; tick += 1) {
    for (const e of links) {
      const a = e.source;
      const b = e.target;
      const dx = (b.x ?? 0) - (a.x ?? 0);
      const dy = (b.y ?? 0) - (a.y ?? 0);
      const dist = Math.max(1, Math.sqrt(dx * dx + dy * dy));
      const desired = 95 + (a.size + b.size) * 1.4;
      const strength = (0.006 + 0.014 * e.weight) * ((dist - desired) / dist);
      const mx = dx * strength;
      const my = dy * strength;
      a.x += mx;
      a.y += my;
      b.x -= mx;
      b.y -= my;
    }
    for (const n of nodes) {
      const [cx, cy] = centerFor(n.kind);
      n.x += (cx - n.x) * 0.006;
      n.y += (cy - n.y) * 0.006;
    }
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

// Continuous, log-scaled label prominence. Labels ease in as you zoom rather
// than popping at fixed budget thresholds — the budget still caps how many are
// drawn, this only controls their opacity so appearances/disappearances fade.
function labelLayerAlpha(zoom: number): number {
  const t = (Math.log(zoom) - Math.log(0.2)) / (Math.log(1.0) - Math.log(0.2));
  return 0.35 + 0.65 * clamp(t, 0, 1);
}

export function GraphView({ onNavigate }: Props) {
  const [data, setData] = useState<GraphData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const mountTsRef = useRef(typeof performance !== 'undefined' ? performance.now() : 0);

  const containerRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const minimapCanvasRef = useRef<HTMLCanvasElement>(null);
  const [viewport, setViewport] = useState({ w: 800, h: 600 });

  // React state mirrors of the displayed view, used only by React-rendered UI
  // (zoom %, cursor, edge tooltip position). The animation loop owns the true
  // displayed values in viewRef and mirrors here when they change.
  const [zoom, setZoom] = useState(1);
  const [pan, setPan] = useState({ x: 0, y: 0 });

  const [hoverNode, setHoverNode] = useState<string | null>(null);
  const [hoverEdge, setHoverEdge] = useState<number | null>(null);
  const [hiddenKinds, setHiddenKinds] = useState<Set<string>>(new Set());
  const [search, setSearch] = useState('');

  const nodesRef = useRef<SimNode[]>([]);
  const rankedNodesRef = useRef<SimNode[]>([]);
  const linksRef = useRef<SimLink[]>([]);
  // Edge indices grouped into weight buckets so the non-incident edge pass is
  // one beginPath+stroke per bucket instead of per edge (critical at ~32k edges).
  const edgeBucketsRef = useRef<number[][]>([]);
  const layoutBoundsRef = useRef<LayoutBounds>({ x: 0, y: 0, w: WORLD_W, h: WORLD_H });
  const minimapCacheRef = useRef<{ key: string; canvas: HTMLCanvasElement } | null>(null);
  const quadtreeRef = useRef<Quadtree<SimNode> | null>(null);
  const maxNodeRadiusRef = useRef(12);
  const [prewarmed, setPrewarmed] = useState(false);
  const [layoutReady, setLayoutReady] = useState(false);

  // Refs mirroring React state so the imperative draw()/animation loop (which
  // runs outside React) always reads current values without stale closures.
  const hoverNodeRef = useRef<string | null>(null);
  const searchLCRef = useRef('');
  const hiddenKindsRef = useRef<Set<string>>(hiddenKinds);
  const layoutReadyRef = useRef(false);
  const viewportRef = useRef(viewport);
  const reduceMotionRef = useRef(prefersReducedMotion());

  // Displayed view + easing targets + pan inertia. The single source of truth
  // for what's on screen; React zoom/pan state is a mirror of this.
  const viewRef = useRef({
    zoom: 1,
    pan: { x: 0, y: 0 },
    tZoom: 1,
    tPan: { x: 0, y: 0 },
    panVel: { x: 0, y: 0 },
    mode: 'idle' as 'idle' | 'ease' | 'inertia',
  });
  const mirrorRef = useRef({ zoom: 1, pan: { x: 0, y: 0 } });
  const focusRef = useRef(0); // eased 0..1 dim amount for hover/search focus

  // Simulation / interpolation state.
  const workerRef = useRef<Worker | null>(null);
  const targetPosRef = useRef<Float32Array | null>(null); // latest snapshot
  const simActiveRef = useRef(false);
  const posLerpRef = useRef(false);
  const snapTimesRef = useRef<number[]>([]);
  const lastMiniInvalidateRef = useRef(0);
  const qtRebuildTickRef = useRef(0);
  const visibleEdgesRef = useRef<number[]>([]);

  const rafRef = useRef<number | null>(null);
  const lastFrameRef = useRef(0);
  const fpsRef = useRef(0);

  useLayoutEffect(() => {
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

  // Deterministic organic layout, computed synchronously. This is the seed the
  // worker force-simulation refines — never a main-thread force loop (a prior
  // synchronous d3-force prewarm blocked >1s on this graph and was removed).
  useLayoutEffect(() => {
    if (!data) return;
    const start = performance.now();
    const { nodes, links } = organicLayout(data);
    const layoutMs = performance.now() - start;
    const perf = graphPerf();
    if (perf) perf.layoutMs = layoutMs;

    nodesRef.current = nodes;
    rankedNodesRef.current = [...nodes].sort((a, b) => b.degree - a.degree);
    linksRef.current = links;
    maxNodeRadiusRef.current = nodes.reduce((m, n) => Math.max(m, n.size / 2), 8);

    // Static weight buckets — weight is immutable per edge, so bucket once.
    const buckets: number[][] = Array.from({ length: EDGE_BUCKETS }, () => []);
    for (let i = 0; i < links.length; i++) {
      const b = clamp(Math.floor(links[i].weight * EDGE_BUCKETS), 0, EDGE_BUCKETS - 1);
      buckets[b].push(i);
    }
    edgeBucketsRef.current = buckets;

    layoutBoundsRef.current = layoutBounds(nodes);
    minimapCacheRef.current = null;
    rebuildQuadtree();
    setPrewarmed(true);
    return () => {
      nodesRef.current = [];
      rankedNodesRef.current = [];
      linksRef.current = [];
      edgeBucketsRef.current = [];
      quadtreeRef.current = null;
      minimapCacheRef.current = null;
      setPrewarmed(false);
      setLayoutReady(false);
    };
  }, [data]);

  // ── Force-simulation worker ──────────────────────────────────────────────
  // Seeded with the organicLayout positions; streams Float32Array snapshots we
  // interpolate. Degrades gracefully: if the worker can't be constructed the
  // graph stays on the static seed layout and every other feature still works.
  useEffect(() => {
    if (!prewarmed) return;
    const nodes = nodesRef.current;
    const links = linksRef.current;
    if (!nodes.length) return;
    // Reduced motion → no settling animation at all: keep the deterministic
    // static seed (already a good layout) and never spin the worker/rAF.
    if (reduceMotionRef.current) return;

    let worker: Worker;
    try {
      worker = new Worker(new URL('../workers/graphSim.ts', import.meta.url), { type: 'module' });
    } catch (err) {
      // No worker → static layout. Not an error state; the seed is usable.
      console.warn('GraphView: force-sim worker unavailable, using static layout', err);
      return;
    }
    workerRef.current = worker;

    const n = nodes.length;
    const x = new Float32Array(n);
    const y = new Float32Array(n);
    const size = new Float32Array(n);
    const cx = new Float32Array(n);
    const cy = new Float32Array(n);
    for (let i = 0; i < n; i++) {
      const nd = nodes[i];
      x[i] = nd.x;
      y[i] = nd.y;
      size[i] = nd.size;
      const c = CLUSTER_CENTERS[nd.kind] ?? [0.5, 0.5];
      cx[i] = c[0] * WORLD_W;
      cy[i] = c[1] * WORLD_H;
    }
    const m = links.length;
    const linkSource = new Int32Array(m);
    const linkTarget = new Int32Array(m);
    const linkWeight = new Float32Array(m);
    for (let i = 0; i < m; i++) {
      linkSource[i] = links[i].source.index;
      linkTarget[i] = links[i].target.index;
      linkWeight[i] = links[i].weight;
    }

    worker.onmessage = (e: MessageEvent) => {
      const msg = e.data as
        | { type: 'tick'; positions: Float32Array; alpha: number; tickMs: number }
        | { type: 'end' };
      if (msg.type === 'tick') {
        targetPosRef.current = msg.positions;
        posLerpRef.current = true;
        simActiveRef.current = true;
        const now = performance.now();
        const times = snapTimesRef.current;
        times.push(now);
        while (times.length > 30) times.shift();
        const perf = graphPerf();
        if (perf) {
          perf.simTickMs = msg.tickMs;
          perf.simAlpha = msg.alpha;
          perf.workerActive = true;
          if (times.length >= 2) {
            const span = times[times.length - 1] - times[0];
            perf.snapshotHz = span > 0 ? ((times.length - 1) / span) * 1000 : 0;
          }
        }
        // The quadtree is rebuilt in the frame loop from the *interpolated*
        // (displayed) coords, not here from the just-arrived target — rebuilding
        // from targets would index positions the nodes haven't moved to yet.
        // The minimap offscreen layer is a full 32k-edge + 10.5k-node redraw, so
        // invalidate it at most ~2Hz during settle instead of every tick.
        if (now - lastMiniInvalidateRef.current > 500) {
          minimapCacheRef.current = null;
          lastMiniInvalidateRef.current = now;
        }
        kick();
      } else if (msg.type === 'end') {
        simActiveRef.current = false;
        minimapCacheRef.current = null; // final settle → accurate minimap
        const perf = graphPerf();
        if (perf) perf.workerActive = false;
        kick();
      }
    };

    // Async worker failures (module load/runtime) surface here, NOT via the
    // synchronous try/catch above. Without this a mid-sim throw would leave
    // simActiveRef true and the rAF loop spinning forever (idle-CPU bug).
    const failSafe = () => {
      // Kill the dead worker and fall back to the static seed layout. Clearing
      // simActive/posLerp lets the rAF loop reach its idle branch and stop.
      try { worker.terminate(); } catch { /* already gone */ }
      if (workerRef.current === worker) workerRef.current = null;
      simActiveRef.current = false;
      posLerpRef.current = false;
      const perf = graphPerf();
      if (perf) perf.workerActive = false;
      kick();
    };
    worker.onerror = failSafe;
    worker.onmessageerror = failSafe;

    worker.postMessage(
      { type: 'init', n, x, y, size, cx, cy, linkSource, linkTarget, linkWeight },
      [x.buffer, y.buffer, size.buffer, cx.buffer, cy.buffer, linkSource.buffer, linkTarget.buffer, linkWeight.buffer],
    );

    return () => {
      worker.terminate();
      workerRef.current = null;
      simActiveRef.current = false;
      posLerpRef.current = false;
      targetPosRef.current = null;
      snapTimesRef.current = [];
    };
    // Keyed on `data` too: if the graph is replaced, batching could otherwise
    // leave `prewarmed` unchanged (false→true collapses) and keep the stale
    // worker simulating the old node set into the new nodesRef.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [prewarmed, data]);

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

  const rebuildQuadtree = useCallback(() => {
    const nodes = nodesRef.current;
    if (!nodes.length) {
      quadtreeRef.current = null;
      return;
    }
    quadtreeRef.current = quadtree<SimNode>()
      .x((n) => n.x)
      .y((n) => n.y)
      .addAll(nodes);
  }, []);

  const fitToViewport = useCallback((animated = false) => {
    const nodes = nodesRef.current;
    const vp = viewportRef.current;
    if (!nodes.length || !vp.w || !vp.h) return;
    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    for (const n of nodes) {
      if (hiddenKindsRef.current.has(n.kind)) continue;
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
    const z = clamp(Math.min(vp.w / w, vp.h / h), MIN_ZOOM, 1.5);
    const cx = (minX + maxX) / 2;
    const cy = (minY + maxY) / 2;
    const nextPan = { x: vp.w / 2 - cx * z, y: vp.h / 2 - cy * z };
    const perf = graphPerf();
    if (perf) {
      perf.fitZoom = z;
      perf.fitPan = nextPan;
      perf.fitViewport = { w: vp.w, h: vp.h };
      perf.fitBounds = { x: minX, y: minY, w, h };
    }
    setView(z, nextPan, animated);
  }, []);

  // ── Canvas rendering ─────────────────────────────────────────────────────
  // The main graph layer (nodes + edges) is drawn imperatively to a <canvas>.
  // Reconciling thousands of SVG primitives per tick/hover was the dominant
  // cost; canvas coalesces tick + hover + pan in a frame into one paint. All
  // displayed inputs are read from refs so the draw is valid when invoked from
  // the animation loop (outside React).

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
      perf.fps = fpsRef.current;
    };
    const canvas = canvasRef.current;
    if (!canvas) return;
    const view = viewRef.current;
    const zoom = view.zoom;
    const pan = view.pan;
    const hoverNode = hoverNodeRef.current;
    const searchLC = searchLCRef.current;
    const hiddenKinds = hiddenKindsRef.current;
    const layoutReady = layoutReadyRef.current;
    const viewport = viewportRef.current;
    const focusAmt = focusRef.current;
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

    // World-space bounds of the viewport, for edge culling when zoomed in.
    const cullPad = 40 / zoom;
    const wl = -pan.x / zoom - cullPad;
    const wr = (viewport.w - pan.x) / zoom + cullPad;
    const wt = -pan.y / zoom - cullPad;
    const wb = (viewport.h - pan.y) / zoom + cullPad;

    const isVisible = (n: SimNode, pad = 80): boolean => {
      const sx = (n.x ?? 0) * zoom + pan.x;
      const sy = (n.y ?? 0) * zoom + pan.y;
      return sx >= -pad && sx <= viewport.w + pad && sy >= -pad && sy <= viewport.h + pad;
    };

    const labelAlpha = labelLayerAlpha(zoom);

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

      // Active/search labels stay crisp; ambient labels fade with zoom.
      const alpha = active || matches ? 1 : labelAlpha;
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
      ctx.globalAlpha = alpha;
      ctx.fillStyle = active || matches ? fgColor : fgMuteColor;
      ctx.fillText(text, sx - tw / 2, sy);
      ctx.globalAlpha = 1;
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
        const matches = !!searchLC && n.titleLC.includes(searchLC);
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
    // Pass 1: non-incident edges, batched into weight buckets. One beginPath +
    // stroke per bucket (a few draw calls) instead of ~32k. Off-viewport edges
    // are culled. Focus (hover/search) dims the whole ambient edge layer, eased.
    ctx.lineCap = 'round';
    ctx.strokeStyle = lineColor;
    const buckets = edgeBucketsRef.current;
    // While edge tooltips are reachable (zoomed in enough), record the on-screen
    // edge indices this frame so the pointer hit-test scans only visible edges
    // instead of all ~32k. Reuse the array (length=0) to avoid per-frame alloc.
    const collectVis = zoom >= EDGE_TOOLTIP_MIN_ZOOM;
    const vis = visibleEdgesRef.current;
    if (collectVis) vis.length = 0;
    for (let b = 0; b < buckets.length; b++) {
      const members = buckets[b];
      if (!members.length) continue;
      const wRep = (b + 0.5) / EDGE_BUCKETS;
      const baseAlpha = 0.18 + 0.45 * wRep;
      ctx.globalAlpha = baseAlpha + (0.05 - baseAlpha) * focusAmt;
      ctx.lineWidth = (0.6 + 0.7 * wRep) / zoom;
      ctx.beginPath();
      for (let k = 0; k < members.length; k++) {
        const e = links[members[k]];
        const a = e.source as SimNode;
        const bb = e.target as SimNode;
        if (!a || !bb || typeof a !== 'object' || typeof bb !== 'object') continue;
        if (hiddenKinds.has(a.kind) || hiddenKinds.has(bb.kind)) continue;
        if (hoverNode != null && (a.id === hoverNode || bb.id === hoverNode)) continue;
        const ax = a.x ?? 0, ay = a.y ?? 0, bx = bb.x ?? 0, by = bb.y ?? 0;
        // Cull edges whose bounding box is fully outside the viewport.
        if ((ax < wl && bx < wl) || (ax > wr && bx > wr) || (ay < wt && by < wt) || (ay > wb && by > wb)) continue;
        if (collectVis) vis.push(members[k]);
        ctx.moveTo(ax, ay);
        ctx.lineTo(bx, by);
      }
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
        ctx.globalAlpha = (0.55 + 0.35 * e.weight) * focusAmt;
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
    // visual primacy. Alpha eases with the focus amount.
    if (hoverNode) {
      const hn = nodes.find((x) => x.id === hoverNode);
      if (hn) {
        const c = kindColor(hn.color);
        ctx.beginPath();
        ctx.arc(hn.x ?? 0, hn.y ?? 0, hn.size / 2 + 10, 0, Math.PI * 2);
        ctx.fillStyle = c;
        ctx.globalAlpha = 0.18 * focusAmt;
        ctx.fill();
        ctx.globalAlpha = 1;
      }
    }

    for (const n of nodes) {
      if (hiddenKinds.has(n.kind)) continue;
      if (!isVisible(n, 40)) continue;
      let dim = false;
      if (hoverNode) dim = !(n.id === hoverNode || (neighbors?.has(n.id) ?? false));
      else if (searchLC) dim = !n.titleLC.includes(searchLC);
      const active = hoverNode === n.id;
      const r = n.size / 2;
      const c = kindColor(n.color);
      ctx.beginPath();
      ctx.arc(n.x ?? 0, n.y ?? 0, r, 0, Math.PI * 2);
      ctx.fillStyle = n.size >= 22 ? c : bgCardColor;
      // Dim eases in/out via focusAmt instead of snapping between 1 and 0.2.
      ctx.globalAlpha = dim ? 1 - 0.8 * focusAmt : 1;
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

  // ── Animation loop ───────────────────────────────────────────────────────
  // A single rAF loop advances every time-based motion (view ease, pan inertia,
  // focus fade, snapshot interpolation) and draws. It runs only while something
  // is unsettled and stops entirely otherwise — zero idle CPU. kick() (re)starts
  // it after any state change that needs animating.
  const frame = useCallback((now: number) => {
    rafRef.current = null;
    const last = lastFrameRef.current || now;
    const dt = Math.min(64, now - last);
    lastFrameRef.current = now;
    if (dt > 0) {
      const instFps = 1000 / dt;
      fpsRef.current = fpsRef.current ? fpsRef.current * 0.9 + instFps * 0.1 : instFps;
    }

    const reduce = reduceMotionRef.current;
    const view = viewRef.current;
    let active = false;

    // View easing / inertia.
    if (view.mode === 'ease') {
      if (reduce) {
        view.zoom = view.tZoom;
        view.pan.x = view.tPan.x;
        view.pan.y = view.tPan.y;
        view.mode = 'idle';
      } else {
        const k = 1 - Math.exp(-dt / TAU_VIEW);
        view.zoom += (view.tZoom - view.zoom) * k;
        view.pan.x += (view.tPan.x - view.pan.x) * k;
        view.pan.y += (view.tPan.y - view.pan.y) * k;
        if (
          Math.abs(view.tZoom - view.zoom) < VIEW_EPS &&
          Math.abs(view.tPan.x - view.pan.x) < PAN_EPS &&
          Math.abs(view.tPan.y - view.pan.y) < PAN_EPS
        ) {
          view.zoom = view.tZoom;
          view.pan.x = view.tPan.x;
          view.pan.y = view.tPan.y;
          view.mode = 'idle';
        } else {
          active = true;
        }
      }
    } else if (view.mode === 'inertia') {
      view.pan.x += view.panVel.x * dt;
      view.pan.y += view.panVel.y * dt;
      const decay = Math.exp(-dt / TAU_INERTIA);
      view.panVel.x *= decay;
      view.panVel.y *= decay;
      view.tPan.x = view.pan.x;
      view.tPan.y = view.pan.y;
      if (Math.hypot(view.panVel.x, view.panVel.y) < INERTIA_SPEED_EPS) {
        view.panVel.x = 0;
        view.panVel.y = 0;
        view.mode = 'idle';
      } else {
        active = true;
      }
    }

    // Focus (hover/search dim) fade.
    const focusTarget = hoverNodeRef.current || searchLCRef.current ? 1 : 0;
    if (reduce) {
      focusRef.current = focusTarget;
    } else {
      const k = 1 - Math.exp(-dt / TAU_FOCUS);
      focusRef.current += (focusTarget - focusRef.current) * k;
      if (Math.abs(focusTarget - focusRef.current) < 0.003) focusRef.current = focusTarget;
      else active = true;
    }

    // Interpolate node positions toward the latest simulation snapshot.
    if (posLerpRef.current && targetPosRef.current) {
      const target = targetPosRef.current;
      const nodes = nodesRef.current;
      const k = reduce ? 1 : 1 - Math.exp(-dt / TAU_POS);
      // The actively-dragged node follows the pointer, not the snapshot — pulling
      // it toward a (stale, pre-pin) snapshot each frame makes it rubber-band.
      const drag = dragRef.current;
      const dragIdx = drag && drag.mode === 'node' ? drag.nodeIndex : -1;
      let maxDelta = 0;
      for (let i = 0; i < nodes.length; i++) {
        if (i === dragIdx) continue;
        const n = nodes[i];
        const tx = target[i * 2];
        const ty = target[i * 2 + 1];
        const dx = tx - n.x;
        const dy = ty - n.y;
        n.x += dx * k;
        n.y += dy * k;
        const ad = Math.abs(dx) + Math.abs(dy);
        if (ad > maxDelta) maxDelta = ad;
      }
      layoutBoundsRef.current = layoutBounds(nodes);
      if (!simActiveRef.current && maxDelta < 0.05) {
        // Converged and worker stopped — snap exactly and stop interpolating.
        for (let i = 0; i < nodes.length; i++) {
          nodes[i].x = target[i * 2];
          nodes[i].y = target[i * 2 + 1];
        }
        rebuildQuadtree();
        posLerpRef.current = false;
      } else {
        // Rebuild the quadtree from the just-written interpolated coords every
        // few frames (not per-frame — a 10.5k addAll is ~1ms) so hit-testing
        // tracks the displayed positions, not the unreached snapshot targets.
        if (++qtRebuildTickRef.current >= 4) {
          qtRebuildTickRef.current = 0;
          rebuildQuadtree();
        }
        active = true;
      }
    }
    if (simActiveRef.current) active = true;

    draw();

    // Mirror displayed view into React state for the tooltip / % / cursor.
    const mirror = mirrorRef.current;
    if (Math.abs(view.zoom - mirror.zoom) > 1e-4) {
      mirror.zoom = view.zoom;
      setZoom(view.zoom);
    }
    if (Math.abs(view.pan.x - mirror.pan.x) > 0.25 || Math.abs(view.pan.y - mirror.pan.y) > 0.25) {
      mirror.pan = { x: view.pan.x, y: view.pan.y };
      setPan({ x: view.pan.x, y: view.pan.y });
    }

    const perf = graphPerf();
    if (active) {
      rafRef.current = requestAnimationFrame(frame);
    } else {
      // Final settle: sync exact mirror and clear the per-loop fps.
      mirror.zoom = view.zoom;
      mirror.pan = { x: view.pan.x, y: view.pan.y };
      setZoom(view.zoom);
      setPan({ x: view.pan.x, y: view.pan.y });
      lastFrameRef.current = 0;
      fpsRef.current = 0;
      if (perf) perf.fps = 0;
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [draw, rebuildQuadtree]);

  const kick = useCallback(() => {
    if (rafRef.current !== null) return;
    lastFrameRef.current = 0;
    rafRef.current = requestAnimationFrame(frame);
  }, [frame]);

  // Redraw once (no animation) — for hidden-kind toggles, theme, resize etc.
  const requestDraw = useCallback(() => {
    kick();
  }, [kick]);

  // Commit a new view. `animated` eases toward it; otherwise it snaps (used for
  // the first fit and reduced-motion).
  const setView = useCallback(
    (nextZoom: number, nextPan: { x: number; y: number }, animated: boolean) => {
      const view = viewRef.current;
      view.tZoom = clamp(nextZoom, MIN_ZOOM, MAX_ZOOM);
      view.tPan = { x: nextPan.x, y: nextPan.y };
      view.panVel = { x: 0, y: 0 };
      if (!animated || reduceMotionRef.current) {
        view.zoom = view.tZoom;
        view.pan = { x: view.tPan.x, y: view.tPan.y };
        view.mode = 'idle';
      } else {
        view.mode = 'ease';
      }
      kick();
    },
    [kick],
  );

  // Keep the state-mirroring refs current.
  useEffect(() => { hoverNodeRef.current = hoverNode; kick(); }, [hoverNode, kick]);
  useEffect(() => { searchLCRef.current = search.trim().toLowerCase(); kick(); }, [search, kick]);
  useEffect(() => { hiddenKindsRef.current = hiddenKinds; requestDraw(); }, [hiddenKinds, requestDraw]);
  useEffect(() => { layoutReadyRef.current = layoutReady; requestDraw(); }, [layoutReady, requestDraw]);
  useEffect(() => { viewportRef.current = viewport; requestDraw(); }, [viewport, requestDraw]);

  useEffect(() => {
    if (!window.matchMedia) return;
    const mq = window.matchMedia('(prefers-reduced-motion: reduce)');
    const onChange = () => { reduceMotionRef.current = mq.matches; };
    mq.addEventListener?.('change', onChange);
    return () => mq.removeEventListener?.('change', onChange);
  }, []);

  useEffect(() => () => {
    if (rafRef.current !== null) cancelAnimationFrame(rafRef.current);
  }, []);

  // One-shot fit after prewarm + viewport measurement — instant, no visible
  // "snap". Window resizes afterwards do NOT auto-refit (would yank the view).
  const firstFitRef = useRef(false);
  useLayoutEffect(() => {
    if (firstFitRef.current) return;
    if (!prewarmed || !nodesRef.current.length || !canFitViewport(viewport)) return;
    firstFitRef.current = true;
    viewportRef.current = viewport;
    fitToViewport(false);
    const perf = graphPerf();
    if (perf) perf.readyMs = performance.now() - mountTsRef.current;
    layoutReadyRef.current = true;
    setLayoutReady(true);
  }, [prewarmed, viewport, fitToViewport]);

  const zoomAt = useCallback(
    (factor: number, cx: number, cy: number) => {
      const view = viewRef.current;
      // Anchor on the current *target* so rapid wheel steps accumulate smoothly.
      const baseZoom = view.mode === 'ease' ? view.tZoom : view.zoom;
      const basely = view.mode === 'ease' ? view.tPan : view.pan;
      const nz = clamp(baseZoom * factor, MIN_ZOOM, MAX_ZOOM);
      const nextPan = {
        x: cx - (cx - basely.x) * (nz / baseZoom),
        y: cy - (cy - basely.y) * (nz / baseZoom),
      };
      setView(nz, nextPan, true);
    },
    [setView],
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
      const vp = viewportRef.current;
      if (e.key === '+' || e.key === '=') {
        zoomAt(1.25, vp.w / 2, vp.h / 2);
        e.preventDefault();
      } else if (e.key === '-' || e.key === '_') {
        zoomAt(0.8, vp.w / 2, vp.h / 2);
        e.preventDefault();
      } else if (e.key === '0') {
        fitToViewport(true);
        e.preventDefault();
      } else if (e.key === 'Escape') {
        setSearch('');
        setHoverNode(null);
      }
    }
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [zoomAt, fitToViewport]);

  const dragRef = useRef<
    | null
    | {
        mode: 'pan' | 'node-pending' | 'node' | 'minimap';
        sx: number;
        sy: number;
        origPan: { x: number; y: number };
        nodeId?: string;
        nodeIndex?: number;
        nodeStart?: { x: number; y: number };
        lastX: number;
        lastY: number;
        lastT: number;
        vx: number;
        vy: number;
      }
  >(null);

  // Convert (clientX, clientY) relative to the canvas wrapper into world
  // coordinates by inverting the *displayed* pan + zoom transform.
  const clientToWorld = (clientX: number, clientY: number) => {
    const el = containerRef.current;
    if (!el) return { x: 0, y: 0 };
    const r = el.getBoundingClientRect();
    const view = viewRef.current;
    return {
      x: (clientX - r.left - view.pan.x) / view.zoom,
      y: (clientY - r.top - view.pan.y) / view.zoom,
    };
  };

  const hitTestNode = (wx: number, wy: number): SimNode | null => {
    const hidden = hiddenKindsRef.current;
    const qt = quadtreeRef.current;
    if (qt) {
      // quadtree.find returns only the nearest CENTER, which is wrong when that
      // node is hidden, or when the pointer sits inside an overlapping node
      // whose centre isn't the closest. Collect every candidate within the
      // pointer's radius bbox via a pruned visit, then return the topmost
      // (highest index = last drawn) whose radius actually contains the pointer.
      const R = maxNodeRadiusRef.current + 2;
      let best: SimNode | null = null;
      qt.visit((node, x0, y0, x1, y1) => {
        if (!('length' in node)) {
          let leaf: QuadtreeLeaf<SimNode> | undefined = node;
          do {
            const d = leaf.data;
            if (d && !hidden.has(d.kind)) {
              const dx = d.x - wx, dy = d.y - wy, r = d.size / 2;
              if (dx * dx + dy * dy <= r * r && (!best || d.index > best.index)) best = d;
            }
            leaf = leaf.next;
          } while (leaf);
        }
        return x0 > wx + R || x1 < wx - R || y0 > wy + R || y1 < wy - R;
      });
      if (best) return best;
      // A miss is authoritative only once settled; while the sim is hot the tree
      // can lag live coords, so fall through to the exact scan.
      if (!simActiveRef.current) return null;
    }
    // Fallback linear scan (quadtree not yet built, or sim-active miss above).
    const nodes = nodesRef.current;
    for (let i = nodes.length - 1; i >= 0; i--) {
      const n = nodes[i];
      if (hidden.has(n.kind)) continue;
      const dx = (n.x ?? 0) - wx;
      const dy = (n.y ?? 0) - wy;
      const r = n.size / 2;
      if (dx * dx + dy * dy <= r * r) return n;
    }
    return null;
  };

  const hitTestEdge = (wx: number, wy: number): number | null => {
    const links = linksRef.current;
    const hidden = hiddenKindsRef.current;
    const tol = EDGE_HIT_TOLERANCE / viewRef.current.zoom;
    const tol2 = tol * tol;
    let bestI = -1;
    let bestD2 = Infinity;
    // Scan only the edges the draw pass marked visible this frame (already
    // viewport-culled), not all ~32k. Callers only invoke this at zoom >=
    // EDGE_TOOLTIP_MIN_ZOOM, which is exactly when the list is populated.
    const candidates = visibleEdgesRef.current;
    for (let ci = 0; ci < candidates.length; ci++) {
      const i = candidates[ci];
      const e = links[i];
      const a = e.source as SimNode;
      const b = e.target as SimNode;
      if (!a || !b || typeof a !== 'object' || typeof b !== 'object') continue;
      if (hidden.has(a.kind) || hidden.has(b.kind)) continue;
      const ax = a.x ?? 0, ay = a.y ?? 0;
      const bx = b.x ?? 0, by = b.y ?? 0;
      // Bounding-box prefilter: skip edges whose (padded) bbox excludes the
      // cursor before doing the projection math.
      if (wx < Math.min(ax, bx) - tol || wx > Math.max(ax, bx) + tol) continue;
      if (wy < Math.min(ay, by) - tol || wy > Math.max(ay, by) + tol) continue;
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
    // A pointer-down interrupts any running inertia/ease.
    viewRef.current.mode = 'idle';
    viewRef.current.panVel = { x: 0, y: 0 };
    const w = clientToWorld(e.clientX, e.clientY);
    const node = hitTestNode(w.x, w.y);
    const base = { lastX: e.clientX, lastY: e.clientY, lastT: performance.now(), vx: 0, vy: 0 };
    if (node) {
      dragRef.current = {
        mode: 'node-pending',
        sx: e.clientX,
        sy: e.clientY,
        origPan: { ...viewRef.current.pan },
        nodeId: node.id,
        nodeIndex: node.index,
        nodeStart: { x: node.x ?? 0, y: node.y ?? 0 },
        ...base,
      };
    } else {
      dragRef.current = {
        mode: 'pan',
        sx: e.clientX,
        sy: e.clientY,
        origPan: { ...viewRef.current.pan },
        ...base,
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
      // Track pointer velocity (px/ms) for pan-release inertia.
      const now = performance.now();
      const mdt = Math.max(1, now - d.lastT);
      d.vx = (e.clientX - d.lastX) / mdt;
      d.vy = (e.clientY - d.lastY) / mdt;
      d.lastX = e.clientX;
      d.lastY = e.clientY;
      d.lastT = now;

      if (d.mode === 'pan') {
        const view = viewRef.current;
        view.pan = { x: d.origPan.x + dx, y: d.origPan.y + dy };
        view.tPan = { x: view.pan.x, y: view.pan.y };
        kick();
      } else if (d.mode === 'node' && d.nodeId && d.nodeStart) {
        const n = nodesRef.current.find((x) => x.id === d.nodeId);
        if (n) {
          n.x = d.nodeStart.x + dx / viewRef.current.zoom;
          n.y = d.nodeStart.y + dy / viewRef.current.zoom;
          // Pin + reheat the simulation so neighbours respond (Obsidian feel).
          // No per-move quadtree/bounds rebuild here — pointermove can fire at
          // 120Hz+ and rebuilding 10.5k nodes each time would jank the drag; the
          // frame loop rebuilds every few frames while the sim is hot, and
          // pointer-up rebuilds once on release (covers the worker-disabled case).
          workerRef.current?.postMessage({ type: 'drag', index: d.nodeIndex, x: n.x, y: n.y });
          kick();
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
    // Skip edge hover while zoomed out, or while the sim is animating (positions
    // in flux → a tooltip would be meaningless and the scan is wasted work).
    if (viewRef.current.zoom < EDGE_TOOLTIP_MIN_ZOOM || simActiveRef.current) {
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
      // Release the pin → node relaxes back into the simulation. Rebuild the
      // spatial structures once here (they're skipped during the move).
      workerRef.current?.postMessage({ type: 'dragEnd', index: d.nodeIndex });
      layoutBoundsRef.current = layoutBounds(nodesRef.current);
      minimapCacheRef.current = null;
      rebuildQuadtree();
      kick();
    } else if (d.mode === 'pan') {
      // Hand the pointer velocity to the inertia integrator.
      const view = viewRef.current;
      if (!reduceMotionRef.current && Math.hypot(d.vx, d.vy) > INERTIA_SPEED_EPS) {
        view.panVel = { x: d.vx, y: d.vy };
        view.mode = 'inertia';
        kick();
      }
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
    const view = viewRef.current;
    const nextPan = { x: viewport.w / 2 - worldX * view.zoom, y: viewport.h / 2 - worldY * view.zoom };
    view.pan = nextPan;
    view.tPan = { ...nextPan };
    view.mode = 'idle';
    kick();
  }

  const onMinimapPointerDown = (e: React.PointerEvent) => {
    if (e.button !== 0) return;
    e.stopPropagation();
    dragRef.current = {
      mode: 'minimap',
      sx: e.clientX,
      sy: e.clientY,
      origPan: { ...viewRef.current.pan },
      lastX: e.clientX,
      lastY: e.clientY,
      lastT: performance.now(),
      vx: 0,
      vy: 0,
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
            onClick={() => fitToViewport(true)}
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
