"""App-level flow with the Cloudflare provider: synchronous send verdicts land
in the ledger, the event poller applies queue events by recipient address (the
REST send response returns no message id), and doctor covers the setup."""

import base64
import json

import httpx
import pytest

from conftest import AUTH, FakeMailer, add_subscriber, make_campaign, make_settings
from museletter import doctor as doctor_mod
from museletter.app import create_app
from museletter.cloudflare import EventPoller
from museletter.mailer import SendResult
from museletter.sender import SenderLoop


class FakeCloudflareMailer(FakeMailer):
    """Cloudflare-shaped fake: sends return no message id (verdict per address
    via results_by_email), and the poller pulls from an in-memory queue."""

    def __init__(self):
        super().__init__()
        self.results_by_email: dict[str, SendResult] = {}
        self.queue: list[dict] = []
        self.acked: list[str] = []
        self.queue_consumers: list[dict] = [{"type": "http_pull"}]

    async def send_email(
        self, to, subject, html, text, *, from_email, from_name="", headers=None, reply_to=""
    ):
        if self.fail_next:
            raise self.fail_next.pop(0)
        self.sent.append(
            {"to": to, "subject": subject, "html": html, "headers": headers or {}, "reply_to": reply_to}
        )
        return self.results_by_email.get(to, SendResult())

    async def pull_events(self, batch_size=50):
        batch, self.queue = self.queue[:batch_size], self.queue[batch_size:]
        return batch

    async def ack_events(self, lease_ids):
        self.acked.extend(lease_ids)

    async def get_suppressions(self):
        return {"success": True, "result": []}

    async def get_queue(self):
        return {"queue_name": "museletter-events", "consumers": self.queue_consumers}


def make_cf_settings(tmp_path):
    settings = make_settings(tmp_path)
    settings.email_provider = "cloudflare"
    settings.cloudflare_account_id = "acct1"
    settings.cloudflare_events_queue_id = "q_1"
    settings.extra["mailer"] = FakeCloudflareMailer()
    return settings


@pytest.fixture
async def cf_app_client(tmp_path):
    app = create_app(make_cf_settings(tmp_path))
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test.local") as client:
            yield app, client


def push_event(mailer, etype, **payload):
    envelope = {
        "type": etype,
        "source": {"type": "email.sending", "domain": "test.local"},
        "payload": {"eventId": f"evt-{len(mailer.queue)}", "sender": "news@test.local", **payload},
        "metadata": {"eventSchemaVersion": 1},
    }
    body = base64.b64encode(json.dumps(envelope).encode()).decode()
    mailer.queue.append({"lease_id": f"lease-{len(mailer.queue)}", "body": body})


async def _start_campaign(client, **kwargs):
    campaign = await make_campaign(client, **kwargs)
    resp = await client.post(
        f"/v1/campaigns/{campaign['id']}/send", json={"confirm": True, "skip_test": True}, headers=AUTH
    )
    assert resp.status_code == 200, resp.text
    return campaign["id"]


async def _drain(app, max_ticks=20):
    loop = SenderLoop(app)
    for _ in range(max_ticks):
        if not await loop.tick():
            break


async def _stats(client, cid):
    resp = await client.get(f"/v1/campaigns/{cid}/stats", headers=AUTH)
    return resp.json()


async def _row(db, cid, email):
    async with db.execute(
        "SELECT * FROM campaign_recipients WHERE campaign_id = ? AND email = ?", (cid, email)
    ) as cur:
        return await cur.fetchone()


async def _suppressed_emails(db):
    async with db.execute("SELECT email FROM suppressions") as cur:
        return {r["email"] for r in await cur.fetchall()}


async def test_send_verdicts_update_ledger(cf_app_client):
    app, client = cf_app_client
    app.state.settings.reply_to = "replies@test.local"
    mailer = app.state.settings.extra["mailer"]
    await add_subscriber(client, "a@x.com")
    await add_subscriber(client, "b@x.com")
    await add_subscriber(client, "c@x.com")
    mailer.results_by_email = {
        "a@x.com": SendResult(outcome="delivered"),
        "b@x.com": SendResult(outcome="bounced", detail="on the provider suppression list"),
    }
    cid = await _start_campaign(client)
    await _drain(app)

    stats = await _stats(client, cid)
    assert stats["status"] == "sent"
    assert (stats["delivered"], stats["bounced"], stats["sent"]) == (1, 1, 1)
    assert all(m["reply_to"] == "replies@test.local" for m in mailer.sent)

    db = app.state.db
    bounced = await _row(db, cid, "b@x.com")
    assert bounced["error"] == "on the provider suppression list"
    assert bounced["ses_message_id"] is None, "Cloudflare REST returns no message id"
    assert await _suppressed_emails(db) == {"b@x.com"}
    async with db.execute("SELECT status FROM subscribers WHERE email = 'b@x.com'") as cur:
        assert (await cur.fetchone())["status"] == "bounced"
    async with db.execute("SELECT type, email FROM events WHERE type = 'bounce'") as cur:
        rows = await cur.fetchall()
    assert [(r["type"], r["email"]) for r in rows] == [("bounce", "b@x.com")]


async def test_poller_applies_queue_events_by_address(cf_app_client):
    app, client = cf_app_client
    mailer = app.state.settings.extra["mailer"]
    for email in ("a@x.com", "b@x.com", "c@x.com", "d@x.com"):
        await add_subscriber(client, email)
    cid = await _start_campaign(client)
    await _drain(app)
    assert (await _stats(client, cid))["sent"] == 4

    push_event(
        mailer,
        "cf.email.sending.message.delivered",
        recipient="a@x.com",
        messageId="cf-a",
        delivery={"status": "delivered"},
    )
    push_event(
        mailer,
        "cf.email.sending.message.bounced",
        recipient="b@x.com",
        messageId="cf-b",
        bounce={"type": "hard", "classification": "permanent_failure", "reason": "no mailbox"},
    )
    push_event(
        mailer,
        "cf.email.sending.message.complained",
        recipient="c@x.com",
        messageId="cf-c",
        complaint={"type": "abuse"},
    )
    push_event(
        mailer,
        "cf.email.sending.message.failed",
        recipient="d@x.com",
        messageId="cf-d",
        failure={"reason": "expired after retries"},
    )

    poller = EventPoller(app)
    assert await poller.tick() is True
    assert await poller.tick() is False, "queue drained"
    assert mailer.acked == ["lease-0", "lease-1", "lease-2", "lease-3"]

    stats = await _stats(client, cid)
    assert (stats["delivered"], stats["bounced"], stats["complained"]) == (1, 2, 1)

    db = app.state.db
    assert (await _row(db, cid, "b@x.com"))["error"] == "hard/no mailbox"
    assert await _suppressed_emails(db) == {"b@x.com", "c@x.com"}, "failed (soft) must not suppress"
    async with db.execute(
        "SELECT status FROM subscribers WHERE email IN ('b@x.com', 'c@x.com') ORDER BY email"
    ) as cur:
        assert [r["status"] for r in await cur.fetchall()] == ["bounced", "complained"]


async def test_address_fallback_skips_rows_that_have_a_message_id(cf_app_client):
    app, client = cf_app_client
    mailer = app.state.settings.extra["mailer"]
    await add_subscriber(client, "a@x.com")
    cid = await _start_campaign(client)
    await _drain(app)
    db = app.state.db
    await db.execute(
        "UPDATE campaign_recipients SET ses_message_id = 'known-id' WHERE campaign_id = ?", (cid,)
    )
    await db.commit()

    push_event(
        mailer,
        "cf.email.sending.message.bounced",
        recipient="a@x.com",
        messageId="some-other-send",
        bounce={"type": "hard", "reason": "no mailbox"},
    )
    await EventPoller(app).tick()

    row = await _row(db, cid, "a@x.com")
    assert row["status"] == "sent", "a row correlated by id is never re-matched by address"
    assert await _suppressed_emails(db) == {"a@x.com"}, "the address itself is still suppressed"


async def test_poller_records_unrecognized_messages(cf_app_client):
    app, _client = cf_app_client
    mailer = app.state.settings.extra["mailer"]
    mailer.queue.append({"lease_id": "lease-x", "body": "not an event"})
    assert await EventPoller(app).tick() is True
    assert mailer.acked == ["lease-x"], "stray messages are acked, not retried forever"
    async with app.state.db.execute("SELECT type, payload FROM events") as cur:
        rows = await cur.fetchall()
    assert [(r["type"], r["payload"]) for r in rows] == [("other", "not an event")]


async def _no_txt(name):
    return []


async def test_doctor_cloudflare_checks(cf_app_client, monkeypatch):
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "cf-token")
    monkeypatch.setattr(doctor_mod, "_resolve_txt", _no_txt)
    _app, client = cf_app_client
    resp = await client.get("/v1/doctor", headers=AUTH)
    data = resp.json()
    by_name = {c["name"]: c for c in data["checks"]}
    assert by_name["cloudflare-credentials"]["status"] == "ok"
    assert by_name["cloudflare-api"]["status"] == "ok"
    assert by_name["cloudflare-events"]["status"] == "ok"
    assert "aws-credentials" not in by_name
    assert "sns-topic" not in by_name, "the SNS warning is SES-only"
    assert not any(c["status"] == "fail" for c in data["checks"]), data


async def test_doctor_cloudflare_missing_token(cf_app_client, monkeypatch):
    monkeypatch.delenv("CLOUDFLARE_API_TOKEN", raising=False)
    monkeypatch.setattr(doctor_mod, "_resolve_txt", _no_txt)
    _app, client = cf_app_client
    data = (await client.get("/v1/doctor", headers=AUTH)).json()
    check = next(c for c in data["checks"] if c["name"] == "cloudflare-credentials")
    assert check["status"] == "fail"
    assert data["status"] == "fail"


async def test_doctor_fails_without_http_pull_consumer(cf_app_client, monkeypatch):
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "cf-token")
    monkeypatch.setattr(doctor_mod, "_resolve_txt", _no_txt)
    app, client = cf_app_client
    app.state.settings.extra["mailer"].queue_consumers = [{"type": "worker"}]
    data = (await client.get("/v1/doctor", headers=AUTH)).json()
    check = next(c for c in data["checks"] if c["name"] == "cloudflare-events")
    assert check["status"] == "fail"
    assert "HTTP pull consumer" in check["detail"]


async def test_doctor_warns_without_events_queue(tmp_path, monkeypatch):
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "cf-token")
    monkeypatch.setattr(doctor_mod, "_resolve_txt", _no_txt)
    settings = make_cf_settings(tmp_path)
    settings.cloudflare_events_queue_id = ""
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test.local") as client:
            data = (await client.get("/v1/doctor", headers=AUTH)).json()
    check = next(c for c in data["checks"] if c["name"] == "cloudflare-events")
    assert check["status"] == "warn"
    assert "auto-suppress" in check["detail"]
