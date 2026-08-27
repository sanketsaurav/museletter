"""Applying normalized delivery events to the ledger and suppression list.

Shared by the SNS webhook (SES), the Cloudflare queue poller, and the sender's
handling of synchronous bounces. Events arrive at-least-once from every source
(SNS redelivers, queue pulls redeliver until acked), so every transition here
is guarded by current row state and replays are no-ops; the events table is an
append-only log.
"""

from .db import utcnow

_ROW_STATUS = {"bounce": "bounced", "complaint": "complained"}


async def record_event(db, type: str, email: str, message_id: str, payload: str) -> None:
    await db.execute(
        "INSERT INTO events (type, email, ses_message_id, payload, created_at) VALUES (?, ?, ?, ?, ?)",
        (type, email, message_id or None, payload[:10000], utcnow()),
    )


async def suppress(db, email: str, reason: str, detail: str) -> None:
    """Permanently suppress an address and opt its subscriptions out. Additive
    only: suppressions are never auto-removed."""
    await db.execute(
        "INSERT OR IGNORE INTO suppressions (email, reason, detail, created_at) VALUES (?, ?, ?, ?)",
        (email, reason, detail, utcnow()),
    )
    await db.execute(
        "UPDATE subscribers SET status = ? WHERE email = ? AND status IN ('active', 'unconfirmed')",
        (_ROW_STATUS.get(reason, "bounced"), email),
    )


async def apply_events(db, events: list[dict], *, match_by_email: bool = False) -> int:
    """Record events ({type, email, message_id, permanent, detail} dicts) and
    move ledger rows. Rows are matched by provider message id when the event
    carries one; match_by_email adds a fallback on the recipient address for
    rows that never got a message id (Cloudflare's REST send response returns
    none). The fallback only ever touches id-less in-flight rows, so it can
    misattribute at most between two overlapping sends to the same address."""
    now = utcnow()
    for event in events:
        await record_event(db, event["type"], event["email"], event["message_id"], event["detail"])
        if event["type"] == "delivery":
            matched = 0
            if event["message_id"]:
                cur = await db.execute(
                    "UPDATE campaign_recipients SET status = 'delivered', updated_at = ? "
                    "WHERE ses_message_id = ? AND status = 'sent'",
                    (now, event["message_id"]),
                )
                matched = cur.rowcount
            if match_by_email and matched <= 0 and event["email"]:
                await db.execute(
                    "UPDATE campaign_recipients SET status = 'delivered', updated_at = ? "
                    "WHERE email = ? AND ses_message_id IS NULL AND status = 'sent'",
                    (now, event["email"]),
                )
        elif event["type"] in ("bounce", "complaint"):
            new_status = _ROW_STATUS[event["type"]]
            matched = 0
            if event["message_id"]:
                cur = await db.execute(
                    "UPDATE campaign_recipients SET status = ?, error = ?, updated_at = ? "
                    "WHERE ses_message_id = ?",
                    (new_status, event["detail"], now, event["message_id"]),
                )
                matched = cur.rowcount
            if match_by_email and matched <= 0 and event["email"]:
                await db.execute(
                    "UPDATE campaign_recipients SET status = ?, error = ?, updated_at = ? "
                    "WHERE email = ? AND ses_message_id IS NULL AND status IN ('sent', 'delivered')",
                    (new_status, event["detail"], now, event["email"]),
                )
            if event["permanent"]:
                await suppress(db, event["email"], event["type"], event["detail"])
    await db.commit()
    return len(events)
