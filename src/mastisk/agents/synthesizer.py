"""Synthesizer — drafts cross-article Synthesis pages via Draft→Critic loop.

Unlike the Compiler (which writes one article per source), the Synthesizer
takes a *cluster* of existing articles and weaves them into a single
Synthesis article that surfaces the shared pattern, tension, or emergent
story.

Cluster selection: Personalized PageRank (HippoRAG2-style).
  1. Seed = articles updated in the last 48h (capped to 10).
  2. Run power-iteration PPR over the links graph (damping=0.85, 20 iters).
  3. Top-K (K<=8) by PPR score = the cluster.
  4. Require at least 3 members and one non-seed, else skip.

Generation: single Claude draft + single Claude critique for bookkeeping.
  - Draft model: Claude (via the local ``claude`` CLI).
  - Critic model: Claude (same family — looping it on its own output is
    recursive and adds latency without value, so we don't).
  - Critic writes rationale bullets first, then ``SCORE: <1-5>``. The score
    is recorded but never triggers a revision; emit policy below covers it.

Emit policy: we do NOT gate on score. Every synthesis ships as a
kind='Synthesis' article with ``updated_by='Synthesizer'``; the UI's
accept/discard layer (Stage 4C) later sets ``user_accepted``. Low-score
drafts still ship so the user has rejection data to calibrate from.

Self-learning: we pull the 3 most recent user-accepted drafts as positive
few-shot exemplars and the 2 most recent rejected drafts with feedback as
negative exemplars. On a cold install with no feedback yet, both blocks
are omitted.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path

from slugify import slugify

from mastisk.agents.base import Agent
from mastisk.bridges import claude_bridge
from mastisk.db import queries as q
from mastisk.db.queries import connect
from mastisk.paths import vault_dir

log = logging.getLogger("mastisk.synthesizer")


# Bumped when the prompt contract changes in a way that invalidates prior
# clusters' cached outputs. Cluster-hash dedup still applies, but a version
# bump means we *could* decide to re-run historic clusters — we currently
# don't, but the column is there for future replays.
PROMPT_VERSION = 1

# PPR hyperparameters. Tuned for a small-to-medium wiki (hundreds of nodes).
# On a larger graph we'd want to trim the adjacency to high-weight edges
# before running, but at this scale 20 iterations over a few thousand edges
# is <10ms.
_PPR_DAMPING = 0.85
_PPR_ITERATIONS = 20

# Cluster shape requirements. Too small and there's nothing to synthesise;
# too large and the LLM can't hold the whole cluster in its head.
_MAX_CLUSTER_SIZE = 8
_MIN_CLUSTER_SIZE = 3
_SEED_WINDOW_HOURS = 48
_SEED_LIMIT = 10

# Bound per-tick compute. We drain up to 3 queued jobs, then *optionally*
# attempt one spontaneous synthesis. APScheduler's max_instances=1 prevents
# overlap with the next tick.
_MAX_QUEUED_JOBS_PER_TICK = 3

# JSON schema the draft model must emit. Mirrors Compiler.SCHEMA_MD but
# slightly slimmer — Synthesis articles don't need aka, their kind is
# fixed, and we don't ask for artifacts (the ArtifactAgent handles those
# on a later pass if the user keeps the draft).
_SCHEMA_MD = """
Return a single JSON object in a ```json``` fenced block matching this shape:

```json
{
  "id": "lowercase-slug",
  "kind": "Synthesis",
  "title": "Human-readable title",
  "summary": "1-2 sentence italic summary that names the thread.",
  "confidence": 0.0,
  "reading_minutes": 5,
  "sections": [
    {"h": "The thread", "kind": "callout", "body": "HTML paragraph naming the pattern."},
    {"h": "What connects them", "body": "HTML paragraph. Use <span class=\\"link\\" data-target=\\"slug\\">wiki link</span> for cross-references back to the source articles."},
    {"h": "Tension / open question", "kind": "open", "body": "HTML paragraph."}
  ],
  "related": [
    {"id": "source-article-slug", "label": "Source article title", "weight": 0.8}
  ]
}
```

Rules:
- "kind" MUST be "Synthesis". Do not emit any other kind.
- "id" and related ids must be kebab-case, ASCII, no spaces.
- "related" SHOULD contain every cluster member you actually leaned on, with
  weights in [0,1]. Link back to each source article via a wiki-link span
  in the body too.
- Body text is HTML. Use <em> for emphasis, <span class="link" data-target="slug">
  for cross-references. Never invent ids — only link to slugs that appear
  in the cluster below.
- Do NOT summarise each article separately — that's already done per-article.
  Find the *thread*, the shared pattern, or the tension between them.
"""


_CRITIC_TEMPLATE = """You are grading a Synthesis article written by a less capable model. The
goal was to find the thread connecting the {n} source articles below.

1. FIRST, in 3-5 bullets, list what's missing, what's fabricated, or what's
   a redundant per-article summary instead of genuine synthesis.
2. THEN output a final line: `SCORE: <1-5>` where 5 = genuinely novel
   cross-cutting insight, 3 = competent but obvious, 1 = fabricated /
   incoherent.

Return ONLY the rationale bullets + SCORE line. No preamble.

─── draft ───
{draft_rendered}

─── source articles ───
{sources_rendered}
"""


class Synthesizer(Agent):
    name = "synthesizer"
    tick_seconds = 1800  # 30 min

    async def run_once(self) -> None:
        # Drain up to N queued synthesizer jobs first (job-driven path). A
        # route that wants to force a synthesis of a specific cluster can
        # enqueue a job with payload {"cluster_ids": [...]}.
        for _ in range(_MAX_QUEUED_JOBS_PER_TICK):
            job = self._pick_job()
            if not job:
                break
            log.info("%s: picking job %s (%s)", self.name, job["id"], job["kind"])
            self._mark_running(job["id"])
            try:
                await self._handle(job)
                self._mark_done(job["id"])
            except Exception as e:
                log.exception("%s: job %s failed", self.name, job["id"])
                self._mark_failed(job["id"], str(e))

        # At most ONE spontaneous synthesis per tick. Keeps the 30-minute
        # cadence honest — without this cap a backlog of clusters could
        # monopolise the compute budget on each tick and mask scheduler
        # behaviour.
        try:
            await self._synthesize_once()
        except Exception:
            log.exception("synthesizer: spontaneous synthesis failed")

    async def _handle(self, job: dict) -> None:
        """Job-driven path. Payload may pin a specific cluster; otherwise
        behave like a spontaneous tick."""
        payload = json.loads(job.get("payload_json") or "{}")
        pinned = payload.get("cluster_ids")
        if pinned and isinstance(pinned, list):
            await self._synthesize_once(cluster_override=[str(x) for x in pinned])
        else:
            await self._synthesize_once()

    # ───── main pipeline ─────

    async def _synthesize_once(
        self, *, cluster_override: list[str] | None = None
    ) -> str | None:
        """Pick a cluster, run Draft→Critic, persist. Return draft article id
        on success, None if nothing ran (no cluster, or already synthesised).
        """
        cluster_ids = cluster_override or self._select_cluster()
        if not cluster_ids:
            log.info("synthesizer: no viable cluster this tick")
            return None

        cluster_hash = self._cluster_hash(cluster_ids)

        with connect() as conn:
            members = self._load_cluster(conn, cluster_ids)
            if len(members) < _MIN_CLUSTER_SIZE:
                log.info(
                    "synthesizer: cluster degenerate after load (%d < %d)",
                    len(members),
                    _MIN_CLUSTER_SIZE,
                )
                return None

            newest_member_ts = max(m["updated_at"] for m in members)
            if self._cluster_was_synthesized(
                conn, cluster_hash, newest_member_ts
            ):
                log.info(
                    "synthesizer: cluster %s already synthesised; skipping",
                    cluster_hash,
                )
                return None

            positives = q.accepted_synthesis_examples(conn, limit=3)
            negatives = q.rejected_synthesis_examples(conn, limit=2)

        draft_data, eval_score, eval_rationale = await self._draft_critic_loop(
            members, positives, negatives
        )

        if draft_data is None:
            # Draft was unusable even after retry. Still record the run so we
            # don't immediately retry the same cluster next tick.
            with connect() as conn:
                q.record_synthesis_run(
                    conn,
                    cluster_hash=cluster_hash,
                    source_article_ids=cluster_ids,
                    draft_article_id=None,
                    eval_score=eval_score,
                    eval_rationale=eval_rationale,
                    prompt_version=PROMPT_VERSION,
                )
            log.info(
                "synthesizer: cluster %s produced no usable draft (score=%s)",
                cluster_hash,
                eval_score,
            )
            return None

        article_id = self._persist_synthesis(draft_data, cluster_ids)

        with connect() as conn:
            q.record_synthesis_run(
                conn,
                cluster_hash=cluster_hash,
                source_article_ids=cluster_ids,
                draft_article_id=article_id,
                eval_score=eval_score,
                eval_rationale=eval_rationale,
                prompt_version=PROMPT_VERSION,
            )

        self.emit_feed(
            verb="synthesized",
            obj=(draft_data.get("title") or article_id)[:80],
            kind="synthesis",
            touched=len(cluster_ids),
            payload={
                "article_id": article_id,
                "cluster_size": len(cluster_ids),
                "eval_score": eval_score,
            },
        )
        return article_id

    # ───── cluster selection ─────

    def _select_cluster(self) -> list[str] | None:
        """Run PPR, return the cluster member ids or None.

        Returns None if any of: no recent seeds, PPR diverges to a single
        node, or fewer than 3 viable nodes have non-zero score.
        """
        with connect() as conn:
            seeds = q.recent_seed_articles(
                conn, hours=_SEED_WINDOW_HOURS, limit=_SEED_LIMIT
            )
            edges = q.list_graph_edges(conn)
            # Also load every article id so isolated nodes appear in the
            # adjacency map (PPR needs every node represented even with empty
            # outgoing edges, otherwise power iteration drops them entirely
            # and score lookups KeyError).
            all_ids = [
                r["id"] for r in conn.execute("SELECT id FROM articles")
            ]

        if not seeds:
            return None

        seed_ids = [s["id"] for s in seeds]
        adjacency = self._build_adjacency(all_ids, edges)
        scores = self._ppr(adjacency, seeds=seed_ids)

        # Filter to non-zero scores and sort. A zero score means the walk
        # never reached the node; including it adds nothing to the cluster.
        nonzero = [(nid, s) for nid, s in scores.items() if s > 0]
        nonzero.sort(key=lambda x: x[1], reverse=True)

        k = min(_MAX_CLUSTER_SIZE, len(nonzero))
        if k < _MIN_CLUSTER_SIZE:
            return None
        member_ids = [nid for nid, _ in nonzero[:k]]

        # Require at least one non-seed member — otherwise the PPR is just
        # echoing back the seed set and there's no real "cluster" to synthesise.
        seed_set = set(seed_ids)
        if all(m in seed_set for m in member_ids):
            # Widen the window slightly: add the next highest non-seed node
            # if one exists. Otherwise give up.
            for nid, _ in nonzero[k:]:
                if nid not in seed_set:
                    member_ids.append(nid)
                    break
            else:
                return None

        return member_ids

    @staticmethod
    def _build_adjacency(
        all_ids: list[str], edges: list[tuple[str, str, float]]
    ) -> dict[str, dict[str, float]]:
        """Bidirectional adjacency with weights.

        Links are stored directionally in the DB, but for PPR traversal we
        treat the graph as undirected — "A links to B" and "B links to A"
        should both contribute to B being near A in a random walk. Duplicate
        edges take the max of their weights.
        """
        adj: dict[str, dict[str, float]] = {i: {} for i in all_ids}
        for frm, to, w in edges:
            if frm not in adj or to not in adj:
                continue  # stale edge pointing at a deleted article
            if frm == to:
                continue
            adj[frm][to] = max(adj[frm].get(to, 0.0), w)
            adj[to][frm] = max(adj[to].get(frm, 0.0), w)
        return adj

    @staticmethod
    def _ppr(
        adjacency: dict[str, dict[str, float]],
        seeds: list[str],
        *,
        damping: float = _PPR_DAMPING,
        iterations: int = _PPR_ITERATIONS,
    ) -> dict[str, float]:
        """Personalized PageRank via power iteration. Pure Python, no numpy.

        ``adjacency[node] = {neighbor: weight}``. We row-normalise the
        transition probabilities so each node's outgoing weights sum to 1,
        then iterate ``r = (1-d)*e + d * P^T r``, where ``e`` is the
        uniform reset distribution over the seed set.
        """
        nodes = list(adjacency.keys())
        n = len(nodes)
        if n == 0 or not seeds:
            return {}
        valid_seeds = [s for s in seeds if s in adjacency]
        if not valid_seeds:
            return {s: 0.0 for s in nodes}
        reset_mass = 1.0 / len(valid_seeds)
        reset = {nid: (reset_mass if nid in valid_seeds else 0.0) for nid in nodes}
        rank = dict(reset)
        # Precompute normalised transitions. Dangling nodes (no outgoing)
        # redistribute via the reset vector on each iteration.
        trans: dict[str, dict[str, float]] = {}
        for src, nbrs in adjacency.items():
            total = sum(nbrs.values())
            trans[src] = {k: v / total for k, v in nbrs.items()} if total > 0 else {}
        for _ in range(iterations):
            new_rank = {nid: (1.0 - damping) * reset[nid] for nid in nodes}
            dangling_mass = 0.0
            for src, nbrs in trans.items():
                if not nbrs:
                    dangling_mass += rank[src]
                    continue
                contrib = damping * rank[src]
                for dst, w in nbrs.items():
                    new_rank[dst] += contrib * w
            if dangling_mass > 0:
                spill = damping * dangling_mass
                for nid in valid_seeds:
                    new_rank[nid] += spill * reset_mass
            rank = new_rank
        return rank

    # ───── cluster hashing & dedup ─────

    @staticmethod
    def _cluster_hash(member_ids: list[str]) -> str:
        """Stable 16-hex-char hash of a sorted id list. The hash is
        membership-only — reordering the same members yields the same hash,
        so cluster dedup catches PPR score fluctuations that reorder the
        top-K without changing who's in it."""
        joined = "|".join(sorted(member_ids))
        return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def _cluster_was_synthesized(
        conn, cluster_hash: str, newest_member_ts: str
    ) -> bool:
        """True if we already synthesised this membership AND no member has
        updated since that run. Comparing on ``created_at`` is safe because
        it and ``updated_at`` are both written as CURRENT_TIMESTAMP in UTC.

        We treat any existing row as authoritative — even a failed run
        (``draft_article_id IS NULL``) counts. Otherwise a permanently-bad
        cluster would be retried every tick forever.
        """
        prev = q.find_recent_synthesis_for_hash(conn, cluster_hash)
        if not prev:
            return False
        # If anything in the cluster has been updated since the last run,
        # a fresh synthesis is warranted.
        return prev["created_at"] >= newest_member_ts

    # ───── prompt assembly ─────

    @staticmethod
    def _load_cluster(conn, cluster_ids: list[str]) -> list[dict]:
        """Load the cluster members as a list of dicts with enough context
        for the draft prompt. Ordering preserved from input; missing ids
        silently dropped."""
        placeholder = ",".join("?" * len(cluster_ids))
        rows = {
            r["id"]: dict(r)
            for r in conn.execute(
                f"""SELECT id, title, kind, summary, body_md, updated_at
                    FROM articles WHERE id IN ({placeholder})""",
                cluster_ids,
            )
        }
        return [rows[i] for i in cluster_ids if i in rows]

    def _draft_prompt(
        self,
        members: list[dict],
        positives: list[dict],
        negatives: list[dict],
    ) -> str:
        identity = self.load_identity()
        positive_block = self._render_positive_examples(positives)
        negative_block = self._render_negative_examples(negatives)
        cluster_block = self._render_cluster_for_draft(members)

        parts = [
            "You are Mastisk's Synthesizer. Read the N articles below and write ONE",
            "cross-cutting Synthesis article that surfaces the *thread* connecting them.",
            "Do not summarise each one — that's already done per-article. Find the",
            "shared pattern, the tension, or the emergent story.",
            "",
            identity,
            "",
        ]
        if positive_block:
            parts.extend([positive_block, ""])
        if negative_block:
            parts.extend([negative_block, ""])
        parts.extend([
            f"─── cluster ({len(members)} articles) ───",
            cluster_block,
            "",
            _SCHEMA_MD,
        ])
        return "\n".join(parts)

    @staticmethod
    def _render_cluster_for_draft(members: list[dict]) -> str:
        chunks: list[str] = []
        for m in members:
            body = (m.get("body_md") or "")[:1200]
            chunks.append(
                f"### [{m['kind']}] {m['title']} (id: {m['id']})\n"
                f"Summary: {m.get('summary') or ''}\n\n"
                f"{body}\n"
            )
        return "\n".join(chunks)

    @staticmethod
    def _render_positive_examples(positives: list[dict]) -> str:
        """Titles + first 200 chars of body for each accepted synthesis.

        Empty string when there are no exemplars — callers omit the block
        entirely on a cold install (zero accepted + zero rejected).
        """
        if not positives:
            return ""
        lines = [
            "─── examples the user has ACCEPTED ───",
            "Match this register: specificity, named threads, concrete links.",
            "",
        ]
        for p in positives:
            title = p.get("title") or "(untitled)"
            excerpt = ((p.get("body_md") or p.get("summary") or "")[:200]).strip()
            lines.append(f"- {title}\n  {excerpt}")
        return "\n".join(lines)

    @staticmethod
    def _render_negative_examples(negatives: list[dict]) -> str:
        if not negatives:
            return ""
        lines = [
            "─── examples the user REJECTED ───",
            "Avoid these failure modes:",
            "",
        ]
        for n in negatives:
            title = n.get("title") or "(untitled)"
            feedback = (n.get("user_feedback") or "").strip()
            excerpt = ((n.get("body_md") or n.get("summary") or "")[:200]).strip()
            reason = f"the user rejected this because: {feedback}" if feedback else \
                     "the user rejected this"
            lines.append(f"- {title} ({reason})\n  {excerpt}")
        return "\n".join(lines)

    def _critic_prompt(self, draft_data: dict, members: list[dict]) -> str:
        # Render the draft as markdown-ish — the critic shouldn't have to
        # parse JSON to grade it, and doing so would reward structural
        # correctness over substance.
        rendered = self._render_draft_for_critic(draft_data)
        sources_block = "\n".join(
            f"- [{m['kind']}] {m['title']}: {m.get('summary') or ''}"
            for m in members
        )
        return _CRITIC_TEMPLATE.format(
            n=len(members),
            draft_rendered=rendered,
            sources_rendered=sources_block,
        )

    @staticmethod
    def _render_draft_for_critic(data: dict) -> str:
        parts = [
            f"# {data.get('title') or '(untitled)'}",
            "",
            f"*{data.get('summary') or ''}*",
            "",
        ]
        for s in data.get("sections") or []:
            parts.append(f"## {s.get('h') or ''}")
            parts.append(str(s.get("body") or ""))
            parts.append("")
        return "\n".join(parts)

    # ───── Draft → Critic loop ─────

    async def _draft_critic_loop(
        self,
        members: list[dict],
        positives: list[dict],
        negatives: list[dict],
    ) -> tuple[dict | None, float | None, str | None]:
        """Run a single Claude draft, then a single Claude critique for a score.

        Returns (draft_data, score, rationale). draft_data is None only if the
        draft call failed or produced malformed JSON. The critic's score is
        recorded for bookkeeping but never triggers a revision.
        """
        prompt = self._draft_prompt(members, positives, negatives)
        try:
            reply = await claude_bridge.run_claude(prompt, timeout_s=300)
        except Exception as e:
            log.warning("synthesizer: draft model unreachable: %s", e)
            return None, None, None

        text = reply.get("text") or ""
        candidate = self._extract_draft_json(text)
        if candidate is None:
            log.info(
                "synthesizer: malformed draft JSON (reply len=%d)",
                len(text),
            )
            return None, None, None

        draft_data = self._normalise_draft(candidate, members)
        score, rationale = await self._critique(draft_data, members)
        # Critic failure → ship the uncritiqued draft. Low score → still ship;
        # the UI's accept/discard layer is what gates emit.
        return draft_data, score, rationale

    async def _critique(
        self, draft_data: dict, members: list[dict]
    ) -> tuple[float | None, str | None]:
        """Run the Claude critic. Returns (score, rationale) or (None, None)
        on failure. A failure here must NOT kill the synthesis — we'd rather
        ship an uncritiqued draft than skip entirely."""
        prompt = self._critic_prompt(draft_data, members)
        try:
            resp = await claude_bridge.run_claude(prompt, timeout_s=180)
        except Exception as e:
            log.warning("synthesizer: critic unreachable (%s); shipping uncritiqued", e)
            return None, None
        text = resp.get("text") or ""
        score = self._parse_score(text)
        return score, text.strip()

    @staticmethod
    def _parse_score(text: str) -> float | None:
        """Pull ``SCORE: <1-5>`` from the critic's output. Accepts ints or
        decimals and clamps to [1,5]. Returns None if no match — caller
        treats that as "critic failed"."""
        m = re.search(r"SCORE\s*[:=]\s*([0-9]+(?:\.[0-9]+)?)", text, flags=re.I)
        if not m:
            return None
        try:
            v = float(m.group(1))
        except ValueError:
            return None
        return max(1.0, min(5.0, v))

    @staticmethod
    def _extract_draft_json(reply: str) -> dict | None:
        """Find the first ```json``` (or plain ```) block in the reply and
        parse it. None if parsing fails — the caller retries or records
        a failed run.
        """
        if not reply:
            return None
        start = reply.find("```json")
        if start == -1:
            start = reply.find("```")
            if start == -1:
                return None
        nl = reply.find("\n", start)
        if nl == -1:
            return None
        end = reply.find("```", nl + 1)
        if end == -1:
            return None
        try:
            data = json.loads(reply[nl + 1 : end].strip())
        except json.JSONDecodeError:
            return None
        return data if isinstance(data, dict) else None

    @staticmethod
    def _normalise_draft(data: dict, members: list[dict]) -> dict:
        """Clamp / fill defaults on the draft JSON so persistence doesn't
        fail on a missing field. Only touches sanity defaults — content is
        left alone.
        """
        data.setdefault("kind", "Synthesis")
        # Force Synthesis even if the model returned something else; this
        # agent's entire job is to emit kind='Synthesis'.
        if data.get("kind") != "Synthesis":
            data["kind"] = "Synthesis"
        if not data.get("id"):
            # Derive a slug from the title; fall back to a cluster-member
            # concatenation so we never emit an empty id.
            title = data.get("title") or "synthesis"
            data["id"] = (slugify(title) or "synthesis")[:80]
        if not data.get("title"):
            data["title"] = data["id"].replace("-", " ").title()
        data.setdefault("summary", "")
        data.setdefault("sections", [])
        # Clamp confidence / reading_minutes to the Compiler's ranges.
        try:
            c = float(data.get("confidence", 0.6))
        except (TypeError, ValueError):
            c = 0.6
        data["confidence"] = max(0.0, min(1.0, c))
        try:
            rm = int(data.get("reading_minutes", 5))
        except (TypeError, ValueError):
            rm = 5
        data["reading_minutes"] = max(1, min(60, rm))
        # Auto-backfill related links: every cluster member that isn't
        # already in data["related"] gets added at weight=0.5 so the graph
        # reflects the synthesis's lineage.
        existing = {r.get("id") for r in (data.get("related") or []) if isinstance(r, dict)}
        related = list(data.get("related") or [])
        for m in members:
            if m["id"] in existing or m["id"] == data["id"]:
                continue
            related.append({"id": m["id"], "label": m["title"], "weight": 0.5})
        data["related"] = related
        return data

    # ───── persistence ─────

    def _persist_synthesis(self, data: dict, cluster_ids: list[str]) -> str:
        """Write the Synthesis article to DB + vault. Returns the article id.

        Mirrors Compiler._persist_article but omits the article_sources
        linkage (Synthesis articles don't have a single upstream source;
        their lineage lives in `links` via `related`, plus the
        synthesis_runs.source_article_ids json).
        """
        article_id = data["id"]
        slug = slugify(data["title"])[:80] or article_id
        vault_path = vault_dir() / "synthesis" / f"{slug}.md"

        with connect() as conn, q.txn(conn):
            q.upsert_article(conn, {
                "id": article_id,
                "kind": "Synthesis",
                "title": data["title"],
                "slug": slug,
                "aka": [],
                "summary": data.get("summary", ""),
                "body_md": _sections_to_md(data.get("sections", [])),
                "confidence": float(data.get("confidence", 0.6)),
                "reading_minutes": int(data.get("reading_minutes", 5)),
                "updated_by": "Synthesizer",
                "vault_path": str(vault_path),
            })
            q.replace_sections(conn, article_id, data.get("sections", []))
            q.set_related(conn, article_id, data.get("related", []))

        vault_path.parent.mkdir(parents=True, exist_ok=True)
        vault_path.write_text(_render_markdown(data))
        return article_id


# ─── module-level markdown helpers (match Compiler's render shape) ───

def _sections_to_md(sections: list[dict]) -> str:
    out: list[str] = []
    for s in sections:
        out.append(f"## {s.get('h', '')}\n")
        out.append(_strip_html(s.get("body", "")))
        out.append("")
    return "\n".join(out)


def _strip_html(html: str) -> str:
    s = re.sub(r'<span class="link" data-target="([^"]+)">([^<]+)</span>', r"[[\2|\1]]", html)
    s = re.sub(r"<em>([^<]+)</em>", r"*\1*", s)
    s = re.sub(r"<[^>]+>", "", s)
    return s


def _render_markdown(data: dict) -> str:
    lines = [
        "---",
        f"id: {data['id']}",
        f"kind: {data['kind']}",
        f"title: {data['title']}",
        f"confidence: {data.get('confidence', 0.6)}",
        f"reading_minutes: {data.get('reading_minutes', 5)}",
        "---",
        "",
        f"# {data['title']}",
        "",
        f"*{data.get('summary', '')}*",
        "",
        _sections_to_md(data.get("sections", [])),
    ]
    return "\n".join(lines)
