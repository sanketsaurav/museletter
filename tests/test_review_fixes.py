"""Regression tests for the defects surfaced by the high-effort code review."""

import httpx
import pytest

from conftest import AUTH, FakeSES, add_subscriber, make_campaign, make_settings
from museletter.app import create_app
from museletter.sender import SenderLoop
from museletter.tokens import make_token


async def _find(client, email):
    resp = await client.get(f"/v1/lists/default/subscribers?q={email}", headers=AUTH)
    subs = resp.json()["subscribers"]
    return subs[0] if subs else None


# ---------- idempotency: auth + only-cache-success ----------


async def test_idempotency_replay_requires_api_key(app_client):
    app, client = app_client
    await add_subscriber(client, "a@x.com")
    campaign = await make_campaign(client)
    cid = campaign["id"]
    key = {"Idempotency-Key": f"send-{cid}"}

    # Unauthenticated send with a valid-looking key: rejected, and must NOT be cached.
    unauth = await client.post(
        f"/v1/campaigns/{cid}/send", json={"confirm": True, "skip_test": True}, headers=key
    )
    assert unauth.status_code == 401
    assert unauth.headers.get("Idempotency-Replayed") is None

    # The owner's real send still executes (the 401 was not poisoned into the cache).
    ok = await client.post(
        f"/v1/campaigns/{cid}/send", json={"confirm": True, "skip_test": True}, headers={**AUTH, **key}
    )
    assert ok.status_code == 200
    assert ok.json()["status"] == "sending"


async def test_idempotency_does_not_cache_4xx(app_client):
    app, client = app_client
    await add_subscriber(client, "a@x.com")
    campaign = await make_campaign(client)
    cid = campaign["id"]
    key = {**AUTH, "Idempotency-Key": f"send-{cid}"}

    # First send fails the test-send guardrail (412) — this must not be cached.
    first = await client.post(f"/v1/campaigns/{cid}/send", json={"confirm": True}, headers=key)
    assert first.status_code == 412

    # After a test send, retrying with the SAME key must execute, not replay the 412.
    await client.post(f"/v1/campaigns/{cid}/test", json={"to": "me@x.com"}, headers=AUTH)
    retry = await client.post(f"/v1/campaigns/{cid}/send", json={"confirm": True}, headers=key)
    assert retry.status_code == 200
    assert retry.headers.get("Idempotency-Replayed") is None


async def test_idempotency_still_dedupes_success(app_client):
    app, client = app_client
    await add_subscriber(client, "a@x.com")
    campaign = await make_campaign(client)
    cid = campaign["id"]
    key = {**AUTH, "Idempotency-Key": f"send-{cid}"}

    first = await client.post(
        f"/v1/campaigns/{cid}/send", json={"confirm": True, "skip_test": True}, headers=key
    )
    assert first.status_code == 200
    replay = await client.post(
        f"/v1/campaigns/{cid}/send", json={"confirm": True, "skip_test": True}, headers=key
    )
    assert replay.headers.get("Idempotency-Replayed") == "true"
    stats = await client.get(f"/v1/campaigns/{cid}/stats", headers=AUTH)
    assert stats.json()["pending"] == 1, "replay must not enqueue a second time"


# ---------- confirm must not resurrect the opted-out ----------


async def test_confirm_does_not_reactivate_unsubscribed(app_client):
    app, client = app_client
    sub = await add_subscriber(client, "gone@x.com", status="unsubscribed")
    token = make_token(app.state.secret, "confirm", sub["id"])
    resp = await client.get(f"/confirm/{token}")
    assert resp.status_code == 200
    assert "not subscribed" in resp.text
    assert (await _find(client, "gone@x.com"))["status"] == "unsubscribed"


async def test_confirm_does_not_reactivate_bounced(app_client):
    app, client = app_client
    sub = await add_subscriber(client, "bounced@x.com", status="bounced")
    token = make_token(app.state.secret, "confirm", sub["id"])
    await client.get(f"/confirm/{token}")
    assert (await _find(client, "bounced@x.com"))["status"] == "bounced"


async def test_confirm_still_activates_unconfirmed(app_client):
    app, client = app_client
    sub = await add_subscriber(client, "new@x.com", status="unconfirmed")
    token = make_token(app.state.secret, "confirm", sub["id"])
    resp = await client.get(f"/confirm/{token}")
    assert "Welcome" in resp.text
    assert (await _find(client, "new@x.com"))["status"] == "active"


# ---------- admin unsubscribe suppresses pending sends ----------


async def test_admin_unsubscribe_suppresses_pending_rows(app_client):
    app, client = app_client
    sub = await add_subscriber(client, "a@x.com")
    await add_subscriber(client, "b@x.com")
    campaign = await make_campaign(client)
    await client.post(
        f"/v1/campaigns/{campaign['id']}/send", json={"confirm": True, "skip_test": True}, headers=AUTH
    )
    # Operator opts the subscriber out mid-flight.
    await client.patch(f"/v1/subscribers/{sub['id']}", json={"status": "unsubscribed"}, headers=AUTH)

    async with app.state.db.execute(
        "SELECT status FROM campaign_recipients WHERE campaign_id = ? AND subscriber_id = ?",
        (campaign["id"], sub["id"]),
    ) as cur:
        assert (await cur.fetchone())["status"] == "suppressed"


# ---------- sender honors a mid-batch opt-out ----------


async def test_sender_skips_subscriber_unsubscribed_after_materialization(app_client):
    app, client = app_client
    fake_ses: FakeSES = app.state.settings.extra["ses"]
    sub = await add_subscriber(client, "opt@x.com")
    await add_subscriber(client, "stay@x.com")
    campaign = await make_campaign(client)
    await client.post(
        f"/v1/campaigns/{campaign['id']}/send", json={"confirm": True, "skip_test": True}, headers=AUTH
    )
    # Unsubscribe lands after the rows are materialized but before the loop runs.
    token = make_token(app.state.secret, "unsubscribe", sub["id"])
    await client.post(f"/unsubscribe/{token}")

    loop = SenderLoop(app)
    for _ in range(10):
        if not await loop.tick():
            break

    sent_to = {m["to"] for m in fake_ses.sent}
    assert "opt@x.com" not in sent_to, "must not email a subscriber who unsubscribed after materialization"
    assert "stay@x.com" in sent_to


# ---------- list deletion guarded while sending ----------


async def test_delete_list_blocked_while_sending(app_client):
    app, client = app_client
    # A second list so the "only list" guard doesn't mask the "sending" guard.
    await client.post("/v1/lists", json={"name": "Other"}, headers=AUTH)
    await add_subscriber(client, "a@x.com")
    campaign = await make_campaign(client)
    await client.post(
        f"/v1/campaigns/{campaign['id']}/send", json={"confirm": True, "skip_test": True}, headers=AUTH
    )
    resp = await client.delete("/v1/lists/default", headers=AUTH)
    assert resp.status_code == 409
    assert "sending" in resp.json()["detail"]


# ---------- SNS topic pinning ----------


@pytest.fixture
async def topic_pinned_client(tmp_path):
    settings = make_settings(tmp_path)
    settings.sns_topic_arn = "arn:aws:sns:us-east-1:123:museletter-events"
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test.local") as client:
            yield app, client


async def test_sns_webhook_rejects_foreign_topic(topic_pinned_client):
    import json

    app, client = topic_pinned_client
    foreign = {
        "Type": "Notification",
        "TopicArn": "arn:aws:sns:us-east-1:999:attacker-topic",
        "Message": json.dumps(
            {
                "eventType": "Bounce",
                "bounce": {
                    "bounceType": "Permanent",
                    "bouncedRecipients": [{"emailAddress": "victim@x.com"}],
                },
                "mail": {"messageId": "m1"},
            }
        ),
    }
    resp = await client.post("/webhooks/sns", json=foreign)
    assert resp.status_code == 403
    assert (await client.get("/v1/suppressions", headers=AUTH)).json()["total"] == 0


async def test_sns_webhook_accepts_pinned_topic(topic_pinned_client):
    import json

    app, client = topic_pinned_client
    good = {
        "Type": "Notification",
        "TopicArn": "arn:aws:sns:us-east-1:123:museletter-events",
        "Message": json.dumps(
            {
                "eventType": "Complaint",
                "complaint": {"complainedRecipients": [{"emailAddress": "c@x.com"}]},
                "mail": {"messageId": "m2"},
            }
        ),
    }
    resp = await client.post("/webhooks/sns", json=good)
    assert resp.status_code == 200
