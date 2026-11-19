from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.auth.security import (
    SESSION_COOKIE_NAME,
    get_client_ip,
    get_user_by_session_token,
)
from app.config import Settings, get_settings
from app.db.session import get_db_session, get_session_factory
from app.sse import SSEBus

router = APIRouter(tags=["stream"])

DEFAULT_SSE_PING_INTERVAL = 25.0
DEFAULT_SSE_MAX_GLOBAL = 1000
DEFAULT_SSE_MAX_PER_IP = 10

SettingsDep = Annotated[Settings, Depends(get_settings)]


def _parse_post_filter(raw: str | None) -> set[int] | None:
    if not raw:
        return None
    parsed: set[int] = set()
    for part in raw.split(","):
        s = part.strip()
        if not s:
            continue
        try:
            val = int(s)
        except ValueError:
            continue
        if val > 0:
            parsed.add(val)
    return parsed or None


def _parse_last_event_id(header_val: str | None, query_val: int | None) -> int | None:
    if header_val is not None:
        raw = header_val.strip()
        if raw.isdigit():
            return int(raw)
    if query_val is not None and query_val >= 0:
        return query_val
    return None


def _get_ip_conn_tracker(request: Request) -> dict[str, int]:
    tracker: dict[str, int] | None = getattr(request.app.state, "sse_ip_counts", None)
    if tracker is None:
        tracker = {}
        request.app.state.sse_ip_counts = tracker
    return tracker


async def _resolve_user_id_short_lived(request: Request) -> int | None:
    raw_token = request.cookies.get(SESSION_COOKIE_NAME)
    if not raw_token:
        return None

    override = request.app.dependency_overrides.get(get_db_session)
    if override is not None:
        gen = override()
        try:
            session = await anext(gen)
            user = await get_user_by_session_token(session, raw_token)
            return user.id if user is not None else None
        finally:
            await gen.aclose()

    factory = get_session_factory()
    async with factory() as session:
        user = await get_user_by_session_token(session, raw_token)
        return user.id if user is not None else None


@router.get("/stream")
async def sse_stream(
    request: Request,
    settings: SettingsDep,
    posts: Annotated[str | None, Query()] = None,
    last_event_id_query: Annotated[int | None, Query(alias="last_event_id", ge=0)] = None,
    last_event_id_header: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> StreamingResponse:
    bus: SSEBus | None = getattr(request.app.state, "sse_bus", None)
    if bus is None:
        bus = SSEBus()
        request.app.state.sse_bus = bus

    max_global = int(
        getattr(request.app.state, "sse_max_global", DEFAULT_SSE_MAX_GLOBAL)
    )
    max_per_ip = int(
        getattr(request.app.state, "sse_max_per_ip", DEFAULT_SSE_MAX_PER_IP)
    )
    ip_counts = _get_ip_conn_tracker(request)
    client_ip = get_client_ip(request, settings)

    if bus.subscriber_count >= max_global:
        raise HTTPException(
            status_code=503,
            detail="SSE server capacity reached; please retry shortly",
        )
    if ip_counts.get(client_ip, 0) >= max_per_ip:
        raise HTTPException(
            status_code=429,
            detail="Too many concurrent SSE connections from this IP",
        )

    post_ids = _parse_post_filter(posts)
    last_event_id = _parse_last_event_id(last_event_id_header, last_event_id_query)
    user_id = await _resolve_user_id_short_lived(request)
    ping_interval: float = float(
        getattr(request.app.state, "sse_ping_interval", DEFAULT_SSE_PING_INTERVAL)
    )

    async def event_generator() -> AsyncIterator[str]:
        sub_id: int | None = None
        ip_registered = False
        try:
            # Subscribe inside try: so pre-start aborts can never leak subscribers
            sub_id, queue, replayed = bus.subscribe(
                post_ids=post_ids,
                user_id=user_id,
                last_event_id=last_event_id,
            )
            ip_counts[client_ip] = ip_counts.get(client_ip, 0) + 1
            ip_registered = True

            for ev in replayed:
                yield ev.to_sse()

            while True:
                if await request.is_disconnected():
                    break
                try:
                    ev = await asyncio.wait_for(queue.get(), timeout=ping_interval)
                except TimeoutError:
                    yield ": ping\n\n"
                    continue
                yield ev.to_sse()
        except asyncio.CancelledError:
            raise
        finally:
            if sub_id is not None:
                bus.unsubscribe(sub_id)
            if ip_registered:
                remaining = ip_counts.get(client_ip, 1) - 1
                if remaining <= 0:
                    ip_counts.pop(client_ip, None)
                else:
                    ip_counts[client_ip] = remaining

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
