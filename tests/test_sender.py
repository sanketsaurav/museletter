import museletter.sender as sender_mod
from conftest import AUTH, add_subscriber, make_campaign
from museletter.sender import SenderLoop
from museletter.ses import SESError


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


async def test_ledger_send_marks_rows_and_completes(app_client):
    app, client = app_client
    fake_ses = app.state.settings.extra["ses"]
    await add_subscriber(client, "a@x.com", name="Ada")
    await add_subscriber(client, "b@x.com")
    await add_subscriber(client, "c@x.com")
    cid = await _start_campaign(client, body="Hi {{name|there}}, new post!")

    # Suppression arriving after materialization must still be honored at send time.
    await client.post("/v1/suppressions", json={"email": "c@x.com"}, headers=AUTH)

    await _drain(app)

    stats = await _stats(client, cid)
    assert stats["status"] == "sent"
    assert stats["sent"] == 2
    assert stats["suppressed"] == 1
    assert stats["pending"] == 0

    assert len(fake_ses.sent) == 2
    for message in fake_ses.sent:
        assert "List-Unsubscribe" in message["headers"]
        assert message["headers"]["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
        assert "http://test.local/unsubscribe/" in message["html"]
    ada = next(m for m in fake_ses.sent if m["to"] == "a@x.com")
    assert "Hi Ada," in ada["html"]
    other = next(m for m in fake_ses.sent if m["to"] == "b@x.com")
    assert "Hi there," in other["html"]
    assert ada["headers"]["List-Unsubscribe"] != other["headers"]["List-Unsubscribe"]


async def test_permanent_error_fails_after_max_attempts(app_client):
    app, client = app_client
    fake_ses = app.state.settings.extra["ses"]
    await add_subscriber(client, "a@x.com")
    cid = await _start_campaign(client)

    fake_ses.fail_next = [SESError(400, "MessageRejected", "nope")] * sender_mod.MAX_ATTEMPTS
    await _drain(app)

    stats = await _stats(client, cid)
    assert stats["failed"] == 1
    assert stats["status"] == "failed", "nobody reached -> campaign failed"


async def test_throttle_backs_off_without_burning_attempts(app_client, monkeypatch):
    monkeypatch.setattr(sender_mod, "THROTTLE_BACKOFF_SECONDS", 0)
    app, client = app_client
    fake_ses = app.state.settings.extra["ses"]
    await add_subscriber(client, "a@x.com")
    cid = await _start_campaign(client)

    loop = SenderLoop(app)
    fake_ses.fail_next = [SESError(429, "TooManyRequestsException", "slow down")]
    assert await loop.tick() is True

    db = app.state.db
    async with db.execute("SELECT * FROM campaign_recipients WHERE campaign_id = ?", (cid,)) as cur:
        row = await cur.fetchone()
    assert row["status"] == "pending"
    assert row["attempts"] == 0, "throttling is not the recipient's failure"

    await _drain(app)
    assert (await _stats(client, cid))["sent"] == 1


async def test_resume_after_restart(app_client):
    app, client = app_client
    fake_ses = app.state.settings.extra["ses"]
    for i in range(5):
        await add_subscriber(client, f"r{i}@x.com")
    cid = await _start_campaign(client)

    first = SenderLoop(app)
    await first.tick()  # one batch (all 5 fit), pretend the process dies afterwards

    fresh = SenderLoop(app)
    for _ in range(10):
        if not await fresh.tick():
            break
    stats = await _stats(client, cid)
    assert stats["status"] == "sent"
    assert stats["sent"] == 5
    assert len(fake_ses.sent) == 5, "no double sends across restarts"
