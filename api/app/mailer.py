from __future__ import annotations

import asyncio
import html
import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage

import aiosmtplib
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.security import format_anon_display_name, make_unsubscribe_token
from app.config import Settings
from app.db.models import Comment, NotificationQueue, User
from app.db.session import get_session_factory
from app.sse import SSEBus

logger = logging.getLogger(__name__)

SmtpSender = Callable[[EmailMessage], Awaitable[None]]

DUE_BATCH_LIMIT = 50
EXTRA_COALESCE_LIMIT = 200
LEASE_SECONDS = 120


class Mailer:
    """Debounced async SMTP notification worker + magic-link email sender."""

    def __init__(
        self,
        settings: Settings,
        sse_bus: SSEBus,
        *,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        smtp_sender: SmtpSender | None = None,
        poll_interval: float = 30.0,
    ) -> None:
        self.settings = settings
        self.sse_bus = sse_bus
        self._session_factory = session_factory
        self._smtp_sender = smtp_sender
        self.poll_interval = poll_interval
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()

    async def _default_smtp_send(self, msg: EmailMessage) -> None:
        if not self.settings.SMTP_HOST:
            logger.info(
                "SMTP_HOST not configured; skipping email to %s (subject=%r)",
                msg["To"],
                msg["Subject"],
            )
            return

        use_implicit_tls = self.settings.SMTP_USE_TLS and self.settings.SMTP_PORT == 465
        use_start_tls = self.settings.SMTP_USE_TLS and self.settings.SMTP_PORT != 465

        await aiosmtplib.send(
            msg,
            hostname=self.settings.SMTP_HOST,
            port=self.settings.SMTP_PORT,
            username=self.settings.SMTP_USER,
            password=self.settings.SMTP_PASSWORD,
            use_tls=use_implicit_tls,
            start_tls=use_start_tls,
            timeout=15,
        )

    async def send_email(self, msg: EmailMessage) -> None:
        if self._smtp_sender is not None:
            await self._smtp_sender(msg)
        else:
            await self._default_smtp_send(msg)

    async def send_magic_link_email(self, email: str, verify_url: str) -> None:
        msg = EmailMessage()
        msg["From"] = self.settings.SMTP_FROM
        msg["To"] = email
        msg["Subject"] = f"Sign in to {self.settings.FEED_TITLE}"

        plain = (
            f"Click the link below to sign in to {self.settings.FEED_TITLE}:\n\n"
            f"{verify_url}\n\n"
            "This link expires in 15 minutes and can only be used once.\n"
            "If you did not request this email, you can safely ignore it.\n"
        )
        safe_title = html.escape(self.settings.FEED_TITLE)
        safe_url = html.escape(verify_url, quote=True)
        html_body = (
            f"<p>Click the button below to sign in to <strong>{safe_title}</strong>:</p>"
            f'<p><a href="{safe_url}">Sign in now</a></p>'
            f"<p><small>Or paste this link into your browser: {safe_url}</small></p>"
            "<p><small>This link expires in 15 minutes and can only be used once.</small></p>"
        )

        msg.set_content(plain)
        msg.add_alternative(html_body, subtype="html")
        await self.send_email(msg)

    def _build_reply_digest_email(
        self,
        *,
        recipient: User,
        post_id: int,
        comments: list[Comment],
        authors_by_user_id: dict[int, User],
    ) -> EmailMessage:
        count = len(comments)
        post_url = f"{self.settings.SITE_URL.rstrip('/')}?shared={post_id}"
        unsub_token = make_unsubscribe_token(recipient.id, self.settings.SECRET_KEY)
        unsub_url = f"{self.settings.api_base_url}/auth/unsubscribe?token={unsub_token}"

        subject = (
            f"New reply on {self.settings.FEED_TITLE}"
            if count == 1
            else f"{count} new replies on {self.settings.FEED_TITLE}"
        )

        plain_lines = [
            f"Hi {recipient.display_name},",
            "",
            (
                f"You received a new reply on post #{post_id}:"
                if count == 1
                else f"You received {count} new replies on post #{post_id}:"
            ),
            "",
        ]
        html_items: list[str] = []

        for c in comments:
            author_name = self._comment_author_name(c, authors_by_user_id)
            excerpt = c.body_text.strip()
            if len(excerpt) > 300:
                excerpt = excerpt[:297] + "..."
            plain_lines.append(f"- {author_name}: {excerpt}")
            html_items.append(
                f"<li><strong>{html.escape(author_name)}</strong>: "
                f"{html.escape(excerpt)}</li>"
            )

        plain_lines.extend(
            [
                "",
                f"View thread: {post_url}",
                "",
                f"Unsubscribe from reply notifications: {unsub_url}",
            ]
        )

        safe_recipient = html.escape(recipient.display_name)
        safe_post_url = html.escape(post_url, quote=True)
        safe_unsub_url = html.escape(unsub_url, quote=True)
        html_body = (
            f"<p>Hi {safe_recipient},</p>"
            f"<p>You received {count} new {'reply' if count == 1 else 'replies'} "
            f"on post #{post_id}:</p>"
            f"<ul>{''.join(html_items)}</ul>"
            f'<p><a href="{safe_post_url}">View thread</a></p>'
            f'<p><small><a href="{safe_unsub_url}">Unsubscribe from reply notifications</a></small></p>'
        )

        msg = EmailMessage()
        msg["From"] = self.settings.SMTP_FROM
        msg["To"] = recipient.email
        msg["Subject"] = subject
        msg["List-Unsubscribe"] = f"<{unsub_url}>"
        msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
        msg.set_content("\n".join(plain_lines))
        msg.add_alternative(html_body, subtype="html")
        return msg

    @staticmethod
    def _comment_author_name(comment: Comment, authors_by_user_id: dict[int, User]) -> str:
        if comment.user_id is not None and comment.user_id in authors_by_user_id:
            return authors_by_user_id[comment.user_id].display_name
        if comment.source == "telegram" and comment.tg_author_name:
            return comment.tg_author_name
        return format_anon_display_name(comment.anon_tag)

    async def _claim_batch(
        self,
        db: AsyncSession,
        *,
        current_time: datetime,
    ) -> list[tuple[int, int, list[int], EmailMessage]]:
        """Claim/lease a bounded batch in a short transaction and release locks immediately."""
        due_stmt = (
            select(NotificationQueue)
            .where(
                NotificationQueue.sent_at.is_(None),
                NotificationQueue.send_after <= current_time,
                NotificationQueue.attempts < 5,
            )
            .order_by(NotificationQueue.id.asc())
            .limit(DUE_BATCH_LIMIT)
            .with_for_update(skip_locked=True)
        )
        due_rows = list((await db.execute(due_stmt)).scalars().all())
        if not due_rows:
            return []

        grouped: dict[tuple[int, int], list[NotificationQueue]] = defaultdict(list)
        due_ids = {row.id for row in due_rows}
        for row in due_rows:
            grouped[(row.recipient_user_id, row.post_id)].append(row)

        # Coalesce any already-queued items for the same (recipient, post)
        for (recipient_id, post_id), rows in grouped.items():
            extra_stmt = (
                select(NotificationQueue)
                .where(
                    NotificationQueue.recipient_user_id == recipient_id,
                    NotificationQueue.post_id == post_id,
                    NotificationQueue.sent_at.is_(None),
                    NotificationQueue.attempts < 5,
                    NotificationQueue.id.notin_(due_ids),
                )
                .order_by(NotificationQueue.id.asc())
                .limit(EXTRA_COALESCE_LIMIT)
                .with_for_update(skip_locked=True)
            )
            extra_rows = list((await db.execute(extra_stmt)).scalars().all())
            rows.extend(extra_rows)

        pending_sends: list[tuple[int, int, list[int], EmailMessage]] = []
        lease_until = current_time + timedelta(seconds=LEASE_SECONDS)

        for (recipient_id, post_id), rows in grouped.items():
            recipient = await db.get(User, recipient_id)
            if (
                recipient is None
                or not recipient.email
                or not recipient.email_verified
                or not recipient.notify_replies
            ):
                for row in rows:
                    row.sent_at = current_time
                continue

            if self.sse_bus.is_user_viewing(recipient_id, post_id):
                for row in rows:
                    row.sent_at = current_time
                continue

            comment_ids = [row.comment_id for row in rows]
            comments_stmt = (
                select(Comment)
                .where(
                    Comment.id.in_(comment_ids),
                    Comment.deleted_at.is_(None),
                    Comment.moderation_status == "approved",
                )
                .order_by(Comment.created_at.asc(), Comment.id.asc())
            )
            comments = list((await db.execute(comments_stmt)).scalars().all())
            if not comments:
                for row in rows:
                    row.sent_at = current_time
                continue

            author_ids = {c.user_id for c in comments if c.user_id is not None}
            authors_by_id: dict[int, User] = {}
            if author_ids:
                author_rows = (
                    await db.execute(select(User).where(User.id.in_(author_ids)))
                ).scalars().all()
                authors_by_id = {u.id: u for u in author_rows}

            msg = self._build_reply_digest_email(
                recipient=recipient,
                post_id=post_id,
                comments=comments,
                authors_by_user_id=authors_by_id,
            )
            # Lease rows so concurrent workers skip them while SMTP I/O runs outside DB tx
            for row in rows:
                row.send_after = lease_until
            pending_sends.append(
                (recipient_id, post_id, [row.id for row in rows], msg)
            )

        # Commit immediately to release all FOR UPDATE row locks before SMTP I/O!
        await db.commit()
        return pending_sends

    async def _finalize_group(
        self,
        db: AsyncSession,
        *,
        row_ids: list[int],
        current_time: datetime,
        error: Exception | None,
    ) -> None:
        rows = list(
            (
                await db.execute(
                    select(NotificationQueue).where(
                        NotificationQueue.id.in_(row_ids),
                        NotificationQueue.sent_at.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        if error is None:
            for row in rows:
                row.sent_at = current_time
                row.last_error = None
        else:
            err_text = str(error)[:500]
            for row in rows:
                row.attempts += 1
                row.last_error = err_text
                backoff_seconds = 60 * (2 ** (row.attempts - 1))
                row.send_after = current_time + timedelta(seconds=backoff_seconds)
        await db.commit()

    async def process_once(
        self,
        session: AsyncSession | None = None,
        *,
        now: datetime | None = None,
    ) -> int:
        """Drain due notification_queue items without holding DB locks across SMTP I/O."""
        current_time = now or datetime.now(UTC)

        if session is not None:
            pending_sends = await self._claim_batch(session, current_time=current_time)
        else:
            factory = self._session_factory or get_session_factory()
            async with factory() as claim_db:
                pending_sends = await self._claim_batch(
                    claim_db, current_time=current_time
                )

        if not pending_sends:
            return 0

        emails_sent = 0
        for recipient_id, post_id, row_ids, msg in pending_sends:
            send_error: Exception | None = None
            try:
                await self.send_email(msg)
            except Exception as exc:
                logger.warning(
                    "Failed sending notification email to user %s for post %s: %s",
                    recipient_id,
                    post_id,
                    exc,
                )
                send_error = exc
            else:
                emails_sent += 1

            if session is not None:
                await self._finalize_group(
                    session,
                    row_ids=row_ids,
                    current_time=current_time,
                    error=send_error,
                )
            else:
                factory = self._session_factory or get_session_factory()
                async with factory() as fin_db:
                    await self._finalize_group(
                        fin_db,
                        row_ids=row_ids,
                        current_time=current_time,
                        error=send_error,
                    )

        return emails_sent

    async def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self.process_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Unexpected error in Mailer worker loop")

            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self.poll_interval
                )
            except TimeoutError:
                continue

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run_loop(), name="corner-mailer-worker")

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None
