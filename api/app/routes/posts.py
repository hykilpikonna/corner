from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db.repo import get_post_by_id, list_posts, serialize_post
from app.db.session import get_db_session
from app.exports import ensure_export_file
from app.schemas import PostsListResponse

router = APIRouter(tags=["posts"])

DbSessionDep = Annotated[AsyncSession, Depends(get_db_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


@router.get("/exports/posts.json")
async def get_exports_posts(
    session: DbSessionDep,
    settings: SettingsDep,
) -> FileResponse:
    target = settings.exports_dir / "posts.json"
    await ensure_export_file(session, settings, target)
    return FileResponse(
        path=target,
        media_type="application/json; charset=utf-8",
        headers={"Cache-Control": "public, max-age=30"},
    )


@router.get("/exports/pages/meta.json")
async def get_exports_pages_meta(
    session: DbSessionDep,
    settings: SettingsDep,
) -> FileResponse:
    target = settings.exports_dir / "pages" / "meta.json"
    await ensure_export_file(session, settings, target)
    return FileResponse(
        path=target,
        media_type="application/json; charset=utf-8",
        headers={"Cache-Control": "public, max-age=30"},
    )


@router.get("/exports/pages/{page}.json")
async def get_exports_page(
    page: int,
    session: DbSessionDep,
    settings: SettingsDep,
) -> FileResponse:
    if page < 0:
        raise HTTPException(status_code=404, detail="Page not found")
    meta_path = settings.exports_dir / "pages" / "meta.json"
    await ensure_export_file(session, settings, meta_path)
    target = settings.exports_dir / "pages" / f"{page}.json"
    if not target.is_file():
        raise HTTPException(status_code=404, detail="Page not found")
    return FileResponse(
        path=target,
        media_type="application/json; charset=utf-8",
        headers={"Cache-Control": "public, max-age=30"},
    )


@router.get("/posts", response_model=PostsListResponse)
async def get_posts_incremental(
    session: DbSessionDep,
    settings: SettingsDep,
    after_id: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> PostsListResponse:
    posts = await list_posts(session, after_id=after_id, limit=limit, descending=False)
    serialized = [serialize_post(p, api_prefix=settings.API_PREFIX) for p in posts]
    return PostsListResponse(posts=serialized)


@router.get("/posts/{post_id}")
async def get_single_post(
    post_id: int,
    session: DbSessionDep,
    settings: SettingsDep,
) -> dict[str, Any]:
    post = await get_post_by_id(session, post_id, include_deleted=False)
    if post is None:
        raise HTTPException(status_code=404, detail="Post not found")
    return serialize_post(post, api_prefix=settings.API_PREFIX)
