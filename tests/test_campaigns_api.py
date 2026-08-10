from conftest import AUTH, add_subscriber, make_campaign


async def test_send_guardrails(app_client):
    app, client = app_client
    await add_subscriber(client, "a@x.com")
    campaign = await make_campaign(client)
    cid = campaign["id"]

    resp = await client.post(f"/v1/campaigns/{cid}/send", json={}, headers=AUTH)
    assert resp.status_code == 400
    assert "confirm" in resp.json()["detail"]

    resp = await client.post(f"/v1/campaigns/{cid}/send", json={"confirm": True}, headers=AUTH)
    assert resp.status_code == 412, "must require a test send first"

    resp = await client.post(f"/v1/campaigns/{cid}/send", json={"dry_run": True}, headers=AUTH)
    assert resp.json() == {
        "dry_run": True,
        "recipient_count": 1,
        "sample": ["a@x.com"],
        "template": "default",
    }

    resp = await client.post(f"/v1/campaigns/{cid}/test", json={"to": "me@x.com"}, headers=AUTH)
    assert resp.status_code == 200
    fake_ses = app.state.settings.extra["ses"]
    assert fake_ses.sent[-1]["subject"].startswith("[test] ")

    resp = await client.post(f"/v1/campaigns/{cid}/send", json={"confirm": True}, headers=AUTH)
    assert resp.status_code == 200
    assert resp.json()["status"] == "sending"
    assert resp.json()["recipient_count"] == 1

    again = await client.post(f"/v1/campaigns/{cid}/send", json={"confirm": True}, headers=AUTH)
    assert again.status_code == 409

    edit = await client.patch(f"/v1/campaigns/{cid}", json={"subject": "x"}, headers=AUTH)
    assert edit.status_code == 409, "sending campaigns are immutable"


async def test_audience_selection(app_client):
    app, client = app_client
    await add_subscriber(client, "active@x.com")
    await add_subscriber(client, "tagged@x.com", tags=["vip"])
    await add_subscriber(client, "pending@x.com", status="unconfirmed")
    await add_subscriber(client, "gone@x.com", status="unsubscribed")
    await add_subscriber(client, "burned@x.com")
    await client.post("/v1/suppressions", json={"email": "burned@x.com"}, headers=AUTH)

    everyone = await make_campaign(client)
    resp = await client.post(f"/v1/campaigns/{everyone['id']}/send", json={"dry_run": True}, headers=AUTH)
    assert resp.json()["recipient_count"] == 2, (
        "active + tagged only; not unconfirmed/unsubscribed/suppressed"
    )

    vips = await make_campaign(client, subject="VIP only", tag="vip")
    resp = await client.post(f"/v1/campaigns/{vips['id']}/send", json={"dry_run": True}, headers=AUTH)
    assert resp.json()["recipient_count"] == 1


async def test_send_idempotency_key_replay(app_client):
    app, client = app_client
    await add_subscriber(client, "a@x.com")
    campaign = await make_campaign(client)
    cid = campaign["id"]
    headers = {**AUTH, "Idempotency-Key": f"send-{cid}"}

    first = await client.post(
        f"/v1/campaigns/{cid}/send", json={"confirm": True, "skip_test": True}, headers=headers
    )
    assert first.status_code == 200

    replay = await client.post(
        f"/v1/campaigns/{cid}/send", json={"confirm": True, "skip_test": True}, headers=headers
    )
    assert replay.status_code == 200
    assert replay.headers.get("Idempotency-Replayed") == "true"
    assert replay.json() == first.json()

    stats = await client.get(f"/v1/campaigns/{cid}/stats", headers=AUTH)
    assert stats.json()["pending"] == 1, "replay must not enqueue twice"


async def test_preview_and_empty_audience(app_client):
    app, client = app_client
    campaign = await make_campaign(client, body="Hi {{name|there}}")
    resp = await client.get(f"/v1/campaigns/{campaign['id']}/preview", headers=AUTH)
    assert "Hi Sam" in resp.json()["html"]

    resp = await client.post(
        f"/v1/campaigns/{campaign['id']}/send", json={"confirm": True, "skip_test": True}, headers=AUTH
    )
    assert resp.status_code == 400, "empty audience refused"


async def test_doctor(app_client, monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIATEST")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    app, client = app_client
    resp = await client.get("/v1/doctor", headers=AUTH)
    data = resp.json()
    names = {c["name"] for c in data["checks"]}
    assert {"config", "aws-credentials", "ses-sending", "database"} <= names
    assert not any(c["status"] == "fail" for c in data["checks"]), data
