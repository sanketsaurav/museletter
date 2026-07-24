import re

from conftest import AUTH
from museletter.tokens import make_token


async def _find_subscriber(client, email):
    resp = await client.get(f"/v1/lists/default/subscribers?q={email}", headers=AUTH)
    subs = resp.json()["subscribers"]
    return subs[0] if subs else None


async def test_subscribe_confirm_unsubscribe_flow(app_client):
    app, client = app_client
    fake_ses = app.state.settings.extra["ses"]

    resp = await client.post("/subscribe/default", json={"email": "reader@example.com", "name": "R"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "pending_confirmation"

    sub = await _find_subscriber(client, "reader@example.com")
    assert sub["status"] == "unconfirmed"

    assert len(fake_ses.sent) == 1
    confirm_email = fake_ses.sent[0]
    assert confirm_email["to"] == "reader@example.com"
    match = re.search(r"http://test\.local/confirm/([^\s)]+)", confirm_email["text"])
    assert match, confirm_email["text"]

    resp = await client.get(f"/confirm/{match.group(1)}")
    assert resp.status_code == 200
    sub = await _find_subscriber(client, "reader@example.com")
    assert sub["status"] == "active"

    unsub_token = make_token(app.state.secret, "unsubscribe", sub["id"])
    page = await client.get(f"/unsubscribe/{unsub_token}")
    assert page.status_code == 200
    assert "form" in page.text, "GET must not unsubscribe (prefetch safety)"
    sub = await _find_subscriber(client, "reader@example.com")
    assert sub["status"] == "active"

    resp = await client.post(f"/unsubscribe/{unsub_token}")
    assert resp.status_code == 200
    sub = await _find_subscriber(client, "reader@example.com")
    assert sub["status"] == "unsubscribed"


async def test_subscribe_honeypot_and_suppressed(app_client):
    app, client = app_client
    fake_ses = app.state.settings.extra["ses"]

    resp = await client.post(
        "/subscribe/default", json={"email": "bot@example.com", "website": "http://spam"}
    )
    assert resp.json()["status"] == "pending_confirmation"
    assert await _find_subscriber(client, "bot@example.com") is None
    assert fake_ses.sent == []

    await client.post("/v1/suppressions", json={"email": "burned@example.com"}, headers=AUTH)
    resp = await client.post("/subscribe/default", json={"email": "burned@example.com"})
    assert resp.json()["status"] == "pending_confirmation"
    assert await _find_subscriber(client, "burned@example.com") is None
    assert fake_ses.sent == []


async def test_subscribe_invalid_inputs(app_client):
    app, client = app_client
    assert (await client.post("/subscribe/default", json={"email": "nope"})).status_code == 422
    assert (await client.post("/subscribe/missing", json={"email": "a@b.co"})).status_code == 404


async def test_confirm_invalid_token(app_client):
    app, client = app_client
    resp = await client.get("/confirm/garbage")
    assert resp.status_code == 200
    assert "not valid" in resp.text
