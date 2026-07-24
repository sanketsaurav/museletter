"""The send loop: drains pending campaign_recipients rows through SES.

The ledger is the source of truth. Every recipient row moves
pending → sent (→ delivered/bounced/complained via SNS webhooks), or to
suppressed/failed. A crash mid-campaign resumes from the remaining pending
rows on restart; delivery is at-least-once with the ledger as dedupe.
"""

import asyncio
import logging

import httpx

from .db import utcnow
from .render import build_email
from .ses import SESError
from .tokens import make_token

logger = logging.getLogger("museletter.sender")

MAX_ATTEMPTS = 5
IDLE_POLL_SECONDS = 15.0
THROTTLE_BACKOFF_SECONDS = 5.0
BATCH_SIZE = 50


class SenderLoop:
    def __init__(self, app):
        self.app = app
        self._stopped = False
        self._wake = asyncio.Event()

    def wake(self) -> None:
        self._wake.set()

    def stop(self) -> None:
        self._stopped = True
        self._wake.set()

    async def run(self) -> None:
        logger.info("sender loop started")
        while not self._stopped:
            try:
                worked = await self.tick()
            except Exception:
                logger.exception("sender tick failed")
                worked = False
                await asyncio.sleep(5)
            if not worked and not self._stopped:
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=IDLE_POLL_SECONDS)
                except TimeoutError:
                    pass
                self._wake.clear()
        logger.info("sender loop stopped")

    async def tick(self) -> bool:
        """Process one batch for the oldest in-flight campaign. Returns True if
        any work was done."""
        db = self.app.state.db
        async with db.execute(
            "SELECT c.*, l.name AS list_name FROM campaigns c JOIN lists l ON l.id = c.list_id "
            "WHERE c.status = 'sending' ORDER BY c.started_at LIMIT 1"
        ) as cur:
            campaign = await cur.fetchone()
        if campaign is None:
            return False

        async with db.execute(
            "SELECT * FROM campaign_recipients WHERE campaign_id = ? AND status = 'pending' LIMIT ?",
            (campaign["id"], BATCH_SIZE),
        ) as cur:
            batch = await cur.fetchall()

        if not batch:
            await self._finalize(campaign)
            return True

        settings = self.app.state.settings
        secret = self.app.state.secret
        delay = 1.0 / max(settings.send_rate, 0.1)

        for recipient in batch:
            if self._stopped:
                return True
            async with db.execute("SELECT 1 FROM suppressions WHERE email = ?", (recipient["email"],)) as cur:
                if await cur.fetchone():
                    await self._mark(recipient, "suppressed", error="suppressed before send")
                    continue

            unsubscribe_url = f"{settings.base_url}/unsubscribe/{make_token(secret, 'unsubscribe', recipient['subscriber_id'])}"
            subject, html, text = build_email(
                campaign["subject"],
                campaign["body_markdown"],
                name=recipient["name"],
                email=recipient["email"],
                unsubscribe_url=unsubscribe_url,
                list_name=campaign["list_name"],
                postal_address=settings.postal_address,
            )
            headers = {
                "List-Unsubscribe": f"<{unsubscribe_url}>",
                "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
            }

            await db.execute(
                "UPDATE campaign_recipients SET attempts = attempts + 1, updated_at = ? "
                "WHERE campaign_id = ? AND subscriber_id = ?",
                (utcnow(), recipient["campaign_id"], recipient["subscriber_id"]),
            )
            await db.commit()
            attempts = recipient["attempts"] + 1

            try:
                message_id = await self.app.state.ses.send_email(
                    recipient["email"],
                    subject,
                    html,
                    text,
                    from_email=settings.from_email,
                    from_name=settings.from_name,
                    headers=headers,
                )
            except SESError as exc:
                if exc.throttled:
                    # Not this recipient's fault: un-count the attempt, back off,
                    # and let the next tick retry the whole batch.
                    await db.execute(
                        "UPDATE campaign_recipients SET attempts = attempts - 1 "
                        "WHERE campaign_id = ? AND subscriber_id = ?",
                        (recipient["campaign_id"], recipient["subscriber_id"]),
                    )
                    await db.commit()
                    logger.warning("SES throttled; backing off %.0fs", THROTTLE_BACKOFF_SECONDS)
                    await asyncio.sleep(THROTTLE_BACKOFF_SECONDS)
                    return True
                await self._handle_send_error(recipient, attempts, f"{exc.code}: {exc.message}")
                continue
            except (httpx.HTTPError, OSError) as exc:
                await self._handle_send_error(recipient, attempts, f"network: {exc}")
                await asyncio.sleep(1)
                continue

            await self._mark(recipient, "sent", message_id=message_id)
            await asyncio.sleep(delay)
        return True

    async def _handle_send_error(self, recipient, attempts: int, error: str) -> None:
        if attempts >= MAX_ATTEMPTS:
            await self._mark(recipient, "failed", error=error)
            logger.error("giving up on %s after %d attempts: %s", recipient["email"], attempts, error)
        else:
            logger.warning("send to %s failed (attempt %d): %s", recipient["email"], attempts, error)

    async def _mark(self, recipient, status: str, message_id: str = "", error: str = "") -> None:
        db = self.app.state.db
        await db.execute(
            "UPDATE campaign_recipients SET status = ?, ses_message_id = ?, error = ?, updated_at = ? "
            "WHERE campaign_id = ? AND subscriber_id = ?",
            (
                status,
                message_id or None,
                error or None,
                utcnow(),
                recipient["campaign_id"],
                recipient["subscriber_id"],
            ),
        )
        await db.commit()

    async def _finalize(self, campaign) -> None:
        db = self.app.state.db
        async with db.execute(
            "SELECT COUNT(*) AS n FROM campaign_recipients "
            "WHERE campaign_id = ? AND status IN ('sent', 'delivered', 'bounced', 'complained')",
            (campaign["id"],),
        ) as cur:
            reached = (await cur.fetchone())["n"]
        status = "sent" if reached > 0 or campaign["recipient_count"] == 0 else "failed"
        await db.execute(
            "UPDATE campaigns SET status = ?, completed_at = ? WHERE id = ?",
            (status, utcnow(), campaign["id"]),
        )
        await db.commit()
        logger.info("campaign %s finished: %s (%d reached)", campaign["id"], status, reached)
