"""BlogWriter — drafts a personal blog post from recent synthesis.

Pulls a window_days slice of classified notes, compiled articles, finished
roundtables and repo-ideator notes; ranks them (recency or theme-relevance +
LLM rerank); assembles one Claude prompt (identity + task + sources + output
contract, all inline); parses a bare JSON draft; resolves inline citations;
atomic-writes the draft to ``vault/blog/drafts/<slug>.md`` and records the
citation ledger.

See docs/superpowers/specs/2026-04-22-blog-writer-design.md §§6-13.
"""
from __future__ import annotations

import json
import logging
import math
import re
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar

from slugify import slugify

from mastisk.agents.base import Agent
from mastisk.bridges import claude_bridge, codex_bridge, ollama_bridge
from mastisk.db import queries as q
from mastisk.db.queries import connect
from mastisk.paths import blog_drafts_dir, vault_dir
from mastisk.routes.notes import atomic_write
from mastisk.settings import get_settings

log = logging.getLogger("mastisk.blog_writer")


# Stoplist for the keyword-overlap pass (spec §6.2). Deliberately short — the
# ranker only needs to strip the noise words that would otherwise dominate the
# overlap count. English-only for v1; revisit if theme inputs drift multilingual.
STOP_WORDS: frozenset[str] = frozenset({
    "the", "a", "an", "is", "are", "was", "were", "and", "or", "but",
    "in", "on", "at", "to", "for", "of", "with", "from", "by", "as",
    "this", "that", "it", "its", "they", "them", "their", "we", "you",
    "your", "i", "me", "my", "he", "she", "his", "her", "have", "has",
    "had", "do", "does", "did", "be", "been", "being", "will", "would",
    "could", "should", "can",
})


# Parses ``[source N]`` or ``[source N, source M]`` citation brackets Claude
# emits inline. The spec instructs Claude to cite either as `[source 2]` or
# `[source 2, source 5]` (two sources backing the same claim), so we accept
# both the single `[source N]` form and the repeated `[source N, source M]`
# form. Capture group is the full inside-brackets payload; _postprocess
# splits and filters at the Python layer.
CITATION_RE = re.compile(r"\[(source\s+\d+(?:\s*,\s*(?:source\s+)?\d+)*)\]")
_NUM_RE = re.compile(r"\d+")


# Appended to the initial Claude prompt on the one-shot retry after an invalid
# JSON response (spec §17). Keeping it blunt and short — the prior attempt
# already carried the full output contract.
_JSON_RETRY_SUFFIX = (
    "\n\nYour previous response was not valid JSON. Respond with a bare JSON "
    "object only, no preamble, no markdown fences."
)


# Personal-evidence boost factors applied to LLM rerank scores. Notes (the
# user's own writing, including repo_ideator-tagged ones) and roundtable
# syntheses outweigh external articles so claims anchor to personal evidence
# first. Articles default to 1.0 (unboosted) via .get fallback. Not user-
# tunable yet — kept here as a single source of truth rather than in
# BlogSettings.
_KIND_BOOST: dict[str, float] = {
    "note": 1.6,        # user's own writing — both plain notes and repo_ideator-tagged
    "roundtable": 1.4,  # user's own synthesis (multi-agent debate they ran)
    # articles default to 1.0 (unboosted) via .get fallback
}


def _try_parse_draft(text: str) -> dict | None:
    """Parse an LLM response into a draft dict or None.

    Returns None (rather than raising) when:
    - the payload isn't valid JSON,
    - the top-level value isn't a dict,
    - ``body_md`` is missing,
    - ``body_md`` isn't a non-empty string.

    The caller uses None to decide whether to retry or fall through.
    """
    try:
        parsed = json.loads(text.strip())
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    body_md = parsed.get("body_md")
    if not isinstance(body_md, str) or not body_md.strip():
        return None
    return parsed


RERANK_PROMPT_TEMPLATE = """You are ranking knowledge-base sources by relevance to a theme. Return STRICT JSON only.

Theme: {theme}

Sources (0-indexed):
{source_list}

Return JSON matching this schema exactly:
{{"scored": [{{"i": <int>, "score": <float in [0,1]>}}, ...]}}

Score semantics:
- 1.0 = directly supports the theme as a thesis claim
- 0.7 = clearly related
- 0.4 = same domain but not on-thesis
- 0.0 = unrelated

Exclude indices that are not relevant. Maximum 30 entries.
Do not include any prose before or after the JSON.
"""


class BlogWriter(Agent):
    """BlogWriter agent. See module docstring + spec §§6-13."""

    name: ClassVar[str] = "blog_writer"
    # Short tick — the user is blocking on this; they just clicked a button.
    tick_seconds: ClassVar[int] = 10

    async def _handle(self, job: dict) -> None:
        payload = json.loads(job["payload_json"] or "{}")
        bp_id = payload.get("blog_post_id")
        if bp_id is None:
            log.warning("blog_writer: no blog_post_id in job %s", job["id"])
            return

        with connect() as conn:
            bp = q.get_blog_post(conn, bp_id)
        if bp is None or bp.get("deleted_at") is not None:
            # Either the row was deleted before we picked up (rare — the DELETE
            # path doesn't dequeue jobs) or it never existed. Either way, nothing
            # to do. Don't raise — let the base wrapper mark the jobs row done.
            return
        if bp["status"] not in ("pending", "running"):
            # Double-process guard: base class picks queued jobs, but a stuck
            # job reclaimed from running to queued could see a blog_posts row
            # that was already finalized on a prior attempt.
            log.info("blog_writer: %s already %s, skipping", bp_id, bp["status"])
            return

        # Regenerate orphan cleanup: if this row carries a ``path`` from a
        # prior successful draft, the file is now stale (reset_blog_post_for_
        # regenerate nulled the DB fields but left the file on disk). Unlink
        # it before drafting so _unique_draft_path doesn't slide to ``-2.md``
        # and orphan the old file. On a fresh run, path is null → no-op.
        stale_path = bp.get("path")
        if stale_path:
            try:
                (vault_dir() / stale_path).unlink(missing_ok=True)
            except OSError as e:
                log.warning(
                    "blog_writer: could not unlink stale draft %s (%s)",
                    stale_path, e,
                )

        with connect() as conn:
            q.update_blog_post_status(conn, bp_id=bp_id, status="running")

        try:
            candidates = self._gather_sources(bp["window_days"])
            if not candidates:
                raise RuntimeError("no sources in window")

            ranked = await self._rank(candidates, theme=bp["theme"])
            settings = get_settings().blog
            ranked = ranked[: settings.max_sources]

            # Build the single-file prompt (identity + task + sources + schema).
            # First render at the Claude budget. If we fall back to Ollama we
            # re-render with a tighter per-source cap.
            prompt = self._render_prompt(
                theme=bp["theme"], ranked=ranked,
                char_budget=settings.total_prompt_char_limit,
                per_source_cap=settings.per_source_char_limit,
            )

            draft_json, model_used = await self._call_llm(
                prompt=prompt, ranked=ranked, theme=bp["theme"],
            )
            if draft_json is None:
                raise RuntimeError("LLM failed (both Claude and Ollama)")

            raw_body = str(draft_json.get("body_md") or "")
            body_md, cited_ns = self._postprocess_citations(raw_body, len(ranked))

            # Race check: the user may have hit DELETE while we were drafting.
            # Spec §7 + §13 steps 6-8: three checkpoints (start of _handle,
            # pre-write, post-write). The start check was the `get_blog_post`
            # above; this is the pre-write one.
            with connect() as conn:
                fresh = q.get_blog_post(conn, bp_id)
            if fresh is None or fresh.get("deleted_at") is not None:
                log.info("blog_writer: %s deleted mid-draft, discarding", bp_id)
                return

            title = (str(draft_json.get("title") or "").strip())[:200]
            tags_raw = draft_json.get("tags") or []
            if not isinstance(tags_raw, list):
                tags_raw = []
            tags = [str(t).strip().lower() for t in tags_raw if str(t).strip()][:5]

            slug = _derive_blog_slug(title, bp_id, bp["created_at"])
            file_path = _unique_draft_path(slug)
            frontmatter = _build_frontmatter(
                bp=bp, bp_id=bp_id, title=title, tags=tags, ranked=ranked,
                cited_ns=cited_ns, model_used=model_used, body_md=body_md,
            )
            atomic_write(file_path, frontmatter + "\n" + body_md)

            # Post-write race check: if DELETE landed between the pre-write
            # check and now, clean up our file and bail without flipping to done.
            with connect() as conn:
                fresh2 = q.get_blog_post(conn, bp_id)
            if fresh2 is None or fresh2.get("deleted_at") is not None:
                try:
                    file_path.unlink(missing_ok=True)
                except OSError:
                    log.warning(
                        "blog_writer: could not unlink orphaned %s", file_path,
                    )
                return

            # Derive the actual slug from the (possibly uniquified) filename so
            # the DB index stays consistent with the vault file.
            actual_slug = file_path.stem
            rel_path = str(file_path.relative_to(vault_dir()))
            word_count = len(body_md.split())
            if (
                word_count < settings.draft_word_count_min
                or word_count > settings.draft_word_count_max
            ):
                log.warning(
                    "blog_writer draft word_count=%d out of range [%d, %d]",
                    word_count,
                    settings.draft_word_count_min,
                    settings.draft_word_count_max,
                )

            with connect() as conn:
                affected = q.update_blog_post_done(
                    conn, bp_id=bp_id, slug=actual_slug, path=rel_path,
                    title=title, tags_json=json.dumps(tags), model=model_used,
                    word_count=word_count,
                    body_preview=body_md[:400],
                )
                if affected == 0:
                    # DELETE landed between the post-write re-check and the
                    # UPDATE. ``WHERE deleted_at IS NULL`` rejected the row,
                    # but the DELETE path saw path=NULL so didn't unlink our
                    # freshly-written file. Clean it up ourselves.
                    try:
                        file_path.unlink(missing_ok=True)
                    except OSError:
                        log.warning(
                            "blog_writer: could not unlink orphaned %s",
                            file_path,
                        )
                    return
                for n, cand in enumerate(ranked, start=1):
                    q.insert_blog_post_source(
                        conn, blog_post_id=bp_id,
                        kind=cand["kind"], ref=str(cand["ref"]),
                        rank=n, used=(n in cited_ns),
                        origin=cand.get("origin"),
                    )
            self.emit_feed(
                verb="blog-done", obj=str(bp_id), kind="blog_post",
                payload={
                    "title": title, "model": model_used,
                    "word_count": len(body_md.split()),
                    "source_count": len(ranked),
                },
            )
        except Exception as e:
            # Write our own row's failure state first, then re-raise so the
            # base wrapper marks the jobs row failed too. Roundtable does this
            # same two-row bookkeeping (src/mastisk/agents/roundtable.py).
            with connect() as conn:
                q.update_blog_post_status(
                    conn, bp_id=bp_id, status="failed",
                    error=str(e)[:500], finished=True,
                )
            raise

    # ───── source gathering ─────

    def _gather_sources(self, window_days: int) -> list[dict]:
        """Pull the in-window candidate pool. See spec §6.1.

        Runs the three candidate queries (notes, articles, roundtables) plus
        an auxiliary query that returns the ids of notes authored by the
        GithubIdeator inside the window so we can tag origin='repo_ideator'
        on matching notes. No fourth "repo idea" source — those notes live
        in the notes table already.
        """
        cutoff_expr = f"datetime('now', '-{int(window_days)} days')"

        with connect() as conn:
            note_rows = conn.execute(
                f"""SELECT id, slug, body, summary, classification, tags_json,
                          created_at AS ts
                   FROM notes
                   WHERE deleted_at IS NULL
                     AND classified_at IS NOT NULL
                     AND (classified_at >= {cutoff_expr} OR created_at >= {cutoff_expr})
                   ORDER BY created_at DESC"""
            ).fetchall()
            article_rows = conn.execute(
                f"""SELECT id, slug, title, summary, body_md, updated_at AS ts
                    FROM articles
                    WHERE updated_at >= {cutoff_expr}"""
            ).fetchall()
            rt_rows = conn.execute(
                f"""SELECT id, prompt, synthesis, finished_at AS ts
                    FROM roundtables
                    WHERE status = 'done'
                      AND synthesis IS NOT NULL
                      AND finished_at >= {cutoff_expr}"""
            ).fetchall()
            # Repo-ideator tag set: notes authored by the ideator in-window.
            # json_each expands the note_ids_json array into one row per id.
            ideator_rows = conn.execute(
                f"""SELECT DISTINCT CAST(value AS INTEGER) AS note_id
                    FROM repo_idea_runs, json_each(repo_idea_runs.note_ids_json)
                    WHERE repo_idea_runs.ideated_at >= {cutoff_expr}
                      AND json_valid(repo_idea_runs.note_ids_json)"""
            ).fetchall()

        ideator_ids: set[int] = {r["note_id"] for r in ideator_rows if r["note_id"] is not None}

        candidates: list[dict] = []
        for r in note_rows:
            try:
                tags = json.loads(r["tags_json"] or "[]")
            except json.JSONDecodeError:
                tags = []
            candidates.append({
                "kind": "note",
                "ref": r["id"],
                "slug": r["slug"],
                "title": None,
                "summary": r["summary"] or "",
                "body": r["body"] or "",
                "classification": r["classification"],
                "tags": tags,
                "ts": r["ts"],
                "origin": "repo_ideator" if r["id"] in ideator_ids else None,
            })
        for r in article_rows:
            candidates.append({
                "kind": "article",
                "ref": r["id"],
                "slug": r["slug"],
                "title": r["title"],
                "summary": r["summary"] or "",
                "body": r["body_md"] or "",
                "ts": r["ts"],
                "origin": None,
            })
        for r in rt_rows:
            candidates.append({
                "kind": "roundtable",
                "ref": r["id"],
                "slug": None,
                "title": (r["prompt"] or "")[:120],
                "summary": (r["synthesis"] or "")[:240],
                "body": r["synthesis"] or "",
                "prompt": r["prompt"] or "",
                "ts": r["ts"],
                "origin": None,
            })
        return candidates

    # ───── ranking ─────

    async def _rank(self, candidates: list[dict], *, theme: str) -> list[dict]:
        """Rank candidates. See spec §6.2.

        No theme → sort by recency (ts DESC).
        Theme → cheap keyword overlap pass → top pre_rank_limit → LLM rerank →
        validated rerank used for ordering (fallback to keyword order on
        validation failure).

        On rerank success: apply kind-based personal-evidence boost (notes
        and repo-ideator-tagged notes 1.6x; roundtables 1.4x; articles 1.0x),
        drop anything below ``settings.blog.min_relevance_score``, sort by
        boosted score DESC then ts DESC. The note multiplier is applied to
        every note regardless of ``origin`` — both plain notes and repo_ideator-
        tagged notes are the user's own evidence (notes the user wrote vs.
        notes derived from the user's commits), so they share the same factor.

        On rerank failure (None) or empty survivors after threshold filter:
        fall back to the keyword-order pre_ranked list AS-IS — the keyword
        pass doesn't have comparable scores.
        """
        if not theme or not theme.strip():
            return sorted(candidates, key=lambda c: (c.get("ts") or ""), reverse=True)

        settings = get_settings().blog
        tokens = self._tokenize(theme)
        scored: list[tuple[float, dict]] = []
        for c in candidates:
            summary_tokens = self._tokenize(c.get("summary") or "")
            body_head_tokens = self._tokenize((c.get("body") or "")[:1000])
            overlap_summary = len(tokens & summary_tokens)
            overlap_body = len(tokens & body_head_tokens)
            score = overlap_summary + 0.5 * overlap_body
            scored.append((score, c))
        # Stable-sort by score DESC, then recency DESC within ties.
        scored.sort(
            key=lambda sc: (sc[0], sc[1].get("ts") or ""), reverse=True,
        )
        pre_ranked = [c for _, c in scored[: settings.pre_rank_limit]]

        # LLM rerank. Validation failure → recency-keyword order.
        reranked = await self._llm_rerank(pre_ranked, theme=theme)
        if reranked is None:
            return pre_ranked

        # Personal-evidence boost: the user's own notes (including those
        # tagged repo_ideator) and roundtable syntheses outweigh external
        # articles, so claims anchor to personal evidence first.
        boosted: list[tuple[dict, float]] = []
        for cand, raw_score in reranked:
            multiplier = _KIND_BOOST.get(cand.get("kind") or "", 1.0)
            boosted.append((cand, raw_score * multiplier))

        threshold = settings.min_relevance_score
        survivors = [(c, s) for c, s in boosted if s >= threshold]
        if not survivors:
            # Threshold ate everything — the LLM produced no usable signal at
            # the requested cutoff. Same fallback as a None rerank: trust the
            # keyword pass rather than draft from zero sources.
            log.warning(
                "blog_writer: rerank survivors empty after threshold; "
                "falling back to keyword order",
            )
            return pre_ranked
        # Sort by boosted score DESC, ts DESC for tiebreak.
        survivors.sort(
            key=lambda cs: (cs[1], cs[0].get("ts") or ""), reverse=True,
        )
        return [c for c, _ in survivors]

    @staticmethod
    def _tokenize(text: str) -> set[str]:
        """Lowercase, alphanumeric tokens, stoplist stripped, length > 1."""
        toks = re.findall(r"[a-z0-9]+", text.lower())
        return {t for t in toks if t not in STOP_WORDS and len(t) > 1}

    async def _llm_rerank(
        self, candidates: list[dict], *, theme: str,
    ) -> list[tuple[dict, float]] | None:
        """Ollama rerank pass. Returns ``None`` on any validation failure.

        Prompt returns a strict JSON object
        ``{"scored": [{"i": int, "score": float}, ...]}``. Theme text is
        user-controlled (§17 attack surface), so we validate hard:
        - Top-level JSON object with ``scored`` key.
        - ``scored`` is a list.
        - Every element is a dict with int ``i`` in ``[0, len(candidates))``
          and a numeric (int or float) ``score``.
        Any violation → log.warning, return None so the caller falls back to
        the keyword-pass ordering. No retry — the rerank is best-effort.

        Duplicates by ``i`` are dropped (first occurrence wins). If every
        surviving entry has score 0, returns None — the LLM said nothing is
        relevant, so the caller should fall back to keyword order rather than
        treating "all zeros" as "all equally non-relevant".
        """
        settings = get_settings().blog
        if not candidates:
            return []
        lines = []
        for i, c in enumerate(candidates):
            kind = c["kind"]
            # Short one-line preview for the ranker. Title > summary > body head.
            preview = (
                c.get("title")
                or c.get("summary")
                or (c.get("body") or "")[:120]
            )
            preview = preview.strip().replace("\n", " ")[:120]
            lines.append(f"{i}. [{kind}] {preview}")
        prompt = RERANK_PROMPT_TEMPLATE.format(
            theme=theme.strip(),
            source_list="\n".join(lines),
        )
        try:
            result = await ollama_bridge.run_ollama(prompt, settings.ollama_model)
            text = result.get("text", "") if isinstance(result, dict) else str(result)
            parsed = json.loads(text.strip())
        except Exception as e:
            log.warning("blog_writer: rerank LLM call failed (%s)", e)
            return None

        if not isinstance(parsed, dict) or "scored" not in parsed:
            log.warning("blog_writer: rerank response missing 'scored' key")
            return None
        raw = parsed["scored"]
        if not isinstance(raw, list):
            log.warning("blog_writer: rerank 'scored' is not a list")
            return None
        seen: set[int] = set()
        scored_pairs: list[tuple[dict, float]] = []
        upper = len(candidates)
        for entry in raw:
            if not isinstance(entry, dict):
                log.warning("blog_writer: rerank entry %r is not a dict", entry)
                return None
            i = entry.get("i")
            score = entry.get("score")
            if not isinstance(i, int) or isinstance(i, bool):
                log.warning("blog_writer: rerank entry i %r is not an int", i)
                return None
            if i < 0 or i >= upper:
                log.warning("blog_writer: rerank index %s out of range", i)
                return None
            if isinstance(score, bool) or not isinstance(score, (int, float)):
                log.warning("blog_writer: rerank entry score %r is not numeric", score)
                return None
            if math.isnan(score) or math.isinf(score):
                log.warning("blog_writer: rerank entry score %r is not finite", score)
                return None
            if score < 0 or score > 1:
                log.warning("blog_writer: rerank entry score %r outside [0,1]", score)
                return None
            if i in seen:
                continue
            seen.add(i)
            scored_pairs.append((candidates[i], float(score)))
        if not scored_pairs:
            # Empty / all-duplicate ranking isn't a useful result — the caller
            # would draft with zero sources. Signal failure so the caller
            # falls back to keyword-pass ordering.
            log.warning("blog_writer: rerank returned empty list; falling back")
            return None
        if all(score == 0 for _, score in scored_pairs):
            # The LLM said every source is unrelated — that's not "no signal",
            # it's "negative signal". Don't draft with all-zero scores; trust
            # the keyword pass instead.
            log.warning("blog_writer: rerank returned all-zero scores; falling back")
            return None
        return scored_pairs

    # ───── prompt rendering ─────

    def _render_prompt(
        self, *, theme: str, ranked: list[dict],
        char_budget: int, per_source_cap: int,
    ) -> str:
        """Build the full Claude prompt string. See spec §8.

        Iteratively halves ``per_source_cap`` (floor = min_per_source_chars)
        before dropping tail items. Dropping is last resort — tail items are
        lowest-ranked but still carry signal.
        """
        settings = get_settings().blog
        identity_preamble = self._identity_preamble()
        voice_skill = self._voice_skill()
        # First attempt at the requested cap; halve down to the floor.
        cap = per_source_cap
        floor = settings.min_per_source_chars
        items = list(ranked)
        while True:
            sources_block = self._render_sources_block(items, cap)
            prompt = self._assemble(
                identity_preamble=identity_preamble,
                theme=theme,
                sources_block=sources_block,
                voice_skill=voice_skill,
            )
            if len(prompt) <= char_budget:
                return prompt
            if cap > floor:
                new_cap = max(floor, cap // 2)
                if new_cap == cap:
                    break
                cap = new_cap
                continue
            # At floor and still over budget — drop tail items one at a time.
            if len(items) <= 1:
                return prompt  # can't shrink further; just return what we have
            items = items[:-1]

    @staticmethod
    def _identity_preamble() -> str:
        """Load identity files and strip the '# About the user' leading H1.

        Agent.load_identity returns a string whose first line is ``# About the
        user`` (see src/mastisk/agents/base.py:83). Embedding that raw would
        nest an H1 inside the prompt and confuse the model's header tree — so
        we strip the header and inline the rest as a paragraph. (spec §8.2)
        """
        raw = Agent.load_identity()
        return raw.removeprefix("# About the user\n").strip() or "(no identity captured yet)"

    @staticmethod
    def _voice_skill() -> str:
        """Load the human-voice rulebook shipped next to this agent.

        ``blog_voice.md`` is a static skill file that codifies anti-AI-tell
        rules for blog drafting (no em-dashes for asides, avoid ``It's not X
        it's Y`` parallels, drop "delve/leverage/landscape" vocabulary, etc.).
        Editable by the user; missing or empty file is a no-op so older
        installs and the test fixtures keep working without it.
        """
        from pathlib import Path
        p = Path(__file__).parent / "blog_voice.md"
        if not p.exists():
            return ""
        raw = p.read_text().strip()
        # The file is readable as a standalone doc with its own H1, but
        # injecting that under our "## Voice rules" header would nest H1
        # inside H2 and confuse the model's section tree (same issue
        # _identity_preamble works around). Strip the first line if it
        # starts with "# ".
        if raw.startswith("# "):
            _, _, rest = raw.partition("\n")
            return rest.lstrip()
        return raw

    def _render_sources_block(self, items: list[dict], per_source_cap: int) -> str:
        """Render the ``## Sources`` section. See spec §8.1."""
        lines = ["## Sources", ""]
        for n, c in enumerate(items, start=1):
            lines.extend(self._render_one_source(n, c, per_source_cap))
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    @staticmethod
    def _render_one_source(n: int, c: dict, per_source_cap: int) -> list[str]:
        """One labeled block per source. The `### Source N —` heading is the
        citation anchor (spec §8.1) — Claude cites with `[source N]`."""
        kind = c["kind"]
        ref = c["ref"]
        lines: list[str] = []
        if kind == "note":
            title_bit = (c.get("summary") or "")[:80].replace("\n", " ").strip()
            lines.append(f'### Source {n} — note #{ref} "{title_bit}"')
            if c.get("classification"):
                lines.append(f"- kind: {c['classification']} (classification)")
            if c.get("ts"):
                lines.append(f"- captured: {c['ts']}")
            if c.get("tags"):
                lines.append(f"- tags: {', '.join(str(t) for t in c['tags'])}")
            if c.get("origin") == "repo_ideator":
                lines.append("- origin: repo_ideator")
        elif kind == "article":
            slug = c.get("slug") or ref
            title_bit = c.get("title") or ref
            lines.append(f'### Source {n} — article "{title_bit}" (slug: {slug})')
            lines.append("- kind: article")
            if c.get("ts"):
                lines.append(f"- updated: {c['ts']}")
        else:  # roundtable
            title_bit = (c.get("title") or "")[:80]
            lines.append(f'### Source {n} — roundtable #{ref} "{title_bit}"')
            lines.append("- kind: roundtable synthesis")
            if c.get("ts"):
                lines.append(f"- finished: {c['ts']}")
        summary = (c.get("summary") or "").strip()
        body = (c.get("body") or "").strip()
        body_trunc = body[:per_source_cap]
        lines.append("")
        if summary and summary != body_trunc[: len(summary)]:
            lines.append(summary)
            lines.append("")
            lines.append("---")
            lines.append("")
        lines.append(body_trunc)
        return lines

    def _assemble(
        self, *, identity_preamble: str, theme: str, sources_block: str,
        voice_skill: str = "",
    ) -> str:
        """Glue the prompt pieces together. Matches spec §8.2 exactly."""
        theme_text = theme.strip() if theme and theme.strip() else "(no theme — pick the strongest thread)"
        # Voice block is appended only when blog_voice.md is present so older
        # installs and the unit tests don't depend on the file.
        voice_block = f"\n## Voice rules\n{voice_skill}\n\n" if voice_skill else "\n"
        return (
            "# Blog draft task\n\n"
            "You are drafting a personal blog post in the author's voice from their "
            "recent notes, articles, roundtable syntheses, and repo-derived ideas.\n\n"
            f"Author identity and voice: {identity_preamble}\n\n"
            "## Theme\n"
            f"{theme_text}\n\n"
            "When a theme is provided, center the draft on it — weave the sources into "
            "a coherent argument about the theme. When no theme is provided, look at "
            "the sources, find the strongest recurring thread across them, and write "
            "about that.\n\n"
            f"{sources_block}"
            f"{voice_block}"
            "## Constraints\n"
            "- 800–2000 words. Aim for 1200.\n"
            "- First-person voice. Use \"I\" naturally; mirror the author's cadence from "
            "the identity/style guidance above.\n"
            "- Lead with a concrete hook — a specific observation, not \"In this post we "
            "will explore…\".\n"
            "- No section headers unless the argument genuinely needs them. Prose > bullets.\n"
            "- No moralizing, no LinkedIn-voice, no corporate summary language.\n"
            "- Cite specifically. Every non-trivial claim must end with `[source N]` "
            "pointing at the exact source that supports it. If two sources back the same "
            "claim, write `[source 2, source 5]`. Do not invent source numbers.\n"
            "- Cite a source only when it directly supports the surrounding claim. If a "
            "source is only loosely related, leave it out — do not reach for it. Drop "
            "tangential examples rather than redirecting them with phrases like \"what's "
            "really interesting here\".\n"
            "- When the user's own notes, commits (in repo-ideator notes), or roundtables "
            "are available, anchor claims to those over external articles. Personal "
            "evidence makes the post specific.\n"
            "- End with one forward-looking question or open thread — something the author "
            "is still thinking about. Do not wrap with a pat conclusion.\n\n"
            "## Output contract\n"
            "Respond with a BARE JSON object — no markdown fences, no preamble, no "
            "reasoning trace. The object MUST match this shape exactly:\n\n"
            "{\n"
            "  \"title\": \"Concrete noun-phrase title, ≤80 chars, in sentence case\",\n"
            "  \"tags\": [\"2-5 lowercased-kebab tags\"],\n"
            "  \"body_md\": \"The full draft as markdown. Newlines escaped as \\\\n. "
            "Inline citations use [source N].\"\n"
            "}\n\n"
            "`body_md` MUST be a JSON string (properly escaped newlines); do NOT wrap it "
            "in ``` fences. Do not emit anything before or after the JSON object.\n"
        )

    # ───── LLM fallback chain ─────

    async def _call_llm(
        self, *, prompt: str, ranked: list[dict], theme: str,
    ) -> tuple[dict | None, str]:
        """Try Claude → Codex → Ollama (with tighter prompt). See spec §13.

        Returns (parsed_json_dict, model_label). ``model_label`` is 'claude',
        'codex', 'ollama', or 'none' on triple failure.

        Spec §17 mandates one retry on JSON-shape failure (bad parse, missing/
        empty body_md) before falling through to Ollama — we catch JSON errors
        distinctly from transport errors so the retry fires only when the
        model *responded* but misformatted. Transport exceptions (timeout,
        non-zero exit) skip the retry and fall straight to the next tier.
        Codex sits between Claude and Ollama as a second cloud-class tier.
        """
        settings = get_settings().blog

        claude_retried = False
        claude_prompt = prompt
        while True:
            try:
                result = await claude_bridge.run_claude(
                    prompt=claude_prompt,
                    timeout_s=settings.claude_timeout_seconds,
                )
            except Exception as e:
                log.warning(
                    "blog_writer: Claude call failed (%s); falling back", e,
                )
                break
            text = result.get("text", "") if isinstance(result, dict) else str(result)
            parsed = _try_parse_draft(text)
            if parsed is not None:
                return parsed, "claude"
            if claude_retried:
                log.warning(
                    "blog_writer: Claude retry also produced invalid JSON; "
                    "falling back to Codex",
                )
                break
            log.warning(
                "blog_writer: Claude JSON invalid/missing body_md; retrying once",
            )
            claude_retried = True
            claude_prompt = prompt + _JSON_RETRY_SUFFIX

        # Codex tier: same prompt as Claude, single attempt, no JSON-retry.
        try:
            result = await codex_bridge.run_codex(
                prompt, timeout=float(settings.claude_timeout_seconds),
            )
            text = result.get("text", "") if isinstance(result, dict) else str(result)
            parsed = _try_parse_draft(text)
            if parsed is not None:
                return parsed, "codex"
            log.warning("blog_writer: Codex JSON invalid/missing body_md; falling back to Ollama")
        except Exception as e:
            log.warning("blog_writer: Codex call failed (%s); falling back to Ollama", e)

        # Re-render with the Ollama-tighter budget. llama3.1:8b degrades past
        # roughly 16-32k context, so we cap the full prompt at 20k by default.
        try:
            ollama_prompt = self._render_prompt(
                theme=theme, ranked=ranked,
                char_budget=settings.ollama_prompt_char_limit,
                per_source_cap=min(
                    settings.per_source_char_limit, 800,
                ),
            )
            result = await ollama_bridge.run_ollama(ollama_prompt, settings.ollama_model)
            text = result.get("text", "") if isinstance(result, dict) else str(result)
            parsed = _try_parse_draft(text)
            if parsed is not None:
                return parsed, "ollama"
            log.warning("blog_writer: Ollama JSON invalid/missing body_md")
        except Exception as e:
            log.warning("blog_writer: Ollama fallback also failed (%s)", e)

        return None, "none"

    # ───── citation post-processing ─────

    @staticmethod
    def _postprocess_citations(
        body_md: str, source_count: int,
    ) -> tuple[str, set[int]]:
        """Extract cited source indices and strip any out-of-range brackets.

        See spec §8.4. A bracket can hold one or more source references — the
        accepted forms are ``[source 2]`` and ``[source 2, source 5]``. If
        *any* integer in a bracket is out of range ``1..source_count``, the
        whole bracket is stripped from the body and a warning logged — mixed
        brackets are rare and stripping a single token is cleaner than
        surgically rewriting the in-range tail.

        The out-of-range sweep also covers brackets that *parse* as citation-
        shaped (``[source 99]``) but name a non-existent source. We do a
        second pass for those because the strict CITATION_RE matches them
        too but we still want to strip + warn.
        """
        cited: set[int] = set()

        def _replace(match: re.Match[str]) -> str:
            payload = match.group(1)
            nums = [int(n) for n in _NUM_RE.findall(payload)]
            out_of_range = any(n < 1 or n > source_count for n in nums)
            if out_of_range or not nums:
                log.warning(
                    "blog_writer: stripped out-of-range citation %r",
                    match.group(0),
                )
                return ""
            cited.update(nums)
            return match.group(0)

        cleaned = CITATION_RE.sub(_replace, body_md)
        # No whitespace post-processing — code blocks and deliberate markdown
        # indentation would be mangled. Citation stripping is the only
        # post-processing step per spec §8.4.
        return cleaned, cited


# ───── module-level helpers (not on the class) ─────


def candidate_count(window_days: int) -> int:
    """Sum of in-window notes + articles + roundtables. See spec §6.4.

    Shared by the HTTP POST endpoint and the ``mastisk blog`` CLI for a
    pre-enqueue empty-pool check — both callers should refuse to create a
    doomed job. The ``repo_idea_runs`` auxiliary is a tagger, not a
    standalone source, so it's not counted here.
    """
    cutoff_expr = f"datetime('now', '-{int(window_days)} days')"
    with connect() as conn:
        n_notes = conn.execute(
            f"""SELECT COUNT(*) AS n FROM notes
                WHERE deleted_at IS NULL
                  AND classified_at IS NOT NULL
                  AND (classified_at >= {cutoff_expr} OR created_at >= {cutoff_expr})"""
        ).fetchone()["n"]
        n_articles = conn.execute(
            f"""SELECT COUNT(*) AS n FROM articles WHERE updated_at >= {cutoff_expr}"""
        ).fetchone()["n"]
        n_rts = conn.execute(
            f"""SELECT COUNT(*) AS n FROM roundtables
                WHERE status = 'done' AND synthesis IS NOT NULL
                  AND finished_at >= {cutoff_expr}"""
        ).fetchone()["n"]
    return int(n_notes + n_articles + n_rts)


def _derive_blog_slug(title: str, bp_id: int, created_at: str | None) -> str:
    """``<YYYY-MM-DD>-<slugified title>``. Falls back to ``draft-<id>``.

    See spec §9.1. ``created_at`` is the DB row's timestamp; we parse the
    date prefix from it if possible, otherwise use today's date.
    """
    if created_at:
        try:
            date_part = str(created_at)[:10]
            # Validate YYYY-MM-DD shape
            datetime.strptime(date_part, "%Y-%m-%d")
        except (ValueError, TypeError):
            date_part = datetime.now().astimezone().strftime("%Y-%m-%d")
    else:
        date_part = datetime.now().astimezone().strftime("%Y-%m-%d")
    slug_part = slugify(title)[:60] if title else ""
    if not slug_part:
        slug_part = f"draft-{bp_id}"
    return f"{date_part}-{slug_part}"


def _unique_draft_path(base_slug: str) -> Path:
    """Find a non-colliding draft path under blog_drafts_dir.

    Mirrors the notes slug-collision retry (insert_note). Append -2, -3, ...
    up to -99. Beyond that, loudly log and return -99 anyway so we never
    silently overwrite.
    """
    drafts = blog_drafts_dir()
    drafts.mkdir(parents=True, exist_ok=True)
    candidate = drafts / f"{base_slug}.md"
    if not candidate.exists():
        return candidate
    for i in range(2, 100):
        candidate = drafts / f"{base_slug}-{i}.md"
        if not candidate.exists():
            return candidate
    log.warning("blog_writer: slug collision exhausted for %s; overwriting -99", base_slug)
    return drafts / f"{base_slug}-99.md"


def _build_frontmatter(
    *,
    bp: dict,
    bp_id: int,
    title: str,
    tags: list[str],
    ranked: list[dict],
    cited_ns: set[int],
    model_used: str,
    body_md: str,
) -> str:
    """Serialize the YAML frontmatter for the draft. See spec §9.2.

    We hand-roll the YAML for two reasons:
    1. The spec's ``sources:`` shape uses inline flow-mappings (`- { n: 1, ... }`)
       which yaml.dump doesn't produce naturally.
    2. No new dependency — yaml isn't currently imported by this module.
    """
    lines = ["---"]
    lines.append(f"blog_post_id: {bp_id}")
    if title:
        lines.append(f"title: {_yaml_quote(title)}")
    lines.append(f"created_at: {bp.get('created_at') or ''}")
    lines.append(f"updated_at: {datetime.now().astimezone().isoformat()}")
    lines.append(f"theme: {_yaml_quote(bp.get('theme') or '')}")
    lines.append(f"window_days: {bp.get('window_days')}")
    lines.append("status: done")
    if tags:
        tag_list = ", ".join(tags)
        lines.append(f"tags: [{tag_list}]")
    else:
        lines.append("tags: []")
    lines.append("sources:")
    for n, c in enumerate(ranked, start=1):
        entry_bits = [f"n: {n}", f"kind: {c['kind']}", f"ref: {_yaml_scalar(c['ref'])}"]
        if c.get("slug"):
            entry_bits.append(f"slug: {_yaml_quote(c['slug'])}")
        entry_bits.append(f"used: {'true' if n in cited_ns else 'false'}")
        if c.get("origin"):
            entry_bits.append(f"origin: {c['origin']}")
        lines.append("  - { " + ", ".join(entry_bits) + " }")
    lines.append(f"model: {model_used}")
    lines.append(f"word_count: {len(body_md.split())}")
    lines.append("---")
    return "\n".join(lines) + "\n"


def _yaml_quote(s: Any) -> str:
    """Emit a safely-quoted YAML scalar string. Doesn't handle embedded quotes
    elegantly — we escape double-quotes with a backslash, which is
    double-quoted YAML's spec.
    """
    text = str(s).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'


def _yaml_scalar(v: Any) -> str:
    """Write an int or quoted-string YAML scalar depending on the ref type."""
    if isinstance(v, int):
        return str(v)
    s = str(v)
    if s.isdigit():
        return s
    return _yaml_quote(s)
