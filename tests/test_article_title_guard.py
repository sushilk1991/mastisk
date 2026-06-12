from __future__ import annotations


def test_generated_article_title_is_truncated_before_storage(db):
    """Generated article titles over the storage cap are shortened deterministically."""
    from mastisk.db import queries as q

    assert q.MAX_GENERATED_ARTICLE_TITLE_CHARS == 70

    long_title = (
        "MTG Bench Found Models Judge a Turn Better Than They Play One. "
        "Everywhere Else This Week, the Judging Job Was Being Reassigned "
        "to a Tailwind Heuristic, a Workplace Norm, a Walked-Back Gate, or Nobody"
    )

    q.upsert_article(db, {
        "id": "too-long",
        "kind": "Synthesis",
        "title": long_title,
        "slug": "too-long",
        "aka": [],
        "summary": "",
        "body_md": "",
        "confidence": 0.6,
        "reading_minutes": 5,
        "updated_by": "Synthesizer",
        "vault_path": None,
    })

    row = db.execute("SELECT title FROM articles WHERE id='too-long'").fetchone()
    assert row is not None
    assert row["title"].endswith("...")
    assert len(row["title"]) <= q.MAX_GENERATED_ARTICLE_TITLE_CHARS
    assert row["title"] == (
        "MTG Bench Found Models Judge a Turn Better Than They Play One..."
    )
