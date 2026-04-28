"""OpinionGapMiner — surfaces 1-2 weekly blog topics from genuine conflicts.

Sibling to TopicSuggester, but tuned for the *contradiction* signal rather
than the *crossing* signal. Where TopicSuggester looks for overlap (user
stake meets world signal on the same theme), this agent looks for
disagreement: a recent assertive note saying X meets a recent external
article claiming not-X. Because the signal is rarer than crossings, the
cadence is weekly, the world pool is restricted to ``Source`` articles
(Synthesis articles are the user's own claims and don't count as external
takes worth pushing back on), and the user pool is filtered to assertive
language only. Many weeks will produce zero topics — that's by design.

The agent is timer-driven, not job-driven. We override ``run_once`` and
self-time on the ``cadence_hours`` window so we don't generate twice in the
same week.
"""
from __future__ import annotations

import json
import logging
import re
from typing import ClassVar

from mastisk.agents.base import Agent
from mastisk.agents.blog_writer import STOP_WORDS
from mastisk.bridges import intelligence
from mastisk.db.queries import connect
from mastisk.settings import get_settings

log = logging.getLogger("mastisk.opinion_gap_miner")


# Markers that flag a note (or its derived summary) as carrying assertive
# language — a stake the user has taken, not a question or a neutral
# observation. Two regexes:
# - _PHRASE_RE matches multi-word phrasal markers with word boundaries so
#   "in fact" doesn't fire on "in factories" and "actually" doesn't fire on
#   names like "actuallya".
# - _FIRST_PERSON_VERB_RE catches commit-style past-tense claims, but ONLY
#   when anchored to a first-person subject ("I shipped X", "we replaced Y").
#   Without the anchor, third-person prose like "Anthropic shipped Claude
#   Code" would over-fire — that's noise, not the user's stake.
# Both are auditable lists; case-insensitive matching is on the regex.
_PHRASE_MARKERS: tuple[str, ...] = (
    "i think",
    "i disagree",
    "i don't agree",
    "is wrong",
    "is right",
    "actually",
    "in fact",
    "the truth",
    "the real",
    "contrary to",
    "the issue is",
    "the problem is",
    "i'd argue",
    "my take",
    "hot take",
    "unpopular",
)
_FIRST_PERSON_VERBS: tuple[str, ...] = (
    "shipped",
    "removed",
    "killed",
    "replaced",
    "deprecated",
    "deleted",
)
_PHRASE_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(m) for m in _PHRASE_MARKERS) + r")\b",
    re.IGNORECASE,
)
_FIRST_PERSON_VERB_RE = re.compile(
    r"\b(?:i|we|i've|we've)\s+(?:" + "|".join(_FIRST_PERSON_VERBS) + r")\b",
    re.IGNORECASE,
)


SUGGEST_PROMPT_TEMPLATE = """You are surfacing blog topics where the user's recent assertions CONTRADICT recent external claims. Pick the 1-2 strongest conflicts — real disagreements with substance, not just different topics, not just different angles. Return STRICT JSON only.

User identity (excerpt):
{identity_preamble}

Topical pairs (each: USER_ASSERTION <-> WORLD_CLAIM with overlap score). Look for genuine disagreement. Skip pairs where the two halves are merely on the same topic — there must be a clash.
{pairs_block}

Return JSON matching this schema exactly:
{{"topics": [
  {{"title": "<10-12 word title naming the disagreement concretely>",
   "hook": "<2-3 sentences: state the user's prior position, then the external claim, then why they disagree>",
   "angle": "<one sentence: the rebuttal the user could write>",
   "user_refs": [{{"kind": "note", "ref": <int>}}, ...],
   "world_refs": [{{"kind": "article", "ref": "<string-id>"}}, ...]}}
]}}

- Maximum 2 topics. Quality over quantity. If none of the pairs are genuine conflicts, return {{"topics": []}}.
- Many days will return zero topics — that's fine. A weak conflict surfaced is worse than no conflict.
- Refs must be drawn verbatim from the pairs provided — note refs are integers, article refs are kebab-case string ids. Do not invent.
- Strict JSON, no prose around it.
"""


class OpinionGapMiner(Agent):
    """Weekly opinion-gap miner. See module docstring."""

    name: ClassVar[str] = "opinion_gap_miner"
    tick_seconds: ClassVar[int] = 3600   # 1h tick — cheap; agent self-times
    cadence_hours: ClassVar[int] = 168   # once per week
    window_hours: ClassVar[int] = 168    # same 7-day lookback for both halves

    # Timer-driven, not job-driven. _handle would only be called if
    # _pick_job returned a job, which it never will for this agent.
    async def _handle(self, job: dict) -> None:  # pragma: no cover - never invoked
        raise NotImplementedError("opinion_gap_miner is timer-driven, not job-driven")

    async def run_once(self) -> None:
        """Tick. Return early if an 'opinion' suggestion was written within
        the cadence window; otherwise generate."""
        with connect() as conn:
            recent = conn.execute(
                f"""SELECT 1 FROM topic_suggestions
                    WHERE kind = 'opinion'
                      AND created_at >= datetime('now', '-{int(self.cadence_hours)} hours')
                    LIMIT 1"""
            ).fetchone()
        if recent is not None:
            return
        try:
            await self._generate_weekly()
        except Exception:
            log.exception("opinion_gap_miner: tick failed")

    # ───── core ─────

    async def _generate_weekly(self) -> None:
        """Pull assertive user signal + recent Source articles, find topical
        pairs, ask the LLM whether any genuinely conflict, validate, persist."""
        user_items = self._pull_user_assertions()
        if not user_items:
            log.info("opinion_gap_miner: no assertive user signal in last %sh; skipping",
                     self.window_hours)
            return
        world_items = self._pull_world_claims()
        if not world_items:
            log.info("opinion_gap_miner: no world claims in last %sh; skipping",
                     self.window_hours)
            return

        pairs = self._find_potential_conflicts(user_items, world_items)
        if not pairs:
            log.info("opinion_gap_miner: no topical pairs above threshold; skipping")
            return

        topics = await self._llm_suggest(pairs)
        if not topics:
            return

        valid_user_pairs: set[tuple[str, int | str]] = {
            ("note", u["ref"]) for u in user_items
        }
        valid_world_pairs: set[tuple[str, int | str]] = {
            ("article", w["ref"]) for w in world_items
        }
        valid_user_kinds = {"note"}
        valid_world_kinds = {"article"}

        validated = self._validate_topics(
            topics,
            valid_user_pairs=valid_user_pairs,
            valid_world_pairs=valid_world_pairs,
            valid_user_kinds=valid_user_kinds,
            valid_world_kinds=valid_world_kinds,
        )

        if not validated:
            log.info("opinion_gap_miner: LLM returned zero topics")
            return

        self._persist(validated)

    # ───── data pulls ─────

    @staticmethod
    def _has_assertion_marker(*texts: str) -> bool:
        """Word-boundary regex scan: phrasal markers OR first-person past-
        tense verbs. Both are case-insensitive. Joining once keeps the regex
        single-pass over the combined haystack."""
        haystack = "\n".join(t for t in texts if t)
        if not haystack:
            return False
        return bool(_PHRASE_RE.search(haystack) or _FIRST_PERSON_VERB_RE.search(haystack))

    def _pull_user_assertions(self) -> list[dict]:
        """Classified notes (incl. repo_ideator) updated in the last 7 days,
        filtered to those whose summary or first 1k chars of body contain
        at least one assertion marker."""
        settings = get_settings().blog
        cap = settings.opinion_gap_miner_max_user_items
        cutoff = f"datetime('now', '-{int(self.window_hours)} hours')"

        with connect() as conn:
            note_rows = conn.execute(
                f"""SELECT id, slug, summary, body, classification, tags_json,
                          created_at AS ts
                   FROM notes
                   WHERE deleted_at IS NULL
                     AND classified_at IS NOT NULL
                     AND created_at >= {cutoff}
                   ORDER BY created_at DESC"""
            ).fetchall()
            # Repo-ideator note id set, same approach as topic_suggester.
            ideator_rows = conn.execute(
                f"""SELECT DISTINCT CAST(value AS INTEGER) AS note_id
                    FROM repo_idea_runs, json_each(repo_idea_runs.note_ids_json)
                    WHERE repo_idea_runs.ideated_at >= {cutoff}
                      AND json_valid(repo_idea_runs.note_ids_json)"""
            ).fetchall()

        ideator_ids: set[int] = {
            r["note_id"] for r in ideator_rows if r["note_id"] is not None
        }
        items: list[dict] = []
        for r in note_rows:
            summary = r["summary"] or ""
            body = r["body"] or ""
            if not self._has_assertion_marker(summary, body[:1000]):
                continue
            try:
                tags = json.loads(r["tags_json"] or "[]")
            except json.JSONDecodeError:
                tags = []
            items.append({
                "kind": "note",
                "ref": r["id"],
                "slug": r["slug"],
                "title": summary[:120],
                "summary": summary,
                "body": body,
                "classification": r["classification"],
                "tags": tags,
                "ts": r["ts"],
                "origin": "repo_ideator" if r["id"] in ideator_ids else None,
            })
            if len(items) >= cap:
                break
        return items

    def _pull_world_claims(self) -> list[dict]:
        """Source articles updated in the last 7 days. Synthesis articles are
        the user's own cross-source claims, not external takes — they don't
        count as ``world_claims`` worth pushing back on, so we filter to
        ``kind='Source'`` only."""
        settings = get_settings().blog
        cap = settings.opinion_gap_miner_max_world_items
        cutoff = f"datetime('now', '-{int(self.window_hours)} hours')"

        with connect() as conn:
            rows = conn.execute(
                f"""SELECT id, slug, kind, title, summary, body_md,
                          updated_at AS ts
                   FROM articles
                   WHERE kind = 'Source'
                     AND updated_at >= {cutoff}
                   ORDER BY updated_at DESC
                   LIMIT ?""",
                (cap,),
            ).fetchall()
        return [
            {
                "kind": "article",
                "ref": r["id"],
                "slug": r["slug"],
                "article_kind": r["kind"],
                "title": r["title"] or "",
                "summary": r["summary"] or "",
                "body": r["body_md"] or "",
                "ts": r["ts"],
            }
            for r in rows
        ]

    # ───── potential conflicts ─────

    @staticmethod
    def _tokenize(text: str) -> set[str]:
        """Lowercase alphanumeric tokens, stoplist stripped, length > 1.
        Mirrors topic_suggester._tokenize so the keyword pre-rank scores
        the same way."""
        toks = re.findall(r"[a-z0-9]+", (text or "").lower())
        return {t for t in toks if t not in STOP_WORDS and len(t) > 1}

    def _find_potential_conflicts(
        self, user_items: list[dict], world_items: list[dict],
    ) -> list[dict]:
        """Score every (user, world) pair on topical overlap; return top-K
        with score > 1. Same shape and tiebreak as topic_suggester._find_crossings.
        The ``conflict?`` decision is the LLM's job — keyword overlap just
        narrows the candidate set to topically-adjacent pairs."""
        settings = get_settings().blog
        max_pairs = settings.opinion_gap_miner_max_pairs

        # Pre-tokenise once per item, keyed by ref, so we don't tokenise N*M
        # times. Parallel dicts keep the input shape clean (no _tokens
        # side-channel keys leaking back into the prompt block).
        user_toks: dict[int | str, tuple[set[str], set[str]]] = {
            u["ref"]: (
                self._tokenize(u.get("summary") or ""),
                self._tokenize((u.get("body") or "")[:1000]),
            )
            for u in user_items
        }
        world_toks: dict[int | str, tuple[set[str], set[str]]] = {
            w["ref"]: (
                self._tokenize(w.get("summary") or ""),
                self._tokenize((w.get("body") or "")[:1000]),
            )
            for w in world_items
        }

        scored: list[tuple[float, dict, dict]] = []
        for u in user_items:
            u_summary_toks, u_body_toks = user_toks[u["ref"]]
            for w in world_items:
                w_summary_toks, w_body_toks = world_toks[w["ref"]]
                overlap_summary = len(u_summary_toks & w_summary_toks)
                overlap_body = len(u_body_toks & w_body_toks)
                score = overlap_summary * 2 + overlap_body * 1
                if score > 1:
                    scored.append((score, u, w))

        # Score DESC, then user.ts DESC, then world.ts DESC for stable tiebreak.
        scored.sort(
            key=lambda t: (
                t[0],
                t[1].get("ts") or "",
                t[2].get("ts") or "",
            ),
            reverse=True,
        )
        return [
            {"score": s, "user": u, "world": w}
            for s, u, w in scored[:max_pairs]
        ]

    # ───── LLM ─────

    @staticmethod
    def _identity_preamble() -> str:
        """Identity files concatenated, with the leading '# About the user'
        H1 stripped — same pattern as topic_suggester._identity_preamble."""
        raw = Agent.load_identity()
        stripped = raw.removeprefix("# About the user\n").strip()
        return stripped or "(no identity captured yet)"

    @staticmethod
    def _format_pairs_block(pairs: list[dict]) -> str:
        """One numbered block per pair showing both halves with id/kind/
        title/summary so the LLM can cite refs back."""
        lines: list[str] = []
        for n, c in enumerate(pairs, start=1):
            u = c["user"]
            w = c["world"]
            score = c["score"]
            lines.append(f"### Pair {n} (score {score:.0f})")
            user_origin = (
                f" origin={u['origin']}" if u.get("origin") else ""
            )
            user_classification = (
                f" classification={u['classification']}"
                if u.get("classification") else ""
            )
            user_title = (u.get("title") or u.get("summary") or "")[:120]
            user_summary = (u.get("summary") or "")[:240].replace("\n", " ")
            lines.append(
                f"- USER_ASSERTION kind=note ref={u['ref']}"
                f"{user_classification}{user_origin}"
            )
            lines.append(f"  title: {user_title}")
            if user_summary:
                lines.append(f"  summary: {user_summary}")
            article_kind = w.get("article_kind") or "Source"
            world_title = (w.get("title") or "")[:120]
            world_summary = (w.get("summary") or "")[:240].replace("\n", " ")
            lines.append(
                f"- WORLD_CLAIM kind=article ref={w['ref']} article_kind={article_kind}"
            )
            lines.append(f"  title: {world_title}")
            if world_summary:
                lines.append(f"  summary: {world_summary}")
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    async def _llm_suggest(self, pairs: list[dict]) -> list[dict] | None:
        """Single Claude call. Returns the parsed topics list or None on
        transport / parse failure (caller logs and writes zero rows)."""
        prompt = SUGGEST_PROMPT_TEMPLATE.format(
            identity_preamble=self._identity_preamble(),
            pairs_block=self._format_pairs_block(pairs),
        )
        try:
            result, _ = await intelligence.run_intelligence(prompt, timeout_s=180)
            text = result.get("text", "") if isinstance(result, dict) else str(result)
            parsed = json.loads(text.strip())
        except Exception as e:
            log.warning("opinion_gap_miner: LLM call failed (%s)", e)
            return None
        if not isinstance(parsed, dict):
            log.warning("opinion_gap_miner: response is not a dict: %r", parsed)
            return None
        topics = parsed.get("topics")
        if not isinstance(topics, list):
            log.warning("opinion_gap_miner: 'topics' missing or not a list")
            return None
        return topics

    # ───── validation ─────

    def _validate_topics(
        self,
        topics: list,
        *,
        valid_user_pairs: set[tuple[str, int | str]],
        valid_world_pairs: set[tuple[str, int | str]],
        valid_user_kinds: set[str],
        valid_world_kinds: set[str],
    ) -> list[dict]:
        """Validate each topic independently. Survivors are returned;
        malformed topics are dropped with a warning naming the failure."""
        out: list[dict] = []
        # Bound the count: spec says "max 2".
        if len(topics) > 2:
            log.warning(
                "opinion_gap_miner: LLM returned %d topics; truncating to 2",
                len(topics),
            )
            topics = topics[:2]

        for idx, topic in enumerate(topics):
            cleaned = self._validate_one_topic(
                topic,
                idx=idx,
                valid_user_pairs=valid_user_pairs,
                valid_world_pairs=valid_world_pairs,
                valid_user_kinds=valid_user_kinds,
                valid_world_kinds=valid_world_kinds,
            )
            if cleaned is not None:
                out.append(cleaned)
        return out

    def _validate_one_topic(
        self,
        topic,
        *,
        idx: int,
        valid_user_pairs: set[tuple[str, int | str]],
        valid_world_pairs: set[tuple[str, int | str]],
        valid_user_kinds: set[str],
        valid_world_kinds: set[str],
    ) -> dict | None:
        """Return the cleaned topic dict, or None if any field is malformed.
        Logs a warning naming the failure so a single bad sibling is
        debuggable without dropping the whole batch."""
        if not isinstance(topic, dict):
            log.warning(
                "opinion_gap_miner: topic[%d] is not a dict: %r", idx, topic,
            )
            return None
        title = topic.get("title")
        if not isinstance(title, str):
            log.warning(
                "opinion_gap_miner: topic[%d] title %r is not a string", idx, title,
            )
            return None
        title = title.strip()
        if not (1 <= len(title) <= 200):
            log.warning(
                "opinion_gap_miner: topic[%d] title length %d out of [1,200]",
                idx, len(title),
            )
            return None
        hook = topic.get("hook")
        if not isinstance(hook, str):
            log.warning(
                "opinion_gap_miner: topic[%d] hook %r is not a string", idx, hook,
            )
            return None
        hook = hook.strip()
        if not (1 <= len(hook) <= 500):
            log.warning(
                "opinion_gap_miner: topic[%d] hook length %d out of [1,500]",
                idx, len(hook),
            )
            return None
        angle = topic.get("angle")
        if angle is not None:
            if not isinstance(angle, str):
                log.warning(
                    "opinion_gap_miner: topic[%d] angle %r is not a string/null",
                    idx, angle,
                )
                return None
            angle = angle.strip()
            if len(angle) > 500:
                log.warning(
                    "opinion_gap_miner: topic[%d] angle length %d > 500",
                    idx, len(angle),
                )
                return None
            if not angle:
                angle = None

        user_refs = topic.get("user_refs") or []
        world_refs = topic.get("world_refs") or []
        if not isinstance(user_refs, list) or not isinstance(world_refs, list):
            log.warning(
                "opinion_gap_miner: topic[%d] user_refs/world_refs must be lists",
                idx,
            )
            return None

        ok_user = self._check_refs(
            user_refs,
            valid_kinds=valid_user_kinds,
            valid_pairs=valid_user_pairs,
            label="user",
            topic_idx=idx,
        )
        if ok_user is None:
            return None
        ok_world = self._check_refs(
            world_refs,
            valid_kinds=valid_world_kinds,
            valid_pairs=valid_world_pairs,
            label="world",
            topic_idx=idx,
        )
        if ok_world is None:
            return None

        return {
            "title": title,
            "hook": hook,
            "angle": angle,
            "user_refs": ok_user,
            "world_refs": ok_world,
        }

    @staticmethod
    def _check_refs(
        refs: list,
        *,
        valid_kinds: set[str],
        valid_pairs: set[tuple[str, int | str]],
        label: str,
        topic_idx: int,
    ) -> list[dict] | None:
        """Validate a list of {kind, ref} entries. Returns the cleaned list,
        or None on any failure. Membership is a (kind, ref) tuple lookup
        against ``valid_pairs`` — catches cross-kind confusion (a 'note' ref
        that happens to match an article id) that flat id-set membership
        would miss."""
        cleaned: list[dict] = []
        for entry in refs:
            if not isinstance(entry, dict):
                log.warning(
                    "opinion_gap_miner: topic[%d] %s_ref entry not a dict: %r",
                    topic_idx, label, entry,
                )
                return None
            kind = entry.get("kind")
            ref = entry.get("ref")
            if not isinstance(kind, str) or kind not in valid_kinds:
                log.warning(
                    "opinion_gap_miner: topic[%d] %s_ref kind %r not in %s",
                    topic_idx, label, kind, valid_kinds,
                )
                return None
            if not isinstance(ref, (int, str)) or isinstance(ref, bool):
                log.warning(
                    "opinion_gap_miner: topic[%d] %s_ref ref %r is not int/str",
                    topic_idx, label, ref,
                )
                return None
            normalised: int | str = ref
            if kind == "note" and isinstance(ref, str):
                if not ref.isdigit():
                    log.warning(
                        "opinion_gap_miner: topic[%d] %s_ref ref %r not an int",
                        topic_idx, label, ref,
                    )
                    return None
                normalised = int(ref)
            if (kind, normalised) not in valid_pairs:
                log.warning(
                    "opinion_gap_miner: topic[%d] %s_ref (%r, %r) unknown "
                    "(not in pairs)",
                    topic_idx, label, kind, ref,
                )
                return None
            cleaned.append({"kind": kind, "ref": normalised})
        return cleaned

    # ───── persistence ─────

    def _persist(self, topics: list[dict]) -> None:
        """One INSERT per topic + one feed row per topic.

        INSERT OR IGNORE plays with the (kind, date(created_at), title)
        unique index — duplicate-titled rows from a racing process or an
        LLM hallucination collapse silently. Distinct-titled topics in one
        batch both land. Feed rows track persisted topics only."""
        persisted: list[dict] = []
        with connect() as conn:
            for topic in topics:
                refs = list(topic["user_refs"]) + list(topic["world_refs"])
                cur = conn.execute(
                    """INSERT OR IGNORE INTO topic_suggestions
                       (kind, title, hook, angle, source_refs_json)
                       VALUES ('opinion', ?, ?, ?, ?)""",
                    (
                        topic["title"],
                        topic["hook"],
                        topic["angle"],
                        json.dumps(refs),
                    ),
                )
                if cur.rowcount == 0:
                    log.warning(
                        "opinion_gap_miner: insert ignored — likely a same-day "
                        "row exists (kind='opinion', title=%r)",
                        topic["title"],
                    )
                    continue
                persisted.append(topic)
        for topic in persisted:
            # verb='surfaced' (vs topic_suggester's 'suggested') so a feed
            # consumer that wants to distinguish daily-suggestions from
            # weekly-opinion-gaps can do so on (agent, verb) without parsing
            # JSON. agent='opinion_gap_miner' alone already disambiguates,
            # but the verb makes the distinction visible at the row level.
            self.emit_feed(
                verb="surfaced",
                obj=topic["title"],
                kind="opinion-gap",
                payload={"hook": topic["hook"]},
            )
