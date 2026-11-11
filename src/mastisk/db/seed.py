"""Seed the DB with the design's sample content so the UI renders on day 1.

Ported from design-source/project/data.js. Idempotent — safe to re-run.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from mastisk.db.queries import (
    connect,
    init_schema,
    pin_article,
    replace_sections,
    set_related,
    txn,
    upsert_article,
)

# ── the Test-time compute article (the one the design reader renders by default) ──
TTC_ARTICLE = {
    "id": "ttc",
    "kind": "Concept",
    "title": "Test-time compute",
    "slug": "test-time-compute",
    "aka": ["Inference-time scaling", "Search at inference"],
    "summary": (
        "A scaling axis that trades extra compute at inference time — typically via search, "
        "sampling, or verification — for higher capability without training a larger model. "
        "Empirically validated by the o-series of models and AlphaProof; theoretical grounding "
        "via process reward models and verifier-generator gaps."
    ),
    "confidence": 0.86,
    "reading_minutes": 7,
    "updated_by": "Compiler",
    "body_md": "",  # sections hold the real content
}

TTC_SECTIONS = [
    {
        "h": "TL;DR",
        "kind": "callout",
        "body": "Instead of scaling parameters or data, you scale the <em>number of forward passes per query</em> — through best-of-N, MCTS, self-consistency, or verifier-guided search. Cheap models with lots of test-time compute can match expensive models with little. The frontier in 2025–26 is figuring out <em>what</em> to spend that compute on.",
    },
    {
        "h": "Mechanism",
        "body": 'Three families dominate: <span class="link" data-target="sampling">parallel sampling</span> with majority vote or learned verifier; <span class="link" data-target="mcts">tree search</span> over partial generations (MCTS-style); and <span class="link" data-target="refine">iterative refinement</span> where a model critiques and edits its own draft. Each family has a different scaling curve — parallel sampling plateaus quickly; tree search keeps paying off; refinement is bounded by the critique signal.',
    },
    {
        "h": "Empirical evidence",
        "body": 'The clearest demo is <span class="link" data-target="o-series">OpenAI\'s o-series</span>, where allowing minutes-long reasoning lifts MATH and competition coding scores by 20+ points without changing the base model. <span class="link" data-target="alphaproof">DeepMind\'s AlphaProof</span> shows the same for formal math: a small Gemini policy paired with a Lean verifier matched IMO-medalist level given a day of compute per problem.',
    },
    {
        "h": "Why it matters",
        "body": 'If inference-time scaling holds, the economic shape of the field changes. Smaller, cheaper base models become viable substrates; the differentiator is the <em>search procedure</em> and the <em>verifier</em>. This is also where <span class="link" data-target="rl-llm">RL on LLMs</span> re-enters: process reward models trained on traces let you prune the search tree.',
    },
    {
        "h": "Open questions",
        "kind": "open",
        "body": "(1) Are there domains where search doesn't help — e.g. tasks without checkable ground truth? (2) How do you learn a verifier without a stronger generator? (3) Does test-time compute scale across modalities, or is it text-only?",
    },
]

TTC_RELATED = [
    {"id": "rl-llm",       "label": "RL + LLMs",                 "weight": 0.92},
    {"id": "prm",          "label": "Process reward models",      "weight": 0.88},
    {"id": "alphaproof",   "label": "AlphaProof",                 "weight": 0.74},
    {"id": "o-series",     "label": "o-series (OpenAI)",          "weight": 0.71},
    {"id": "mcts",         "label": "MCTS in language models",    "weight": 0.65},
    {"id": "verifier-gap", "label": "Verifier-generator gap",     "weight": 0.61},
]

# Placeholder articles so the "related" links don't dangle
RELATED_STUBS = [
    {"id": "rl-llm", "kind": "Concept", "title": "RL + LLMs", "slug": "rl-llms",
     "summary": "Reinforcement learning applied to large language models — from RLHF to process reward models.",
     "confidence": 0.78, "reading_minutes": 5, "body_md": "*stub — will be compiled from sources*"},
    {"id": "prm", "kind": "Concept", "title": "Process reward models", "slug": "process-reward-models",
     "summary": "Verifiers trained on intermediate reasoning traces rather than final answers.",
     "confidence": 0.72, "reading_minutes": 4, "body_md": "*stub*"},
    {"id": "alphaproof", "kind": "Entity", "title": "AlphaProof", "slug": "alphaproof",
     "summary": "DeepMind's 2024 formal-math system pairing a Gemini policy with a Lean verifier.",
     "confidence": 0.82, "reading_minutes": 3, "body_md": "*stub*"},
    {"id": "o-series", "kind": "Entity", "title": "o-series (OpenAI)", "slug": "o-series",
     "summary": "OpenAI's reasoning models (o1, o3) that trade latency for accuracy.",
     "confidence": 0.8, "reading_minutes": 3, "body_md": "*stub*"},
    {"id": "mcts", "kind": "Concept", "title": "MCTS in language models", "slug": "mcts-in-llms",
     "summary": "Monte Carlo tree search adapted for token-level generation.",
     "confidence": 0.7, "reading_minutes": 5, "body_md": "*stub*"},
    {"id": "verifier-gap", "kind": "Concept", "title": "Verifier-generator gap", "slug": "verifier-generator-gap",
     "summary": "The empirical observation that verifying a solution is easier than generating one.",
     "confidence": 0.65, "reading_minutes": 3, "body_md": "*stub*"},
]

# More wiki starters — match the sidebar from data.js
MORE_ARTICLES = [
    {"id": "ssm", "kind": "Concept", "title": "State-space models", "slug": "state-space-models",
     "summary": "Linear-recurrence sequence models (S4, Mamba) that scale with sequence length.",
     "confidence": 0.68, "reading_minutes": 5, "body_md": "*stub*"},
    {"id": "world", "kind": "Concept", "title": "World models", "slug": "world-models",
     "summary": "Learned simulators of environments, spanning RL agents, video generation, and perception.",
     "confidence": 0.7, "reading_minutes": 6, "body_md": "*stub*"},
    {"id": "mech", "kind": "Concept", "title": "Mech interp", "slug": "mechanistic-interpretability",
     "summary": "Reverse-engineering neural network computations into human-readable mechanisms.",
     "confidence": 0.73, "reading_minutes": 6, "body_md": "*stub*"},
    {"id": "karpathy", "kind": "Entity", "title": "Andrej Karpathy", "slug": "andrej-karpathy",
     "summary": "Founding OpenAI member, ex-Tesla, now independent.",
     "confidence": 0.9, "reading_minutes": 2, "body_md": "*stub*"},
    {"id": "sutton", "kind": "Entity", "title": "Richard Sutton", "slug": "richard-sutton",
     "summary": "Pioneer of reinforcement learning; co-author of the canonical textbook.",
     "confidence": 0.9, "reading_minutes": 2, "body_md": "*stub*"},
    {"id": "anthropic", "kind": "Entity", "title": "Anthropic", "slug": "anthropic",
     "summary": "AI safety company; maker of Claude.",
     "confidence": 0.9, "reading_minutes": 2, "body_md": "*stub*"},
    {"id": "deepmind", "kind": "Entity", "title": "DeepMind", "slug": "deepmind",
     "summary": "Google's research lab for artificial general intelligence.",
     "confidence": 0.9, "reading_minutes": 2, "body_md": "*stub*"},
    {"id": "dwarkesh", "kind": "Source", "title": "Dwarkesh × Sutton", "slug": "dwarkesh-sutton-2025",
     "summary": "Long-form interview on continual learning and the limits of next-token prediction.",
     "confidence": 0.85, "reading_minutes": 3, "body_md": "*stub*"},
    {"id": "wiki-gist", "kind": "Source", "title": "karpathy/llm-wiki.md", "slug": "karpathy-llm-wiki",
     "summary": "Karpathy's gist proposing an LLM-maintained personal wiki.",
     "confidence": 0.85, "reading_minutes": 3, "body_md": "*stub*"},
    {"id": "syn-rl", "kind": "Synthesis", "title": "RL + LLMs: 5 takes", "slug": "rl-llms-5-takes",
     "summary": "Cross-source synthesis of disagreements across Sutton, Karpathy, LeCun, Hassabis, Altman.",
     "confidence": 0.7, "reading_minutes": 8, "body_md": "*stub*"},
    {"id": "syn-mem", "kind": "Synthesis", "title": "What is agent memory?", "slug": "agent-memory-synthesis",
     "summary": "Seven wiki pages use the phrase with three incompatible definitions. Flagged by Linter.",
     "confidence": 0.55, "reading_minutes": 6, "body_md": "*stub*"},
]

PINNED_IDS = ["ttc", "rl-llm", "syn-rl"]

# Sources used by the TTC article — shows in the bottom "Sources used in this page" block
TTC_SOURCES = [
    ("src-ttc-1", "blog",    "Karpathy — Test-time compute is underrated",    "2026-04-12", "https://karpathy.github.io/ttc"),
    ("src-ttc-2", "paper",   "Snell et al. — Scaling LLM Test-Time Compute",  "2024-08-01", "https://arxiv.org/abs/2408.03314"),
    ("src-ttc-3", "podcast", "Latent Space — Inference scaling, ep. 184",      "2026-03-28", "https://latent.space/p/ep184"),
    ("src-ttc-4", "youtube", "Yannic Kilcher — o1 deep-dive",                  "2024-09-15", "https://youtu.be/abc"),
    ("src-ttc-5", "blog",    "DeepMind — AlphaProof technical post",           "2024-07-25", "https://deepmind.google/alphaproof"),
]

# Seed feed — the 24/7 ticker
SAMPLE_FEED = [
    ("scout",        "clipped",     'Karpathy — "LLM Wiki, revisited"',       "blog",     7),
    ("listener",     "transcribed", "Dwarkesh × Sutton — Continual learning", "podcast", 12),
    ("compiler",     "merged",      "World Models ↔ Predictive Coding",        "concept",  4),
    ("synthesizer",  "drafted",     "Synthesis: 5 takes on RL + LLMs",         "synthesis", 9),
    ("scout",        "watched",     "Lex × Hassabis — Project Astra (1h 42m)", "youtube", 14),
    ("linter",       "fixed",       "3 broken backlinks in /concepts",         "system",   3),
    ("compiler",     "wrote",       "Test-time compute (new entity page)",     "concept",  6),
    ("scout",        "queued",      "12 new items from Hacker News digest",    "system",   0),
    ("synthesizer",  "linked",      "Mamba ↔ State-space models ↔ S4",        "concept",  5),
    ("listener",     "transcribed", "Latent Space — RL renaissance",           "podcast",  8),
]


def seed() -> dict:
    """Create schema and populate with sample content. Returns count summary."""
    conn = connect()
    init_schema(conn)

    with txn(conn):
        # Insert all articles first so foreign keys resolve
        upsert_article(conn, TTC_ARTICLE)
        for a in RELATED_STUBS + MORE_ARTICLES:
            upsert_article(conn, a)

        # Now sections + relations
        replace_sections(conn, "ttc", TTC_SECTIONS)
        set_related(conn, "ttc", TTC_RELATED)

        # Pinned
        for pid in PINNED_IDS:
            pin_article(conn, pid)

        # Back-link rows — reverse of ttc's related, so the rail has real data
        for r in TTC_RELATED[:3]:
            conn.execute(
                "INSERT OR IGNORE INTO links (from_article, to_article, weight, snippet) VALUES (?, ?, ?, ?)",
                (r["id"], "ttc", r["weight"],
                 "…the strongest empirical case for [[Test-time compute]] appears in…"),
            )

        # Sources used by TTC
        for sid, kind, title, date, url in TTC_SOURCES:
            conn.execute(
                "INSERT OR IGNORE INTO sources (id, kind, title, published_at, url) VALUES (?, ?, ?, ?, ?)",
                (sid, kind, title, date, url),
            )
            conn.execute(
                "INSERT OR IGNORE INTO article_sources (article_id, source_id) VALUES (?, ?)",
                ("ttc", sid),
            )

        # Feed entries — space them out by minutes so 't' values vary. The feed
        # table has no natural key, so skip rows a previous seed already wrote.
        now = datetime.utcnow()
        for i, (agent, verb, obj, kind, touched) in enumerate(SAMPLE_FEED):
            ts = now - timedelta(minutes=i * 7 + 2)
            conn.execute(
                """INSERT INTO feed (ts, agent, verb, obj, kind, touched_pages)
                   SELECT ?, ?, ?, ?, ?, ?
                   WHERE NOT EXISTS (
                     SELECT 1 FROM feed WHERE agent = ? AND verb = ? AND obj = ? AND kind IS ?
                   )""",
                (ts.isoformat(sep=" "), agent, verb, obj, kind, touched, agent, verb, obj, kind),
            )

    conn.close()
    return {"articles": 1 + len(RELATED_STUBS) + len(MORE_ARTICLES), "feed": len(SAMPLE_FEED)}
