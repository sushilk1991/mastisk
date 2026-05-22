"""Real graph data — nodes are articles, edges are links, clusters are kinds."""
from __future__ import annotations

import json
import time

from fastapi import APIRouter, Response

from mastisk.db.queries import connect

router = APIRouter(tags=["graph"])

KIND_COLOR = {
    "Concept":   "var(--kind-concept)",
    "Entity":    "var(--kind-entity)",
    "Source":    "var(--kind-source)",
    "Synthesis": "var(--kind-synth)",
}

GRAPH_CACHE_TTL_SEC = 30
_graph_cache: dict[str, object] = {"expires_at": 0.0, "full": b"", "compact": b""}


def _cached_response(key: str) -> Response | None:
    cached_body = _graph_cache.get(key)
    if cached_body and time.monotonic() < float(_graph_cache.get("expires_at", 0.0)):
        return Response(
            content=cached_body,
            media_type="application/json",
            headers={"Cache-Control": f"private, max-age={GRAPH_CACHE_TTL_SEC}"},
        )
    return None


@router.get("/graph")
def graph():
    cached = _cached_response("full")
    if cached:
        return cached
    _refresh_cache("full")
    return _cached_response("full") or Response(content=b"{}", media_type="application/json")


@router.get("/graph/compact")
def graph_compact():
    cached = _cached_response("compact")
    if cached:
        return cached
    _refresh_cache("compact")
    return _cached_response("compact") or Response(content=b"{}", media_type="application/json")


def _refresh_cache(format_: str) -> None:
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

    kind_index = {kind: i for i, kind in enumerate(KIND_COLOR)}
    clusters = {k: 0 for k in KIND_COLOR}
    nodes = [] if format_ == "full" else None
    compact_nodes = [] if format_ == "compact" else None
    node_index = {}
    for idx, a in enumerate(articles):
        degree = a["backlinks"] + a["forwardlinks"]
        kind = a["kind"]
        size = 10 + min(30, degree)
        if nodes is not None:
            nodes.append({
                "id":       a["id"],
                "title":    a["title"],
                "kind":     kind,
                "color":    KIND_COLOR.get(kind, "var(--kind-system)"),
                # Node size reflects importance (sum of degree). Hand-tuned baseline.
                "size":     size,
                "degree":   degree,
            })
        node_index[a["id"]] = idx
        if compact_nodes is not None:
            compact_nodes.append([a["id"], a["title"], kind_index.get(kind, -1), size, degree])
        if kind in clusters:
            clusters[kind] += 1

    cluster_list = [
        {"kind": k, "color": v, "count": clusters[k]}
        for k, v in KIND_COLOR.items()
    ]
    stats = {
        "pages":       len(articles),
        "connections": len(edges),
    }
    if nodes is not None:
        _graph_cache["full"] = json.dumps({
            "nodes": nodes,
            "edges": edges,
            "clusters": cluster_list,
            "stats": stats,
        }, separators=(",", ":")).encode()
    if compact_nodes is not None:
        compact_edges = []
        for edge in edges:
            source = node_index.get(edge["from_article"])
            target = node_index.get(edge["to_article"])
            if source is not None and target is not None:
                compact_edges.append([source, target, edge["weight"]])
        _graph_cache["compact"] = json.dumps({
            "v": 1,
            "kinds": cluster_list,
            "nodes": compact_nodes,
            "edges": compact_edges,
            "stats": stats,
        }, separators=(",", ":")).encode()
    _graph_cache["expires_at"] = time.monotonic() + GRAPH_CACHE_TTL_SEC
