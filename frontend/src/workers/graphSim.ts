// Force-simulation worker for GraphView.
//
// Runs a d3-force simulation off the main thread so a ~10.5k-node / ~32k-edge
// graph can "come alive" and respond to drag without ever blocking the UI
// thread. (A previous synchronous main-thread prewarm blocked >1s and was
// removed; this is its worker-resident replacement.)
//
// The simulation is *seeded* with the deterministic organicLayout positions
// computed on the main thread, so it refines/relaxes rather than re-deriving a
// layout — the settle is subtle and starts from the intended cloud. Positions
// are shipped back as transferable Float32Array snapshots at <=30Hz; the main
// thread interpolates between snapshots at display refresh.
//
// It cools to alpha 0 and stops (zero idle CPU). On node drag the main thread
// pins fx/fy and raises alphaTarget to 0.3 (Obsidian-style reheat — neighbours
// respond); on release fx/fy clear and alphaTarget returns to 0 so the node
// relaxes and the sim settles again.

import {
  forceSimulation,
  forceLink,
  forceManyBody,
  forceX,
  forceY,
  type Simulation,
  type SimulationNodeDatum,
  type SimulationLinkDatum,
} from 'd3-force';

interface SimNodeDatum extends SimulationNodeDatum {
  index: number;
  size: number;
  cx: number;
  cy: number;
}

type SimLinkDatum = SimulationLinkDatum<SimNodeDatum> & { weight: number };

interface InitMessage {
  type: 'init';
  n: number;
  x: Float32Array;
  y: Float32Array;
  size: Float32Array;
  cx: Float32Array;
  cy: Float32Array;
  linkSource: Int32Array;
  linkTarget: Int32Array;
  linkWeight: Float32Array;
}

type InMessage =
  | InitMessage
  | { type: 'drag'; index: number; x: number; y: number }
  | { type: 'dragEnd'; index: number }
  | { type: 'reheat'; alpha?: number }
  | { type: 'dispose' };

// 30Hz snapshot ceiling. Display interpolates the gaps.
const SNAP_INTERVAL_MS = 1000 / 30;

let sim: Simulation<SimNodeDatum, SimLinkDatum> | null = null;
let nodes: SimNodeDatum[] = [];
let running = false;
let disposed = false;
let lastSnapshot = 0;
let scheduled = false;

function postSnapshot(alpha: number, tickMs: number) {
  const n = nodes.length;
  const positions = new Float32Array(n * 2);
  for (let i = 0; i < n; i++) {
    positions[i * 2] = nodes[i].x ?? 0;
    positions[i * 2 + 1] = nodes[i].y ?? 0;
  }
  (postMessage as (msg: unknown, transfer: Transferable[]) => void)(
    { type: 'tick', positions, alpha, tickMs },
    [positions.buffer],
  );
}

function step() {
  scheduled = false;
  if (disposed || !sim) return;
  const t0 = performance.now();
  sim.tick();
  const tickMs = performance.now() - t0;
  const alpha = sim.alpha();
  const now = performance.now();
  const settling = alpha <= sim.alphaMin();
  if (now - lastSnapshot >= SNAP_INTERVAL_MS || settling) {
    postSnapshot(alpha, tickMs);
    lastSnapshot = now;
  }
  if (settling) {
    running = false;
    postMessage({ type: 'end' });
  } else {
    schedule();
  }
}

function schedule() {
  if (scheduled || disposed) return;
  scheduled = true;
  // setTimeout(0) yields between ticks so the worker event loop can process
  // incoming drag messages promptly; each tick already costs several ms at
  // scale so this does not meaningfully cap throughput.
  setTimeout(step, 0);
}

function ensureRunning() {
  if (!running && !disposed && sim) {
    running = true;
    lastSnapshot = 0;
    schedule();
  }
}

function init(msg: InitMessage) {
  const { n, x, y, size, cx, cy, linkSource, linkTarget, linkWeight } = msg;
  nodes = new Array(n);
  for (let i = 0; i < n; i++) {
    nodes[i] = { index: i, x: x[i], y: y[i], size: size[i], cx: cx[i], cy: cy[i] };
  }
  const links: SimLinkDatum[] = new Array(linkSource.length);
  for (let i = 0; i < linkSource.length; i++) {
    links[i] = { source: linkSource[i], target: linkTarget[i], weight: linkWeight[i] };
  }

  sim = forceSimulation<SimNodeDatum, SimLinkDatum>(nodes)
    .force(
      'link',
      forceLink<SimNodeDatum, SimLinkDatum>(links)
        .id((d) => d.index)
        .distance((l) => {
          const s = l.source as SimNodeDatum;
          const t = l.target as SimNodeDatum;
          return 70 + (s.size + t.size) * 1.2;
        })
        .strength((l) => 0.05 + 0.25 * l.weight),
    )
    // Barnes-Hut with a bounded interaction radius keeps repulsion cost flat at
    // ~10k nodes and stops far clusters from flying apart.
    .force('charge', forceManyBody<SimNodeDatum>().strength(-26).theta(0.9).distanceMax(520))
    // forceX/forceY toward each node's cluster centre preserves the kind
    // grouping (Concept/Synthesis/Entity/Source) instead of a single global
    // centre.
    .force('x', forceX<SimNodeDatum>((d) => d.cx).strength(0.035))
    .force('y', forceY<SimNodeDatum>((d) => d.cy).strength(0.035))
    .alphaDecay(0.035)
    .velocityDecay(0.55)
    .stop();

  // Start subtle: the seed is already the intended shape, so a low initial
  // alpha refines it rather than reshuffling.
  sim.alpha(0.35).alphaTarget(0);
  ensureRunning();
}

self.onmessage = (e: MessageEvent<InMessage>) => {
  const msg = e.data;
  switch (msg.type) {
    case 'init':
      init(msg);
      break;
    case 'drag': {
      if (!sim) break;
      const node = nodes[msg.index];
      if (node) {
        node.fx = msg.x;
        node.fy = msg.y;
      }
      sim.alphaTarget(0.3);
      if (sim.alpha() < 0.3) sim.alpha(0.3);
      ensureRunning();
      break;
    }
    case 'dragEnd': {
      if (!sim) break;
      const node = nodes[msg.index];
      if (node) {
        node.fx = null;
        node.fy = null;
      }
      sim.alphaTarget(0);
      ensureRunning();
      break;
    }
    case 'reheat': {
      if (!sim) break;
      sim.alpha(Math.max(sim.alpha(), msg.alpha ?? 0.3));
      ensureRunning();
      break;
    }
    case 'dispose':
      disposed = true;
      if (sim) sim.stop();
      sim = null;
      nodes = [];
      break;
  }
};
