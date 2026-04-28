"""Real graph data — nodes are articles, edges are links, clusters are kinds."""
from __future__ import annotations

from fastapi import APIRouter

from mastisk.db.queries import connect

router = APIRouter(tags=["graph"])

KIND_COLOR = {
    "Concept":   "var(--kind-concept)",
    "Entity":    "var(--kind-entity)",
    "Source":    "var(--kind-source)",
    "Synthesis": "var(--kind-synth)",
}


@router.get("/graph")
def graph():
    with connect() as conn:
        # backlinks_count / forwardlinks_count are maintained by the
        # links_ai / links_ad triggers (see schema.sql), so reading them is
        # cheaper than recounting via correlated subqueries on every request.
        articles = [dict(r) for r in conn.execute(
            """SELECT id, title, kind,
                      backlinks_count    AS backlinks,
                      forwardlinks_count AS forwardlinks
               FROM articles ORDER BY updated_at DESC"""
        )]
        edges = [dict(r) for r in conn.execute(
            "SELECT from_article, to_article, weight FROM links"
        )]

    nodes = [
        {
            "id":       a["id"],
            "title":    a["title"],
            "kind":     a["kind"],
            "color":    KIND_COLOR.get(a["kind"], "var(--kind-system)"),
            # Node size reflects importance (sum of degree). Hand-tuned baseline.
            "size":     10 + min(30, a["backlinks"] + a["forwardlinks"]),
            "degree":   a["backlinks"] + a["forwardlinks"],
        }
        for a in articles
    ]

    return {
        "nodes": nodes,
        "edges": edges,
        "clusters": [
            {"kind": k, "color": v, "count": sum(1 for n in nodes if n["kind"] == k)}
            for k, v in KIND_COLOR.items()
        ],
        "stats": {
            "pages":       len(nodes),
            "connections": len(edges),
        },
    }
