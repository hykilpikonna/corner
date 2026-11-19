from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db.session import get_db_session
from app.exports import ensure_export_file

router = APIRouter(prefix="/feeds", tags=["feeds"])

DbSessionDep = Annotated[AsyncSession, Depends(get_db_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


@router.get("/rss.xml")
async def get_rss_feed(
    session: DbSessionDep,
    settings: SettingsDep,
) -> FileResponse:
    target = settings.exports_dir / "rss.xml"
    await ensure_export_file(session, settings, target)
    return FileResponse(
        path=target,
        media_type="application/rss+xml; charset=utf-8",
        headers={"Cache-Control": "public, max-age=60"},
    )


@router.get("/atom.xml")
async def get_atom_feed(
    session: DbSessionDep,
    settings: SettingsDep,
) -> FileResponse:
    target = settings.exports_dir / "atom.xml"
    await ensure_export_file(session, settings, target)
    return FileResponse(
        path=target,
        media_type="application/atom+xml; charset=utf-8",
        headers={"Cache-Control": "public, max-age=60"},
    )


@router.get("/feed.json")
async def get_json_feed(
    session: DbSessionDep,
    settings: SettingsDep,
) -> FileResponse:
    target = settings.exports_dir / "feed.json"
    await ensure_export_file(session, settings, target)
    return FileResponse(
        path=target,
        media_type="application/feed+json; charset=utf-8",
        headers={"Cache-Control": "public, max-age=60"},
    )
