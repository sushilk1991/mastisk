from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(vault_tmp, data_tmp, db):
    from mastisk.settings import reload_settings
    reload_settings()
    from mastisk.app import create_app
    app = create_app()
    with TestClient(app) as c:
        yield c


def _fake_meta(owner, name):
    return {
        "slug": f"{owner}/{name}", "owner": owner, "name": name,
        "display_name": f"{owner}/{name}", "description": "d",
        "default_branch": "main", "is_private": 0,
        "stars_count": 10, "forks_count": 2,
    }


def test_add_repo(client, db):
    async def fake_meta(o, n): return _fake_meta(o, n)
    with patch("mastisk.routes.repos_route.github_bridge.fetch_repo_metadata", side_effect=fake_meta):
        r = client.post("/api/repos", json={"slug": "Anthropics/claude-code"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["slug"] == "anthropics/claude-code"
    # DB row
    row = db.execute("SELECT * FROM repos WHERE slug='anthropics/claude-code'").fetchone()
    assert row is not None
    # Poll job enqueued
    jrow = db.execute("SELECT 1 FROM jobs WHERE agent='github_poller'").fetchone()
    assert jrow is not None


def test_add_repo_404_for_unknown(client):
    from mastisk.bridges import github_bridge
    async def raise_nf(o, n): raise github_bridge.GithubNotFound("nope")
    with patch("mastisk.routes.repos_route.github_bridge.fetch_repo_metadata", side_effect=raise_nf):
        r = client.post("/api/repos", json={"slug": "nope/nope"})
    assert r.status_code == 404


def test_add_repo_422_for_bad_slug(client):
    r = client.post("/api/repos", json={"slug": "no-slash"})
    assert r.status_code == 422


def test_list_repos_empty(client):
    r = client.get("/api/repos")
    assert r.status_code == 200
    assert r.json() == []


def test_list_repos_with_one(client, db):
    db.execute("INSERT INTO repos (slug, owner, name, display_name) VALUES ('a/b', 'a', 'b', 'a/b')")
    r = client.get("/api/repos")
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 1
    assert rows[0]["slug"] == "a/b"


def test_get_repo_detail(client, db):
    db.execute("INSERT INTO repos (slug, owner, name, display_name, description, context_md) VALUES ('a/b', 'a', 'b', 'a/b', 'd', '# ctx')")
    r = client.get("/api/repos/a/b")
    assert r.status_code == 200
    body = r.json()
    assert body["slug"] == "a/b"
    assert body["context_md"] == "# ctx"


def test_delete_repo(client, db):
    db.execute("INSERT INTO repos (slug, owner, name) VALUES ('a/b', 'a', 'b')")
    r = client.delete("/api/repos/a/b")
    assert r.status_code == 204
    row = db.execute("SELECT deleted_at FROM repos WHERE slug='a/b'").fetchone()
    assert row["deleted_at"] is not None


def test_poll_now_enqueues(client, db):
    db.execute("INSERT INTO repos (slug, owner, name) VALUES ('a/b', 'a', 'b')")
    r = client.post("/api/repos/a/b/poll-now")
    assert r.status_code == 202
    jrow = db.execute("SELECT 1 FROM jobs WHERE agent='github_poller'").fetchone()
    assert jrow is not None


def test_ideate_now_enqueues(client, db):
    db.execute("INSERT INTO repos (slug, owner, name) VALUES ('a/b', 'a', 'b')")
    r = client.post("/api/repos/a/b/ideate-now")
    assert r.status_code == 202
    jrow = db.execute("SELECT 1 FROM jobs WHERE agent='github_ideator'").fetchone()
    assert jrow is not None


def test_ideate_now_404_on_unknown(client):
    r = client.post("/api/repos/gone/gone/ideate-now")
    assert r.status_code == 404


def test_add_repo_404_message_varies_by_pat_config(client, monkeypatch):
    """When no PAT is configured, the 404 hints about private repos."""
    from mastisk.bridges import github_bridge
    async def raise_nf(o, n): raise github_bridge.GithubNotFound("nope")
    with patch("mastisk.routes.repos_route.github_bridge.fetch_repo_metadata", side_effect=raise_nf):
        # Ensure no PAT
        monkeypatch.setenv("MASTISK_GITHUB_PAT", "")
        # Reload settings so the empty PAT takes effect (note: pydantic-settings caches;
        # reload_settings() clears the lru_cache)
        from mastisk.settings import reload_settings
        reload_settings()
        r = client.post("/api/repos", json={"slug": "nope/nope"})
    assert r.status_code == 404
    # Message must suggest PAT when none is set
    assert "PAT" in r.json()["detail"]


def test_add_repo_404_message_with_pat(client, monkeypatch):
    """When a PAT is configured, the 404 implies the PAT may lack access."""
    from mastisk.bridges import github_bridge
    async def raise_nf(o, n): raise github_bridge.GithubNotFound("nope")
    monkeypatch.setenv("MASTISK_GITHUB_PAT", "ghp_fake_for_test")
    # pydantic-settings may or may not read this env; if not, patch settings directly:
    from unittest.mock import patch as _patch

    from mastisk.settings import get_settings
    s = get_settings()
    with _patch.object(s.github, "pat", "ghp_fake_for_test"):
        with patch("mastisk.routes.repos_route.github_bridge.fetch_repo_metadata", side_effect=raise_nf):
            r = client.post("/api/repos", json={"slug": "nope/nope"})
    assert r.status_code == 404
    # Should mention "doesn't have access" or similar, not "add a PAT"
    body = r.json()["detail"]
    assert "access" in body or "doesn't have" in body


def test_add_local_repo_endpoint(client, db, tmp_path):
    import subprocess
    p = tmp_path / "myproj"
    p.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=p, check=True)
    (p / "README.md").write_text("# x")
    subprocess.run(["git", "-c", "user.email=x@x.x", "-c", "user.name=x", "add", "."], cwd=p, check=True)
    subprocess.run(["git", "-c", "user.email=x@x.x", "-c", "user.name=x", "commit", "-q", "-m", "i"], cwd=p, check=True)

    r = client.post("/api/repos/local", json={"path": str(p)})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["slug"].startswith("local:")
    assert body["local_path"] == str(p.resolve())

    # DB row + poll job
    assert db.execute("SELECT 1 FROM repos WHERE slug = ?", (body["slug"],)).fetchone()
    assert db.execute("SELECT 1 FROM jobs WHERE agent='github_poller'").fetchone()


def test_add_local_repo_rejects_non_git_dir(client, tmp_path):
    # Directory exists but no .git
    (tmp_path / "plain").mkdir()
    r = client.post("/api/repos/local", json={"path": str(tmp_path / "plain")})
    assert r.status_code == 422
    assert "not a git repo" in r.json()["detail"]


def test_add_local_repo_rejects_missing_dir(client, tmp_path):
    r = client.post("/api/repos/local", json={"path": str(tmp_path / "nope")})
    assert r.status_code == 422


def test_get_repo_by_slug_github(client, db):
    db.execute(
        "INSERT INTO repos (slug, source_type, owner, name, display_name) "
        "VALUES ('a/b', 'github', 'a', 'b', 'a/b')"
    )
    # `{slug:path}` accepts the slash directly
    r = client.get("/api/repos/by-slug/a/b")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["slug"] == "a/b"
    assert body["source_type"] == "github"


def test_get_repo_by_slug_local(client, db, tmp_path):
    slug = f"local:{tmp_path.resolve()}"
    db.execute(
        """INSERT INTO repos (slug, source_type, owner, name, display_name, local_path)
           VALUES (?, 'local', 'local', 'tmp', 'local:tmp', ?)""",
        (slug, str(tmp_path)),
    )
    r = client.get(f"/api/repos/by-slug/{slug}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["slug"] == slug
    assert body["source_type"] == "local"
    assert body["local_path"] == str(tmp_path)


def test_delete_repo_by_slug(client, db, tmp_path):
    slug = f"local:{tmp_path.resolve()}"
    db.execute(
        """INSERT INTO repos (slug, source_type, owner, name, display_name, local_path)
           VALUES (?, 'local', 'local', 'tmp', 'local:tmp', ?)""",
        (slug, str(tmp_path)),
    )
    r = client.delete(f"/api/repos/by-slug/{slug}")
    assert r.status_code == 204
    row = db.execute("SELECT deleted_at FROM repos WHERE slug = ?", (slug,)).fetchone()
    assert row["deleted_at"] is not None


def test_poll_by_slug_enqueues(client, db, tmp_path):
    slug = f"local:{tmp_path.resolve()}"
    db.execute(
        """INSERT INTO repos (slug, source_type, owner, name, display_name, local_path)
           VALUES (?, 'local', 'local', 'tmp', 'local:tmp', ?)""",
        (slug, str(tmp_path)),
    )
    r = client.post(f"/api/repos/poll/{slug}")
    assert r.status_code == 202, r.text
    assert db.execute("SELECT 1 FROM jobs WHERE agent='github_poller'").fetchone()


def test_ideate_by_slug_enqueues(client, db, tmp_path):
    slug = f"local:{tmp_path.resolve()}"
    db.execute(
        """INSERT INTO repos (slug, source_type, owner, name, display_name, local_path)
           VALUES (?, 'local', 'local', 'tmp', 'local:tmp', ?)""",
        (slug, str(tmp_path)),
    )
    r = client.post(f"/api/repos/ideate/{slug}")
    assert r.status_code == 202, r.text
    assert db.execute("SELECT 1 FROM jobs WHERE agent='github_ideator'").fetchone()


def test_browse_defaults_to_home(client, monkeypatch, tmp_path):
    """With no path param, browse starts at the user's home directory."""
    import mastisk.routes.repos_route as rr
    # Use a dedicated subdir as "home" so it isn't polluted by sibling fixtures
    # (vault_tmp/data_tmp share the top-level tmp_path).
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(rr.Path, "home", classmethod(lambda cls: home))
    (home / "subdir").mkdir()
    (home / "file.txt").write_text("hi")

    r = client.get("/api/repos/browse")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["path"] == str(home)
    assert body["parent"] is None  # at home
    names = [e["name"] for e in body["entries"]]
    assert "subdir" in names
    assert "file.txt" in names
    # Dirs sort before files
    assert body["entries"][0]["name"] == "subdir"


def test_browse_respects_home_boundary(client, monkeypatch, tmp_path):
    """Browsing outside the home dir returns 403."""
    import mastisk.routes.repos_route as rr
    monkeypatch.setattr(rr.Path, "home", classmethod(lambda cls: tmp_path))
    r = client.get("/api/repos/browse?path=/etc")
    assert r.status_code == 403
    assert "home" in r.json()["detail"]


def test_browse_flags_git_repos(client, monkeypatch, tmp_path):
    import mastisk.routes.repos_route as rr
    monkeypatch.setattr(rr.Path, "home", classmethod(lambda cls: tmp_path))
    (tmp_path / "not-a-repo").mkdir()
    repo = tmp_path / "my-repo"
    repo.mkdir()
    (repo / ".git").mkdir()

    r = client.get("/api/repos/browse")
    body = r.json()
    by_name = {e["name"]: e for e in body["entries"]}
    assert by_name["not-a-repo"]["is_git_repo"] is False
    assert by_name["my-repo"]["is_git_repo"] is True


def test_browse_navigates_into_subdirs(client, monkeypatch, tmp_path):
    import mastisk.routes.repos_route as rr
    monkeypatch.setattr(rr.Path, "home", classmethod(lambda cls: tmp_path))
    nested = tmp_path / "outer" / "inner"
    nested.mkdir(parents=True)

    r = client.get(f"/api/repos/browse?path={tmp_path / 'outer'}")
    body = r.json()
    assert body["parent"] == str(tmp_path)
    names = [e["name"] for e in body["entries"]]
    assert "inner" in names


def test_browse_hides_hidden_except_git(client, monkeypatch, tmp_path):
    import mastisk.routes.repos_route as rr
    monkeypatch.setattr(rr.Path, "home", classmethod(lambda cls: tmp_path))
    (tmp_path / ".DS_Store").write_text("")
    (tmp_path / ".hidden").mkdir()
    (tmp_path / ".git").mkdir()
    r = client.get("/api/repos/browse")
    names = [e["name"] for e in r.json()["entries"]]
    assert ".DS_Store" not in names
    assert ".hidden" not in names
    # .git is not filtered; it should be present.
    assert ".git" in names


def test_readd_local_repo_keeps_history_and_untombstones(client, db, tmp_path):
    import subprocess
    p = tmp_path / "myproj"
    p.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=p, check=True)

    slug = client.post("/api/repos/local", json={"path": str(p)}).json()["slug"]
    db.execute(
        "UPDATE repos SET context_md = 'ctx', last_polled_at = '2026-01-01 00:00:00',"
        " added_at = '2025-12-31 00:00:00' WHERE slug = ?",
        (slug,),
    )
    db.execute(
        "INSERT INTO repo_snapshots (repo_slug, polled_at) VALUES (?, '2026-01-01 00:00:00')",
        (slug,),
    )
    db.execute(
        "INSERT INTO repo_idea_runs (repo_slug, ideated_at) VALUES (?, '2026-01-01 00:00:00')",
        (slug,),
    )
    db.commit()
    assert client.delete(f"/api/repos/by-slug/{slug}").status_code == 204

    r = client.post("/api/repos/local", json={"path": str(p)})
    assert r.status_code == 201, r.text
    assert r.json()["slug"] == slug

    row = db.execute("SELECT * FROM repos WHERE slug = ?", (slug,)).fetchone()
    assert row["deleted_at"] is None
    assert row["context_md"] == "ctx"
    assert row["last_polled_at"] == "2026-01-01 00:00:00"
    assert row["added_at"] == "2025-12-31 00:00:00"
    for table in ("repo_snapshots", "repo_idea_runs"):
        n = db.execute(
            f"SELECT COUNT(*) AS n FROM {table} WHERE repo_slug = ?", (slug,),
        ).fetchone()["n"]
        assert n == 1, table
