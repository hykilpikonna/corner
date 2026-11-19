from __future__ import annotations

import asyncio
import html
import json
import mimetypes
import os
import re
import tempfile
from datetime import UTC, datetime
from email.utils import format_datetime
from pathlib import Path
from typing import Any
from weakref import WeakKeyDictionary
from xml.sax.saxutils import escape as xml_escape

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models import Post
from app.db.repo import (
    compute_merged_poll,
    format_iso_utc,
    list_posts,
    resolve_absolute_media_url,
    sanitize_post_html,
    serialize_post,
)

# XML 1.0 illegal control chars (including DEL 0x7F and U+FFFE/U+FFFF)
_XML_ILLEGAL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\ufffe\uffff]")
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_REL_ATTR_URL_RE = re.compile(
    r"""(?P<attr>\b(?:src|href|data-src|data-full|emoji-src))=(?P<q>["'])(?P<val>[^"']+)(?P=q)""",
    re.IGNORECASE,
)

# Per-event-loop export locks keyed by resolved exports_dir path
_EXPORT_LOCKS: WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, asyncio.Lock]] = (
    WeakKeyDictionary()
)


def _get_export_lock(exports_dir: Path) -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    per_loop = _EXPORT_LOCKS.get(loop)
    if per_loop is None:
        per_loop = {}
        _EXPORT_LOCKS[loop] = per_loop
    key = str(exports_dir.resolve())
    lock = per_loop.get(key)
    if lock is None:
        lock = asyncio.Lock()
        per_loop[key] = lock
    return lock


def _to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _strip_illegal_xml(value: str) -> str:
    return _XML_ILLEGAL_RE.sub("", value)


def _xml_text(value: str) -> str:
    return xml_escape(_strip_illegal_xml(value))


def _xml_attr(value: str) -> str:
    return xml_escape(_strip_illegal_xml(value), {'"': "&quot;", "'": "&apos;"})


def _cdata(value: str) -> str:
    safe = _strip_illegal_xml(value).replace("]]>", "]]]]><![CDATA[>")
    return f"<![CDATA[{safe}]]>"


def _html_to_plain_text(html_str: str | None) -> str:
    if not html_str:
        return ""
    text = re.sub(r"<br\s*/?>", "\n", html_str, flags=re.IGNORECASE)
    text = re.sub(r"</(?:p|div|blockquote|li|h[1-6])>", "\n", text, flags=re.IGNORECASE)
    text = _HTML_TAG_RE.sub("", text)
    text = html.unescape(text)
    lines = [line.strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def _guess_mime(url: str, fallback: str = "image/jpeg") -> str:
    guessed, _ = mimetypes.guess_type(url)
    return guessed or fallback


def _post_permalink(site_url: str, post_id: int) -> str:
    base = site_url.rstrip("/")
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}shared={post_id}"


def _post_title(post: Post, feed_title: str) -> str:
    plain = _strip_illegal_xml(_html_to_plain_text(post.text_html))
    if plain:
        first_line = plain.splitlines()[0].strip()
        if len(first_line) > 80:
            return first_line[:77].rstrip() + "..."
        return first_line
    if post.poll is not None and post.poll.question:
        q = _strip_illegal_xml(post.poll.question.strip())
        return f"Poll: {q[:74]}..." if len(q) > 74 else f"Poll: {q}"
    return f"{feed_title} #{post.id}"


def _absolutize_html_urls(html_content: str, base_url: str, api_prefix: str) -> str:
    def _repl(match: re.Match[str]) -> str:
        attr = match.group("attr")
        quote = match.group("q")
        val = match.group("val").strip()
        lowered = val.lower()
        if lowered.startswith(("http://", "https://", "mailto:", "tel:", "#")):
            return match.group(0)
        resolved = resolve_absolute_media_url(val, base_url=base_url, api_prefix=api_prefix)
        if not resolved:
            return f"{attr}={quote}{quote}"
        return f"{attr}={quote}{resolved}{quote}"

    return _REL_ATTR_URL_RE.sub(_repl, html_content)


def render_post_feed_html(post: Post, settings: Settings) -> str:
    """Build sanitized rich HTML for RSS <content:encoded>, Atom <content>, and JSON Feed."""
    parts: list[str] = []

    text_block = ""
    if post.text_html:
        clean_text = sanitize_post_html(_strip_illegal_xml(post.text_html))
        abs_text = _absolutize_html_urls(
            clean_text, base_url=settings.BASE_URL, api_prefix=settings.API_PREFIX
        )
        # Re-sanitize after URL absolutization so no disallowed scheme can slip through
        abs_text = sanitize_post_html(abs_text)
        text_block = f'<div class="post-text">{abs_text}</div>'

    image_blocks: list[str] = []
    for img in sorted(post.images, key=lambda i: i.sort_order):
        abs_url = resolve_absolute_media_url(
            img.url, base_url=settings.BASE_URL, api_prefix=settings.API_PREFIX
        )
        if not abs_url:
            continue
        dims = ""
        if img.width > 0 and img.height > 0:
            dims = f' width="{img.width}" height="{img.height}"'
        image_blocks.append(
            f'<figure><img src="{html.escape(abs_url, quote=True)}"{dims} alt="" /></figure>'
        )

    if post.show_caption_above_media:
        if text_block:
            parts.append(text_block)
        parts.extend(image_blocks)
    else:
        parts.extend(image_blocks)
        if text_block:
            parts.append(text_block)

    if post.files:
        for f in post.files:
            if not isinstance(f, dict):
                continue
            raw_url = f.get("url")
            if not raw_url:
                continue
            abs_url = resolve_absolute_media_url(
                str(raw_url), base_url=settings.BASE_URL, api_prefix=settings.API_PREFIX
            )
            if not abs_url:
                continue
            label = f.get("original_name") or f.get("title") or f.get("media_type") or "Attachment"
            parts.append(
                f'<p><a href="{html.escape(abs_url, quote=True)}">{html.escape(_strip_illegal_xml(str(label)))}</a></p>'
            )

    if post.poll is not None:
        merged = compute_merged_poll(post.poll)
        items_html = "".join(
            f"<li>{html.escape(_strip_illegal_xml(opt['text']))} ({opt['voter_count']} votes)</li>"
            for opt in merged["options"]
        )
        parts.append(
            '<div class="poll">'
            f"<p><strong>{html.escape(_strip_illegal_xml(merged['question']))}</strong></p>"
            f"<ul>{items_html}</ul>"
            f"<p><em>Total votes: {merged['total_voter_count']}</em></p>"
            "</div>"
        )

    return _strip_illegal_xml("\n".join(parts))


def build_rss_xml(posts: list[Post], settings: Settings, updated_at: datetime) -> str:
    """Build RSS 2.0 XML with multi-image <content:encoded> and Media RSS <media:content>."""
    feed_posts = [p for p in posts if p.type != "service"][: settings.FEED_MAX_ITEMS]
    site_url = settings.SITE_URL.rstrip("/")
    self_url = f"{settings.api_base_url}/feeds/rss.xml"
    updated_utc = _to_utc(updated_at)

    lines: list[str] = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0"'
        ' xmlns:content="http://purl.org/rss/1.0/modules/content/"'
        ' xmlns:media="http://search.yahoo.com/mrss/"'
        ' xmlns:atom="http://www.w3.org/2005/Atom">',
        "  <channel>",
        f"    <title>{_xml_text(settings.FEED_TITLE)}</title>",
        f"    <link>{_xml_text(site_url)}</link>",
        f"    <description>{_xml_text(settings.FEED_DESCRIPTION)}</description>",
        f"    <language>{_xml_text(settings.FEED_LANGUAGE)}</language>",
        f"    <lastBuildDate>{format_datetime(updated_utc, usegmt=True)}</lastBuildDate>",
        f'    <atom:link href="{_xml_attr(self_url)}" rel="self" type="application/rss+xml" />',
    ]

    if settings.FEED_IMAGE_URL:
        lines.extend(
            [
                "    <image>",
                f"      <url>{_xml_text(settings.FEED_IMAGE_URL)}</url>",
                f"      <title>{_xml_text(settings.FEED_TITLE)}</title>",
                f"      <link>{_xml_text(site_url)}</link>",
                "    </image>",
            ]
        )

    for post in feed_posts:
        link = _post_permalink(site_url, post.id)
        title = _post_title(post, settings.FEED_TITLE)
        pub_date = format_datetime(_to_utc(post.date), usegmt=True)
        content_html = render_post_feed_html(post, settings)
        plain_summary = _strip_illegal_xml(_html_to_plain_text(post.text_html)) or title

        lines.append("    <item>")
        lines.append(f"      <title>{_xml_text(title)}</title>")
        lines.append(f"      <link>{_xml_text(link)}</link>")
        lines.append(f'      <guid isPermaLink="true">{_xml_text(link)}</guid>')
        lines.append(f"      <pubDate>{pub_date}</pubDate>")
        lines.append(f"      <description>{_xml_text(plain_summary)}</description>")
        lines.append(f"      <content:encoded>{_cdata(content_html)}</content:encoded>")

        sorted_images = sorted(post.images, key=lambda i: i.sort_order)
        for idx, img in enumerate(sorted_images):
            abs_url = resolve_absolute_media_url(
                img.url, base_url=settings.BASE_URL, api_prefix=settings.API_PREFIX
            )
            if not abs_url:
                continue
            mime = _guess_mime(abs_url, "image/jpeg")
            attrs = [
                f'url="{_xml_attr(abs_url)}"',
                'medium="image"',
                f'type="{_xml_attr(mime)}"',
            ]
            if img.width > 0:
                attrs.append(f'width="{img.width}"')
            if img.height > 0:
                attrs.append(f'height="{img.height}"')
            if img.size:
                attrs.append(f'fileSize="{img.size}"')

            if idx == 0:
                enc_len = img.size or 0
                lines.append(
                    f'      <enclosure url="{_xml_attr(abs_url)}" length="{enc_len}" type="{_xml_attr(mime)}" />'
                )

            if img.thumb_url:
                abs_thumb = resolve_absolute_media_url(
                    img.thumb_url, base_url=settings.BASE_URL, api_prefix=settings.API_PREFIX
                )
                if abs_thumb:
                    lines.append(f"      <media:content {' '.join(attrs)}>")
                    lines.append(f'        <media:thumbnail url="{_xml_attr(abs_thumb)}" />')
                    lines.append("      </media:content>")
                    continue
            lines.append(f"      <media:content {' '.join(attrs)} />")

        lines.append("    </item>")

    lines.extend(["  </channel>", "</rss>", ""])
    return "\n".join(lines)


def build_atom_xml(posts: list[Post], settings: Settings, updated_at: datetime) -> str:
    """Build Atom 1.0 XML feed with full HTML <content type="html"> and accurate <updated>."""
    feed_posts = [p for p in posts if p.type != "service"][: settings.FEED_MAX_ITEMS]
    site_url = settings.SITE_URL.rstrip("/")
    self_url = f"{settings.api_base_url}/feeds/atom.xml"

    latest_dt = _to_utc(updated_at)
    if feed_posts:
        latest_dt = max(_to_utc(p.edited_at or p.date) for p in feed_posts)

    lines: list[str] = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<feed xmlns="http://www.w3.org/2005/Atom">',
        f"  <id>{_xml_text(site_url)}</id>",
        f"  <title>{_xml_text(settings.FEED_TITLE)}</title>",
        f"  <subtitle>{_xml_text(settings.FEED_DESCRIPTION)}</subtitle>",
        f"  <updated>{format_iso_utc(latest_dt)}</updated>",
        f'  <link href="{_xml_attr(site_url)}" rel="alternate" type="text/html" />',
        f'  <link href="{_xml_attr(self_url)}" rel="self" type="application/atom+xml" />',
        "  <author>",
        f"    <name>{_xml_text(settings.FEED_AUTHOR)}</name>",
        "  </author>",
    ]

    for post in feed_posts:
        link = _post_permalink(site_url, post.id)
        title = _post_title(post, settings.FEED_TITLE)
        published = format_iso_utc(post.date)
        modified = format_iso_utc(post.edited_at or post.date)
        content_html = render_post_feed_html(post, settings)

        lines.append("  <entry>")
        lines.append(f"    <id>{_xml_text(link)}</id>")
        lines.append(f"    <title>{_xml_text(title)}</title>")
        lines.append(f'    <link href="{_xml_attr(link)}" rel="alternate" type="text/html" />')
        lines.append(f"    <published>{published}</published>")
        lines.append(f"    <updated>{modified}</updated>")
        if post.author:
            lines.append(f"    <author><name>{_xml_text(post.author)}</name></author>")
        lines.append(f'    <content type="html">{_xml_text(content_html)}</content>')
        lines.append("  </entry>")

    lines.extend(["</feed>", ""])
    return "\n".join(lines)


def build_json_feed(posts: list[Post], settings: Settings) -> dict[str, Any]:
    """Build JSON Feed 1.1 with content_html and native multi-image attachments[]."""
    feed_posts = [p for p in posts if p.type != "service"][: settings.FEED_MAX_ITEMS]
    site_url = settings.SITE_URL.rstrip("/")
    self_url = f"{settings.api_base_url}/feeds/feed.json"

    items: list[dict[str, Any]] = []
    for post in feed_posts:
        link = _post_permalink(site_url, post.id)
        title = _post_title(post, settings.FEED_TITLE)
        content_html = render_post_feed_html(post, settings)
        content_text = _strip_illegal_xml(_html_to_plain_text(post.text_html))

        attachments: list[dict[str, Any]] = []
        first_image_url: str | None = None
        for img in sorted(post.images, key=lambda i: i.sort_order):
            abs_url = resolve_absolute_media_url(
                img.url, base_url=settings.BASE_URL, api_prefix=settings.API_PREFIX
            )
            if not abs_url:
                continue
            if first_image_url is None:
                first_image_url = abs_url
            att: dict[str, Any] = {
                "url": abs_url,
                "mime_type": _guess_mime(abs_url, "image/jpeg"),
            }
            if img.original_name:
                att["title"] = img.original_name
            if img.size is not None:
                att["size_in_bytes"] = img.size
            attachments.append(att)

        if post.files:
            for f in post.files:
                if not isinstance(f, dict) or not f.get("url"):
                    continue
                abs_url = resolve_absolute_media_url(
                    str(f["url"]), base_url=settings.BASE_URL, api_prefix=settings.API_PREFIX
                )
                if not abs_url:
                    continue
                att = {
                    "url": abs_url,
                    "mime_type": str(
                        f.get("mime_type") or _guess_mime(abs_url, "application/octet-stream")
                    ),
                }
                if f.get("original_name") or f.get("title"):
                    att["title"] = str(f.get("original_name") or f.get("title"))
                if f.get("size") is not None:
                    att["size_in_bytes"] = int(f["size"])
                attachments.append(att)

        item: dict[str, Any] = {
            "id": str(post.id),
            "url": link,
            "title": title,
            "content_html": content_html,
            "date_published": format_iso_utc(post.date),
        }
        if content_text:
            item["content_text"] = content_text
        if post.edited_at is not None:
            item["date_modified"] = format_iso_utc(post.edited_at)
        if post.author:
            item["authors"] = [{"name": post.author}]
        if first_image_url:
            item["image"] = first_image_url
        if attachments:
            item["attachments"] = attachments

        items.append(item)

    feed: dict[str, Any] = {
        "version": "https://jsonfeed.org/version/1.1",
        "title": settings.FEED_TITLE,
        "home_page_url": site_url,
        "feed_url": self_url,
        "description": settings.FEED_DESCRIPTION,
        "language": settings.FEED_LANGUAGE,
        "authors": [{"name": settings.FEED_AUTHOR}],
        "items": items,
    }
    if settings.FEED_IMAGE_URL:
        feed["icon"] = settings.FEED_IMAGE_URL
    return feed


def atomic_write_bytes(target: Path, data: bytes) -> None:
    """Atomically write bytes to target using a same-directory temporary file + os.replace."""
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as tmp:
            tmp_path = Path(tmp.name)
            tmp.write(data)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp_path, target)
    except Exception:
        if tmp_path is not None and tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        raise


def atomic_write_text(target: Path, content: str) -> None:
    atomic_write_bytes(target, content.encode("utf-8"))


def atomic_write_json(target: Path, payload: Any) -> None:
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    atomic_write_text(target, raw)


def _encode_json_bytes(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _write_export_bundle_sync(
    exports_dir: Path,
    *,
    posts_json_bytes: bytes,
    page_files: dict[str, bytes],
    meta_bytes: bytes,
    rss_bytes: bytes,
    atom_bytes: bytes,
    feed_json_bytes: bytes,
) -> None:
    """Execute all blocking disk I/O (temp writes, fsync, os.replace, stale unlink) in a worker thread."""
    pages_dir = exports_dir / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)

    atomic_write_bytes(exports_dir / "posts.json", posts_json_bytes)

    for filename, chunk_bytes in page_files.items():
        atomic_write_bytes(pages_dir / filename, chunk_bytes)

    for existing in pages_dir.glob("*.json"):
        if existing.name == "meta.json":
            continue
        if existing.name not in page_files:
            existing.unlink(missing_ok=True)

    atomic_write_bytes(pages_dir / "meta.json", meta_bytes)
    atomic_write_bytes(exports_dir / "rss.xml", rss_bytes)
    atomic_write_bytes(exports_dir / "atom.xml", atom_bytes)
    atomic_write_bytes(exports_dir / "feed.json", feed_json_bytes)


async def _generate_exports_unlocked(session: AsyncSession, settings: Settings) -> dict[str, Any]:
    now = datetime.now(UTC)
    posts_asc = await list_posts(session, descending=False, include_deleted=False)
    posts_desc = list(reversed(posts_asc))

    serialized_asc = [serialize_post(p, api_prefix=settings.API_PREFIX) for p in posts_asc]
    serialized_desc = list(reversed(serialized_asc))

    page_size = max(1, int(settings.EXPORT_PAGE_SIZE))
    chunks: list[list[dict[str, Any]]] = [
        serialized_desc[i : i + page_size] for i in range(0, len(serialized_desc), page_size)
    ]
    if not chunks:
        chunks = [[]]

    page_files: dict[str, bytes] = {
        f"{page_idx}.json": _encode_json_bytes(chunk) for page_idx, chunk in enumerate(chunks)
    }

    meta = {
        "total_posts": len(serialized_asc),
        "page_size": page_size,
        "latest_id": serialized_asc[-1]["id"] if serialized_asc else None,
        "updated_at": format_iso_utc(now),
    }

    posts_json_bytes = _encode_json_bytes(serialized_asc)
    meta_bytes = _encode_json_bytes(meta)
    rss_bytes = build_rss_xml(posts_desc, settings, updated_at=now).encode("utf-8")
    atom_bytes = build_atom_xml(posts_desc, settings, updated_at=now).encode("utf-8")
    feed_json_bytes = _encode_json_bytes(build_json_feed(posts_desc, settings))

    await asyncio.to_thread(
        _write_export_bundle_sync,
        settings.exports_dir,
        posts_json_bytes=posts_json_bytes,
        page_files=page_files,
        meta_bytes=meta_bytes,
        rss_bytes=rss_bytes,
        atom_bytes=atom_bytes,
        feed_json_bytes=feed_json_bytes,
    )
    return meta


async def generate_exports(session: AsyncSession, settings: Settings) -> dict[str, Any]:
    """Atomically regenerate posts.json, paginated pages/*, rss.xml, atom.xml, and feed.json."""
    lock = _get_export_lock(settings.exports_dir)
    async with lock:
        return await _generate_exports_unlocked(session, settings)


async def ensure_export_file(session: AsyncSession, settings: Settings, target: Path) -> None:
    """Ensure an export file exists on disk without thundering-herd regeneration."""
    if target.is_file():
        return
    lock = _get_export_lock(settings.exports_dir)
    async with lock:
        if target.is_file():
            return
        await _generate_exports_unlocked(session, settings)
