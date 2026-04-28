"""TopicSuggester — surfaces 1-2 daily blog topics from crossings.

Looks at the user's recent personal work (classified notes + repo-ideator
notes from the last 48h) alongside the world's recent signal (Source articles
+ Synthesis articles updated in the last 48h). For each (user_item,
world_item) pair we score keyword overlap; the top crossings get handed to a
cheap LLM that writes 1-2 topic-seed rows into ``topic_suggestions``.

The agent is timer-driven, not job-driven. We override ``run_once`` directly
and self-time on the ``cadence_hours`` window so we don't generate twice in
the same day.
"""
from __future__ import annotations

import json
import logging
import re
from typing import ClassVar

from mastisk.agents.base import Agent
from mastisk.agents.blog_writer import STOP_WORDS
from mastisk.bridges import ollama_bridge
from mastisk.db.queries import connect
from mastisk.settings import get_settings

log = logging.getLogger("mastisk.topic_suggester")


SUGGEST_PROMPT_TEMPLATE = """You are surfacing blog topics from crossings between the user's recent work and the world's recent signal. Pick the 1-2 strongest topics — where the user has personal stakes that intersect with something happening externally. Return STRICT JSON only.

User identity (excerpt):
{identity_preamble}

Crossings (each: USER_ITEM <-> WORLD_ITEM with the overlap score):
{crossings_block}

Return JSON matching this schema exactly:
{{"topics": [
  {{"title": "<10-12 word title that names the crossing concretely>",
   "hook": "<1-2 sentences naming the user's stake AND the world signal>",
   "angle": "<one sentence: the writing angle to take>",
   "user_refs": [{{"kind": "note", "ref": <int>}}, ...],
   "world_refs": [{{"kind": "article", "ref": "<string-id>"}}, ...]}}
]}}

- Maximum 2 topics. Quality over quantity. If none of the crossings have a real personal stake, return {{"topics": []}}.
- title fills the user's blog theme field, so make it concrete (not "AI agents" but "Why our pod_available flag is the kind of witness Anthropic's Claude Code lacked").
- Refs must be drawn verbatim from the crossings provided — note refs are integers, article refs are kebab-case string ids. Do not invent or transform them.
- Strict JSON, no prose around it.
"""


class TopicSuggester(Agent):
    """Daily topic-suggester. See module docstring."""

    name: ClassVar[str] = "topic_suggester"
    tick_seconds: ClassVar[int] = 600  # 10 min — fast enough to recover from a missed wake-up
    cadence_hours: ClassVar[int] = 22   # generate once per ~day; slop covers DST + missed ticks
    window_hours: ClassVar[int] = 48    # how far back we look for both user and world items

    # Timer-driven, not job-driven: we override run_once and never enqueue
    # jobs for ourselves. The base-class abstract method still has to be
    # satisfied — _handle would only be called if _pick_job returned a job,
    # which it never will for this agent.
    async def _handle(self, job: dict) -> None:  # pragma: no cover - never invoked
        raise NotImplementedError("topic_suggester is timer-driven, not job-driven")

    async def run_once(self) -> None:
        """Tick. Return early if a 'daily' suggestion was written within the
        cadence window; otherwise generate."""
        with connect() as conn:
            recent = conn.execute(
                f"""SELECT 1 FROM topic_suggestions
                    WHERE kind = 'daily'
                      AND created_at >= datetime('now', '-{int(self.cadence_hours)} hours')
                    LIMIT 1"""
            ).fetchone()
        if recent is not None:
            return
        try:
            await self._generate_daily()
        except Exception:
            log.exception("topic_suggester: tick failed")

    # ───── core ─────

    async def _generate_daily(self) -> None:
        """Pull signal, find crossings, ask the LLM, validate, persist."""
        user_items = self._pull_user_signal()
        if not user_items:
            log.info("topic_suggester: no user signal in last %sh; skipping",
                     self.window_hours)
            return
        world_items = self._pull_world_signal()
        if not world_items:
            log.info("topic_suggester: no world signal in last %sh; skipping",
                     self.window_hours)
            return

        crossings = self._find_crossings(user_items, world_items)
        if not crossings:
            log.info("topic_suggester: no crossings above threshold; skipping")
            return

        topics = await self._llm_suggest(crossings)
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
            log.info("topic_suggester: LLM returned zero topics")
            return

        self._persist(validated)

    # ───── data pulls ─────

    def _pull_user_signal(self) -> list[dict]:
        """Classified notes (incl. repo_ideator) updated in the last 48h."""
        settings = get_settings().blog
        cap = settings.topic_suggester_max_user_items
        cutoff = f"datetime('now', '-{int(self.window_hours)} hours')"

        with connect() as conn:
            note_rows = conn.execute(
                f"""SELECT id, slug, summary, body, classification, tags_json,
                          created_at AS ts
                   FROM notes
                   WHERE deleted_at IS NULL
                     AND classified_at IS NOT NULL
                     AND created_at >= {cutoff}
                   ORDER BY created_at DESC
                   LIMIT ?""",
                (cap,),
            ).fetchall()
            # Repo-ideator note id set, same approach as BlogWriter._gather_sources.
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
            try:
                tags = json.loads(r["tags_json"] or "[]")
            except json.JSONDecodeError:
                tags = []
            items.append({
                "kind": "note",
                "ref": r["id"],
                "slug": r["slug"],
                "title": (r["summary"] or "")[:120],
                "summary": r["summary"] or "",
                "body": r["body"] or "",
                "classification": r["classification"],
                "tags": tags,
                "ts": r["ts"],
                "origin": "repo_ideator" if r["id"] in ideator_ids else None,
            })
        return items

    def _pull_world_signal(self) -> list[dict]:
        """Source + Synthesis articles updated in the last 48h."""
        settings = get_settings().blog
        cap = settings.topic_suggester_max_world_items
        cutoff = f"datetime('now', '-{int(self.window_hours)} hours')"

        with connect() as conn:
            rows = conn.execute(
                f"""SELECT id, slug, kind, title, summary, body_md,
                          updated_at AS ts
                   FROM articles
                   WHERE kind IN ('Source', 'Synthesis')
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

    # ───── crossings ─────

    @staticmethod
    def _tokenize(text: str) -> set[str]:
        """Lowercase alphanumeric tokens, stoplist stripped, length > 1.
        Mirrors BlogWriter._tokenize so crossings score the same way as the
        keyword pre-rank pass."""
        toks = re.findall(r"[a-z0-9]+", (text or "").lower())
        return {t for t in toks if t not in STOP_WORDS and len(t) > 1}

    def _find_crossings(
        self, user_items: list[dict], world_items: list[dict],
    ) -> list[dict]:
        """Score every (user, world) pair; return top-K with score > 1.

        Score = 2 * |user.summary tokens & world.summary tokens|
              +     |user.body[:1000] tokens & world.body[:1000] tokens|
        Summary overlap weighs more because summaries already carry the
        thesis-y signal; body overlap is a soft tiebreak.
        """
        settings = get_settings().blog
        max_crossings = settings.topic_suggester_max_crossings

        # Pre-tokenise once per item, keyed by ref, so we don't tokenise N*M
        # times and we don't mutate the caller's dicts. Side-channel cache
        # would leak `_summary_tokens` keys back through `crossings` into the
        # prompt block; parallel dicts keep the input shape clean.
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
            for s, u, w in scored[:max_crossings]
        ]

    # ───── LLM ─────

    @staticmethod
    def _identity_preamble() -> str:
        """Identity files concatenated, with the leading '# About the user'
        H1 stripped — same pattern as BlogWriter._identity_preamble."""
        raw = Agent.load_identity()
        stripped = raw.removeprefix("# About the user\n").strip()
        return stripped or "(no identity captured yet)"

    @staticmethod
    def _format_crossings_block(crossings: list[dict]) -> str:
        """One numbered block per crossing showing both halves with id/kind/
        title/summary so the LLM can cite refs back."""
        lines: list[str] = []
        for n, c in enumerate(crossings, start=1):
            u = c["user"]
            w = c["world"]
            score = c["score"]
            lines.append(f"### Crossing {n} (score {score:.0f})")
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
                f"- USER_ITEM kind=note ref={u['ref']}"
                f"{user_classification}{user_origin}"
            )
            lines.append(f"  title: {user_title}")
            if user_summary:
                lines.append(f"  summary: {user_summary}")
            article_kind = w.get("article_kind") or "Source"
            world_title = (w.get("title") or "")[:120]
            world_summary = (w.get("summary") or "")[:240].replace("\n", " ")
            lines.append(
                f"- WORLD_ITEM kind=article ref={w['ref']} article_kind={article_kind}"
            )
            lines.append(f"  title: {world_title}")
            if world_summary:
                lines.append(f"  summary: {world_summary}")
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    async def _llm_suggest(self, crossings: list[dict]) -> list[dict] | None:
        """Single Ollama call. Returns the parsed topics list or None on
        transport / parse failure (caller logs and writes zero rows)."""
        s = get_settings()
        prompt = SUGGEST_PROMPT_TEMPLATE.format(
            identity_preamble=self._identity_preamble(),
            crossings_block=self._format_crossings_block(crossings),
        )
        try:
            result = await ollama_bridge.run_ollama(
                prompt, s.summarize_model_cheap,
            )
            text = result.get("text", "") if isinstance(result, dict) else str(result)
            parsed = json.loads(text.strip())
        except Exception as e:
            log.warning("topic_suggester: LLM call failed (%s)", e)
            return None
        if not isinstance(parsed, dict):
            log.warning("topic_suggester: response is not a dict: %r", parsed)
            return None
        topics = parsed.get("topics")
        if not isinstance(topics, list):
            log.warning("topic_suggester: 'topics' missing or not a list")
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
        malformed topics are dropped with a warning naming the failure.

        With the 22h cadence, refusing the whole batch on one bad sibling
        meant a full day without suggestions. Per-topic skip lets the
        valid topics through; the all-bad case returns [] and the caller
        already handles empty gracefully (no cadence-blocking row written).
        """
        out: list[dict] = []
        # Bound the count: spec says "max 2".
        if len(topics) > 2:
            log.warning(
                "topic_suggester: LLM returned %d topics; truncating to 2",
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
                "topic_suggester: topic[%d] is not a dict: %r", idx, topic,
            )
            return None
        title = topic.get("title")
        if not isinstance(title, str):
            log.warning(
                "topic_suggester: topic[%d] title %r is not a string", idx, title,
            )
            return None
        title = title.strip()
        if not (1 <= len(title) <= 200):
            log.warning(
                "topic_suggester: topic[%d] title length %d out of [1,200]",
                idx, len(title),
            )
            return None
        hook = topic.get("hook")
        if not isinstance(hook, str):
            log.warning(
                "topic_suggester: topic[%d] hook %r is not a string", idx, hook,
            )
            return None
        hook = hook.strip()
        if not (1 <= len(hook) <= 500):
            log.warning(
                "topic_suggester: topic[%d] hook length %d out of [1,500]",
                idx, len(hook),
            )
            return None
        angle = topic.get("angle")
        if angle is not None:
            if not isinstance(angle, str):
                log.warning(
                    "topic_suggester: topic[%d] angle %r is not a string/null",
                    idx, angle,
                )
                return None
            angle = angle.strip()
            if len(angle) > 500:
                log.warning(
                    "topic_suggester: topic[%d] angle length %d > 500",
                    idx, len(angle),
                )
                return None
            if not angle:
                angle = None

        user_refs = topic.get("user_refs") or []
        world_refs = topic.get("world_refs") or []
        if not isinstance(user_refs, list) or not isinstance(world_refs, list):
            log.warning(
                "topic_suggester: topic[%d] user_refs/world_refs must be lists",
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
        or None on any failure (which makes the topic itself a skip).

        Membership is a (kind, ref) tuple lookup against `valid_pairs` —
        catches cross-kind confusion (a 'note' ref that happens to match an
        article id) that simple id-set membership would miss.
        """
        cleaned: list[dict] = []
        for entry in refs:
            if not isinstance(entry, dict):
                log.warning(
                    "topic_suggester: topic[%d] %s_ref entry not a dict: %r",
                    topic_idx, label, entry,
                )
                return None
            kind = entry.get("kind")
            ref = entry.get("ref")
            if not isinstance(kind, str) or kind not in valid_kinds:
                log.warning(
                    "topic_suggester: topic[%d] %s_ref kind %r not in %s",
                    topic_idx, label, kind, valid_kinds,
                )
                return None
            if not isinstance(ref, (int, str)) or isinstance(ref, bool):
                log.warning(
                    "topic_suggester: topic[%d] %s_ref ref %r is not int/str",
                    topic_idx, label, ref,
                )
                return None
            # Notes are int ids; articles are str ids. Coerce digit-strings
            # to int when the kind expects an int (note); leave string
            # article refs alone. Anything else is rejected by the
            # tuple-membership check below.
            normalised: int | str = ref
            if kind == "note" and isinstance(ref, str):
                if not ref.isdigit():
                    log.warning(
                        "topic_suggester: topic[%d] %s_ref ref %r not an int",
                        topic_idx, label, ref,
                    )
                    return None
                normalised = int(ref)
            if (kind, normalised) not in valid_pairs:
                log.warning(
                    "topic_suggester: topic[%d] %s_ref (%r, %r) unknown "
                    "(not in crossings)",
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
        LLM hallucination collapse silently. Two genuinely-different
        topics in one batch have different titles and both land. We log
        + skip the feed row for ignored inserts so the user-visible feed
        stays in sync with what's actually persisted.
        """
        persisted: list[dict] = []
        with connect() as conn:
            for topic in topics:
                refs = list(topic["user_refs"]) + list(topic["world_refs"])
                cur = conn.execute(
                    """INSERT OR IGNORE INTO topic_suggestions
                       (kind, title, hook, angle, source_refs_json)
                       VALUES ('daily', ?, ?, ?, ?)""",
                    (
                        topic["title"],
                        topic["hook"],
                        topic["angle"],
                        json.dumps(refs),
                    ),
                )
                if cur.rowcount == 0:
                    log.warning(
                        "topic_suggester: insert ignored — likely a same-day "
                        "row exists (kind='daily', title=%r)",
                        topic["title"],
                    )
                    continue
                persisted.append(topic)
        for topic in persisted:
            self.emit_feed(
                verb="suggested",
                obj=topic["title"],
                kind="topic",
                payload={"hook": topic["hook"]},
            )
