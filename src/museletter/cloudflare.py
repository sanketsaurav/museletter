"""Cloudflare Email Service: sending over the REST API and ingesting delivery
events from a Cloudflare Queue.

Sending is a bearer-token JSON POST (no request signing). The REST send
response classifies the recipient synchronously (delivered / permanent_bounces
/ queued) but carries no message id, so ledger correlation for async events
falls back to the recipient address; museletter sends exactly one recipient
per request, which keeps that fallback narrow.

Events: a Queues event subscription on the sending domain publishes
cf.email.sending.message.* events; EventPoller drains the queue with the
Queues HTTP pull API. Outbound HTTPS only - no Worker, no inbound webhook,
and the one-process design stays intact.
"""

import asyncio
import base64
import json
import logging
import os

import httpx

from .events import apply_events
from .mailer import SendError, SendResult

logger = logging.getLogger("museletter.cloudflare")

API_BASE = "https://api.cloudflare.com/client/v4"

EVENT_POLL_BATCH = 50
# An unacked pull (crash mid-apply) is redelivered after this long.
EVENT_VISIBILITY_TIMEOUT_MS = 60_000


class CloudflareEmailError(SendError):
    provider = "cloudflare"


class CloudflareEmail:
    def __init__(self, account_id: str, events_queue_id: str = "", http: httpx.AsyncClient | None = None):
        self.account_id = account_id
        self.events_queue_id = events_queue_id
        self._http = http

    def _token(self) -> str:
        token = os.environ.get("CLOUDFLARE_API_TOKEN", "")
        if not token:
            raise CloudflareEmailError(0, "NoCredentials", "CLOUDFLARE_API_TOKEN is not set")
        return token

    async def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=30)
        headers = {"authorization": f"Bearer {self._token()}"}
        content = json.dumps(body).encode() if body is not None else None
        if content is not None:
            headers["content-type"] = "application/json"
        resp = await self._http.request(method, API_BASE + path, content=content, headers=headers)
        try:
            data = resp.json()
        except ValueError:
            data = {}
        if resp.status_code >= 400 or not data.get("success", True):
            errors = data.get("errors") or []
            first = errors[0] if errors else {}
            raise CloudflareEmailError(
                resp.status_code,
                str(first.get("code", "UnknownError")),
                str(first.get("message") or resp.text[:500]),
            )
        return data

    async def send_email(
        self,
        to: str,
        subject: str,
        html: str,
        text: str,
        *,
        from_email: str,
        from_name: str = "",
        headers: dict[str, str] | None = None,
        reply_to: str = "",
    ) -> SendResult:
        """Send one email. Cloudflare returns no message id over REST, so
        message_id is normally empty and the outcome carries the synchronous
        verdict instead."""
        sender = {"address": from_email, "name": from_name} if from_name else from_email
        body: dict = {"from": sender, "to": to, "subject": subject, "html": html, "text": text}
        if reply_to:
            body["reply_to"] = reply_to
        if headers:
            body["headers"] = headers
        data = await self._request("POST", f"/accounts/{self.account_id}/email/sending/send", body)
        result = data.get("result") or {}
        message_id = str(result.get("message_id") or result.get("messageId") or "")
        recipient = to.lower()

        def listed(key: str) -> bool:
            return any(str(addr).lower() == recipient for addr in result.get(key) or [])

        if listed("permanent_bounces"):
            return SendResult(message_id, "bounced", "permanent bounce reported in the send response")
        if listed("delivered"):
            return SendResult(message_id, "delivered")
        return SendResult(message_id)

    async def get_suppressions(self) -> dict:
        """Cheapest authenticated Email Sending call; doctor uses it as a probe."""
        return await self._request("GET", f"/accounts/{self.account_id}/email/sending/suppressions")

    async def get_queue(self) -> dict | None:
        try:
            data = await self._request("GET", f"/accounts/{self.account_id}/queues/{self.events_queue_id}")
        except CloudflareEmailError as exc:
            if exc.status == 404:
                return None
            raise
        return data.get("result") or {}

    async def pull_events(self, batch_size: int = EVENT_POLL_BATCH) -> list[dict]:
        """Pull pending queue messages as [{'lease_id', 'body'}, ...]."""
        if not self.events_queue_id:
            return []
        data = await self._request(
            "POST",
            f"/accounts/{self.account_id}/queues/{self.events_queue_id}/messages/pull",
            {"batch_size": batch_size, "visibility_timeout_ms": EVENT_VISIBILITY_TIMEOUT_MS},
        )
        messages = (data.get("result") or {}).get("messages") or []
        return [
            {"lease_id": m.get("lease_id", ""), "body": str(m.get("body", ""))}
            for m in messages
            if m.get("lease_id")
        ]

    async def ack_events(self, lease_ids: list[str]) -> None:
        if not lease_ids:
            return
        await self._request(
            "POST",
            f"/accounts/{self.account_id}/queues/{self.events_queue_id}/messages/ack",
            {"acks": [{"lease_id": lease} for lease in lease_ids]},
        )

    @staticmethod
    def has_credentials() -> bool:
        return bool(os.environ.get("CLOUDFLARE_API_TOKEN"))


# event type suffix -> (normalized type, permanent); None = read from the bounce object
_EVENT_TYPES = {
    "message.delivered": ("delivery", False),
    "message.bounced": ("bounce", None),
    "message.complained": ("complaint", True),
    "message.failed": ("bounce", False),
    "message.rejected": ("bounce", False),
    "message.deferred": ("deferred", False),
}


def _b64_utf8(raw: str) -> str | None:
    try:
        return base64.b64decode(raw, validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None


def parse_cloudflare_events(raw_body: str) -> list[dict]:
    """Normalize one queue message into the shared event dict shape
    ({type, email, message_id, permanent, detail}); [] when unrecognizable.
    The pull API base64-encodes JSON queue messages; accept plain JSON too."""
    data = None
    for candidate in (raw_body, _b64_utf8(raw_body)):
        if candidate is None:
            continue
        try:
            data = json.loads(candidate)
            break
        except (ValueError, TypeError):
            continue
    if not isinstance(data, dict):
        return []
    etype = str(data.get("type", ""))
    matched = next((spec for suffix, spec in _EVENT_TYPES.items() if etype.endswith(suffix)), None)
    if matched is None:
        return []
    kind, permanent = matched
    payload = data.get("payload") or {}
    email = str(payload.get("recipient") or "").lower().strip()
    if not email:
        return []
    bounce = payload.get("bounce") or {}
    if permanent is None:
        permanent = bounce.get("type") == "hard" or bounce.get("classification") == "permanent_failure"
    if etype.endswith("message.failed"):
        detail = "failed/" + str((payload.get("failure") or {}).get("reason", ""))
    elif etype.endswith("message.rejected"):
        detail = "rejected/" + str((payload.get("rejection") or {}).get("reason", ""))
    elif kind == "complaint":
        detail = str((payload.get("complaint") or {}).get("type", ""))
    elif kind == "delivery":
        detail = ""
    else:
        detail = f"{bounce.get('type', '')}/{bounce.get('reason', '')}"
    return [
        {
            "type": kind,
            "email": email,
            "message_id": str(payload.get("messageId") or ""),
            "permanent": bool(permanent),
            "detail": detail,
        }
    ]


class EventPoller:
    """Background loop draining the events queue into the ledger. Same
    at-least-once contract as the SNS webhook: apply first, ack after, so a
    crash between the two replays the batch and the status-guarded updates
    dedupe it."""

    def __init__(self, app):
        self.app = app
        self._stopped = False
        self._wake = asyncio.Event()

    def stop(self) -> None:
        self._stopped = True
        self._wake.set()

    async def run(self) -> None:
        logger.info("cloudflare event poller started")
        poll_seconds = max(self.app.state.settings.cloudflare_poll_seconds, 1.0)
        while not self._stopped:
            try:
                worked = await self.tick()
            except Exception:
                logger.exception("event poll failed")
                worked = False
                await asyncio.sleep(5)
            if not worked and not self._stopped:
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=poll_seconds)
                except TimeoutError:
                    pass
                self._wake.clear()
        logger.info("cloudflare event poller stopped")

    async def tick(self) -> bool:
        """Pull one batch, apply it, ack it. True when messages were processed."""
        mailer = self.app.state.mailer
        messages = await mailer.pull_events()
        if not messages:
            return False
        events: list[dict] = []
        for message in messages:
            parsed = parse_cloudflare_events(message["body"])
            if not parsed:
                # Log-and-ack, or a stray message would block the queue forever.
                parsed = [
                    {
                        "type": "other",
                        "email": "",
                        "message_id": "",
                        "permanent": False,
                        "detail": message["body"][:10000],
                    }
                ]
            events.extend(parsed)
        await apply_events(self.app.state.db, events, match_by_email=True)
        await mailer.ack_events([m["lease_id"] for m in messages])
        return True
