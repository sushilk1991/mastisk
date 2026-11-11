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

Generation: draft -> critique -> at most one revision -> critique.
  - The intelligence bridge supplies both drafting and critique.
  - The cluster is a loose automatic grouping, so the drafter may use only
    the members that share something, or skip the cluster outright.
  - Prose guidance comes from ``wiki_voice.md`` (shared with the Compiler).
  - Critic checks grounding, the connection, and readability against the
    same full articles the drafter saw, then writes ``SCORE: <1-5>``.
  - Publication requires at least 4/5; missing or invalid scores fail closed.

Withheld attempts retain cluster IDs, score, and rationale in synthesis_runs,
without an article or vault page. The human accept/discard layer can still
review previously published drafts.

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

from slugify import slugify

from mastisk.agents.base import Agent, wiki_voice
from mastisk.agents.compiler import _render_markdown, _sections_to_md
from mastisk.agents.registry import resolve_prompt
from mastisk.bridges import intelligence
from mastisk.db import queries as q
from mastisk.db.queries import connect
from mastisk.paths import vault_dir
from mastisk.synthesis_policy import (
    _CRITIC_MAX_SCORE,
    _CRITIC_MIN_SCORE,
    _MAX_REVISIONS,
    _score_passes,
)
from mastisk.vault_io import write_vault_text

log = logging.getLogger("mastisk.synthesizer")


# Bumped when the prompt contract changes in a way that invalidates prior
# clusters' cached outputs. Cluster-hash dedup still applies, but a version
# bump means we *could* decide to re-run historic clusters — we currently
# don't, but the column is there for future replays.
PROMPT_VERSION = 5

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

# The drafter and the critic read each member article whole: the page can
# only be as concrete as its inputs, and at 1,200 chars the drafter saw
# little more than each lead paragraph and filled the rest with abstraction.
# The cap is a guard against a runaway page, not a budget. The longest
# article on 2026-11-11 was 17k chars, so eight full members stay near 35k
# tokens.
_MEMBER_BODY_MAX_CHARS = 24_000

# The reader profile for a Synthesis. Interests, dislikes and learnings pick
# what enters the wiki; here the cluster is already chosen, and that long list
# of recurring themes pulled every page toward the same few subjects.
_READER_PROFILE_FILES = ("identity", "style")

_LINK_TARGET_RE = re.compile(r'data-target="([^"]+)"')

_SKIP_PREFIX = "SKIPPED:"

# JSON schema the draft model must emit. Mirrors Compiler.SCHEMA_MD but
# slightly slimmer — Synthesis articles don't need aka, their kind is
# fixed, and we don't ask for artifacts (the ArtifactAgent handles those
# on a later pass if the user keeps the draft). Section headings are left
# to the drafter: fixed "thread / what connects them / tension" headings
# forced a grand unifying claim onto clusters that did not have one.
_SCHEMA_MD = """
Return a single JSON object in a ```json``` fenced block matching this shape:

```json
{
  "skip": false,
  "skip_reason": "",
  "id": "lowercase-slug",
  "kind": "Synthesis",
  "title": "Human-readable title",
  "summary": "One plain sentence naming what the articles have in common. Not a copy of the TL;DR.",
  "confidence": 0.0,
  "reading_minutes": 5,
  "sections": [
    {"h": "TL;DR", "kind": "callout", "body": "<p>2-3 sentences: what these tools, incidents or papers share and why it matters to the reader, with their names and numbers.</p>"},
    {"h": "A heading that states this section's point", "body": "<p>Paragraphs. Use <span class=\\"link\\" data-target=\\"slug\\">wiki link</span> for cross-references back to the source articles.</p>"},
    {"h": "Open questions", "kind": "open", "body": "<p>What is still unsettled.</p>"}
  ],
  "related": [
    {"id": "source-article-slug", "label": "Source article title", "weight": 0.8}
  ]
}
```

Rules:
- If no real connection exists (see the task above), return only
  {"skip": true, "skip_reason": "one plain sentence"}.
- "kind" MUST be "Synthesis". Do not emit any other kind.
- "title" must be a single clause, max __TITLE_MAX_CHARS__ characters. No subtitles or subtitle sentences; no em-dash appendages.
- "id" and related ids must be kebab-case, ASCII, no spaces.
- "related" lists only the articles you actually used, with weights in
  [0,1]. Link to each of them with a wiki-link span in the body too.
- Body text is HTML. Use <p> for paragraphs, <em> for emphasis, <code> for
  commands and flags, and <span class="link" data-target="slug"> for
  cross-references. No markdown inside bodies. Never invent
  ids: only link to slugs that appear in the articles above.
- Sections: open with the "TL;DR" callout. Then two to four sections whose
  headings state their point. Add "Open questions" (kind "open") only if the
  articles really leave something unsettled; omit it otherwise.
- Aim for 400-900 words in total. Shorter is fine when the connection is
  simple.
- The user's profile decides structure (TL;DR first, Open questions, linking). The writing guide decides how sentences are written.
- "confidence" is how well the articles support the connection, not how
  well the page reads.
""".replace("__TITLE_MAX_CHARS__", str(q.MAX_GENERATED_ARTICLE_TITLE_CHARS))


_CRITIC_TEMPLATE = """You are reviewing a draft Synthesis page for a personal wiki before it is
published. The draft claims that some of the {n} source articles below have
something in common. Its reader is one engineer who will not have the sources
open, so the page has to stand on its own.

Check three things against the source articles:

1. Grounding. Is every number, date, name and incident in the draft present
   in the source articles? Quote each one that is not.
2. Connection. Is the claimed connection specific to these articles, or is
   it so general it would fit most technical writing? Is any article forced
   into it?
3. Readability. Could the reader follow the page without the sources? Quote
   sentences that state a conclusion without the facts behind it, use a
   coined label or metaphor in place of a plain description, or would have
   to be read twice.

The italic line and the TL;DR are summaries: judge them only on whether the
sections below support them. A sentence the draft marks as a guess or
inference is not a grounding failure unless it states a specific number,
date or name.

Write 3-6 bullets. Each bullet quotes the sentence it is about and says what
to change, so the writer can fix exactly that sentence. If a check finds
nothing wrong, say so in one bullet.

Then output a final line `SCORE: <1-5>`. Score how serious the problems are,
not how many bullets you wrote. Wording you would polish, on a page that is
accurate and easy to follow, is still a 4.
- 5: fully grounded, a specific connection, and plain enough to retell.
- 4: accurate and readable; the remaining fixes are word choice in at most
  two sentences.
- 3: the reader would be misled or lost somewhere: the connection is real,
  but a section is too abstract to follow without the sources, a specific
  that matters is unsupported, or more than two sentences state a
  conclusion without its facts, use a coined label, or end a paragraph on
  a slogan.
- 2: the connection is forced or generic, or several claims are unsupported.
- 1: mostly fabricated or incoherent.

Return ONLY the bullets and the SCORE line. No preamble.

─── draft ───
{draft_rendered}

─── source articles ───
{sources_rendered}
"""


class Synthesizer(Agent):
    name = "synthesizer"
    tick_seconds = 1800  # 30 min

    async def run_once(self) -> None:
        if self.disabled_tick():
            return
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

        if draft_data is None or not _score_passes(eval_score):
            # Failed grades retain audit evidence but never create a wiki/vault page.
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
            log.info("synthesizer: withheld cluster %s (score=%s)", cluster_hash, eval_score)
            self.emit_feed(
                verb="withheld",
                obj=(draft_data or {}).get("title") or (
                    "No shared thread in cluster"
                    if (eval_rationale or "").startswith(_SKIP_PREFIX)
                    else "Synthesis failed critic gate"
                ),
                kind="synthesis",
                payload={"cluster_size": len(cluster_ids), "eval_score": eval_score},
            )
            return None

        draft_data["id"] = self._free_synthesis_id(draft_data["id"])
        draft_data["related"] = [
            r for r in draft_data["related"] if r.get("id") != draft_data["id"]
        ]
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
            synthesis_ids = frozenset(
                r["id"]
                for r in conn.execute(
                    "SELECT id FROM articles WHERE kind = 'Synthesis'"
                )
            )

        if not seeds:
            return None

        seed_ids = [s["id"] for s in seeds]
        adjacency = self._build_adjacency(all_ids, edges)
        scores = self._ppr(adjacency, seeds=seed_ids)

        # Exclude Synthesis articles from cluster membership. Seeds are
        # already filtered (queries.recent_seed_articles), but the PPR walk
        # reaches prior syntheses through related links — each synthesis
        # has 8 outgoing edges, making it a high-degree attractor. Without
        # this filter, later ticks synthesize their own output, producing
        # near-duplicate meta-syntheses that dominate the digest.
        nonzero = [
            (nid, s)
            for nid, s in scores.items()
            if s > 0 and nid not in synthesis_ids
        ]
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
        identity = self.load_identity(_READER_PROFILE_FILES)
        positive_block = self._render_positive_examples(positives)
        negative_block = self._render_negative_examples(negatives)
        title_block = self._render_title_guidance(members)
        cluster_block = self._render_cluster(members)

        parts = [
            "You are writing a Synthesis page for a personal wiki.",
            "",
            f"Below are {len(members)} articles that the wiki's link graph grouped",
            "together. The grouping is automatic and loose. Often only some of the",
            "articles have anything real in common, and sometimes none do.",
            "",
            "Find one concrete thing that at least three of them share: the same",
            "problem, the same cause, a disagreement, or one that explains another.",
            "Then write a page that explains it to someone who has not read them.",
            "",
            "- Use only the articles that fit. Leave the others out; do not stretch",
            "  an article to make it belong.",
            "- Before you write, state the connection to yourself in two plain",
            "  sentences. If those sentences would also be true of most technical",
            "  articles, there is no real connection here: return the skip object",
            "  described at the end. A skipped cluster is a good outcome.",
            "- Organize the page by its points, not as one paragraph per article.",
            "  But give the facts from each article that a point depends on: what",
            "  the thing is, what was found, the number. The reader has not read",
            "  the articles and should not need to.",
            "- Where the articles disagree or the connection breaks down, say so.",
            "- The summary and TL;DR may claim only what the sections go on to show.",
            "  Say 'two of the three' when that is the case, not 'each' or 'all'.",
            "",
            identity,
            "",
        ]
        if positive_block:
            parts.extend([positive_block, ""])
        if negative_block:
            parts.extend([negative_block, ""])
        parts.extend([title_block, ""])
        parts.extend([
            f"─── articles ({len(members)}) ───",
            cluster_block,
            "",
        ])
        voice = wiki_voice()
        if voice:
            parts.extend(["─── how to write the page ───", voice, ""])
        # Output contract last, so the JSON shape is the final thing read.
        parts.append(resolve_prompt("synthesizer", "schema", _SCHEMA_MD))
        return "\n".join(parts)

    @staticmethod
    def _render_cluster(members: list[dict]) -> str:
        """Full member articles, shared by the drafter and the critic so the
        critic judges grounding against what the drafter actually saw."""
        chunks: list[str] = []
        for m in members:
            body = (m.get("body_md") or "")[:_MEMBER_BODY_MAX_CHARS]
            chunks.append(
                f"### [{m['kind']}] {m['title']} (id: {m['id']})\n"
                f"Summary: {m.get('summary') or ''}\n\n"
                f"{body}\n"
            )
        return "\n".join(chunks)

    @staticmethod
    def _render_positive_examples(positives: list[dict]) -> str:
        """Titles of the accepted syntheses.

        Empty string when there are no exemplars — callers omit the block
        entirely on a cold install (zero accepted + zero rejected).
        """
        if not positives:
            return ""
        lines = [
            "─── examples the user has ACCEPTED ───",
            "These show which connections the user found worth keeping. Learn the",
            "kind of connection from them. Do not clone their wording, headline",
            "cadence or sentence shape: they predate the writing guide below.",
            "",
        ]
        # Titles only. Body excerpts of pages written before the guide
        # few-shot the old voice and its retired "The thread" heading.
        for p in positives:
            lines.append(f"- {p.get('title') or '(untitled)'}")
        return "\n".join(lines)

    @staticmethod
    def _render_title_guidance(members: list[dict]) -> str:
        """Prompt guardrails that keep titles specific to the current cluster.

        This deliberately does not remove or down-rank feedback examples. The
        accepted/rejected loop still teaches taste; this block tells the model
        not to reuse those examples' surface headline forms.
        """
        lines = [
            "─── title diversity guardrails ───",
            "The title must be specific enough that it would look wrong on a",
            "different synthesis cluster.",
            "- Learn judgment from the accepted/rejected examples, not their title",
            "  template.",
            "- Prefer concrete nouns, proper names, products, laws, papers, or",
            "  mechanisms from the current cluster.",
            "- Avoid reusable aphorism templates such as \"The X moved...\",",
            "  \"Where the X lives\", \"One move\", \"Find the...\", or",
            "  \"X got cheap\" unless the source material itself uses that language.",
            "- Avoid high-level title nouns like layer, boundary, constraint, check,",
            "  gate, thread, pattern, or rule when a cluster-specific noun can carry",
            "  the title.",
            "- Name the shared subject the way you would say it aloud to a colleague.",
            "  Do not build the title by listing the sources.",
            f"- Title must be a single clause, max {q.MAX_GENERATED_ARTICLE_TITLE_CHARS} characters.",
            "- No subtitles or subtitle sentences; no em-dash appendages.",
            "",
            "Titles of the articles in this cluster, for vocabulary:",
        ]
        for member in members:
            title = str(member.get("title") or "").strip()
            if title:
                lines.append(f"- {title}")
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
        sources_block = self._render_cluster(members)
        return resolve_prompt("synthesizer", "critic", _CRITIC_TEMPLATE).format(
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
        """Draft -> critique -> one optional repair; persistence applies the score gate."""
        base_prompt = self._draft_prompt(members, positives, negatives)
        prompt = base_prompt
        draft_data, score, rationale = None, None, None

        # Bound repair cost; preserve the failed grade if a repair cannot be generated.
        for attempt in range(_MAX_REVISIONS + 1):
            try:
                reply, _ = await intelligence.run_intelligence(prompt, timeout_s=300)
            except Exception as exc:
                log.warning("synthesizer: draft model unreachable: %s", exc)
                return draft_data, score, rationale

            candidate = self._extract_draft_json(reply.get("text") or "")
            if candidate is None:
                log.info("synthesizer: malformed draft JSON")
                return draft_data, score, rationale
            if candidate.get("skip"):
                if draft_data is not None:
                    # A repair that gives up keeps the failed draft's grade.
                    return draft_data, score, rationale
                reason = str(candidate.get("skip_reason") or "no shared thread").strip()
                log.info("synthesizer: drafter skipped cluster: %s", reason)
                return None, None, f"{_SKIP_PREFIX} {reason}"

            draft_data = self._normalise_draft(candidate, members)
            score, rationale = await self._critique(draft_data, members)
            if _score_passes(score) or score is None or attempt == _MAX_REVISIONS:
                return draft_data, score, rationale

            # Keep the original evidence and failed draft visible to the repair model.
            prompt = (
                f"{base_prompt}\n\nRevise the draft once using the critic below. "
                "Fix the sentences it quotes. Remove unsupported claims; do not invent "
                "evidence to satisfy the critic. Drop an article the critic says was "
                "forced in. Return the same JSON schema.\n\n"
                f"Critic feedback:\n{rationale}\n\n"
                f"Previous draft:\n{self._render_draft_for_critic(draft_data)}"
            )

        return draft_data, score, rationale

    async def _critique(
        self, draft_data: dict, members: list[dict]
    ) -> tuple[float | None, str | None]:
        """Return the critic's grade and evidence; an unavailable grade withholds publication."""
        prompt = self._critic_prompt(draft_data, members)
        try:
            resp, _ = await intelligence.run_intelligence(prompt, timeout_s=180)
        except Exception as e:
            log.warning("synthesizer: critic unreachable (%s); withholding draft", e)
            return None, None
        text = resp.get("text") or ""
        score = self._parse_score(text)
        return score, text.strip()

    @staticmethod
    def _parse_score(text: str) -> float | None:
        """Use the last score line; malformed or out-of-range grades must fail closed."""
        matches = re.findall(r"^\s*SCORE\s*[:=]\s*(.*?)\s*$", text, flags=re.I | re.M)
        if not matches:
            return None

        try:
            value = float(matches[-1])
        except ValueError:
            return None

        if not _CRITIC_MIN_SCORE <= value <= _CRITIC_MAX_SCORE:
            return None

        return value

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
        data["title"] = q.clamp_generated_title(data["title"])
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
        # Backfill related links at weight=0.5 for members the body links to
        # but the model forgot to list, so the graph reflects the page's
        # lineage. Members the drafter left out stay out: a link to an
        # article the page never mentions is a false edge. A draft that
        # names no member at all falls back to the whole cluster.
        related = [r for r in (data.get("related") or []) if isinstance(r, dict)]
        existing = {r.get("id") for r in related}
        linked: set[str] = set()
        for section in data.get("sections") or []:
            if isinstance(section, dict):
                linked.update(_LINK_TARGET_RE.findall(str(section.get("body") or "")))
        member_ids = {m["id"] for m in members}
        used = (existing | linked) & member_ids
        for m in members:
            if m["id"] in existing:
                continue
            if used and m["id"] not in linked:
                continue
            related.append({"id": m["id"], "label": m["title"], "weight": 0.5})
        data["related"] = related
        return data

    # ───── persistence ─────

    @staticmethod
    def _free_synthesis_id(article_id: str) -> str:
        """The drafter picks the id, and has picked a source article's id.

        Upserting that would overwrite the source page with the synthesis.
        Only an existing Synthesis may be refreshed in place.
        """
        candidate, n = article_id, 1
        with connect() as conn:
            while True:
                row = conn.execute(
                    "SELECT kind FROM articles WHERE id = ?", (candidate,)
                ).fetchone()
                if row is None or row["kind"] == "Synthesis":
                    return candidate
                n += 1
                candidate = f"{article_id}-synthesis" if n == 2 else f"{article_id}-synthesis-{n}"

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
        write_vault_text(vault_path, _render_markdown(data))
        return article_id
