"""mastisk CLI — the self-serve surface.

Wired up in pyproject.toml as the console_script entry point.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from mastisk import __version__
from mastisk.paths import (
    APP_SUPPORT,
    ICLOUD_ROOT,
    config_path,
    data_dir,
    db_path,
    ensure_dirs,
    log_dir,
    self_dir,
    vault_dir,
    vault_is_icloud,
)

app = typer.Typer(
    name="mastisk",
    help="Personal knowledge wiki with 24/7 agents (Claude + Ollama, vault in iCloud).",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()


def _api_client():
    import httpx

    from mastisk.settings import get_settings

    default_url = f"http://127.0.0.1:{get_settings().port}"
    base_url = os.environ.get("MASTISK_URL", default_url).rstrip("/")
    return httpx.Client(base_url=base_url, timeout=420.0)


def _api_failure(exc: Exception) -> None:
    import httpx

    if isinstance(exc, httpx.HTTPStatusError):
        try:
            payload = exc.response.json()
            detail = payload.get("detail") or payload
        except Exception:
            detail = exc.response.text or str(exc)
        typer.secho(f"Mastisk rejected the request: {detail}", fg="red", err=True)
    elif isinstance(exc, httpx.RequestError):
        typer.secho(
            "Mastisk daemon is unavailable. Expected it at "
            f"{exc.request.url.scheme}://{exc.request.url.host}:{exc.request.url.port}.",
            fg="red",
            err=True,
        )
    else:
        typer.secho(f"Mastisk request failed: {exc}", fg="red", err=True)
    raise typer.Exit(code=1)


def _version_callback(value: bool):
    if value:
        console.print(f"mastisk {__version__}")
        raise typer.Exit()


def _wait_for_oauth_redirect(server, result: dict[str, str], timeout_seconds: int) -> None:
    import time

    deadline = time.monotonic() + max(timeout_seconds, 0)
    while not result.get("code") and not result.get("error"):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        server.timeout = min(1.0, remaining)
        server.handle_request()


@app.callback()
def _main(
    version: bool = typer.Option(
        False, "--version", "-V", callback=_version_callback, is_eager=True,
        help="Show version and exit.",
    ),
):
    pass


@app.command()
def ask(
    question: str = typer.Argument(..., help="Question or task to recall from Mastisk."),
    json_output: bool = typer.Option(False, "--json", help="Emit the complete API response as JSON."),
) -> None:
    """Use Mastisk's intelligence to recall and assess relevant knowledge."""
    if question == "-":
        question = typer.get_text_stream("stdin").read().strip()
        if not question:
            typer.secho("nothing to ask (stdin was empty)", fg="yellow", err=True)
            raise typer.Exit(code=1)
    try:
        with _api_client() as client:
            response = client.post(
                "/api/ask",
                json={"question": question, "intelligent": True},
            )
            response.raise_for_status()
            payload = response.json()
    except Exception as exc:
        _api_failure(exc)

    if json_output:
        typer.echo(json.dumps(payload, ensure_ascii=False))
        return

    typer.echo(str(payload.get("answer") or ""))
    sources = payload.get("sources") or []
    source_heading = "Sources:"
    if not sources:
        sources = payload.get("retrieved_sources") or []
        source_heading = "Retrieved sources (not explicitly cited):"
    if sources:
        typer.echo(f"\n{source_heading}")
        for source in sources:
            typer.echo(f"- {source.get('id')}: {source.get('title')}")


@app.command()
def ingest(
    source: str = typer.Argument(..., help="Text, a local file path, a URL, or '-' for stdin."),
    json_output: bool = typer.Option(False, "--json", help="Emit the complete API response as JSON."),
) -> None:
    """Submit material to Mastisk's compounding ingestion pipeline."""
    from urllib.parse import urlparse

    if source == "-":
        source = typer.get_text_stream("stdin").read().strip()
        if not source:
            typer.secho("nothing to ingest (stdin was empty)", fg="yellow", err=True)
            raise typer.Exit(code=1)
    candidate = Path(source).expanduser()
    try:
        is_file = candidate.is_file()
    except OSError:
        is_file = False
    parsed = urlparse(source)
    is_url = parsed.scheme in {"http", "https"} and bool(parsed.netloc)
    try:
        with _api_client() as client:
            if is_file:
                with candidate.open("rb") as handle:
                    response = client.post(
                        "/api/ingest/document",
                        files={"file": (candidate.name, handle)},
                    )
            elif is_url:
                response = client.post(
                    "/api/listen",
                    json={"url": source, "exact_url": True},
                )
            else:
                response = client.post(
                    "/api/notes",
                    json={"text": source, "source": "cli"},
                )
            response.raise_for_status()
            payload = response.json()
    except Exception as exc:
        _api_failure(exc)

    if json_output:
        typer.echo(json.dumps(payload, ensure_ascii=False))
        return
    if payload.get("id") is not None:
        typer.echo(f"Ingested note #{payload['id']} into Mastisk.")
    elif payload.get("job_id") is not None:
        status = payload.get("status") or "accepted"
        duplicate = ", duplicate" if payload.get("duplicate") else ""
        typer.echo(f"Accepted Mastisk job {payload['job_id']} ({status}{duplicate}).")
    elif payload.get("source_id") is not None:
        status = payload.get("status") or "accepted"
        typer.echo(f"Accepted source {payload['source_id']} ({status}).")
    else:
        typer.echo("Accepted by Mastisk.")


@app.command()
def learn(
    topic: str = typer.Argument(..., help="What you want to learn."),
    days: int | None = typer.Option(
        None, "--days", min=1, max=365,
        help="Timeline in days. Shorter timelines pack more concepts into each lesson.",
    ),
    level: str | None = typer.Option(
        None, "--level", help="Your starting level, e.g. 'complete beginner'.",
    ),
    minutes: int | None = typer.Option(
        None, "--minutes", min=5, max=240, help="Minutes per day you can spend.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit the complete API response as JSON."),
) -> None:
    """Start learning a topic — Guru plans a syllabus and teaches a daily lesson."""
    body: dict = {"topic": topic}
    if days is not None:
        body["timeline_days"] = days
    if level is not None:
        body["level"] = level
    if minutes is not None:
        body["minutes_per_day"] = minutes
    try:
        with _api_client() as client:
            response = client.post("/api/learning/goals", json=body)
            response.raise_for_status()
            payload = response.json()
    except Exception as exc:
        _api_failure(exc)

    if json_output:
        typer.echo(json.dumps(payload, ensure_ascii=False))
        return
    line = f"Learning goal #{payload['id']} created ({payload['slug']})."
    if payload.get("target_date"):
        line += f" Target: {payload['target_date']}."
    typer.echo(line)
    typer.echo("Guru is planning the syllabus; the first lesson lands on the next daily tick.")


# ═════════════════════════════════ start / dev ═════════════════════════════════

@app.command()
def start(
    host: str = typer.Option("0.0.0.0", "--host", "-h", envvar="MASTISK_HOST"),
    port: int = typer.Option(5555, "--port", "-p", envvar="MASTISK_PORT"),
):
    """Run Mastisk in the foreground."""
    import uvicorn
    ensure_dirs()
    _ensure_db()
    console.print(f"[bold]Mastisk {__version__}[/bold] starting on http://{host}:{port}")
    console.print(f"  vault: {vault_dir()} {'[dim](iCloud)[/dim]' if vault_is_icloud() else '[dim](local)[/dim]'}")
    console.print(f"  data:  {data_dir()}")
    uvicorn.run("mastisk.app:create_app", factory=True, host=host, port=port, log_level="info")


@app.command()
def dev(port: int = typer.Option(5555)):
    """Dev mode: vite dev server + hot-reloading backend. Requires repo checkout with frontend/."""
    from concurrent.futures import ThreadPoolExecutor
    ensure_dirs()
    _ensure_db()
    frontend = Path.cwd() / "frontend"
    if not frontend.exists():
        console.print("[red]frontend/ not found — run this from the repo checkout[/red]")
        raise typer.Exit(1)
    console.print("[bold]dev mode: vite + uvicorn[/bold]")
    with ThreadPoolExecutor(max_workers=2) as pool:
        pool.submit(_run, ["npm", "run", "dev"], cwd=frontend)
        pool.submit(_run, [sys.executable, "-m", "uvicorn", "mastisk.app:create_app",
                           "--factory", "--reload", "--host", "0.0.0.0", "--port", str(port)])


def _run(cmd, cwd: Optional[Path] = None):
    proc = subprocess.Popen(cmd, cwd=cwd)
    proc.wait()


# ═════════════════════════════════ doctor ═════════════════════════════════

@app.command()
def status(
    ping: bool = typer.Option(False, "--ping", help="Also ping Claude + Ollama bridges (slower)"),
):
    """Full status report: DB contents, agents, identity files, bridges."""
    import asyncio
    from mastisk.db.queries import connect, user_info

    _ensure_db()
    with connect() as conn:
        u = user_info(conn)
        counts = {
            "articles":     conn.execute("SELECT COUNT(*) AS n FROM articles").fetchone()["n"],
            "sources":      conn.execute("SELECT COUNT(*) AS n FROM sources").fetchone()["n"],
            "links":        conn.execute("SELECT COUNT(*) AS n FROM links").fetchone()["n"],
            "feed_entries": conn.execute("SELECT COUNT(*) AS n FROM feed").fetchone()["n"],
            "signals":      conn.execute("SELECT COUNT(*) AS n FROM signals").fetchone()["n"],
        }
        feeds = [dict(r) for r in conn.execute(
            "SELECT url, title, last_fetched, enabled FROM rss_feeds ORDER BY added_at DESC"
        )]
        jobs_by = {
            r["status"]: r["n"] for r in conn.execute(
                "SELECT status, COUNT(*) AS n FROM jobs GROUP BY status"
            )
        }

    console.print()
    console.print(f"[bold]{u['name']}[/bold]  —  {u['stats']['pages']} pages · {u['stats']['sources']} sources · {u['stats']['feeds']} feeds")
    console.print(f"[dim]vault:[/dim] {vault_dir()}  {'[dim](iCloud)[/dim]' if vault_is_icloud() else '[dim](local)[/dim]'}")
    console.print()

    tbl = Table(title="Content", show_header=True, header_style="bold")
    tbl.add_column("what"); tbl.add_column("count", justify="right")
    for k, n in counts.items():
        tbl.add_row(k, str(n))
    console.print(tbl)

    if feeds:
        tbl2 = Table(title="Feeds", show_header=True, header_style="bold")
        tbl2.add_column("title"); tbl2.add_column("last synced"); tbl2.add_column("enabled")
        for f in feeds:
            tbl2.add_row(
                (f["title"] or f["url"])[:50],
                str(f["last_fetched"] or "never"),
                "✓" if f["enabled"] else "✗",
            )
        console.print(tbl2)
    else:
        console.print("[yellow]no feeds yet[/yellow] — run: mastisk add-feed <url>")

    if jobs_by:
        tbl3 = Table(title="Jobs", show_header=True, header_style="bold")
        tbl3.add_column("status"); tbl3.add_column("n", justify="right")
        for s, n in jobs_by.items():
            tbl3.add_row(s, str(n))
        console.print(tbl3)

    # Self files
    tbl4 = Table(title="Identity files", show_header=True, header_style="bold")
    tbl4.add_column("file"); tbl4.add_column("status")
    for name in ("identity", "interests", "dislikes", "style", "learnings"):
        p = self_dir() / f"{name}.md"
        if p.exists():
            size = p.stat().st_size
            tbl4.add_row(f"{name}.md", f"[green]present[/green] · {size} bytes")
        else:
            tbl4.add_row(f"{name}.md", "[red]missing[/red]")
    console.print(tbl4)

    if ping:
        console.print()
        console.print("[dim]pinging bridges (this takes ~15s)…[/dim]")
        from mastisk.bridges import claude_bridge, ollama_bridge

        async def go():
            results = {}
            try:
                r = await asyncio.wait_for(
                    claude_bridge.run_claude("Reply with: OK", timeout_s=60), timeout=60
                )
                results["claude"] = ("ok", r["text"][:60])
            except Exception as e:
                results["claude"] = ("fail", str(e)[:200])
            try:
                r = await asyncio.wait_for(ollama_bridge.chat("ping", cheap=True), timeout=30)
                results["ollama chat"] = ("ok", r[:60])
            except Exception as e:
                results["ollama chat"] = ("fail", str(e)[:200])
            try:
                v = await asyncio.wait_for(ollama_bridge.embed(["ping"]), timeout=20)
                dim = len(v[0]) if v and v[0] else 0
                results["ollama embed"] = ("ok", f"dim={dim}") if dim else ("fail", "empty")
            except Exception as e:
                results["ollama embed"] = ("fail", str(e)[:200])
            return results

        results = asyncio.run(go())
        tbl5 = Table(title="Bridges", show_header=True, header_style="bold")
        tbl5.add_column("bridge"); tbl5.add_column("result")
        for k, (state, detail) in results.items():
            color = "green" if state == "ok" else "red"
            tbl5.add_row(k, f"[{color}]{state}[/{color}] · {detail}")
        console.print(tbl5)


@app.command()
def doctor():
    """Check preconditions: claude, ollama, tailscale, iCloud dir, deps."""
    tbl = Table(show_header=True, header_style="bold")
    tbl.add_column("check")
    tbl.add_column("status")
    tbl.add_column("notes")

    def ok(name, note=""):
        tbl.add_row(name, "[green]ok[/green]", note)

    def bad(name, note=""):
        tbl.add_row(name, "[red]missing[/red]", note)

    def warn(name, note=""):
        tbl.add_row(name, "[yellow]warn[/yellow]", note)

    # Python
    v = sys.version_info
    if v >= (3, 11):
        ok("python", f"{v.major}.{v.minor}.{v.micro}")
    else:
        bad("python", f"need >= 3.11, have {v.major}.{v.minor}")

    # claude
    claude = shutil.which("claude")
    if claude:
        try:
            out = subprocess.check_output([claude, "--version"], stderr=subprocess.STDOUT, timeout=5).decode().strip()
            ok("claude", out)
        except Exception as e:
            warn("claude", f"{claude} but --version failed: {e}")
    else:
        bad("claude", "install from https://claude.com/claude-code")

    # ollama
    ollama = shutil.which("ollama")
    if ollama:
        try:
            out = subprocess.check_output([ollama, "--version"], stderr=subprocess.STDOUT, timeout=5).decode().strip()
            ok("ollama", out)
        except Exception as e:
            warn("ollama", f"{ollama} but --version failed: {e}")
    else:
        warn("ollama", "optional for local fallback — brew install ollama")

    # tailscale
    ts = _tailscale_binary()
    if ts:
        hostname = _tailscale_hostname()
        ok("tailscale", hostname or "installed (not logged in?)")
    else:
        warn("tailscale", "optional for phone access — brew install --cask tailscale")

    # iCloud
    if ICLOUD_ROOT.is_dir():
        ok("iCloud", f"vault will live in {ICLOUD_ROOT}/Mastisk/vault")
    else:
        warn("iCloud", f"not detected; vault will fall back to {APP_SUPPORT}/vault")

    # Config
    if config_path().exists():
        ok("config", str(config_path()))
    else:
        warn("config", f"not found; run `mastisk init` to create {config_path()}")

    console.print(tbl)


# ═════════════════════════════════ init ═════════════════════════════════

@app.command()
def init(
    ollama_key: Optional[str] = typer.Option(None, "--ollama-key", help="Ollama Cloud API key"),
    demo: bool = typer.Option(False, "--demo", help="Also load the demo wiki (Test-time compute + friends)"),
    force: bool = typer.Option(False, "--force", help="Overwrite existing config + self files"),
):
    """First-time setup: create dirs, empty DB, starter self files. No demo data unless --demo."""
    ensure_dirs()
    _write_self_templates(force=force)

    # Config
    if not config_path().exists() or force:
        key = ollama_key or os.environ.get("OLLAMA_CLOUD_KEY", "")
        if not key and sys.stdin.isatty():
            key = typer.prompt(
                "Ollama Cloud API key (press Enter to skip; local Ollama will be used)",
                default="",
                show_default=False,
            )
        config_path().write_text(_config_template(key))
        config_path().chmod(0o600)
        console.print(f"[green]wrote[/green] {config_path()}")

    # DB schema only
    from mastisk.db.queries import init_schema
    init_schema()
    console.print(f"[green]db ready[/green] {db_path()} [dim](empty)[/dim]")

    if demo:
        _seed_demo()

    # Summary
    console.print()
    console.print("[bold]Mastisk ready.[/bold]")
    console.print(f"  vault: {vault_dir()}  {'(iCloud)' if vault_is_icloud() else '(local)'}")
    console.print(f"  data:  {data_dir()}")
    console.print(f"  self:  {self_dir()}  [dim]← edit identity.md, interests.md, style.md[/dim]")
    console.print()
    console.print("  next:  mastisk start")
    if not demo:
        console.print("  tip:   mastisk add-feed <url>  [dim]— subscribe a feed to kick off Scout[/dim]")


@app.command(name="capture-token")
def capture_token(
    rotate: bool = typer.Option(False, "--rotate", help="Replace an existing token."),
) -> None:
    """Generate and store the bearer token for the /api/capture ingress."""
    import secrets

    from mastisk.settings import get_settings, reload_settings, update_toml_key

    existing = get_settings().capture.bearer_token
    if existing and not rotate:
        typer.secho(
            "A capture token already exists. Re-run with --rotate to replace it "
            "(this invalidates the old token in your shortcut).",
            fg="yellow",
        )
        raise typer.Exit(code=1)

    token = secrets.token_urlsafe(32)
    update_toml_key("capture", "bearer_token", token)
    reload_settings()

    typer.secho("capture ingress token (store this - shown once):", fg="green")
    typer.echo(token)
    typer.echo("")
    typer.secho("In your Apple Watch shortcut, set the header:", fg="cyan")
    typer.echo(f"  Authorization: Bearer {token}")
    typer.echo("")
    typer.secho(
        "Reachability: the Watch is NOT a Tailscale client. Expose ONLY /api/capture "
        "via Cloudflare Tunnel (see README -> 'Capturing from your Apple Watch').",
        fg="cyan",
    )


@app.command(name="calendar-connect")
def calendar_connect(
    no_browser: bool = typer.Option(False, "--no-browser", help="Print the auth URL without opening a browser."),
    timeout_seconds: int = typer.Option(180, "--timeout", help="How long to wait for the loopback redirect."),
) -> None:
    """Connect read-only Google Calendar using a Desktop app OAuth client.

    Reads ``[calendar].client_id`` and ``[calendar].client_secret`` from
    config.toml, requests only ``calendar.readonly``, and stores tokens in the
    local data dir with 0600 permissions.
    """
    import webbrowser
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from urllib.parse import parse_qs, urlparse

    from mastisk.google_calendar import (
        CALENDAR_READONLY_SCOPE,
        exchange_authorization_code,
        make_authorization_url,
        make_pkce_pair,
        write_calendar_tokens,
    )
    from mastisk.settings import get_settings, reload_settings

    ensure_dirs()
    _ensure_db()
    reload_settings()
    calendar = get_settings().calendar
    if not calendar.client_id or not calendar.client_secret:
        typer.secho("calendar OAuth client credentials are missing.", fg="red")
        typer.echo(f"Edit {config_path()} and add:")
        typer.echo("")
        typer.echo("[calendar]")
        typer.echo('client_id = "..."')
        typer.echo('client_secret = "..."')
        typer.echo("sync_interval_minutes = 15")
        typer.echo('calendar_ids = []  # optional extra calendar IDs; primary is always synced')
        typer.echo("")
        typer.echo("In Google Cloud Console:")
        typer.echo("  1. Enable Google Calendar API.")
        typer.echo("  2. Configure OAuth consent for your own account.")
        typer.echo("  3. Create OAuth client ID with application type: Desktop app.")
        typer.echo(f"  4. Keep scopes to: {CALENDAR_READONLY_SCOPE}")
        raise typer.Exit(1)

    code_verifier, code_challenge = make_pkce_pair()
    state = os.urandom(16).hex()
    result: dict[str, str] = {}

    class OAuthHandler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - stdlib callback name
            params = parse_qs(urlparse(self.path).query)
            result["code"] = params.get("code", [""])[0]
            result["state"] = params.get("state", [""])[0]
            result["error"] = params.get("error", [""])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(
                b"<html><body><h1>Mastisk Calendar connected.</h1>"
                b"<p>You can close this tab and return to the terminal.</p></body></html>"
            )

        def log_message(self, format, *args):  # noqa: A002,N802 - stdlib callback name
            return

    server = HTTPServer(("127.0.0.1", 0), OAuthHandler)
    server.timeout = timeout_seconds
    redirect_uri = f"http://127.0.0.1:{server.server_port}"
    auth_url = make_authorization_url(
        client_id=calendar.client_id,
        redirect_uri=redirect_uri,
        state=state,
        code_challenge=code_challenge,
    )

    typer.secho("Opening Google OAuth consent in your browser.", fg="green")
    typer.echo(f"Requested scope: {CALENDAR_READONLY_SCOPE}")
    typer.echo("")
    typer.echo(auth_url)
    typer.echo("")
    if not no_browser:
        webbrowser.open(auth_url)
    typer.echo(f"Waiting for redirect on {redirect_uri} ...")
    _wait_for_oauth_redirect(server, result, timeout_seconds)

    if result.get("error"):
        typer.secho(f"Google returned OAuth error: {result['error']}", fg="red")
        raise typer.Exit(1)
    if not result.get("code") or result.get("state") != state:
        typer.secho("OAuth redirect did not contain a valid code/state.", fg="red")
        raise typer.Exit(1)

    try:
        tokens = exchange_authorization_code(
            client_id=calendar.client_id,
            client_secret=calendar.client_secret,
            code=result["code"],
            redirect_uri=redirect_uri,
            code_verifier=code_verifier,
        )
    except Exception as e:
        typer.secho(f"Token exchange failed: {e}", fg="red")
        raise typer.Exit(1) from e

    path = write_calendar_tokens(tokens, replace_connection=True)
    typer.secho("Google Calendar connected read-only.", fg="green")
    typer.echo(f"Tokens: {path} (0600)")
    typer.echo("")
    typer.secho("Storage note:", fg="yellow")
    typer.echo(
        "Tokens are protected by local file permissions only, not encrypted. "
        "This matches Mastisk's single-user local Mac trust model for Phase 9: "
        "the launchctl daemon would hit Keychain ACL prompts during headless "
        "background sync, while FileVault covers at-rest disk encryption."
    )
    typer.echo("")
    typer.echo(
        "If Mastisk is already running, click force sync in the PWA now and "
        "restart the daemon before relying on the periodic calendar_sync job."
    )


@app.command()
def update(
    check: bool = typer.Option(False, "--check", help="Only show whether updates are available, don't apply"),
    skip_build: bool = typer.Option(False, "--skip-build", help="Skip the npm frontend rebuild"),
    skip_restart: bool = typer.Option(False, "--skip-restart", help="Don't kickstart the launchd agent after update"),
):
    """Pull latest code, rebuild frontend, reinstall package, restart launchd agent if loaded."""
    source = _load_install_source()
    if not source:
        console.print("[red]no install source recorded[/red] — run ./install.sh from your clone once")
        raise typer.Exit(1)

    if not Path(source).exists():
        console.print(f"[red]install source missing:[/red] {source}")
        console.print("re-clone the repo and run ./install.sh, then `mastisk update` will work")
        raise typer.Exit(1)

    repo = Path(source)

    # Fetch so we can compare before deciding to pull
    console.print(f"[dim]repo:[/dim] {repo}")
    _run_in(repo, ["git", "fetch", "--quiet"])
    behind = _git_output(repo, ["rev-list", "--count", "HEAD..@{u}"]).strip() or "0"
    ahead  = _git_output(repo, ["rev-list", "--count", "@{u}..HEAD"]).strip() or "0"
    dirty  = bool(_git_output(repo, ["status", "--porcelain"]).strip())
    current = _git_output(repo, ["rev-parse", "--short", "HEAD"]).strip()

    console.print(f"[dim]current:[/dim] {current}  [dim]behind:[/dim] {behind}  [dim]ahead:[/dim] {ahead}"
                  + (f"  [yellow]local changes[/yellow]" if dirty else ""))

    if check:
        if behind == "0":
            console.print("[green]up to date[/green]")
        else:
            console.print(f"[yellow]{behind} new commit(s) available[/yellow] — run `mastisk update`")
            _run_in(repo, ["git", "log", "--oneline", f"HEAD..@{{u}}"], capture=False)
        raise typer.Exit(0)

    if behind == "0" and not dirty:
        console.print("[green]already up to date[/green]")
        # Even so, rebuild+reinstall lets us be idempotent (useful after tweaks)
        if not typer.confirm("re-run rebuild + reinstall anyway?", default=False):
            raise typer.Exit(0)
    elif dirty:
        console.print("[yellow]uncommitted changes in repo — pull skipped. rebuild+reinstall only.[/yellow]")
    else:
        console.print(f"[cyan]pulling {behind} commit(s)…[/cyan]")
        _run_in(repo, ["git", "pull", "--ff-only", "--quiet"])

    if not skip_build:
        if (repo / "frontend").is_dir():
            console.print("[cyan]rebuilding frontend…[/cyan]")
            if not (repo / "frontend" / "node_modules").is_dir():
                _run_in(repo / "frontend", ["npm", "install", "--silent"])
            _run_in(repo / "frontend", ["npm", "run", "build"], capture=True)

    console.print("[cyan]reinstalling (uv tool install --force)…[/cyan]")
    _run_in(repo, ["uv", "tool", "install", "--force", "--reinstall", "."])

    # Refresh the breadcrumb in case the repo moved
    (data_dir() / "install_source").write_text(str(repo))

    new_commit = _git_output(repo, ["rev-parse", "--short", "HEAD"]).strip()
    console.print(f"[green]✓ installed[/green] {current} → {new_commit}")

    if not skip_restart and _launchd_loaded():
        console.print("[cyan]restarting running agent via launchctl…[/cyan]")
        subprocess.run(
            ["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{_PLIST_LABEL}"],
            check=False,
        )
        console.print("[green]✓ restarted[/green]")


def _load_install_source() -> Optional[str]:
    p = data_dir() / "install_source"
    if p.exists():
        return p.read_text().strip()
    # Legacy fallback: look for a clone next to ~/Code
    for guess in ["~/Code/mastisk", "~/mastisk"]:
        gp = Path(guess).expanduser()
        if (gp / ".git").is_dir() and (gp / "pyproject.toml").exists():
            return str(gp)
    return None


def _run_in(cwd: Path, cmd: list[str], *, capture: bool = True) -> None:
    if capture:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    else:
        r = subprocess.run(cmd, cwd=cwd)
    if r.returncode != 0:
        if capture:
            console.print(f"[red]{' '.join(cmd)}[/red]\n{r.stdout}\n{r.stderr}")
        raise typer.Exit(r.returncode)


def _git_output(cwd: Path, args: list[str]) -> str:
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else ""


def _launchd_loaded() -> bool:
    r = subprocess.run(["launchctl", "list"], capture_output=True, text=True)
    return _PLIST_LABEL in r.stdout


@app.command(name="seed-demo")
def seed_demo_cmd():
    """Load the Test-time compute demo wiki (what the design shows). Safe to run again."""
    _seed_demo()


@app.command()
def reset(
    keep_feeds: bool = typer.Option(True, "--keep-feeds/--wipe-feeds", help="Keep subscribed RSS feeds"),
    keep_vault: bool = typer.Option(True, "--keep-vault/--wipe-vault", help="Keep vault markdown files"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Don't prompt for confirmation"),
):
    """Wipe all wiki content (articles, sources, feed ticker, jobs, signals). Keeps identity + config."""
    if not yes:
        if not typer.confirm(
            f"About to wipe all wiki content from {db_path()}.\n"
            f"  feeds:  {'kept' if keep_feeds else 'wiped'}\n"
            f"  vault:  {'kept' if keep_vault else 'wiped'}\n"
            f"  identity files + config are always kept.\n"
            f"Proceed?",
            default=False,
        ):
            console.print("aborted")
            raise typer.Exit(1)

    from mastisk.db.queries import connect, init_schema
    init_schema()
    tables = [
        "signals", "jobs", "feed",
        "article_sources", "article_embeddings", "article_sections",
        "links", "pinned",
        "sources", "articles",
    ]
    if not keep_feeds:
        tables.append("rss_feeds")
    with connect() as conn:
        for t in tables:
            conn.execute(f"DELETE FROM {t}")
        conn.execute("DELETE FROM sqlite_sequence WHERE name IN ('feed','jobs','signals')")
        conn.execute("INSERT INTO articles_fts(articles_fts) VALUES('rebuild')")
    console.print(f"[green]wiped[/green] {', '.join(tables)}")

    if not keep_vault:
        import shutil
        for sub in ("concepts", "entities", "sources", "synthesis"):
            d = vault_dir() / sub
            if d.exists():
                shutil.rmtree(d)
                d.mkdir()
        console.print(f"[green]wiped vault[/green] (kept _self/)")


def _seed_demo() -> None:
    from mastisk.db.seed import seed
    s = seed()
    console.print(f"[green]seeded demo[/green] {s['articles']} articles, {s['feed']} feed entries")


# ═════════════════════════════════ add-feed / add-youtube ═════════════════════════════════

@app.command(name="add-feed")
def add_feed(url: str, title: Optional[str] = typer.Option(None, "--title", "-t")):
    """Subscribe to an RSS feed."""
    from mastisk.db.queries import connect
    _ensure_db()
    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO rss_feeds (url, title, enabled) VALUES (?, ?, 1)",
            (url, title or url),
        )
    console.print(f"[green]subscribed[/green] {url}")


@app.command(name="add-youtube")
def add_youtube(url: str):
    """Queue a YouTube URL for the Listener agent."""
    from mastisk.integrations import youtube
    from mastisk.routes.listen_route import enqueue_listener_once

    _ensure_db()
    canonical_url = youtube.canonicalize_url(url)
    queued = enqueue_listener_once(
        {"url": canonical_url, "media_type": "video"},
        reuse_done=True,
    )
    job_id = queued["job_id"]
    if queued["duplicate"]:
        if job_id:
            console.print(f"[yellow]already {queued['status']}[/yellow] job {job_id}")
        else:
            console.print("[green]already ingested[/green]")
        return
    console.print(f"[green]queued[/green] job {job_id}  (Listener will pick it up on next tick)")


@app.command(name="add-podcast")
def add_podcast(url: str):
    """Queue a podcast URL (RSS feed, Apple Podcasts, or direct audio) for the Listener agent."""
    import asyncio
    from mastisk.agents.base import enqueue
    from mastisk.integrations import podcasts

    _ensure_db()
    try:
        cls, resolved_url = asyncio.run(podcasts.classify_and_resolve(url))
    except Exception as e:
        console.print(f"[red]classify failed:[/red] {e}")
        raise typer.Exit(1)

    if cls == "spotify":
        console.print(
            "[red]Spotify podcasts are DRM-protected and can't be ingested.[/red] "
            "Try the podcast's RSS feed URL or Apple Podcasts link."
        )
        raise typer.Exit(1)
    if cls == "unknown":
        console.print(
            f"[red]can't ingest {url}[/red] — unknown type "
            "(supported: YouTube, podcast RSS, direct audio, podcast show pages with feed link discovery)"
        )
        raise typer.Exit(1)

    if resolved_url != url:
        console.print(f"[dim]auto-discovered feed:[/dim] {resolved_url}")
    job_id = enqueue("listener", "transcribe", {"url": resolved_url})
    console.print(
        f"[green]queued[/green] job {job_id}  "
        f"([dim]{cls}[/dim]) — Listener will pick it up on next tick"
    )


@app.command(name="backfill-segments")
def backfill_segments(
    only_kind: str | None = typer.Option(
        None, "--kind", help="Limit to one source kind (podcast | youtube). Default: both."
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="List sources that would be re-transcribed; do nothing."),
    limit: int | None = typer.Option(None, "--limit", help="Cap how many sources to process this run."),
) -> None:
    """Re-run mlx-whisper on existing podcast/youtube sources to populate segments.

    The original Listener used to discard whisper's segment data and only kept
    joined text. This command re-transcribes sources that have an audio URL
    but no rows in source_transcript_segments, populating them so the
    PodcastView can render a clickable, time-anchored transcript.

    Skips sources that already have segments (idempotent — safe to re-run).
    Skips sources whose URL we can't re-download (private/expired audio).
    """
    import asyncio
    from pathlib import Path
    from mastisk.agents.listener import _download_to, _hash16
    from mastisk.db.queries import connect
    from mastisk.integrations import whisper
    from mastisk.paths import tmp_dir

    _ensure_db()
    if not whisper.is_available():
        console.print(
            "[red]mlx-whisper is not installed.[/red] "
            "Install with: uv tool install --force --reinstall --with mlx-whisper mastisk"
        )
        raise typer.Exit(1)

    kinds = (only_kind,) if only_kind else ("podcast", "youtube")
    placeholders = ",".join("?" * len(kinds))
    with connect() as conn:
        rows = conn.execute(
            f"""SELECT s.id, s.kind, s.url, s.title
                FROM sources s
                WHERE s.kind IN ({placeholders})
                  AND s.url IS NOT NULL AND s.url != ''
                  AND NOT EXISTS (
                    SELECT 1 FROM source_transcript_segments seg
                    WHERE seg.source_id = s.id
                  )
                ORDER BY s.fetched_at DESC""",
            kinds,
        ).fetchall()

    if limit is not None:
        rows = rows[:limit]

    if not rows:
        console.print("[dim]Nothing to backfill — every podcast/youtube source already has segments.[/dim]")
        return

    console.print(f"[yellow]{len(rows)} source(s) to backfill[/yellow] (kinds: {', '.join(kinds)})")
    if dry_run:
        for r in rows:
            console.print(f"  - [{r['kind']}] {r['title'][:60]}  ({r['url'][:60]})")
        console.print("[dim]dry-run: nothing written.[/dim]")
        return

    done = 0
    failed: list[tuple[str, str]] = []
    for r in rows:
        title = r["title"] or r["url"]
        console.print(f"  [cyan]→[/cyan] {title[:80]}  ([dim]{r['kind']}[/dim])")
        work_dir = tmp_dir() / f"backfill-{_hash16(r['url'])}"
        work_dir.mkdir(parents=True, exist_ok=True)
        try:
            audio_path: Path
            if r["kind"] == "youtube":
                # YouTube: re-download via yt-dlp because s.url is the page URL,
                # not a direct audio URL.
                from mastisk.integrations import youtube
                audio_path = asyncio.run(youtube.download_audio(r["url"], work_dir))
            else:
                # Podcast/direct audio: s.url is the audio URL itself.
                audio_path = asyncio.run(_download_to(r["url"], work_dir))

            result = asyncio.run(whisper.transcribe(audio_path))
            if not result.segments:
                console.print(f"    [yellow]skip[/yellow] — whisper returned no segments")
                continue
            with connect() as conn:
                conn.executemany(
                    """INSERT INTO source_transcript_segments
                         (source_id, idx, start_sec, end_sec, text)
                       VALUES (?, ?, ?, ?, ?)""",
                    [
                        (r["id"], s.idx, s.start_sec, s.end_sec, s.text)
                        for s in result.segments
                    ],
                )
            console.print(f"    [green]✓[/green] {len(result.segments)} segments")
            done += 1
        except Exception as e:
            console.print(f"    [red]✗[/red] {e}")
            failed.append((title, str(e)))
        finally:
            import shutil
            shutil.rmtree(work_dir, ignore_errors=True)

    console.print(
        f"\n[green]Backfilled {done}[/green] / {len(rows)} sources."
        + (f"  [red]{len(failed)} failed.[/red]" if failed else "")
    )


@app.command()
def note(
    text: str | None = typer.Argument(None, help="Note body. If omitted, $EDITOR opens."),
) -> None:
    """Capture a note. Writes to vault/_notes/inbox/ and indexes it in the DB.

    Examples:
        mastisk note "test-time compute is about spending inference cycles"
        mastisk note   # opens $EDITOR
    """
    import os
    import subprocess
    import tempfile

    from mastisk.paths import ensure_dirs

    ensure_dirs()
    _ensure_db()

    if text is None:
        editor = os.environ.get("EDITOR", "vi")
        with tempfile.NamedTemporaryFile(mode="w+", suffix=".md", delete=False) as tf:
            tf.write("# Write your note here; save and quit to capture.\n\n")
            tf.flush()
            tmp_path = tf.name
        try:
            subprocess.run([editor, tmp_path], check=True)
            text = open(tmp_path).read()
            text = text.replace("# Write your note here; save and quit to capture.\n\n", "", 1).strip()
        finally:
            os.unlink(tmp_path)

    if not text or not text.strip():
        typer.secho("nothing to capture (empty note)", fg="yellow")
        raise typer.Exit(code=1)

    from datetime import datetime

    from mastisk.db import queries as q
    from mastisk.db.queries import connect
    from mastisk.paths import notes_inbox_dir, vault_dir
    from mastisk.routes.notes import atomic_write, derive_slug

    ts = datetime.now().astimezone()
    slug = derive_slug(text, ts)
    filename = f"{slug}.md"
    target = notes_inbox_dir() / filename
    atomic_write(target, text)
    rel_path = str(target.relative_to(vault_dir()))
    with connect() as conn:
        note_id = q.insert_note(
            conn, slug=slug, path=rel_path, body=text,
            source="cli", created_at=ts,
        )
        row = q.get_note(conn, note_id)
    actual_path = vault_dir() / row["path"]
    if actual_path != target:
        target.rename(actual_path)
    typer.secho(f"captured #{note_id}: {row['slug']}", fg="green")


@app.command()
def blog(
    theme: str | None = typer.Argument(
        None,
        help="Optional theme for the draft. Omit to let the writer pick a thread.",
    ),
    window_days: int = typer.Option(
        14, "--window", "-w", help="Source window in days (7/14/30/90).",
    ),
) -> None:
    """Draft a blog post from recent synthesis. Writes vault/blog/drafts/<slug>.md.

    Matches the note/roundtable CLI pattern — writes the DB row and enqueues a
    job directly, no HTTP. The daemon picks up the job on its next scheduler
    tick; this command succeeds even if no daemon is running.

    Examples:
        mastisk blog "test-time compute"
        mastisk blog --window 30
        mastisk blog
    """
    from mastisk.agents.base import enqueue
    from mastisk.agents.blog_writer import candidate_count
    from mastisk.db import queries as q
    from mastisk.db.queries import connect
    from mastisk.settings import get_settings

    _ensure_db()

    settings = get_settings().blog
    if window_days not in settings.allowed_window_days:
        typer.secho(
            f"--window must be one of {settings.allowed_window_days}",
            fg="red",
        )
        raise typer.Exit(code=2)

    # Empty-pool pre-check — mirrors the HTTP POST endpoint so a CLI-created
    # job doesn't get enqueued only to fail inside the agent loop.
    if candidate_count(window_days) == 0:
        typer.secho(
            f"no sources in the last {window_days} days — "
            "add some notes/articles first, or try --window 30/90",
            fg="red",
        )
        raise typer.Exit(code=1)

    theme_text = (theme or "").strip()
    with connect() as conn:
        bp_id = q.create_blog_post(
            conn, theme=theme_text, window_days=window_days,
        )
    enqueue("blog_writer", "draft", {"blog_post_id": bp_id})
    typer.secho(
        f"created blog post #{bp_id} — daemon will draft it; "
        f"poll via PWA or `mastisk status`",
        fg="green",
    )


@app.command(name="list-blogs")
def list_blogs() -> None:
    """List recent blog drafts with status and word count.

    Reads ``blog_posts`` directly via queries; does not hit the HTTP API.
    """
    from mastisk.db import queries as q
    from mastisk.db.queries import connect

    _ensure_db()
    with connect() as conn:
        rows = q.list_blog_posts(conn, limit=20)
    if not rows:
        typer.echo("(no drafts yet; use `mastisk blog \"<theme>\"`)")
        return
    for r in rows:
        title = r.get("title") or "(drafting…)"
        wc = r.get("word_count")
        wc_str = f" · {wc} words" if wc else ""
        typer.echo(f"  #{r['id']} [{r['status']}] {title[:60]}{wc_str}")


@app.command()
def roundtable(
    text: str | None = typer.Argument(None, help="The prompt to run through all backends. If omitted, $EDITOR opens."),
    note: int | None = typer.Option(None, help="Attach the roundtable to an existing note_id as context."),
    article: str | None = typer.Option(None, help="Attach the roundtable to an article by id."),
) -> None:
    """Fan a prompt out to all configured LLM backends and synthesize the answers.

    Examples:
        mastisk roundtable "what's the core tension in test-time compute?"
        mastisk roundtable --note 42
        mastisk roundtable --article test-time-compute "how does this relate to chain-of-thought?"
    """
    import os
    import subprocess
    import tempfile

    _ensure_db()

    if text is None:
        editor = os.environ.get("EDITOR", "vi")
        with tempfile.NamedTemporaryFile(mode="w+", suffix=".md", delete=False) as tf:
            tf.write("# Write your roundtable prompt here; save and quit to submit.\n\n")
            tf.flush()
            tmp_path = tf.name
        try:
            subprocess.run([editor, tmp_path], check=True)
            text = open(tmp_path).read()
            text = text.replace("# Write your roundtable prompt here; save and quit to submit.\n\n", "", 1).strip()
        finally:
            os.unlink(tmp_path)

    if not text or not text.strip():
        typer.secho("nothing to send (empty prompt)", fg="yellow")
        raise typer.Exit(code=1)

    if note is not None and article is not None:
        typer.secho("cannot use both --note and --article", fg="red")
        raise typer.Exit(code=2)

    if note is not None:
        input_type, input_ref = "note", str(note)
    elif article is not None:
        input_type, input_ref = "article", article
    else:
        input_type, input_ref = "prompt", ""

    from mastisk.agents.base import enqueue
    from mastisk.db import queries as q
    from mastisk.db.queries import connect

    # Validate input_ref exists, if applicable
    if input_type == "note":
        with connect() as conn:
            n = q.get_note(conn, int(input_ref))
        if n is None or n.get("deleted_at") is not None:
            typer.secho(f"note #{input_ref} not found", fg="red")
            raise typer.Exit(code=1)
    elif input_type == "article":
        with connect() as conn:
            row = conn.execute("SELECT 1 FROM articles WHERE id = ?", (input_ref,)).fetchone()
        if row is None:
            typer.secho(f"article '{input_ref}' not found", fg="red")
            raise typer.Exit(code=1)

    with connect() as conn:
        rt_id = q.create_roundtable(conn, input_type=input_type, input_ref=input_ref, prompt=text)
    enqueue("roundtable", "fan_out", {"roundtable_id": rt_id})
    typer.secho(f"created roundtable #{rt_id} — check status with `mastisk status` or open the PWA", fg="green")


# ═════════════════════════════════ url / logs / vault-path ═════════════════════════════════

@app.command(name="add-repo")
def add_repo(slug: str = typer.Argument(..., help="GitHub slug like 'owner/repo'")) -> None:
    """Register a GitHub repo for ingestion + daily ideation."""
    import asyncio
    from mastisk.agents.base import enqueue
    from mastisk.bridges import github_bridge
    from mastisk.db import queries as q
    from mastisk.db.queries import connect

    _ensure_db()

    if "/" not in slug or slug.count("/") != 1:
        typer.secho("slug must be 'owner/repo'", fg="red")
        raise typer.Exit(code=2)
    owner, name = slug.lower().split("/", 1)
    try:
        meta = asyncio.run(github_bridge.fetch_repo_metadata(owner, name))
    except github_bridge.GithubNotFound:
        typer.secho(f"repo not found on GitHub: {slug}", fg="red")
        raise typer.Exit(code=1)
    except github_bridge.GithubAuthError:
        typer.secho("GitHub PAT invalid or missing scope", fg="red")
        raise typer.Exit(code=1)
    except github_bridge.GithubRateLimited:
        typer.secho("GitHub rate limit hit; try later or set a PAT", fg="red")
        raise typer.Exit(code=1)

    with connect() as conn:
        q.insert_repo(
            conn, slug=meta["slug"], owner=meta["owner"], name=meta["name"],
            display_name=meta["display_name"], description=meta["description"],
            default_branch=meta["default_branch"], is_private=meta["is_private"],
        )
    enqueue("github_poller", "poll", {"repo_slug": meta["slug"]})
    typer.secho(f"added {meta['slug']} — first poll enqueued", fg="green")


@app.command(name="add-local-repo")
def add_local_repo(path: str = typer.Argument(..., help="Absolute path to a local git repo")) -> None:
    """Track a local git repository. Mastisk polls its state and generates idea-notes like it does for GitHub repos."""
    from pathlib import Path as _P
    from mastisk.agents.base import enqueue
    from mastisk.bridges import local_git_bridge
    from mastisk.db.queries import connect

    _ensure_db()
    p = _P(path).expanduser().resolve()
    if not p.is_dir():
        typer.secho(f"not a directory: {p}", fg="red")
        raise typer.Exit(code=1)
    if not (p / ".git").exists():
        typer.secho(f"not a git repo: {p}", fg="red")
        raise typer.Exit(code=1)
    slug = local_git_bridge.derive_local_slug(p)
    with connect() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO repos
               (slug, source_type, owner, name, display_name, description,
                default_branch, is_private, local_path, added_at, deleted_at)
               VALUES (?, 'local', 'local', ?, ?, NULL, NULL, 0, ?,
                       COALESCE((SELECT added_at FROM repos WHERE slug = ?), CURRENT_TIMESTAMP),
                       NULL)""",
            (slug, p.name, f"local:{p.name}", str(p), slug),
        )
    enqueue("github_poller", "poll", {"repo_slug": slug})
    typer.secho(f"added {slug} — first poll enqueued", fg="green")


@app.command(name="list-repos")
def list_repos() -> None:
    """List tracked GitHub repos."""
    from mastisk.db import queries as q
    from mastisk.db.queries import connect
    _ensure_db()
    with connect() as conn:
        rows = q.list_repos(conn)
    if not rows:
        typer.echo("(no repos tracked; use `mastisk add-repo owner/repo`)")
        return
    for r in rows:
        last = r.get("last_polled_at") or "never"
        typer.echo(f"  {r['slug']} · last polled: {last}")


@app.command(name="remove-repo")
def remove_repo(slug: str = typer.Argument(..., help="GitHub slug 'owner/repo'")) -> None:
    """Tombstone a tracked repo; historical snapshots and notes are kept."""
    from mastisk.db import queries as q
    from mastisk.db.queries import connect
    _ensure_db()
    s = slug.strip().lower()
    with connect() as conn:
        r = q.get_repo(conn, s)
        if r is None or r.get("deleted_at") is not None:
            typer.secho(f"repo not tracked: {s}", fg="yellow")
            raise typer.Exit(code=1)
        q.soft_delete_repo(conn, s)
    typer.secho(f"removed {s} (historical data kept)", fg="green")


@app.command()
def url():
    """Print desktop, LAN, and Tailnet URLs for this Mastisk instance."""
    from mastisk.settings import get_settings
    s = get_settings()
    port = s.port

    console.print(f"[bold]Desktop[/bold]  http://127.0.0.1:{port}")

    lan = _lan_ip()
    if lan:
        console.print(f"[bold]LAN    [/bold]  http://{lan}:{port}")

    tailnet = _tailscale_hostname()
    if tailnet:
        console.print(f"[bold]Tailnet[/bold]  http://{tailnet}:{port}  [dim]← open this in Safari on your phone[/dim]")
    else:
        console.print("[dim]Tailnet: not available (open Tailscale app and log in, then retry)[/dim]")


@app.command()
def logs(n: int = 50):
    """Tail the latest N feed entries (agent activity)."""
    from mastisk.db import queries as q
    from mastisk.db.queries import connect
    with connect() as conn:
        entries = q.recent_feed(conn, limit=n)
    for e in reversed(entries):
        console.print(f"[dim]{e['t']:>4}[/dim]  [cyan]{e['agent']:<12}[/cyan]  {e['verb']:<11} {e['obj']}")


@app.command(name="vault-path")
def vault_path_cmd():
    """Print where the vault lives."""
    console.print(str(vault_dir()))


@app.command(name="repair-graph")
def repair_graph_cmd(
    dry_run: bool = typer.Option(
        False, "--dry-run",
        help="Report what would change without writing anything.",
    ),
):
    """Backfill stub articles for dangling wiki links, then rebuild the links table.

    Walks every article body for ``<span class="link" data-target="slug">LABEL</span>``
    references. For each slug that isn't already an article, creates an Entity stub
    titled from the most-common display label. Then calls Linter.repair_graph() to
    populate the ``links`` table. Idempotent — a second run reports 0 new stubs.

    NOTE: this is a force-everything escape hatch — it mints stubs for ALL
    dangling targets, bypassing the stub gate's Suggestions queue
    (settings.compiler.stub_gate_min_sources). Pending suggestions whose slug
    gets minted here read as promoted from then on.
    """
    import re
    from collections import Counter, defaultdict
    from mastisk.db import queries as q
    from mastisk.db.queries import connect
    from mastisk.agents.linter import Linter

    _ensure_db()
    LINK_RE = re.compile(
        r'<span class="link"\s+data-target="([^"]+)"[^>]*>([^<]*)</span>'
    )

    # Gather (slug, label) pairs across all section bodies.
    labels_by_slug: dict[str, Counter[str]] = defaultdict(Counter)
    with connect() as conn:
        for r in conn.execute("SELECT body FROM article_sections"):
            body = r["body"] or ""
            if "data-target=" not in body:
                continue
            for m in LINK_RE.finditer(body):
                slug = m.group(1).strip()
                label = (m.group(2) or "").strip() or slug
                if slug:
                    labels_by_slug[slug][label] += 1
        known_ids = {r["id"] for r in conn.execute("SELECT id FROM articles")}

    missing = [s for s in labels_by_slug if s not in known_ids]
    # Pick title per slug: prefer a label whose slug-form matches the target
    # exactly (so `agent-architectures` picks "agent architectures" over a
    # shorter one-word "agent"). Then most-common, ties → shortest, alphabetical.
    from slugify import slugify
    chosen: dict[str, str] = {}
    for slug in missing:
        counter = labels_by_slug[slug]
        matching = [lbl for lbl in counter if slugify(lbl) == slug]
        if matching:
            matching.sort(key=lambda s: (-counter[s], len(s), s))
            chosen[slug] = matching[0]
            continue
        top_n = max(counter.values())
        tied = [lbl for lbl, n in counter.items() if n == top_n]
        tied.sort(key=lambda s: (len(s), s))
        chosen[slug] = tied[0]

    if dry_run:
        console.print(
            f"[dim]dry run:[/dim] would create [bold]{len(chosen)}[/bold] stub "
            f"article(s); link rows would be reinserted by the linter pass."
        )
        for slug, title in sorted(chosen.items())[:20]:
            console.print(f"  [cyan]{slug}[/cyan]  →  {title!r}")
        if len(chosen) > 20:
            console.print(f"  [dim]…and {len(chosen) - 20} more[/dim]")
        return

    created = 0
    with connect() as conn, q.txn(conn):
        for slug, title in chosen.items():
            if q.ensure_stub_article(conn, id=slug, title=title, kind="Entity"):
                created += 1

    inserted = Linter.repair_graph()
    console.print(
        f"[green]Created {created} stub article(s); inserted {inserted} link row(s).[/green]"
    )


@app.command()
def backup(output_dir: Optional[Path] = typer.Option(None, "--output-dir", "-o")):
    """Tar the DB + config to a timestamped archive."""
    import tarfile
    from datetime import datetime
    target_dir = output_dir or Path.cwd()
    target_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    out = target_dir / f"mastisk-backup-{ts}.tar.gz"
    with tarfile.open(out, "w:gz") as tar:
        if db_path().exists():
            tar.add(db_path(), arcname="mastisk.db")
        if config_path().exists():
            tar.add(config_path(), arcname="config.toml")
    console.print(f"[green]backup[/green] {out}")


# ═════════════════════════════════ autostart ═════════════════════════════════

_PLIST_LABEL = "com.mastisk.agents"
_PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{_PLIST_LABEL}.plist"


@app.command(name="enable-autostart")
def enable_autostart():
    """Install a launchd agent that starts Mastisk on login."""
    binary = shutil.which("mastisk") or sys.argv[0]
    log_file = log_dir() / "mastisk.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    _PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    _PLIST_PATH.write_text(_plist_template(binary, str(log_file)))
    subprocess.run(["launchctl", "load", str(_PLIST_PATH)], check=False)
    console.print(f"[green]enabled[/green] {_PLIST_PATH}")
    console.print(f"       logs at {log_file}")


@app.command(name="disable-autostart")
def disable_autostart():
    """Remove the launchd agent."""
    subprocess.run(["launchctl", "unload", str(_PLIST_PATH)], check=False)
    if _PLIST_PATH.exists():
        _PLIST_PATH.unlink()
        console.print(f"[green]removed[/green] {_PLIST_PATH}")
    else:
        console.print(f"[dim]already disabled[/dim] ({_PLIST_PATH})")


# ═════════════════════════════════ helpers ═════════════════════════════════

def _ensure_db() -> None:
    """Ensure the DB schema exists. Does NOT seed — seeding is explicit via `init --demo` or `seed-demo`."""
    from mastisk.db.queries import init_schema
    init_schema()


def _lan_ip() -> Optional[str]:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return None


def _tailscale_binary() -> Optional[str]:
    for candidate in [
        shutil.which("tailscale"),
        "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
        "/usr/local/bin/tailscale",
    ]:
        if candidate and Path(candidate).exists():
            return candidate
    return None


def _tailscale_hostname() -> Optional[str]:
    ts = _tailscale_binary()
    if not ts:
        return None
    try:
        out = subprocess.check_output([ts, "status", "--json"], stderr=subprocess.DEVNULL, timeout=3).decode()
        data = json.loads(out)
        self_node = data.get("Self") or {}
        dns = self_node.get("DNSName", "")
        return dns.rstrip(".") or None
    except Exception:
        return None


def _write_self_templates(force: bool = False) -> None:
    self_dir().mkdir(parents=True, exist_ok=True)
    templates = {
        "identity.md": _IDENTITY_TEMPLATE,
        "interests.md": _INTERESTS_TEMPLATE,
        "dislikes.md": _DISLIKES_TEMPLATE,
        "style.md": _STYLE_TEMPLATE,
        "learnings.md": _LEARNINGS_TEMPLATE,
    }
    for name, content in templates.items():
        p = self_dir() / name
        if p.exists() and not force:
            continue
        p.write_text(content)


def _config_template(ollama_key: str) -> str:
    return f'''# Mastisk config — written by `mastisk init`. Safe to edit.
# Secrets live here (never in the iCloud vault).

ollama_cloud_key = "{ollama_key}"
ollama_local_only = {"true" if not ollama_key else "false"}

# embed_model = "nomic-embed-text"
# summarize_model_heavy = "llama3.3:70b"
# summarize_model_cheap = "llama3.2:3b"

[budget]
scout = 500
listener = 20
compiler = 100
linter = 50
synthesizer = 10

[calendar]
# Google Cloud OAuth client type: Desktop app. Calendar sync requests only
# https://www.googleapis.com/auth/calendar.readonly.
client_id = ""
client_secret = ""
sync_interval_minutes = 15
calendar_ids = []
'''


def _plist_template(binary: str, log_path: str) -> str:
    home = str(Path.home())
    # launchd's default PATH is minimal — extend it so subprocess calls to
    # `claude`, `ollama`, `node`, etc. installed in the user's ~/.local/bin
    # or /opt/homebrew/bin actually resolve.
    agent_path = (
        f"{home}/.local/bin:{home}/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
    )
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{_PLIST_LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{binary}</string>
    <string>start</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><false/>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>{agent_path}</string>
    <key>HOME</key><string>{home}</string>
  </dict>
  <key>StandardOutPath</key><string>{log_path}</string>
  <key>StandardErrorPath</key><string>{log_path}</string>
</dict>
</plist>
'''


_IDENTITY_TEMPLATE = """# Who I am

## Role
(e.g. "Staff engineer at X, building AI infra")

## Expertise — deep
(bullet topics where a 1-line summary is enough; you want depth and nuance)
-

## Expertise — learning
(bullet topics where you want beginner framing)
-

## Reading preference
(Serif or sans? Long-form or bullets? Default: serif long-form with TL;DR-first)
"""

_INTERESTS_TEMPLATE = """# Topics I want tracked
(One bullet per topic. Scout gates RSS items against these.)
- AI research (test-time compute, RL on LLMs, mech interp)
- AI infrastructure
- Developer tools
"""

_DISLIKES_TEMPLATE = """# Topics to filter out
(One per line. Substring match on article title + summary. Lowercase fine.)
- celebrity
- crypto price
- meme stocks
"""

_STYLE_TEMPLATE = """# How I want articles written

- Lead with a TL;DR callout
- Be concrete about what's new vs what's old
- Flag uncertainty — don't assert what isn't known
- No marketing tone. No em-dashes-for-drama. Plain prose.
- If the source is advocacy, note it
- If there's a counter-argument, include it
"""

_LEARNINGS_TEMPLATE = """# Auto-learnings
(Appended by the Reflection agent once it's running. Prune freely.)
"""
