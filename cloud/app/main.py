"""Petmate cloud API (INTERFACES.md §6). Run: `uvicorn app.main:app`."""
from __future__ import annotations

import logging
import mimetypes
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from .config import Settings
from .db import Database
from .hub import InMemoryHub, SignalingHub
from .push import PushSender, build_push_sender
from .ratelimit import RateLimiter
from . import netinfo
from .routers import auth, devices, info, signal
from .storage import LocalStorage, Storage

API_PREFIX = "/v1"
CONSOLE_DIR = Path(__file__).parent / "static" / "console"

# Some OS mime databases (Windows registry, minimal containers) map .js to text/plain,
# which browsers refuse for ES modules.
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/javascript", ".mjs")
mimetypes.add_type("text/css", ".css")

log = logging.getLogger("petmate.main")


class _NoCacheStatic(StaticFiles):
    """Test console: always revalidate (ETag) so edits show up without a hard reload."""

    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        resp.headers.setdefault("Cache-Control", "no-cache")
        return resp


def create_app(
    settings: Optional[Settings] = None,
    *,
    hub: Optional[SignalingHub] = None,
    storage: Optional[Storage] = None,
    push: Optional[PushSender] = None,
) -> FastAPI:
    settings = (settings or Settings.from_env()).finalize()
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))

    db = Database(settings.database_url)
    db.create_all()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if not settings.public_base_url:
            urls = netinfo.base_urls(settings.port)
            log.info("LAN base URLs (app / robot cloud_url): %s ; console: http://localhost:%d/console/",
                     ", ".join(u + API_PREFIX for u in urls) or "none detected (set LAN_HOSTS)", settings.port)
        yield
        db.engine.dispose()

    app = FastAPI(title="Petmate Cloud", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.db = db
    app.state.hub = hub or InMemoryHub()
    app.state.storage = storage or LocalStorage(settings.storage_dir)
    app.state.push = push or build_push_sender(settings.fcm_project_id, settings.fcm_credentials_file)
    app.state.limiter = RateLimiter()
    app.state.limiter.enabled = settings.rate_limit_enabled

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials="*" not in settings.cors_origins,
        allow_methods=["*"],
        allow_headers=["Authorization", "Content-Type"],
    )

    api = APIRouter(prefix=API_PREFIX)

    @api.get("/health")
    def health():
        return {"ok": True}

    api.include_router(auth.router)
    api.include_router(devices.router)
    api.include_router(signal.router)
    api.include_router(info.router)
    app.include_router(api)

    # Web test console (same origin as the API; no build step). See README.md "Console di test".
    if CONSOLE_DIR.is_dir():
        @app.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
        @app.api_route("/console", methods=["GET", "HEAD"], include_in_schema=False)
        def console_redirect():
            return RedirectResponse("/console/")

        app.mount("/console", _NoCacheStatic(directory=CONSOLE_DIR, html=True), name="console")

    @app.get("/health", include_in_schema=False)
    def root_health():
        return {"ok": True}

    return app


def _lazy_app() -> FastAPI:
    return create_app()


# `uvicorn app.main:app` ; tests use create_app() directly.
app = _lazy_app() if os.environ.get("PETMATE_NO_AUTOAPP") != "1" else None  # type: ignore[assignment]
