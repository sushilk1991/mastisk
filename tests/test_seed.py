"""Demo seed tests: `seed-demo` documents itself as safe to re-run."""
from __future__ import annotations


def test_seed_twice_does_not_duplicate_rows(db, vault_tmp, data_tmp):
    from mastisk.db.seed import SAMPLE_FEED, seed

    seed()
    counts = {
        t: db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        for t in ("feed", "articles", "links", "sources", "article_sources")
    }
    assert counts["feed"] == len(SAMPLE_FEED)

    seed()
    for table, expected in counts.items():
        assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == expected, table
