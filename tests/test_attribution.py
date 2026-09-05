from conftest import AUTH, add_subscriber, make_campaign
from museletter.sender import SenderLoop


async def _drain(app, max_ticks=20):
    loop = SenderLoop(app)
    for _ in range(max_ticks):
        if not await loop.tick():
            break


async def _exercise_every_render_path(app, client) -> dict:
    """Confirmation email, campaign test send, template test send, the ledger
    send itself, and the campaign preview. Returns the preview JSON."""
    resp = await client.post("/subscribe/default", json={"email": "reader@example.com"})
    assert resp.status_code == 200, resp.text

    await add_subscriber(client, "a@x.com")
    campaign = await make_campaign(client)
    resp = await client.post(f"/v1/campaigns/{campaign['id']}/test", json={"to": "me@x.com"}, headers=AUTH)
    assert resp.status_code == 200, resp.text

    resp = await client.post("/v1/templates/default/test", json={"to": "me@x.com"}, headers=AUTH)
    assert resp.status_code == 200, resp.text

    resp = await client.post(f"/v1/campaigns/{campaign['id']}/send", json={"confirm": True}, headers=AUTH)
    assert resp.status_code == 200, resp.text
    await _drain(app)

    resp = await client.get(f"/v1/campaigns/{campaign['id']}/preview", headers=AUTH)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_attribution_is_on_by_default(app_client):
    app, client = app_client
    fake = app.state.settings.extra["mailer"]

    preview = await _exercise_every_render_path(app, client)

    assert len(fake.sent) == 4
    assert all("Sent with" in m["html"] and "Sent with Museletter" in m["text"] for m in fake.sent)
    assert "Sent with" in preview["html"] and "Sent with Museletter" in preview["text"]


async def test_attribution_off_drops_the_line_everywhere(app_client):
    app, client = app_client
    app.state.settings.attribution = False
    fake = app.state.settings.extra["mailer"]

    preview = await _exercise_every_render_path(app, client)

    assert len(fake.sent) == 4
    assert not any("Sent with" in m["html"] or "Sent with" in m["text"] for m in fake.sent)
    assert "Sent with" not in preview["html"] and "Sent with" not in preview["text"]

    # The parts the law needs still ship with the real campaign send.
    ledger_send = fake.sent[-1]
    assert ledger_send["to"] == "a@x.com"
    assert "/unsubscribe/" in ledger_send["html"] and "1 Test Street, Testville" in ledger_send["html"]
    assert "Unsubscribe: http://test.local/unsubscribe/" in ledger_send["text"]
