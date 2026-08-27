from conftest import AUTH, add_subscriber, make_campaign
from museletter.sender import SenderLoop


async def _drain(app, max_ticks=20):
    loop = SenderLoop(app)
    for _ in range(max_ticks):
        if not await loop.tick():
            break


async def test_reply_to_flows_through_every_send_path(app_client):
    app, client = app_client
    app.state.settings.reply_to = "replies@test.local"
    fake = app.state.settings.extra["mailer"]

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

    # confirmation, campaign test, template test, and the ledger send itself
    assert len(fake.sent) == 4
    assert all(m["reply_to"] == "replies@test.local" for m in fake.sent)


async def test_reply_to_defaults_to_empty(app_client):
    app, client = app_client
    fake = app.state.settings.extra["mailer"]

    resp = await client.post("/subscribe/default", json={"email": "reader@example.com"})
    assert resp.status_code == 200, resp.text

    assert len(fake.sent) == 1
    assert fake.sent[0]["reply_to"] == ""
