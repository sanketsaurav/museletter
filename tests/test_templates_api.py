"""Server-managed campaign templates: CRUD + guardrails, and how lists,
campaigns, previews, test sends, and the sender resolve the effective one."""

from conftest import AUTH, add_subscriber, make_campaign
from museletter.sender import SenderLoop

CUSTOM = '<html><body><div id="custom-shell">$content</div><footer>$footer</footer></body></html>'
SECOND = '<html><body><div id="second-shell">$content</div>$footer</body></html>'


async def make_template(client, name="fancy", html=CUSTOM):
    resp = await client.post("/v1/templates", json={"name": name, "html": html}, headers=AUTH)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _drain(app, max_ticks=20):
    loop = SenderLoop(app)
    for _ in range(max_ticks):
        if not await loop.tick():
            break


async def _campaign_detail(client, cid):
    resp = await client.get(f"/v1/campaigns/{cid}", headers=AUTH)
    assert resp.status_code == 200
    return resp.json()


# ---------- CRUD ----------


async def test_builtin_default_is_listed_and_protected(app_client):
    _, client = app_client
    resp = await client.get("/v1/templates", headers=AUTH)
    templates = resp.json()["templates"]
    assert templates[0]["id"] == "default"
    assert templates[0]["builtin"] is True

    resp = await client.get("/v1/templates/default", headers=AUTH)
    assert "$content" in resp.json()["html"]

    resp = await client.patch("/v1/templates/default", json={"html": CUSTOM}, headers=AUTH)
    assert resp.status_code == 409
    assert "cannot be edited" in resp.json()["detail"]

    resp = await client.delete("/v1/templates/default", headers=AUTH)
    assert resp.status_code == 409
    assert "cannot be deleted" in resp.json()["detail"]


async def test_create_show_edit_delete_roundtrip(app_client):
    _, client = app_client
    created = await make_template(client)
    assert created["name"] == "fancy"
    assert created["id"].startswith("tpl_")
    assert created["builtin"] is False

    listed = (await client.get("/v1/templates", headers=AUTH)).json()["templates"]
    assert [t["name"] for t in listed] == ["default", "fancy"]

    by_name = (await client.get("/v1/templates/fancy", headers=AUTH)).json()
    by_id = (await client.get(f"/v1/templates/{created['id']}", headers=AUTH)).json()
    assert by_name["html"] == CUSTOM
    assert by_id["id"] == by_name["id"]

    resp = await client.patch("/v1/templates/fancy", json={"html": SECOND, "name": "Sleek"}, headers=AUTH)
    assert resp.status_code == 200
    assert resp.json()["name"] == "sleek"
    assert (await client.get("/v1/templates/sleek", headers=AUTH)).json()["html"] == SECOND
    assert (await client.get("/v1/templates/fancy", headers=AUTH)).status_code == 404

    assert (await client.delete("/v1/templates/sleek", headers=AUTH)).status_code == 204
    assert (await client.get("/v1/templates/sleek", headers=AUTH)).status_code == 404


async def test_create_duplicates_builtin_via_copy_of(app_client):
    _, client = app_client
    resp = await client.post("/v1/templates", json={"name": "mine", "copy_of": "default"}, headers=AUTH)
    assert resp.status_code == 201
    builtin_html = (await client.get("/v1/templates/default", headers=AUTH)).json()["html"]
    assert (await client.get("/v1/templates/mine", headers=AUTH)).json()["html"] == builtin_html

    # The copy is editable even though the original is not.
    resp = await client.patch("/v1/templates/mine", json={"html": CUSTOM}, headers=AUTH)
    assert resp.status_code == 200


async def test_create_validation(app_client):
    _, client = app_client

    async def create(payload, expect_status, expect_detail):
        resp = await client.post("/v1/templates", json=payload, headers=AUTH)
        assert resp.status_code == expect_status, resp.text
        assert expect_detail in resp.json()["detail"]

    await create({"name": "x", "html": CUSTOM, "copy_of": "default"}, 422, "not both")
    await create({"name": "x"}, 422, "provide either html or copy_of")
    await create({"name": "default", "html": CUSTOM}, 422, "reserved")
    await create({"name": "none", "html": CUSTOM}, 422, "reserved")
    await create({"name": "!!!", "html": CUSTOM}, 422, "letters or numbers")
    await create({"name": "x", "html": "  "}, 422, "empty")
    await create({"name": "x", "html": "<b>$footer</b>"}, 422, "$content")
    await create({"name": "x", "html": "<b>$content</b>"}, 422, "$footer")
    await create({"name": "x", "html": "$content $footer $oops"}, 422, "$oops")
    await create({"name": "x", "html": "$content $footer costs 5$"}, 422, "placeholder syntax")
    await create({"name": "x", "copy_of": "ghost"}, 404, "template not found")

    # Names are slugified; duplicates collide on the normalized form.
    await make_template(client, name="My Fancy!")
    resp = await client.post("/v1/templates", json={"name": "my-fancy", "html": CUSTOM}, headers=AUTH)
    assert resp.status_code == 409

    # A literal dollar works when escaped, and $subject/$header stay optional.
    resp = await client.post(
        "/v1/templates", json={"name": "escaped", "html": "$content $footer costs 5$$"}, headers=AUTH
    )
    assert resp.status_code == 201


async def test_edit_rejects_invalid_html_and_rename_collisions(app_client):
    _, client = app_client
    await make_template(client, name="one")
    await make_template(client, name="two", html=SECOND)

    resp = await client.patch("/v1/templates/one", json={"html": "<b>no placeholders</b>"}, headers=AUTH)
    assert resp.status_code == 422

    resp = await client.patch("/v1/templates/one", json={"name": "two"}, headers=AUTH)
    assert resp.status_code == 409


# ---------- test sends ----------


async def test_template_test_send(app_client):
    app, client = app_client
    fake_ses = app.state.settings.extra["ses"]
    await make_template(client)

    resp = await client.post("/v1/templates/fancy/test", json={"to": "me@x.com"}, headers=AUTH)
    assert resp.status_code == 200
    assert resp.json()["sent_to"] == "me@x.com"
    message = fake_ses.sent[-1]
    assert message["subject"].startswith("[template fancy]")
    assert 'id="custom-shell"' in message["html"]
    assert "Unsubscribe" in message["html"], "the sample must carry the footer"

    resp = await client.post("/v1/templates/default/test", json={"to": "me@x.com"}, headers=AUTH)
    assert resp.status_code == 200
    assert "custom-shell" not in fake_ses.sent[-1]["html"]

    assert (
        await client.post("/v1/templates/fancy/test", json={"to": "nope"}, headers=AUTH)
    ).status_code == 422
    assert (
        await client.post("/v1/templates/ghost/test", json={"to": "me@x.com"}, headers=AUTH)
    ).status_code == 404


# ---------- resolution: campaign > list > built-in ----------


async def test_list_default_and_campaign_override_resolution(app_client):
    _, client = app_client
    await make_template(client, name="listwide")
    await make_template(client, name="special", html=SECOND)

    resp = await client.patch("/v1/lists/default", json={"template": "listwide"}, headers=AUTH)
    assert resp.json()["template"] == "listwide"
    detail = (await client.get("/v1/lists/default", headers=AUTH)).json()
    assert detail["template"] == "listwide"

    inheriting = await make_campaign(client)
    assert inheriting["template"] is None, "no pin: inherits the list default at send time"
    preview = (await client.get(f"/v1/campaigns/{inheriting['id']}/preview", headers=AUTH)).json()
    assert preview["template"] == "listwide"
    assert 'id="custom-shell"' in preview["html"]

    resp = await client.post(
        "/v1/lists/default/campaigns",
        json={"subject": "s", "body_markdown": "b", "template": "special"},
        headers=AUTH,
    )
    pinned = resp.json()
    assert pinned["template"] == "special"
    preview = (await client.get(f"/v1/campaigns/{pinned['id']}/preview", headers=AUTH)).json()
    assert preview["template"] == "special"
    assert 'id="second-shell"' in preview["html"]

    # Pinning 'default' beats the list's custom default.
    resp = await client.post(
        "/v1/lists/default/campaigns",
        json={"subject": "s", "body_markdown": "b", "template": "default"},
        headers=AUTH,
    )
    stock = resp.json()
    assert stock["template"] == "default"
    preview = (await client.get(f"/v1/campaigns/{stock['id']}/preview", headers=AUTH)).json()
    assert preview["template"] == "default"
    assert "custom-shell" not in preview["html"]

    # Unknown template references fail loudly on create and edit.
    resp = await client.post(
        "/v1/lists/default/campaigns",
        json={"subject": "s", "body_markdown": "b", "template": "ghost"},
        headers=AUTH,
    )
    assert resp.status_code == 404
    assert (
        await client.patch("/v1/lists/default", json={"template": "ghost"}, headers=AUTH)
    ).status_code == 404

    # Clearing the list default falls back to the built-in.
    resp = await client.patch("/v1/lists/default", json={"template": ""}, headers=AUTH)
    assert resp.json()["template"] == "default"
    preview = (await client.get(f"/v1/campaigns/{inheriting['id']}/preview", headers=AUTH)).json()
    assert preview["template"] == "default"


async def test_campaign_test_send_uses_effective_template(app_client):
    app, client = app_client
    fake_ses = app.state.settings.extra["ses"]
    await make_template(client)
    campaign = await make_campaign(client)
    resp = await client.patch(f"/v1/campaigns/{campaign['id']}", json={"template": "fancy"}, headers=AUTH)
    assert resp.json()["template"] == "fancy"

    resp = await client.post(f"/v1/campaigns/{campaign['id']}/test", json={"to": "me@x.com"}, headers=AUTH)
    assert resp.status_code == 200
    assert 'id="custom-shell"' in fake_ses.sent[-1]["html"]


async def test_sender_renders_through_effective_template(app_client):
    app, client = app_client
    fake_ses = app.state.settings.extra["ses"]
    await add_subscriber(client, "a@x.com", name="Ada")
    await make_template(client, name="listwide")
    await make_template(client, name="special", html=SECOND)
    await client.patch("/v1/lists/default", json={"template": "listwide"}, headers=AUTH)

    inheriting = await make_campaign(client)
    await client.post(
        f"/v1/campaigns/{inheriting['id']}/send", json={"confirm": True, "skip_test": True}, headers=AUTH
    )
    await _drain(app)
    message = fake_ses.sent[-1]
    assert 'id="custom-shell"' in message["html"]
    assert "http://test.local/unsubscribe/" in message["html"], "footer must survive a custom shell"

    resp = await client.post(
        "/v1/lists/default/campaigns",
        json={"subject": "s2", "body_markdown": "b2", "template": "special"},
        headers=AUTH,
    )
    await client.post(
        f"/v1/campaigns/{resp.json()['id']}/send", json={"confirm": True, "skip_test": True}, headers=AUTH
    )
    await _drain(app)
    assert 'id="second-shell"' in fake_ses.sent[-1]["html"]


async def test_sender_falls_back_to_builtin_when_template_row_vanishes(app_client):
    app, client = app_client
    fake_ses = app.state.settings.extra["ses"]
    await add_subscriber(client, "a@x.com")
    await make_template(client)
    campaign = await make_campaign(client)
    await client.patch(f"/v1/campaigns/{campaign['id']}", json={"template": "fancy"}, headers=AUTH)
    await client.post(
        f"/v1/campaigns/{campaign['id']}/send", json={"confirm": True, "skip_test": True}, headers=AUTH
    )
    # Deletion of a referenced template is refused by the API, so only manual DB
    # surgery can produce this; the queue must keep draining regardless.
    await app.state.db.execute(
        "UPDATE campaigns SET template_id = 'tpl_gone' WHERE id = ?", (campaign["id"],)
    )
    await app.state.db.commit()

    await _drain(app)
    assert fake_ses.sent[-1]["to"] == "a@x.com"
    assert "custom-shell" not in fake_ses.sent[-1]["html"]


# ---------- guardrails ----------


async def test_send_preflight_rejects_missing_template(app_client):
    app, client = app_client
    await add_subscriber(client, "a@x.com")
    await make_template(client)
    campaign = await make_campaign(client)
    await client.patch(f"/v1/campaigns/{campaign['id']}", json={"template": "fancy"}, headers=AUTH)
    await app.state.db.execute(
        "UPDATE campaigns SET template_id = 'tpl_gone' WHERE id = ?", (campaign["id"],)
    )
    await app.state.db.commit()

    for payload in ({"dry_run": True}, {"confirm": True, "skip_test": True}):
        resp = await client.post(f"/v1/campaigns/{campaign['id']}/send", json=payload, headers=AUTH)
        assert resp.status_code == 412
        assert "missing template" in resp.json()["detail"]


async def test_template_edit_clears_stale_test_sends(app_client):
    _, client = app_client
    await make_template(client)
    await client.patch("/v1/lists/default", json={"template": "fancy"}, headers=AUTH)

    inheriting = await make_campaign(client, subject="inherits")
    resp = await client.post(
        "/v1/lists/default/campaigns",
        json={"subject": "pins", "body_markdown": "b", "template": "fancy"},
        headers=AUTH,
    )
    pinned = resp.json()
    resp = await client.post(
        "/v1/lists/default/campaigns",
        json={"subject": "stock", "body_markdown": "b", "template": "default"},
        headers=AUTH,
    )
    stock = resp.json()
    for c in (inheriting, pinned, stock):
        await client.post(f"/v1/campaigns/{c['id']}/test", json={"to": "me@x.com"}, headers=AUTH)
        assert (await _campaign_detail(client, c["id"]))["test_sent_at"] is not None

    # A rename does not change what renders, so tests stay valid.
    await client.patch("/v1/templates/fancy", json={"name": "fancier"}, headers=AUTH)
    assert (await _campaign_detail(client, inheriting["id"]))["test_sent_at"] is not None

    # An HTML change invalidates every draft that would render through it.
    resp = await client.patch("/v1/templates/fancier", json={"html": SECOND}, headers=AUTH)
    assert resp.status_code == 200
    assert (await _campaign_detail(client, inheriting["id"]))["test_sent_at"] is None
    assert (await _campaign_detail(client, pinned["id"]))["test_sent_at"] is None
    assert (await _campaign_detail(client, stock["id"]))["test_sent_at"] is not None

    # Re-test, then swap the list default: only inheriting drafts go stale.
    await client.post(f"/v1/campaigns/{inheriting['id']}/test", json={"to": "me@x.com"}, headers=AUTH)
    await client.post(f"/v1/campaigns/{pinned['id']}/test", json={"to": "me@x.com"}, headers=AUTH)
    await client.patch("/v1/lists/default", json={"template": ""}, headers=AUTH)
    assert (await _campaign_detail(client, inheriting["id"]))["test_sent_at"] is None
    assert (await _campaign_detail(client, pinned["id"]))["test_sent_at"] is not None


async def test_delete_guards_for_referenced_templates(app_client):
    _, client = app_client
    await make_template(client)

    await client.patch("/v1/lists/default", json={"template": "fancy"}, headers=AUTH)
    resp = await client.delete("/v1/templates/fancy", headers=AUTH)
    assert resp.status_code == 409
    assert "default" in resp.json()["detail"], "the list slug should be named"

    await client.patch("/v1/lists/default", json={"template": ""}, headers=AUTH)
    resp = await client.post(
        "/v1/lists/default/campaigns",
        json={"subject": "s", "body_markdown": "b", "template": "fancy"},
        headers=AUTH,
    )
    campaign = resp.json()
    resp = await client.delete("/v1/templates/fancy", headers=AUTH)
    assert resp.status_code == 409
    assert "unsent campaign" in resp.json()["detail"]

    await client.patch(f"/v1/campaigns/{campaign['id']}", json={"template": ""}, headers=AUTH)
    assert (await client.delete("/v1/templates/fancy", headers=AUTH)).status_code == 204


async def test_sent_campaign_does_not_block_template_delete(app_client):
    app, client = app_client
    await add_subscriber(client, "a@x.com")
    await make_template(client)
    campaign = await make_campaign(client)
    await client.patch(f"/v1/campaigns/{campaign['id']}", json={"template": "fancy"}, headers=AUTH)
    await client.post(
        f"/v1/campaigns/{campaign['id']}/send", json={"confirm": True, "skip_test": True}, headers=AUTH
    )
    await _drain(app)
    assert (await _campaign_detail(client, campaign["id"]))["status"] == "sent"

    assert (await client.delete("/v1/templates/fancy", headers=AUTH)).status_code == 204
    # History keeps the dangling id; the display name degrades to null.
    assert (await _campaign_detail(client, campaign["id"]))["template"] is None


async def test_template_edit_blocked_while_campaign_sending(app_client):
    app, client = app_client
    await add_subscriber(client, "a@x.com")
    await make_template(client)
    campaign = await make_campaign(client)
    await client.patch(f"/v1/campaigns/{campaign['id']}", json={"template": "fancy"}, headers=AUTH)
    await client.post(
        f"/v1/campaigns/{campaign['id']}/send", json={"confirm": True, "skip_test": True}, headers=AUTH
    )
    # The sender has not drained yet: the campaign is mid-send.
    resp = await client.patch("/v1/templates/fancy", json={"html": SECOND}, headers=AUTH)
    assert resp.status_code == 409
    assert "currently sending" in resp.json()["detail"]
    # A rename is harmless mid-send.
    assert (
        await client.patch("/v1/templates/fancy", json={"name": "renamed"}, headers=AUTH)
    ).status_code == 200

    await _drain(app)
    assert (
        await client.patch("/v1/templates/renamed", json={"html": SECOND}, headers=AUTH)
    ).status_code == 200
