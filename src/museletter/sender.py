"""The send loop: drains pending campaign_recipients rows through SES.

The ledger is the source of truth. Every recipient row moves
pending → sent (→ delivered/bounced/complained via SNS webhooks), or to
suppressed/failed. A crash mid-campaign resumes from the remaining pending
rows on restart; delivery is at-least-once with the ledger as dedupe.
"""

import asyncio
import logging
from string import Template

import httpx

from .db import BUILTIN_TEMPLATE_ID, utcnow
from .render import personalize_email, render_campaign, validate_template
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
            "SELECT c.*, l.name AS list_name, l.template_id AS list_template_id "
            "FROM campaigns c JOIN lists l ON l.id = c.list_id "
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
        # Render the Markdown once for the whole campaign; only per-recipient
        # personalization (cheap token substitution) runs inside the loop.
        body = render_campaign(campaign["subject"], campaign["body_markdown"])
        template = await self._campaign_template(campaign)

        for recipient in batch:
            if self._stopped:
                return True
            # The batch is an in-memory snapshot; re-read the row and subscriber
            # fresh so a suppression, unsubscribe, or opt-out that landed after
            # the fetch is honored before we hit SES.
            async with db.execute(
                "SELECT cr.status AS row_status, s.status AS sub_status, "
                "(SELECT 1 FROM suppressions sup WHERE sup.email = cr.email) AS suppressed "
                "FROM campaign_recipients cr JOIN subscribers s ON s.id = cr.subscriber_id "
                "WHERE cr.campaign_id = ? AND cr.subscriber_id = ?",
                (recipient["campaign_id"], recipient["subscriber_id"]),
            ) as cur:
                current = await cur.fetchone()
            if current is None or current["row_status"] != "pending":
                continue  # deleted or already resolved by a concurrent writer
            if current["suppressed"] or current["sub_status"] != "active":
                await self._mark(recipient, "suppressed", error="suppressed before send")
                continue

            unsubscribe_url = f"{settings.base_url}/unsubscribe/{make_token(secret, 'unsubscribe', recipient['subscriber_id'])}"
            subject, html, text = personalize_email(
                body,
                name=recipient["name"],
                email=recipient["email"],
                unsubscribe_url=unsubscribe_url,
                list_name=campaign["list_name"],
                postal_address=settings.postal_address,
                template=template,
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

            # Guard on status = 'pending': if an unsubscribe flipped this row to
            # 'suppressed' during the SES call, don't overwrite that with 'sent'.
            await self._mark(recipient, "sent", message_id=message_id, only_pending=True)
            await asyncio.sleep(delay)
        return True

    async def _campaign_template(self, campaign) -> Template | None:
        """Compiled custom template for a campaign, or None for the built-in.
        The API validates templates on write and blocks deleting a referenced
        one, so a missing or invalid row here means manual DB surgery; fall back
        to the built-in (loudly) rather than wedge the whole send queue."""
        template_id = campaign["template_id"] or campaign["list_template_id"]
        if not template_id or template_id == BUILTIN_TEMPLATE_ID:
            return None
        async with self.app.state.db.execute(
            "SELECT html FROM templates WHERE id = ?", (template_id,)
        ) as cur:
            row = await cur.fetchone()
        if row is None or validate_template(row["html"]):
            logger.error(
                "template %s for campaign %s is missing or invalid; using the built-in default",
                template_id,
                campaign["id"],
            )
            return None
        return Template(row["html"])

    async def _handle_send_error(self, recipient, attempts: int, error: str) -> None:
        if attempts >= MAX_ATTEMPTS:
            await self._mark(recipient, "failed", error=error)
            logger.error("giving up on %s after %d attempts: %s", recipient["email"], attempts, error)
        else:
            logger.warning("send to %s failed (attempt %d): %s", recipient["email"], attempts, error)

    async def _mark(
        self, recipient, status: str, message_id: str = "", error: str = "", only_pending: bool = False
    ) -> None:
        db = self.app.state.db
        guard = " AND status = 'pending'" if only_pending else ""
        await db.execute(
            "UPDATE campaign_recipients SET status = ?, ses_message_id = ?, error = ?, updated_at = ? "
            f"WHERE campaign_id = ? AND subscriber_id = ?{guard}",
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
