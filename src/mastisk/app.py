"""FastAPI application factory."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from mastisk.paths import pwa_dir
from mastisk.routes import (
    articles,
    artifacts_route,
    ask,
    attachments,
    blog_route,
    calendar_route,
    capture,
    capture_triage,
    content,
    dashboard_intelligence,
    digest_route,
    domains,
    editing_route,
    feed_route,
    graph_route,
    ingest,
    inventory,
    journal,
    library,
    listen_route,
    notes,
    open_questions_route,
    people,
    podcasts_route,
    projects,
    reminders,
    repos_route,
    roundtable_route,
    routines,
    search,
    settings_route,
    signals_route,
    sources_route,
    stats_route,
    synthesis_route,
    tasks,
    templates,
    topic_suggester_route,
    tweet_route,
    vault_route,
)
from mastisk.settings import get_settings

log = logging.getLogger("mastisk.app")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Start background scheduler lazily on first tick to avoid partial state on startup errors
    log.info("mastisk starting")
    try:
        from mastisk.scheduler import start_scheduler, stop_scheduler
        scheduler_handle = await start_scheduler()
    except Exception as e:
        log.warning("scheduler failed to start: %s", e)
        scheduler_handle = None
    try:
        yield
    finally:
        if scheduler_handle:
            await stop_scheduler(scheduler_handle)


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Mastisk",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )

    # Trust model: the PWA is served by this daemon and calls the API same-origin,
    # so browsers do not need cross-origin API access. Non-browser local clients
    # are not governed by CORS. The tunnel-exposed capture path is /api/capture,
    # which has its own bearer-token check; raw vault/editor APIs must not be
    # readable or writable by arbitrary web pages the user visits.
    if settings.server.allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.server.allowed_origins,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    app.include_router(articles.router, prefix="/api")
    app.include_router(attachments.router)
    app.include_router(artifacts_route.router, prefix="/api")
    app.include_router(digest_route.router, prefix="/api")
    app.include_router(feed_route.router, prefix="/api")
    app.include_router(ask.router, prefix="/api")
    app.include_router(open_questions_route.router, prefix="/api")
    app.include_router(search.router, prefix="/api")
    app.include_router(signals_route.router, prefix="/api")
    app.include_router(sources_route.router, prefix="/api")
    app.include_router(vault_route.router, prefix="/api")
    app.include_router(graph_route.router, prefix="/api")
    app.include_router(stats_route.router, prefix="/api")
    app.include_router(settings_route.router, prefix="/api")
    app.include_router(synthesis_route.router, prefix="/api")
    app.include_router(listen_route.router, prefix="/api")
    app.include_router(podcasts_route.router, prefix="/api")
    app.include_router(notes.router)
    app.include_router(capture.router)
    app.include_router(ingest.capture_router)
    app.include_router(ingest.router)
    app.include_router(capture_triage.router)
    app.include_router(calendar_route.router, prefix="/api")
    app.include_router(dashboard_intelligence.router)
    app.include_router(domains.router)
    app.include_router(editing_route.router)
    app.include_router(projects.router)
    app.include_router(templates.router)
    app.include_router(tasks.router)
    app.include_router(routines.router)
    app.include_router(journal.router)
    app.include_router(people.router)
    app.include_router(library.router)
    app.include_router(inventory.router)
    app.include_router(content.router)
    app.include_router(reminders.router)
    app.include_router(notes.articles_notes_router)
    app.include_router(repos_route.router)
    app.include_router(roundtable_route.router)
    app.include_router(blog_route.router)
    app.include_router(topic_suggester_route.router)
    app.include_router(tweet_route.router)

    @app.get("/api/health")
    def health():
        return {"ok": True}

    # Static frontend — the wheel ships the built PWA under src/mastisk/pwa/
    pwa = pwa_dir()
    if (pwa / "index.html").exists():
        app.mount(
            "/assets",
            StaticFiles(directory=str(pwa / "assets")),
            name="assets",
        )

        # SPA catch-all: serve index.html for any non-API route so client routing works
        @app.get("/{full_path:path}")
        async def spa(full_path: str, request: Request):
            if full_path.startswith("api"):
                return JSONResponse({"detail": "Not Found"}, status_code=404)
            candidate = pwa / full_path
            if candidate.is_file():
                return FileResponse(candidate)
            return FileResponse(pwa / "index.html")
    else:
        @app.get("/")
        async def stub():
            return JSONResponse({
                "message": "Frontend not built. Run `cd frontend && npm install && npm run build`.",
                "api_docs": "/api/docs",
            })

    return app
