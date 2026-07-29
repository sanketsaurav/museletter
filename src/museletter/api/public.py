import html as html_mod
import json
import time
from datetime import UTC, datetime

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from ..db import new_id, utcnow
from ..render import load_template, render_confirmation
from ..sns import is_amazon_sns_url, parse_ses_events
from ..tokens import make_token, verify_token
from .common import normalize_email, valid_email

router = APIRouter()


class RateLimiter:
    """Small in-memory limiter for the public subscribe endpoint."""

    def __init__(self, limit: int = 10, window: float = 60.0):
        self.limit = limit
        self.window = window
        self.hits: dict[str, list[float]] = {}

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        hits = [t for t in self.hits.get(key, []) if now - t < self.window]
        if len(hits) >= self.limit:
            self.hits[key] = hits
            return False
        hits.append(now)
        self.hits[key] = hits
        if len(self.hits) > 10_000:
            self.hits = {k: v for k, v in self.hits.items() if v and now - v[-1] < self.window}
        return True


def client_ip(request: Request) -> str:
    """The requester's IP. Behind a proxy (Fly/Railway/Cloudflare) the socket
    peer is the proxy, so all visitors share one bucket unless we read the
    forwarded header, and only when the operator opts in via trust_proxy."""
    if request.app.state.settings.trust_proxy:
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


_PAGE_TEMPLATE = load_template("page.html")


_MARK_SVG = (
    '<svg class="mark" viewBox="0 0 32 32" aria-hidden="true">'
    '<rect x="9.40" y="7.85" width="13.20" height="2.8" rx="1.40" transform="rotate(0 16 9.25)"/>'
    '<rect x="9.40" y="7.85" width="13.20" height="2.8" rx="1.40" transform="rotate(60 16 9.25)"/>'
    '<rect x="9.40" y="7.85" width="13.20" height="2.8" rx="1.40" transform="rotate(120 16 9.25)"/>'
    '<rect x="1.60" y="21.35" width="13.20" height="2.8" rx="1.40" transform="rotate(0 8.2 22.75)"/>'
    '<rect x="1.60" y="21.35" width="13.20" height="2.8" rx="1.40" transform="rotate(60 8.2 22.75)"/>'
    '<rect x="1.60" y="21.35" width="13.20" height="2.8" rx="1.40" transform="rotate(120 8.2 22.75)"/>'
    '<rect x="17.20" y="21.35" width="13.20" height="2.8" rx="1.40" transform="rotate(0 23.8 22.75)"/>'
    '<rect x="17.20" y="21.35" width="13.20" height="2.8" rx="1.40" transform="rotate(60 23.8 22.75)"/>'
    '<rect x="17.20" y="21.35" width="13.20" height="2.8" rx="1.40" transform="rotate(120 23.8 22.75)"/>'
    "</svg>"
)


def _page(
    heading: str,
    body: str,
    *,
    variant: str = "accent",
    list_name: str = "Museletter",
    action: str = "",
    code: str = "",
) -> HTMLResponse:
    """Render a public page in the split-panel brand layout. `body` and `action`
    are trusted HTML built by the caller; `heading`, `list_name`, `code` are escaped."""
    code_html = f'<div class="panel-code">{html_mod.escape(code)}</div>' if code else ""
    action_html = f'<div class="actions">{action}</div>' if action else ""
    content = (
        f'<div class="panel {variant}">{_MARK_SVG}'
        f'<div><div class="panel-name">{html_mod.escape(list_name)}</div>{code_html}</div></div>'
        f'<div class="content"><div><h1>{html_mod.escape(heading)}</h1>{body}{action_html}</div>'
        '<div class="foot">Powered by '
        '<a href="https://github.com/sanketsaurav/museletter" style="text-decoration:underline;">Museletter</a>'
        "</div></div>"
    )
    return HTMLResponse(_PAGE_TEMPLATE.substitute(title=html_mod.escape(heading), content=content))


def _seconds_since(iso: str) -> float:
    try:
        then = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return float("inf")
    return (datetime.now(UTC) - then).total_seconds()


async def _send_confirmation_email(request: Request, lst, subscriber_id: str, email: str) -> None:
    settings = request.app.state.settings
    secret = request.app.state.secret
    confirm_url = f"{settings.base_url}/confirm/{make_token(secret, 'confirm', subscriber_id)}"
    subject, html, text = render_confirmation(
        list_name=lst["name"], confirm_url=confirm_url, postal_address=settings.postal_address
    )
    await request.app.state.ses.send_email(
        email, subject, html, text, from_email=settings.from_email, from_name=settings.from_name
    )


@router.post("/subscribe/{slug}")
async def subscribe(request: Request, slug: str):
    db = request.app.state.db
    settings = request.app.state.settings

    if not settings.public_subscribe:
        # Operator adds subscribers via the authenticated admin API; hide this route.
        raise HTTPException(status_code=404, detail="not found")

    if not request.app.state.rate_limiter.allow(client_ip(request)):
        raise HTTPException(status_code=429, detail="too many requests; try again in a minute")

    content_type = request.headers.get("content-type", "")
    if "json" in content_type:
        try:
            data = await request.json()
        except ValueError:
            raise HTTPException(status_code=422, detail="invalid JSON body") from None
    else:
        form = await request.form()
        data = dict(form)

    email = normalize_email(str(data.get("email", "")))
    name = str(data.get("name", "")).strip()[:200]
    honeypot = str(data.get("website", "")).strip()

    async with db.execute("SELECT * FROM lists WHERE slug = ?", (slug,)) as cur:
        lst = await cur.fetchone()
    if lst is None:
        raise HTTPException(status_code=404, detail="list not found")
    if not valid_email(email):
        raise HTTPException(status_code=422, detail="a valid email is required")

    if settings.turnstile_secret:
        token = str(data.get("cf-turnstile-response") or data.get("turnstile_token") or "")
        if not await request.app.state.turnstile_verify(settings.turnstile_secret, token, client_ip(request)):
            raise HTTPException(status_code=403, detail="human verification failed; please try again")

    # One response for every success path, so an attacker cannot probe whether an
    # address is already on the list.
    ok = (
        {"status": "pending_confirmation", "message": "Check your inbox to confirm your subscription."}
        if settings.opt_in == "double"
        else {"status": "subscribed", "message": "You're subscribed."}
    )

    # Bots fill the hidden field; suppressed addresses must not be resurrected by
    # form spam. Both look identical to a real signup from the outside.
    if honeypot:
        return JSONResponse(ok)
    async with db.execute("SELECT 1 FROM suppressions WHERE email = ?", (email,)) as cur:
        if await cur.fetchone():
            return JSONResponse(ok)

    async with db.execute(
        "SELECT * FROM subscribers WHERE list_id = ? AND email = ?", (lst["id"], email)
    ) as cur:
        existing = await cur.fetchone()

    if existing and existing["status"] == "active":
        return JSONResponse(ok)

    if existing:
        subscriber_id = existing["id"]
        if name:
            await db.execute("UPDATE subscribers SET name = ? WHERE id = ?", (name, subscriber_id))
    else:
        subscriber_id = new_id("sub")
        status = "active" if settings.opt_in == "single" else "unconfirmed"
        now = utcnow()
        await db.execute(
            "INSERT INTO subscribers (id, list_id, email, name, status, created_at, confirmed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (subscriber_id, lst["id"], email, name, status, now, now if status == "active" else None),
        )
    await db.commit()

    if settings.opt_in == "single":
        if existing:
            await db.execute(
                "UPDATE subscribers SET status = 'active', confirmed_at = ? WHERE id = ?",
                (utcnow(), subscriber_id),
            )
            await db.commit()
        return JSONResponse(ok)

    # Per-address cooldown: never send more than one confirmation email to an
    # address within the window, so distributed subscription-bombing of a victim
    # cannot make this instance flood their inbox (per-IP limits do not stop it).
    last_sent = existing["confirmation_sent_at"] if existing else None
    if last_sent and _seconds_since(last_sent) < settings.confirmation_cooldown:
        return JSONResponse(ok)

    try:
        await _send_confirmation_email(request, lst, subscriber_id, email)
    except Exception as exc:
        raise HTTPException(
            status_code=502, detail="could not send the confirmation email; try again later"
        ) from exc
    await db.execute(
        "UPDATE subscribers SET confirmation_sent_at = ? WHERE id = ?", (utcnow(), subscriber_id)
    )
    await db.commit()
    return JSONResponse(ok)


@router.get("/confirm/{token}")
async def confirm(request: Request, token: str):
    db = request.app.state.db
    invalid = ("Invalid link", "<p>This confirmation link is not valid.</p>")
    subscriber_id = verify_token(request.app.state.secret, token, "confirm")
    if subscriber_id is None:
        return _page(*invalid, variant="muted", code="link_invalid")
    async with db.execute(
        "SELECT s.*, l.name AS list_name FROM subscribers s JOIN lists l ON l.id = s.list_id WHERE s.id = ?",
        (subscriber_id,),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return _page(*invalid, variant="muted", code="link_invalid")
    lst = row["list_name"]
    escaped = html_mod.escape(lst)
    # Only a pending double-opt-in may be confirmed. A confirm link is a GET
    # that link scanners and prefetchers follow, so it must never resurrect a
    # subscriber who has since unsubscribed, bounced, or complained.
    if row["status"] == "unconfirmed":
        await db.execute(
            "UPDATE subscribers SET status = 'active', confirmed_at = ? WHERE id = ?",
            (utcnow(), subscriber_id),
        )
        await db.commit()
        return _page(
            "You're subscribed",
            f"<p>Welcome to <strong>{escaped}</strong>. Your first issue is on its way.</p>",
            list_name=lst,
        )
    if row["status"] == "active":
        return _page(
            "You're subscribed",
            f"<p>You are already subscribed to <strong>{escaped}</strong>.</p>",
            list_name=lst,
        )
    return _page(
        "Subscription closed",
        f"<p>This address is not subscribed to {escaped}. "
        f"Sign up again from the website if that is a mistake.</p>",
        variant="muted",
        list_name=lst,
    )


async def _subscriber_list_name(db, subscriber_id: str) -> str:
    async with db.execute(
        "SELECT l.name AS list_name FROM subscribers s JOIN lists l ON l.id = s.list_id WHERE s.id = ?",
        (subscriber_id,),
    ) as cur:
        row = await cur.fetchone()
    return row["list_name"] if row else "Museletter"


@router.get("/unsubscribe/{token}")
async def unsubscribe_page(request: Request, token: str):
    # GET must not mutate: link scanners and prefetchers follow every URL in an
    # email, and a destructive GET would silently unsubscribe real readers.
    subscriber_id = verify_token(request.app.state.secret, token, "unsubscribe")
    if subscriber_id is None:
        return _page(
            "Invalid link", "<p>This unsubscribe link is not valid.</p>", variant="muted", code="link_invalid"
        )
    list_name = await _subscriber_list_name(request.app.state.db, subscriber_id)
    action = (
        f'<form method="post" action="/unsubscribe/{html_mod.escape(token)}">'
        '<button class="btn btn-danger" type="submit">Unsubscribe</button></form>'
    )
    return _page(
        "Unsubscribe?",
        f"<p>You will stop receiving emails from <strong>{html_mod.escape(list_name)}</strong>. "
        "This takes effect immediately.</p>",
        variant="gray",
        list_name=list_name,
        action=action,
    )


@router.post("/unsubscribe/{token}")
async def unsubscribe(request: Request, token: str):
    db = request.app.state.db
    subscriber_id = verify_token(request.app.state.secret, token, "unsubscribe")
    if subscriber_id is None:
        return _page(
            "Invalid link", "<p>This unsubscribe link is not valid.</p>", variant="muted", code="link_invalid"
        )
    list_name = await _subscriber_list_name(db, subscriber_id)
    async with db.execute("SELECT * FROM subscribers WHERE id = ?", (subscriber_id,)) as cur:
        row = await cur.fetchone()
    if row is not None and row["status"] != "unsubscribed":
        await db.execute(
            "UPDATE subscribers SET status = 'unsubscribed', unsubscribed_at = ? WHERE id = ?",
            (utcnow(), subscriber_id),
        )
        await db.execute(
            "UPDATE campaign_recipients SET status = 'suppressed', updated_at = ? "
            "WHERE subscriber_id = ? AND status = 'pending'",
            (utcnow(), subscriber_id),
        )
        await db.commit()
    return _page(
        "Unsubscribed",
        "<p>You won't receive these emails anymore. You can re-subscribe any time from the website.</p>",
        list_name=list_name,
    )


@router.post("/webhooks/sns")
async def sns_webhook(request: Request):
    db = request.app.state.db
    settings = request.app.state.settings
    try:
        envelope = json.loads(await request.body())
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid JSON") from None
    if not isinstance(envelope, dict):
        raise HTTPException(status_code=400, detail="invalid SNS message")

    # A valid SNS signature only proves the message came from *some* AWS SNS
    # topic. Pin it to our own topic so an attacker can't sign a fake bounce
    # from a topic in their own account and suppress our subscribers.
    if settings.sns_topic_arn and envelope.get("TopicArn") != settings.sns_topic_arn:
        raise HTTPException(status_code=403, detail="unexpected SNS topic")

    if not settings.extra.get("skip_sns_verify"):
        if not await request.app.state.sns_verifier.verify(envelope):
            raise HTTPException(status_code=403, detail="SNS signature verification failed")

    msg_type = envelope.get("Type", "")

    if msg_type == "SubscriptionConfirmation":
        subscribe_url = envelope.get("SubscribeURL", "")
        if not is_amazon_sns_url(subscribe_url):
            raise HTTPException(status_code=400, detail="invalid SubscribeURL")
        async with httpx.AsyncClient(timeout=10) as client:
            await client.get(subscribe_url)
        return {"ok": True, "confirmed": True}

    if msg_type != "Notification":
        return {"ok": True, "ignored": msg_type}

    message = envelope.get("Message", "")
    events = parse_ses_events(message)
    now = utcnow()
    if not events:
        await db.execute(
            "INSERT INTO events (type, email, ses_message_id, payload, created_at) VALUES (?, ?, ?, ?, ?)",
            ("other", "", None, str(message)[:10000], now),
        )
        await db.commit()
        return {"ok": True, "processed": 0}

    for event in events:
        await db.execute(
            "INSERT INTO events (type, email, ses_message_id, payload, created_at) VALUES (?, ?, ?, ?, ?)",
            (event["type"], event["email"], event["message_id"], event["detail"], now),
        )
        if event["type"] == "delivery":
            await db.execute(
                "UPDATE campaign_recipients SET status = 'delivered', updated_at = ? "
                "WHERE ses_message_id = ? AND status = 'sent'",
                (now, event["message_id"]),
            )
        elif event["type"] in ("bounce", "complaint"):
            new_status = "bounced" if event["type"] == "bounce" else "complained"
            await db.execute(
                "UPDATE campaign_recipients SET status = ?, error = ?, updated_at = ? WHERE ses_message_id = ?",
                (new_status, event["detail"], now, event["message_id"]),
            )
            if event["permanent"]:
                await db.execute(
                    "INSERT OR IGNORE INTO suppressions (email, reason, detail, created_at) VALUES (?, ?, ?, ?)",
                    (event["email"], event["type"], event["detail"], now),
                )
                await db.execute(
                    "UPDATE subscribers SET status = ? WHERE email = ? AND status IN ('active', 'unconfirmed')",
                    (new_status, event["email"]),
                )
    await db.commit()
    return {"ok": True, "processed": len(events)}
