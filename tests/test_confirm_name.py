"""Readers who confirm without a name can add one on the confirmation page."""

import pytest

from conftest import AUTH, add_subscriber
from museletter.tokens import make_token

NAME_FIELD = 'name="first_name"'


async def subscriber(client):
    [sub] = (await client.get("/v1/lists/default/subscribers", headers=AUTH)).json()["subscribers"]
    return sub


async def signup(app, client, **fields):
    resp = await client.post("/subscribe/default", json={"email": "ada@example.com", **fields})
    assert resp.status_code == 200
    return make_token(app.state.secret, "confirm", (await subscriber(client))["id"])


async def test_confirm_page_offers_name_form_that_saves_on_post(app_client):
    app, client = app_client
    token = await signup(app, client)

    page = await client.get(f"/confirm/{token}")
    assert "Welcome to" in page.text
    assert f'action="/confirm/{token}"' in page.text
    assert NAME_FIELD in page.text and 'name="last_name"' in page.text
    sub = await subscriber(client)
    assert sub["status"] == "active"
    assert sub["name"] == ""

    saved = await client.post(f"/confirm/{token}", data={"first_name": " Ada ", "last_name": " Lovelace "})
    assert saved.status_code == 200
    assert "Thanks, Ada" in saved.text
    assert NAME_FIELD not in saved.text
    assert (await subscriber(client))["name"] == "Ada Lovelace"

    again = await client.get(f"/confirm/{token}")
    assert "already subscribed" in again.text
    assert NAME_FIELD not in again.text


async def test_confirm_page_skips_name_form_when_signup_had_a_name(app_client):
    app, client = app_client
    token = await signup(app, client, first_name="Ada")
    page = await client.get(f"/confirm/{token}")
    assert "Welcome to" in page.text
    assert NAME_FIELD not in page.text


async def test_name_form_still_offered_after_link_scanner_confirms_first(app_client):
    app, client = app_client
    token = await signup(app, client)
    await client.get(f"/confirm/{token}")
    page = await client.get(f"/confirm/{token}")
    assert "already subscribed" in page.text
    assert f'action="/confirm/{token}"' in page.text
    assert (await subscriber(client))["name"] == ""


async def test_post_confirms_pending_reader_and_saves_name(app_client):
    app, client = app_client
    token = await signup(app, client)
    resp = await client.post(f"/confirm/{token}", data={"first_name": "Ada"})
    assert "Thanks, Ada" in resp.text
    sub = await subscriber(client)
    assert sub["status"] == "active"
    assert sub["confirmed_at"] is not None
    assert sub["name"] == "Ada"


async def test_empty_submission_keeps_form_and_name_is_escaped(app_client):
    app, client = app_client
    token = await signup(app, client)
    empty = await client.post(f"/confirm/{token}", data={"first_name": "  ", "last_name": ""})
    assert NAME_FIELD in empty.text
    assert (await subscriber(client))["name"] == ""

    resp = await client.post(f"/confirm/{token}", data={"first_name": "<b>Ada</b>", "last_name": "b" * 150})
    assert "<b>Ada</b>" not in resp.text
    assert "Thanks, &lt;b&gt;Ada&lt;/b&gt;" in resp.text
    assert (await subscriber(client))["name"] == "<b>Ada</b> " + "b" * 100


@pytest.mark.parametrize("status", ["unsubscribed", "bounced", "complained"])
async def test_post_never_names_or_reopens_closed_subscriptions(app_client, status):
    app, client = app_client
    sub = await add_subscriber(client, "ada@example.com", status=status)
    token = make_token(app.state.secret, "confirm", sub["id"])
    resp = await client.post(f"/confirm/{token}", data={"first_name": "Ada"})
    assert "Subscription closed" in resp.text
    current = await subscriber(client)
    assert current["status"] == status
    assert current["name"] == ""


async def test_post_rejects_invalid_and_wrong_purpose_tokens(app_client):
    app, client = app_client
    sub = await add_subscriber(client, "ada@example.com")
    for token in ("garbage", make_token(app.state.secret, "unsubscribe", sub["id"])):
        resp = await client.post(f"/confirm/{token}", data={"first_name": "Mallory"})
        assert "not valid" in resp.text
    assert (await subscriber(client))["name"] == ""
