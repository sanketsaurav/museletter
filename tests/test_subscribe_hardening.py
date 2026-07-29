"""Tests for the public subscribe hardening: cooldown, uniform response,
Turnstile, and the disable flag."""

import httpx
import pytest

from conftest import FakeSES, add_subscriber, make_settings
from museletter.app import create_app


async def _client(settings):
    app = create_app(settings)
    ctx = app.router.lifespan_context(app)
    await ctx.__aenter__()
    transport = httpx.ASGITransport(app=app)
    client = httpx.AsyncClient(transport=transport, base_url="http://test.local")
    return app, client, ctx


@pytest.fixture
async def custom(tmp_path):
    """Factory: build an app+client from settings with the given overrides."""
    created = []

    async def build(**overrides):
        settings = make_settings(tmp_path)
        for key, value in overrides.items():
            setattr(settings, key, value)
        app, client, ctx = await _client(settings)
        created.append((client, ctx))
        return app, client

    yield build
    for client, ctx in created:
        await client.aclose()
        await ctx.__aexit__(None, None, None)


# ---------- per-address cooldown ----------


async def test_confirmation_email_sent_once_within_cooldown(custom):
    app, client = await custom()  # default cooldown 3600s
    fake: FakeSES = app.state.settings.extra["ses"]

    for _ in range(3):
        resp = await client.post("/subscribe/default", json={"email": "victim@x.com"})
        assert resp.json()["status"] == "pending_confirmation"

    assert len(fake.sent) == 1, "only one confirmation email despite repeated submits"


async def test_confirmation_resends_after_cooldown(custom):
    app, client = await custom(confirmation_cooldown=0.0)
    fake: FakeSES = app.state.settings.extra["ses"]

    await client.post("/subscribe/default", json={"email": "r@x.com"})
    await client.post("/subscribe/default", json={"email": "r@x.com"})
    assert len(fake.sent) == 2, "with a zero cooldown each submit resends"


# ---------- uniform response ----------


async def test_response_does_not_reveal_membership(custom):
    app, client = await custom()
    fake: FakeSES = app.state.settings.extra["ses"]
    await add_subscriber(client, "member@x.com")  # active subscriber

    # Probing an existing active member looks identical to a fresh signup.
    existing = await client.post("/subscribe/default", json={"email": "member@x.com"})
    fresh = await client.post("/subscribe/default", json={"email": "stranger@x.com"})
    assert existing.json() == fresh.json()
    assert existing.json()["status"] == "pending_confirmation"
    assert "member@x.com" not in {m["to"] for m in fake.sent}, "no email to an already-active member"


# ---------- Turnstile ----------


async def _fake_turnstile(secret, token, remoteip=""):
    return token == "good-token"


async def test_turnstile_required_when_configured(custom):
    app, client = await custom(turnstile_secret="sekret")
    app.state.turnstile_verify = _fake_turnstile
    fake: FakeSES = app.state.settings.extra["ses"]

    missing = await client.post("/subscribe/default", json={"email": "a@x.com"})
    assert missing.status_code == 403
    bad = await client.post("/subscribe/default", json={"email": "a@x.com", "cf-turnstile-response": "nope"})
    assert bad.status_code == 403
    assert fake.sent == []

    good = await client.post(
        "/subscribe/default", json={"email": "a@x.com", "cf-turnstile-response": "good-token"}
    )
    assert good.status_code == 200
    assert len(fake.sent) == 1


async def test_turnstile_off_by_default(custom):
    app, client = await custom()  # no secret
    resp = await client.post("/subscribe/default", json={"email": "a@x.com"})
    assert resp.status_code == 200


# ---------- disable flag ----------


async def test_public_subscribe_can_be_disabled(custom):
    app, client = await custom(public_subscribe=False)

    resp = await client.post("/subscribe/default", json={"email": "a@x.com"})
    assert resp.status_code == 404

    # Adding via the authenticated admin API still works.
    added = await add_subscriber(client, "b@x.com")
    assert added["email"] == "b@x.com"

    # Confirm/unsubscribe links must still work for existing readers.
    from museletter.tokens import make_token

    token = make_token(app.state.secret, "unsubscribe", added["id"])
    page = await client.get(f"/unsubscribe/{token}")
    assert page.status_code == 200
