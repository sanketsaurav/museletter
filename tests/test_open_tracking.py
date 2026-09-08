import asyncio
import re
import sqlite3
from string import Template

import httpx
import pytest

from conftest import AUTH, add_subscriber, make_campaign
from museletter.db import open_db
from museletter.events import apply_events
from museletter.mailer import SendResult
from museletter.render import personalize_email, render_campaign
from museletter.tokens import make_open_token, make_token, verify_open_token, verify_token


def pixel_url(message):
    urls = re.findall(r'src="([^"]+/open/[^"/]+\.gif)"', message["html"])
    assert len(urls) == 1
    assert "/open/" not in message["text"]
    return urls[0]


async def start(client, cid):
    response = await client.post(
        f"/v1/campaigns/{cid}/send", json={"confirm": True, "skip_test": True}, headers=AUTH
    )
    assert response.status_code == 200, response.text


async def stats(client, cid):
    response = await client.get(f"/v1/campaigns/{cid}/stats", headers=AUTH)
    assert response.status_code == 200
    return response.json()


async def test_opens_are_per_recipient_and_campaign_and_survive_delivery_events(app_client, monkeypatch):
    app, client = app_client
    alice = await add_subscriber(client, "alice@example.com")
    await add_subscriber(client, "bob@example.com")
    first = await make_campaign(client)
    second = await make_campaign(client)
    for campaign in (first, second):
        await start(client, campaign["id"])
        await app.state.sender.tick()
        await app.state.sender.tick()
    messages = app.state.mailer.sent
    urls = [pixel_url(message) for message in messages]
    assert len(set(urls)) == 4
    assert all("alice@example.com" not in url for url in urls)

    monkeypatch.setattr("museletter.api.public.utcnow", lambda: "2026-09-08T12:00:00.000Z")
    response = await client.get(urls[0])
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/gif"
    assert "no-store" in response.headers["cache-control"]
    assert response.content.startswith(b"GIF89a\x01\x00\x01\x00")
    monkeypatch.setattr("museletter.api.public.utcnow", lambda: "2026-09-08T13:00:00.000Z")
    # SQL increments must not lose concurrent image loads or double-count unique readers.
    responses = await asyncio.gather(*(client.get(urls[0]) for _ in range(10)))
    assert all(r.status_code == 200 for r in responses)
    report = await stats(client, first["id"])
    assert (report["unique_opens"], report["total_opens"], report["open_rate"]) == (1, 11, 50.0)
    assert report["sent"] == 2
    assert (await stats(client, second["id"]))["unique_opens"] == 0

    async with app.state.db.execute(
        "SELECT * FROM campaign_recipients WHERE campaign_id = ? AND subscriber_id = ?",
        (first["id"], alice["id"]),
    ) as cur:
        row = await cur.fetchone()
    assert row["first_opened_at"] == "2026-09-08T12:00:00.000Z"
    assert row["last_opened_at"] == "2026-09-08T13:00:00.000Z"
    event = {
        "type": "delivery",
        "email": alice["email"],
        "message_id": row["ses_message_id"],
        "permanent": False,
        "detail": "delivered",
    }
    await apply_events(app.state.db, [event, event])
    await client.get(urls[1])
    report = await stats(client, first["id"])
    assert report["delivered"] == 1 and report["sent"] == 1
    assert (report["unique_opens"], report["total_opens"], report["open_rate"]) == (2, 12, 100.0)
    detail = (await client.get(f"/v1/campaigns/{first['id']}", headers=AUTH)).json()
    assert detail["stats"]["unique_opens"] == 2


async def test_invalid_unattempted_and_deleted_links_are_inert(app_client):
    app, client = app_client
    sub = await add_subscriber(client, "a@example.com")
    campaign = await make_campaign(client)
    cid = campaign["id"]
    token = make_open_token(app.state.secret, cid, sub["id"])
    url = f"/open/{token}.gif"
    await start(client, cid)
    await client.get(url)
    assert (await stats(client, cid))["total_opens"] == 0
    await app.state.sender.tick()
    await app.state.sender.tick()
    invalid_tokens = [
        "garbage",
        token + "x",
        "YQ.café",
        make_open_token("wrong secret", cid, sub["id"]),
        make_open_token(app.state.secret, "cmp_missing", sub["id"]),
        make_open_token(app.state.secret, cid, "sub_missing"),
        make_token(app.state.secret, "unsubscribe", sub["id"]),
        make_token(app.state.secret, "confirm", sub["id"]),
        make_token(app.state.secret, "open", "missing-campaign"),
    ]
    for invalid in invalid_tokens:
        response = await client.get(f"/open/{invalid}.gif")
        assert response.status_code == 200
        assert response.content.startswith(b"GIF89a")
    head = await client.head(url)
    assert head.status_code == 200 and head.content == b""
    assert (await stats(client, cid))["total_opens"] == 0
    assert (await client.get(f"/v1/campaigns/{cid}/stats")).status_code == 401
    await client.delete(f"/v1/subscribers/{sub['id']}", headers=AUTH)
    assert (await client.get(url)).status_code == 200
    assert (await stats(client, cid))["total_opens"] == 0
    await client.delete(f"/v1/campaigns/{cid}", headers=AUTH)
    assert (await client.get(url)).status_code == 200


async def test_tracking_setting_and_test_sends(app_client):
    app, client = app_client
    await add_subscriber(client, "a@example.com")
    campaign = await make_campaign(client)
    cid = campaign["id"]
    assert campaign["track_opens"] is True
    assert (await stats(client, cid))["open_rate"] == 0.0
    preview = await client.get(f"/v1/campaigns/{cid}/preview", headers=AUTH)
    assert "/open/" not in preview.json()["html"]
    await client.post(f"/v1/campaigns/{cid}/test", json={"to": "me@example.com"}, headers=AUTH)
    assert "/open/" not in app.state.mailer.sent[-1]["html"]
    response = await client.patch(f"/v1/campaigns/{cid}", json={"track_opens": False}, headers=AUTH)
    assert response.json()["track_opens"] is False
    assert response.json()["test_sent_at"] is None
    response = await client.patch(f"/v1/campaigns/{cid}", json={"subject": "New subject"}, headers=AUTH)
    assert response.json()["track_opens"] is False
    await start(client, cid)
    await app.state.sender.tick()
    assert "/open/" not in app.state.mailer.sent[-1]["html"]
    report = await stats(client, cid)
    assert report["track_opens"] is False and report["unique_opens"] == 0
    async with app.state.db.execute("SELECT subscriber_id FROM campaign_recipients") as cur:
        sub = await cur.fetchone()
    token = make_open_token(app.state.secret, cid, sub["subscriber_id"])
    await client.get(f"/open/{token}.gif")
    assert (await stats(client, cid))["total_opens"] == 0
    response = await client.patch(f"/v1/campaigns/{cid}", json={"track_opens": True}, headers=AUTH)
    assert response.status_code == 409
    response = await client.post(
        "/v1/lists/default/campaigns",
        json={"subject": "Untracked", "body_markdown": "Body", "track_opens": False},
        headers=AUTH,
    )
    assert response.status_code == 201 and response.json()["track_opens"] is False


@pytest.mark.parametrize("message_id", ["ses-id", ""])
async def test_open_during_provider_call_and_retry_preserves_counts(app_client, monkeypatch, message_id):
    app, client = app_client
    await add_subscriber(client, "a@example.com")
    campaign = await make_campaign(client)
    await start(client, campaign["id"])
    urls = []

    async def sending(to, subject, html, text, **kwargs):
        url = pixel_url({"html": html, "text": text})
        urls.append(url)
        assert (await client.get(url)).status_code == 200
        if len(urls) == 1:
            raise httpx.ReadTimeout("provider accepted but response was lost")
        return SendResult(message_id=message_id)

    monkeypatch.setattr(app.state.mailer, "send_email", sending)
    await app.state.sender.tick()
    pending = await stats(client, campaign["id"])
    assert pending["pending"] == 1 and pending["unique_opens"] == 1
    assert pending["open_rate"] == 0.0
    await app.state.sender.tick()
    await app.state.sender.tick()
    assert urls[0] == urls[1]
    report = await stats(client, campaign["id"])
    assert report["sent"] == 1
    assert (report["unique_opens"], report["total_opens"], report["open_rate"]) == (1, 2, 100.0)


@pytest.mark.parametrize(
    "template",
    [None, Template("<HTML><BODY>$content$footer</BODY></HTML>"), Template("<main>$content$footer</main>")],
)
def test_pixel_works_with_builtin_and_custom_templates(template):
    subject, html, text = personalize_email(
        render_campaign("Subject", "[Original link](https://example.com/post)"),
        template=template,
        attribution=False,
        unsubscribe_url="https://example.com/unsubscribe/tok",
        open_tracking_url='https://example.com/open/tok.gif?a=1&b="2"',
    )
    assert html.count("/open/tok.gif") == 1
    assert 'width="1" height="1"' in html
    assert "?a=1&amp;b=&quot;2&quot;" in html
    assert 'href="https://example.com/post"' in html
    assert 'href="https://example.com/unsubscribe/tok"' in html
    assert "/open/" not in text and subject == "Subject"
    if "</body>" in html.lower():
        assert html.index("/open/") < html.lower().index("</body>")


def test_open_tokens_cannot_be_used_for_other_actions():
    token = make_open_token("secret", "cmp_a", "sub_b")
    assert verify_open_token("secret", token) == ("cmp_a", "sub_b")
    for purpose in ("confirm", "unsubscribe"):
        assert verify_token("secret", token, purpose) is None
    for identifier in ("cmp_a:", ":sub_b", "cmp_a:sub_b:extra"):
        assert verify_open_token("secret", make_token("secret", "open", identifier)) is None


async def test_migration_preserves_existing_campaigns_and_is_repeatable(tmp_path):
    path = str(tmp_path / "old.db")
    with sqlite3.connect(path) as old:
        old.executescript("""
            CREATE TABLE campaigns (
                id TEXT PRIMARY KEY, list_id TEXT NOT NULL, subject TEXT NOT NULL,
                body_markdown TEXT NOT NULL, tag_id TEXT, template_id TEXT,
                status TEXT NOT NULL DEFAULT 'draft', recipient_count INTEGER NOT NULL DEFAULT 0,
                test_sent_at TEXT, created_at TEXT NOT NULL, started_at TEXT, completed_at TEXT
            );
            CREATE TABLE campaign_recipients (
                campaign_id TEXT NOT NULL, subscriber_id TEXT NOT NULL, email TEXT NOT NULL,
                name TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0, ses_message_id TEXT, error TEXT,
                updated_at TEXT NOT NULL, PRIMARY KEY (campaign_id, subscriber_id)
            );
            INSERT INTO campaigns (id, list_id, subject, body_markdown, status, created_at)
            VALUES ('cmp_sent', 'list_1', 'Sent', 'Body', 'sent', '2026-09-01T00:00:00.000Z'),
                   ('cmp_sending', 'list_1', 'Sending', 'Body', 'sending', '2026-09-01T00:00:00.000Z'),
                   ('cmp_draft', 'list_1', 'Draft', 'Body', 'draft', '2026-09-01T00:00:00.000Z');
            INSERT INTO campaign_recipients
                (campaign_id, subscriber_id, email, status, attempts, ses_message_id, updated_at)
            VALUES ('cmp_sent', 'sub_1', 'a@example.com', 'delivered', 1, 'msg1', '2026-09-01T00:00:00.000Z');
        """)
    for _ in range(2):
        db = await open_db(path)
        try:
            async with db.execute("SELECT id, track_opens FROM campaigns") as cur:
                enabled = {r["id"]: r["track_opens"] for r in await cur.fetchall()}
            assert enabled == {"cmp_sent": 0, "cmp_sending": 0, "cmp_draft": 1}
            async with db.execute("SELECT * FROM campaign_recipients") as cur:
                row = await cur.fetchone()
            assert row is not None
            assert row["status"] == "delivered" and row["ses_message_id"] == "msg1"
            assert row["open_count"] == 0 and row["first_opened_at"] is None and row["last_opened_at"] is None
        finally:
            await db.close()
