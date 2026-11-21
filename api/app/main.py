from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from typing import Annotated, Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.security import SlidingWindowRateLimiter
from app.config import Settings, get_settings
from app.db.session import close_db, get_db_session, init_db
from app.mailer import Mailer
from app.routes import auth, feeds, interact, media, posts, stream, upload
from app.schemas import HealthResponse
from app.sse import SSEBus
from app.tg import PeriodicReconciler, TelegramBridge, TelegramService

_MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _extract_origin(url: str) -> str | None:
    parsed = urlsplit(url.strip())
    if parsed.scheme in ("http", "https") and parsed.netloc:
        return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"
    return None


def create_app(settings: Settings | None = None) -> FastAPI:
    app_logger = logging.getLogger("app")
    if not logging.getLogger().handlers and not app_logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        app_logger.addHandler(handler)
    if app_logger.level == logging.NOTSET:
        app_logger.setLevel(logging.INFO)

    cfg = settings or get_settings()
    sse_bus = SSEBus()
    rate_limiter = SlidingWindowRateLimiter()
    mailer = Mailer(cfg, sse_bus)
    background_tasks: set[asyncio.Task[Any]] = set()

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        cfg.media_dir.mkdir(parents=True, exist_ok=True)
        cfg.exports_dir.mkdir(parents=True, exist_ok=True)
        _, session_factory = init_db(cfg)

        tg_bridge = TelegramBridge(session_factory=session_factory, settings=cfg)
        tg_service = TelegramService(
            session_factory=session_factory,
            settings=cfg,
            sse_publish=sse_bus.publish,
            bridge=tg_bridge,
        )
        reconciler = PeriodicReconciler(
            session_factory=session_factory,
            settings=cfg,
            telegram_service=tg_service,
            sse_publish=sse_bus.publish,
        )

        _app.state.tg_bridge = tg_bridge
        _app.state.tg_service = tg_service
        _app.state.reconciler = reconciler

        mailer.start()
        try:
            if cfg.TG_BOT_TOKEN:
                await tg_service.start()
                await reconciler.start()
            yield
        finally:
            await reconciler.stop()
            await tg_service.stop()
            await mailer.stop()
            for task in list(background_tasks):
                task.cancel()
            for task in list(background_tasks):
                with suppress(asyncio.CancelledError):
                    await task
            background_tasks.clear()
            await close_db()

    app = FastAPI(
        title="corner /life v2 API",
        version="0.1.0",
        lifespan=lifespan,
    )

    if settings is not None:
        app.dependency_overrides[get_settings] = lambda: cfg

    app.state.settings = cfg
    app.state.sse_bus = sse_bus
    app.state.sse_publish = sse_bus.publish
    app.state.rate_limiter = rate_limiter
    app.state.mailer = mailer
    app.state.tg_bridge = None
    app.state.tg_service = None
    app.state.reconciler = None
    app.state.background_tasks = background_tasks
    app.state.sse_ip_counts = {}

    # Build allowed Origin sets for CSRF Origin / Sec-Fetch-Site protection
    cors_origin_set: set[str] = set()
    for raw in cfg.CORS_ORIGINS:
        origin = _extract_origin(raw)
        if origin:
            cors_origin_set.add(origin)

    allowed_origin_set: set[str] = set(cors_origin_set)
    for base_candidate in (cfg.BASE_URL, cfg.SITE_URL):
        origin = _extract_origin(base_candidate)
        if origin:
            allowed_origin_set.add(origin)

    api_prefix = cfg.API_PREFIX.rstrip("/")
    unsubscribe_path = f"{api_prefix}/auth/unsubscribe"
    upload_prefix = f"{api_prefix}/upload/"

    @app.middleware("http")
    async def csrf_origin_middleware(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        if request.method in _MUTATING_METHODS:
            path = request.url.path
            is_exempt = path == unsubscribe_path or path.startswith(upload_prefix)
            if not is_exempt:
                raw_origin = request.headers.get("origin")
                normalized_origin = raw_origin.strip().lower() if raw_origin else None
                if normalized_origin is not None:
                    if normalized_origin not in allowed_origin_set:
                        return JSONResponse(
                            {"detail": "Cross-origin request blocked"},
                            status_code=403,
                        )

                sec_fetch_site = request.headers.get("sec-fetch-site")
                if sec_fetch_site and sec_fetch_site.strip().lower() == "cross-site":
                    if normalized_origin is None or normalized_origin not in cors_origin_set:
                        return JSONResponse(
                            {"detail": "Cross-site request blocked"},
                            status_code=403,
                        )

        return await call_next(request)

    if cfg.CORS_ORIGINS:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cfg.CORS_ORIGINS,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    @app.get("/health", response_model=HealthResponse, tags=["health"])
    async def health(
        session: Annotated[AsyncSession, Depends(get_db_session)],
    ) -> HealthResponse:
        await session.execute(text("SELECT 1"))
        return HealthResponse(status="ok", database="ok")

    api_router = APIRouter(prefix=cfg.API_PREFIX)
    api_router.include_router(posts.router)
    api_router.include_router(feeds.router)
    api_router.include_router(media.router)
    api_router.include_router(auth.router)
    api_router.include_router(interact.router)
    api_router.include_router(stream.router)
    api_router.include_router(upload.router)

    app.include_router(api_router)
    return app


app = create_app()
