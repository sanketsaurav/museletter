"""Authenticated website signup always uses double opt-in, without admin imports."""

import asyncio
import re

import pytest

from conftest import AUTH, add_subscriber
from museletter.mailer import SendResult
from museletter.tokens import make_token

SIGNUP = "/v1/lists/default/subscribe"


async def subscribers(client, list_ref="default"):
    response = await client.get(f"/v1/lists/{list_ref}/subscribers", headers=AUTH)
    assert response.status_code == 200
    return response.json()["subscribers"]


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong-key"}])
async def test_requires_authentication_without_side_effects(app_client, headers):
    app, client = app_client
    response = await client.post(SIGNUP, json={"email": "reader@example.com"}, headers=headers)
    assert response.status_code == 401
    assert await subscribers(client) == []
    assert app.state.mailer.sent == []


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"email": "invalid"},
        {"email": "a" * 321},
        {"email": "a@b.co", "status": "active"},
        {"email": "a@b.co", "name": "a" * 201},
    ],
)
async def test_rejects_invalid_input(app_client, body):
    app, client = app_client
    response = await client.post(SIGNUP, json=body, headers=AUTH)
    assert response.status_code == 422
    assert await subscribers(client) == []
    assert app.state.mailer.sent == []


async def test_signup_requires_confirmation_even_with_public_signup_disabled_and_single_opt_in(app_client):
    app, client = app_client
    app.state.settings.public_subscribe = False
    app.state.settings.opt_in = "single"
    app.state.settings.turnstile_secret = "public-form-secret"

    public = await client.post("/subscribe/default", json={"email": "reader@example.com"})
    assert public.status_code == 404
    response = await client.post(
        SIGNUP, json={"email": " Reader@Example.com ", "name": " Reader "}, headers=AUTH
    )
    assert response.status_code == 202
    assert response.json()["status"] == "pending_confirmation"
    [subscriber] = await subscribers(client)
    assert subscriber["email"] == "reader@example.com"
    assert subscriber["name"] == "Reader"
    assert subscriber["status"] == "unconfirmed"
    assert subscriber["confirmed_at"] is None
    [message] = app.state.mailer.sent
    assert message["to"] == "reader@example.com"
    match = re.search(r"http://test\.local/confirm/([^\s)]+)", message["text"])
    assert match
    confirmation = await client.get(f"/confirm/{match.group(1)}")
    assert confirmation.status_code == 200
    [subscriber] = await subscribers(client)
    assert subscriber["status"] == "active"
    assert subscriber["confirmed_at"] is not None


async def test_cooldown_is_shared_with_public_signup_and_allows_later_retry(app_client):
    app, client = app_client
    await client.post("/subscribe/default", json={"email": "reader@example.com"})
    response = await client.post(SIGNUP, json={"email": "reader@example.com"}, headers=AUTH)
    assert response.status_code == 202
    assert len(app.state.mailer.sent) == 1
    await app.state.db.execute("UPDATE subscribers SET confirmation_sent_at = '2000-01-01T00:00:00+00:00'")
    await app.state.db.commit()
    assert (await client.post(SIGNUP, json={"email": "reader@example.com"}, headers=AUTH)).status_code == 202
    await client.post("/subscribe/default", json={"email": "reader@example.com"})
    assert len(app.state.mailer.sent) == 2


async def test_concurrent_signups_create_one_subscriber_and_send_once(app_client):
    app, client = app_client
    responses = await asyncio.gather(
        *[client.post(SIGNUP, json={"email": "reader@example.com"}, headers=AUTH) for _ in range(8)]
    )
    assert all(response.status_code == 202 for response in responses)
    assert len(await subscribers(client)) == 1
    assert len(app.state.mailer.sent) == 1


@pytest.mark.parametrize("status", ["active", "unsubscribed", "bounced", "complained"])
async def test_existing_reader_status_is_preserved_without_another_email(app_client, status):
    app, client = app_client
    subscriber = await add_subscriber(client, "reader@example.com", status=status)
    response = await client.post(SIGNUP, json={"email": subscriber["email"]}, headers=AUTH)
    assert response.status_code == 202
    assert response.json()["status"] == "pending_confirmation"
    assert app.state.mailer.sent == []
    token = make_token(app.state.secret, "confirm", subscriber["id"])
    await client.get(f"/confirm/{token}")
    [current] = await subscribers(client)
    assert current["status"] == status


async def test_suppression_returns_uniform_success_without_creating_or_sending(app_client):
    app, client = app_client
    await client.post("/v1/suppressions", json={"email": "reader@example.com"}, headers=AUTH)
    response = await client.post(SIGNUP, json={"email": "reader@example.com"}, headers=AUTH)
    assert response.status_code == 202
    assert response.json()["status"] == "pending_confirmation"
    assert await subscribers(client) == []
    assert app.state.mailer.sent == []


async def test_failed_send_releases_cooldown_for_retry_without_exposing_provider_error(app_client):
    app, client = app_client
    app.state.mailer.fail_next.append(RuntimeError("private provider detail"))
    response = await client.post(SIGNUP, json={"email": "reader@example.com"}, headers=AUTH)
    assert response.status_code == 502
    assert "private provider detail" not in response.text
    assert app.state.mailer.sent == []
    [subscriber] = await subscribers(client)
    assert subscriber["status"] == "unconfirmed"
    retry = await client.post(SIGNUP, json={"email": "reader@example.com"}, headers=AUTH)
    assert retry.status_code == 202
    assert len(app.state.mailer.sent) == 1
    assert len(await subscribers(client)) == 1


async def test_list_isolation_and_missing_list(app_client):
    app, client = app_client
    created = await client.post("/v1/lists", json={"name": "Preview", "slug": "preview"}, headers=AUTH)
    assert created.status_code == 201
    # A list id and a slug both address the same isolated list.
    response = await client.post(
        f"/v1/lists/{created.json()['id']}/subscribe", json={"email": "reader@example.com"}, headers=AUTH
    )
    assert response.status_code == 202
    assert await subscribers(client) == []
    assert len(await subscribers(client, "preview")) == 1
    missing = await client.post(
        "/v1/lists/missing/subscribe", json={"email": "reader@example.com"}, headers=AUTH
    )
    assert missing.status_code == 404
    assert len(app.state.mailer.sent) == 1


async def test_admin_import_does_not_send_confirmation(app_client):
    app, client = app_client
    await add_subscriber(client, "import@example.com", status="unconfirmed")
    assert app.state.mailer.sent == []


async def test_synchronous_bounce_suppresses_address_and_blocks_old_confirmation(app_client):
    app, client = app_client
    app.state.mailer.result_next.append(SendResult(outcome="bounced", detail="permanent bounce"))
    response = await client.post(SIGNUP, json={"email": "reader@example.com"}, headers=AUTH)
    assert response.status_code == 202
    assert response.json()["status"] == "pending_confirmation"
    [subscriber] = await subscribers(client)
    assert subscriber["status"] == "bounced"
    token = make_token(app.state.secret, "confirm", subscriber["id"])
    await client.get(f"/confirm/{token}")
    await client.post(SIGNUP, json={"email": "reader@example.com"}, headers=AUTH)
    [current] = await subscribers(client)
    assert current["status"] == "bounced"
    assert len(app.state.mailer.sent) == 1
    async with app.state.db.execute(
        "SELECT reason FROM suppressions WHERE email = ?", (subscriber["email"],)
    ) as cur:
        assert (await cur.fetchone())["reason"] == "bounce"
