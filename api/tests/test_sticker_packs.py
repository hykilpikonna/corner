from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from pyrogram import enums

from app.config import DEFAULT_STICKER_PACKS as CONFIG_DEFAULT_STICKER_PACKS
from app.config import Settings
from app.media.stickers import (
    DEFAULT_STICKER_PACKS,
    list_sticker_packs,
    load_dismissed_default_packs,
    parse_sticker_set_ref,
    parse_sticker_set_url,
    remove_sticker_pack,
)
from app.tg.client import TelegramService
from app.tg.reactions_cmd import execute_owner_reaction_command


@dataclass
class _FakeStickerTgClient:
    sticker_sets: dict[str, Any] = field(default_factory=dict)
    set_errors: dict[str, Exception] = field(default_factory=dict)
    downloads: dict[str, bytes] = field(default_factory=dict)
    download_errors: set[str] = field(default_factory=set)
    download_calls: list[str] = field(default_factory=list)
    sent_messages: list[dict[str, Any]] = field(default_factory=list)
    handlers: list[Any] = field(default_factory=list)

    def add_handler(self, handler: Any) -> None:
        self.handlers.append(handler)

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def get_me(self) -> Any:
        return SimpleNamespace(id=999000, username="sticker_test_bot")

    async def get_sticker_set(self, name: str) -> Any:
        if name in self.set_errors:
            raise self.set_errors[name]
        if name not in self.sticker_sets:
            raise RuntimeError(f"STICKERSET_INVALID: {name}")
        return self.sticker_sets[name]

    async def download_media(self, target: Any, *, file_name: str) -> str | None:
        key = str(target)
        self.download_calls.append(key)
        if key in self.download_errors:
            Path(file_name).parent.mkdir(parents=True, exist_ok=True)
            Path(file_name).write_bytes(b"")
            raise RuntimeError("Simulated download failure")
        payload = self.downloads.get(key)
        if payload is None:
            return None
        dest = Path(file_name)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(payload)
        return str(dest)

    async def send_message(self, chat_id: int, text: str, **_kwargs: Any) -> Any:
        self.sent_messages.append({"chat_id": int(chat_id), "text": text})
        return SimpleNamespace(id=len(self.sent_messages))


def _make_service(tmp_path: Path, client: Any | None) -> tuple[Settings, TelegramService]:
    cfg = Settings(
        DATA_DIR=tmp_path / "data",
        OWNER_TG_ID=777001,
        API_PREFIX="/api/life",
    )
    service = TelegramService(
        session_factory=cast(Any, None),
        settings=cfg,
        client=client,
        bot_id=999000,
    )
    return cfg, service


def test_parse_sticker_set_ref_and_url() -> None:
    valid_urls = [
        "https://t.me/addstickers/SiameseCatLive",
        "http://t.me/addstickers/SiameseCatLive/",
        "t.me/addstickers/SiameseCatLive?single=1#frag",
        "https://telegram.me/addstickers/SiameseCatLive",
        "https://www.t.me/addstickers/SiameseCatLive",
        "tg://addstickers?set=SiameseCatLive",
        "tg://addstickers?set=SiameseCatLive&foo=bar",
    ]
    for raw in valid_urls:
        assert parse_sticker_set_url(raw) == "SiameseCatLive", raw
        assert parse_sticker_set_ref(raw) == "SiameseCatLive", raw

    # Bare short name allowed by ref parser, not URL parser
    assert parse_sticker_set_url("SiameseCatLive") is None
    assert parse_sticker_set_ref("SiameseCatLive") == "SiameseCatLive"
    assert parse_sticker_set_ref("cat_pack_01") == "cat_pack_01"

    invalid_inputs = [
        "",
        "   ",
        "https://t.me/addemoji/SiameseCatLive",
        "tg://addemoji?set=SiameseCatLive",
        "https://example.com/addstickers/SiameseCatLive",
        "javascript:alert(1)",
        "ftp://t.me/addstickers/SiameseCatLive",
        "https://t.me/addstickers/../etc",
        "https://t.me/addstickers/..",
        "https://t.me/addstickers/SiameseCatLive/extra",
        "../SiameseCatLive",
        "Siamese/Cat",
        "Siamese Cat",
        "https://t.me/addstickers/Siamese Cat",
        "123invalid",
        "_leading_underscore",
        "a" * 65,
    ]
    for bad in invalid_inputs:
        assert parse_sticker_set_url(bad) is None, bad
        assert parse_sticker_set_ref(bad) is None, bad


def test_remove_sticker_pack_safety_and_no_auto_seed(tmp_path: Path) -> None:
    media_dir = tmp_path / "media"
    p1 = media_dir / "stickers" / "pack_one"
    p2 = media_dir / "stickers" / "pack_two"
    p1.mkdir(parents=True)
    p2.mkdir(parents=True)
    (p1 / "a.webp").write_bytes(b"webp-a")
    (p2 / "b.webp").write_bytes(b"webp-b")

    # Rejects traversal / invalid / non-existent
    for bad in ("", "../media", "pack_one/a.webp", ".hidden", "missing_pack"):
        ok, _ = remove_sticker_pack(media_dir, bad)
        assert not ok

    # Removing first pack leaves second pack intact and does not set .no_auto_seed
    ok1, msg1 = remove_sticker_pack(media_dir, "pack_one")
    assert ok1
    assert "pack_one" in msg1
    assert not p1.exists()
    assert not (media_dir / "stickers" / ".no_auto_seed").exists()

    # Removing last pack creates .no_auto_seed
    ok2, _ = remove_sticker_pack(media_dir, "pack_two")
    assert ok2
    assert not p2.exists()
    assert (media_dir / "stickers" / ".no_auto_seed").is_file()


@pytest.mark.asyncio
async def test_import_sticker_set_formats_title_and_idempotency(tmp_path: Path) -> None:
    fake_tg = _FakeStickerTgClient(
        sticker_sets={
            "SiameseCatLive": SimpleNamespace(
                id=4242,
                name="SiameseCatLive",
                title="Siamese Cat Live",
                sticker_type=enums.StickerType.REGULAR,
                stickers=[
                    SimpleNamespace(
                        file_id="fid_1",
                        file_unique_id="uid_webp",
                        emoji="🐱",
                        width=512,
                        height=512,
                        is_animated=False,
                        is_video=False,
                    ),
                    SimpleNamespace(
                        file_id="fid_2",
                        file_unique_id="uid_tgs",
                        emoji="✨",
                        width=512,
                        height=512,
                        is_animated=True,
                        is_video=False,
                    ),
                    SimpleNamespace(
                        file_id="fid_3",
                        file_unique_id="uid_webm",
                        emoji="🎬",
                        width=512,
                        height=512,
                        is_animated=False,
                        is_video=True,
                    ),
                ],
            )
        },
        downloads={
            "fid_1": b"fake-webp-bytes",
            "fid_2": b"fake-tgs-bytes",
            "fid_3": b"fake-webm-bytes",
        },
    )
    cfg, service = _make_service(tmp_path, fake_tg)
    stickers_root = cfg.media_dir / "stickers"
    stickers_root.mkdir(parents=True, exist_ok=True)
    (stickers_root / ".no_auto_seed").touch()

    ok, msg = await service.import_sticker_set("https://t.me/addstickers/SiameseCatLive")
    assert ok
    assert "Siamese Cat Live" in msg
    assert "SiameseCatLive" in msg
    assert "3 saved" in msg
    assert not (stickers_root / ".no_auto_seed").exists()

    pack_dir = stickers_root / "SiameseCatLive"
    assert (pack_dir / "uid_webp.webp").read_bytes() == b"fake-webp-bytes"
    assert (pack_dir / "uid_tgs.tgs").read_bytes() == b"fake-tgs-bytes"
    assert (pack_dir / "uid_webm.webm").read_bytes() == b"fake-webm-bytes"

    manifest = json.loads((pack_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["title"] == "Siamese Cat Live"
    assert [s["file"] for s in manifest["stickers"]] == [
        "uid_webp.webp",
        "uid_tgs.tgs",
        "uid_webm.webm",
    ]

    packs = list_sticker_packs(cfg.media_dir, cfg.API_PREFIX, auto_seed=False)
    assert len(packs) == 1
    assert packs[0]["title"] == "Siamese Cat Live"
    assert [s["format"] for s in packs[0]["stickers"]] == ["webp", "tgs", "webm"]

    # Re-run with 1 new sticker added: existing 3 are skipped without re-downloading
    fake_tg.download_calls.clear()
    fake_tg.sticker_sets["SiameseCatLive"].stickers.append(
        SimpleNamespace(
            file_id="fid_4",
            file_unique_id="uid_new",
            emoji="🐾",
            width=512,
            height=512,
            is_animated=False,
            is_video=False,
        )
    )
    fake_tg.downloads["fid_4"] = b"new-webp-bytes"

    ok2, msg2 = await service.import_sticker_set("SiameseCatLive")
    assert ok2
    assert "1 saved" in msg2
    assert "3 skipped" in msg2
    assert fake_tg.download_calls == ["fid_4"]
    assert (pack_dir / "uid_new.webp").read_bytes() == b"new-webp-bytes"


@pytest.mark.asyncio
async def test_import_sticker_set_error_and_partial_failure_handling(tmp_path: Path) -> None:
    fake_tg = _FakeStickerTgClient(
        sticker_sets={
            "EmojiPack": SimpleNamespace(
                id=1,
                name="EmojiPack",
                title="Custom Emojis",
                sticker_type=enums.StickerType.CUSTOM_EMOJI,
                stickers=[SimpleNamespace(file_id="e1", file_unique_id="e1")],
            ),
            "PartialPack": SimpleNamespace(
                id=2,
                name="PartialPack",
                title="Partial Pack",
                sticker_type=None,  # mock objects without sticker_type allowed
                stickers=[
                    SimpleNamespace(
                        file_id="ok_1",
                        file_unique_id="ok_1",
                        emoji="😺",
                        width=512,
                        height=512,
                        is_animated=False,
                        is_video=False,
                    ),
                    SimpleNamespace(
                        file_id="bad_err",
                        file_unique_id="bad_err",
                        emoji="😿",
                        width=512,
                        height=512,
                        is_animated=False,
                        is_video=False,
                    ),
                    SimpleNamespace(
                        file_id="bad_empty",
                        file_unique_id="bad_empty",
                        emoji="🙀",
                        width=512,
                        height=512,
                        is_animated=False,
                        is_video=False,
                    ),
                ],
            ),
            "AllFailPack": SimpleNamespace(
                id=3,
                name="AllFailPack",
                title="All Fail",
                sticker_type=enums.StickerType.REGULAR,
                stickers=[
                    SimpleNamespace(file_id="bad_err", file_unique_id="only_bad")
                ],
            ),
        },
        downloads={
            "e1": b"emoji-webp",
            "ok_1": b"good-webp",
            "bad_empty": b"",  # 0-byte download should be rejected & cleaned up
        },
        download_errors={"bad_err"},
    )
    cfg, service = _make_service(tmp_path, fake_tg)

    # Missing client
    _, no_client_service = _make_service(tmp_path, None)
    ok_nc, msg_nc = await no_client_service.import_sticker_set("SiameseCatLive")
    assert not ok_nc
    assert "not connected" in msg_nc

    # Bad reference
    ok_bad_ref, _ = await service.import_sticker_set("https://t.me/addemoji/EmojiPack")
    assert not ok_bad_ref

    # RPC not found
    ok_nf, msg_nf = await service.import_sticker_set("DoesNotExist")
    assert not ok_nf
    assert "Could not fetch sticker pack" in msg_nf

    # CUSTOM_EMOJI packs (e.g. SiameseEmoji) are allowed and imported cleanly
    ok_ce, msg_ce = await service.import_sticker_set("EmojiPack")
    assert ok_ce
    assert "1 saved" in msg_ce
    assert (cfg.media_dir / "stickers" / "EmojiPack" / "e1.webp").read_bytes() == b"emoji-webp"

    # Partial failure saves valid sticker and cleans up partial/0-byte temp files
    ok_part, msg_part = await service.import_sticker_set("PartialPack")
    assert ok_part
    assert "1 saved" in msg_part
    assert "2 failed" in msg_part
    part_dir = cfg.media_dir / "stickers" / "PartialPack"
    assert (part_dir / "ok_1.webp").is_file()
    assert not (part_dir / "bad_err.webp").exists()
    assert not (part_dir / "bad_empty.webp").exists()
    assert [p.name for p in part_dir.iterdir() if p.name.startswith(".")] == []

    # Total failure removes empty pack directory
    ok_all_fail, _ = await service.import_sticker_set("AllFailPack")
    assert not ok_all_fail
    assert not (cfg.media_dir / "stickers" / "AllFailPack").exists()


@pytest.mark.asyncio
async def test_owner_dm_commands_and_auto_url_import(tmp_path: Path) -> None:
    fake_tg = _FakeStickerTgClient(
        sticker_sets={
            "SiameseCatLive": SimpleNamespace(
                id=77,
                name="SiameseCatLive",
                title="Siamese Cat Live",
                sticker_type=enums.StickerType.REGULAR,
                stickers=[
                    SimpleNamespace(
                        file_id="cat_1",
                        file_unique_id="cat_1",
                        emoji="🐈",
                        width=512,
                        height=512,
                        is_animated=False,
                        is_video=False,
                    )
                ],
            )
        },
        downloads={"cat_1": b"cat-webp"},
    )
    cfg, service = _make_service(tmp_path, fake_tg)
    owner = SimpleNamespace(id=777001, first_name="Owner")
    stranger = SimpleNamespace(id=999111, first_name="Stranger")

    # Non-owner DM URL is ignored
    await service.handle_owner_dm(
        SimpleNamespace(id=1, from_user=stranger, text="https://t.me/addstickers/SiameseCatLive")
    )
    assert fake_tg.sent_messages == []

    # Plain non-URL text from owner does not trigger sticker import
    await service.handle_owner_dm(
        SimpleNamespace(id=2, from_user=owner, text="hello world")
    )
    assert fake_tg.sent_messages == []

    # Owner /stickers add command
    await service.handle_owner_dm(
        SimpleNamespace(
            id=3,
            from_user=owner,
            text="/stickers add https://t.me/addstickers/SiameseCatLive",
        )
    )
    assert "✅ Imported sticker pack **Siamese Cat Live** (`SiameseCatLive`): 1 saved." == fake_tg.sent_messages[-1]["text"]
    assert (cfg.media_dir / "stickers" / "SiameseCatLive" / "cat_1.webp").is_file()

    # /stickers list displays title, pack id, and management hints
    await service.handle_owner_dm(SimpleNamespace(id=4, from_user=owner, text="/stickers list"))
    list_reply = fake_tg.sent_messages[-1]["text"]
    assert "Siamese Cat Live [SiameseCatLive] (1)" in list_reply
    assert "SiameseCatLive/cat_1.webp" in list_reply
    assert "/stickers add" in list_reply
    assert "/stickers rmpack" in list_reply

    # /stickers rmpack removes entire pack
    await service.handle_owner_dm(
        SimpleNamespace(id=5, from_user=owner, text="/stickers rmpack SiameseCatLive")
    )
    assert "Removed sticker pack `SiameseCatLive`" in fake_tg.sent_messages[-1]["text"]
    assert not (cfg.media_dir / "stickers" / "SiameseCatLive").exists()

    # Owner pasting tg://addstickers?set=... directly in DM auto-imports
    await service.handle_owner_dm(
        SimpleNamespace(id=6, from_user=owner, text="tg://addstickers?set=SiameseCatLive")
    )
    assert "✅ Imported sticker pack **Siamese Cat Live**" in fake_tg.sent_messages[-1]["text"]
    assert (cfg.media_dir / "stickers" / "SiameseCatLive" / "cat_1.webp").is_file()

    # /help text mentions /stickers add and /stickers rmpack
    help_text, _ = await execute_owner_reaction_command(
        "/help", None, session_factory=cast(Any, None)
    )
    assert "/stickers add" in help_text
    assert "/stickers rmpack" in help_text


@pytest.mark.asyncio
async def test_default_sticker_packs_config_ordering_and_dismissed_sync(tmp_path: Path) -> None:
    expected_defaults = [
        "SiameseCatLive",
        "azuki_san",
        "SiameseEmoji",
        "timoxixiaodui",
        "LINE_Ghost_and_black_cat_TL",
        "azalea_anim",
        "azalea_2022",
    ]
    assert list(DEFAULT_STICKER_PACKS) == expected_defaults
    assert list(CONFIG_DEFAULT_STICKER_PACKS) == expected_defaults

    cfg_defaults = Settings(
        DATA_DIR=tmp_path / "d1",
        DEFAULT_STICKER_PACKS="SiameseCatLive, azuki_san",
    )
    assert cfg_defaults.DEFAULT_STICKER_PACKS == ["SiameseCatLive", "azuki_san"]

    # Empty dir does not auto-seed Pillow starter pack
    assert list_sticker_packs(cfg_defaults.media_dir, cfg_defaults.API_PREFIX) == []
    assert not (cfg_defaults.media_dir / "stickers" / "starter").exists()

    # Custom installed packs + subset of default packs in reverse order -> default order wins, extras alphabetical
    stickers_root = cfg_defaults.media_dir / "stickers"
    for pname in ("zeta_pack", "azalea_2022", "alpha_pack", "siamesecatlive", "SiameseEmoji"):
        pdir = stickers_root / pname
        pdir.mkdir(parents=True, exist_ok=True)
        (pdir / "s.webp").write_bytes(b"webp")
    ordered_names = [
        p["name"] for p in list_sticker_packs(cfg_defaults.media_dir, cfg_defaults.API_PREFIX)
    ]
    assert ordered_names == [
        "siamesecatlive",
        "SiameseEmoji",
        "azalea_2022",
        "alpha_pack",
        "zeta_pack",
    ]

    # Background startup sync pulls defaults (including CUSTOM_EMOJI SiameseEmoji), tolerates missing/failing packs,
    # and respects dismissed packs across restarts unless re-added.
    fake_tg = _FakeStickerTgClient(
        sticker_sets={
            "SiameseCatLive": SimpleNamespace(
                id=1,
                name="SiameseCatLive",
                title="Siamese Cat Live",
                sticker_type=enums.StickerType.REGULAR,
                stickers=[SimpleNamespace(file_id="f1", file_unique_id="u1")],
            ),
            "SiameseEmoji": SimpleNamespace(
                id=2,
                name="SiameseEmoji",
                title="Siamese Emoji",
                sticker_type=enums.StickerType.CUSTOM_EMOJI,
                stickers=[SimpleNamespace(file_id="f2", file_unique_id="u2", is_animated=True)],
            ),
        },
        downloads={"f1": b"cat-webp", "f2": b"emoji-tgs"},
    )
    cfg = Settings(
        DATA_DIR=tmp_path / "sync_data",
        OWNER_TG_ID=777001,
        DEFAULT_STICKER_PACKS=["SiameseCatLive", "SiameseEmoji", "azuki_san"],
    )
    service = TelegramService(
        session_factory=cast(Any, None),
        settings=cfg,
        client=fake_tg,
        bot_id=999000,
    )
    await service.start()
    try:
        assert service._default_stickers_task is not None
        counts = await service._default_stickers_task
        assert counts == {"imported": 2, "skipped": 0, "failed": 1}
    finally:
        await service.stop()
    assert service._default_stickers_task is None

    sync_root = cfg.media_dir / "stickers"
    assert (sync_root / "SiameseCatLive" / "u1.webp").is_file()
    assert (sync_root / "SiameseEmoji" / "u2.tgs").is_file()

    # Owner removes SiameseEmoji -> marked in .dismissed_defaults.json and not re-pulled by sync
    ok_rm, _ = remove_sticker_pack(cfg.media_dir, "SiameseEmoji")
    assert ok_rm
    assert "siameseemoji" in load_dismissed_default_packs(sync_root)

    counts2 = await service.ensure_default_sticker_packs()
    assert counts2 == {"imported": 0, "skipped": 2, "failed": 1}
    assert not (sync_root / "SiameseEmoji").exists()

    # Explicit /stickers add clears dismissed status; /stickers defaults reports counts cleanly
    owner = SimpleNamespace(id=777001, first_name="Owner")
    await service.handle_owner_dm(
        SimpleNamespace(id=10, from_user=owner, text="/stickers add SiameseEmoji")
    )
    assert "siameseemoji" not in load_dismissed_default_packs(sync_root)
    assert (sync_root / "SiameseEmoji" / "u2.tgs").is_file()

    await service.handle_owner_dm(
        SimpleNamespace(id=11, from_user=owner, text="/stickers defaults")
    )
    assert "Synced default sticker packs: 0 imported, 2 skipped, 1 failed." in fake_tg.sent_messages[-1]["text"]

    # Ensure stop() cleanly cancels an in-flight background sync task
    slow_client = _FakeStickerTgClient()

    async def _slow_get(name: str) -> Any:
        await asyncio.sleep(5)
        return None

    slow_client.get_sticker_set = _slow_get  # type: ignore[method-assign]
    slow_svc = TelegramService(
        session_factory=cast(Any, None),
        settings=cfg,
        client=slow_client,
        bot_id=999000,
    )
    await slow_svc.start()
    assert slow_svc._default_stickers_task is not None
    await slow_svc.stop()
    assert slow_svc._default_stickers_task is None

