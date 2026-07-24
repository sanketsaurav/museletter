import json

from conftest import AUTH, add_subscriber, make_campaign
from museletter.sender import SenderLoop
from museletter.sns import canonical_message, is_amazon_sns_url, parse_ses_events


def test_is_amazon_sns_url():
    assert is_amazon_sns_url("https://sns.us-east-1.amazonaws.com/whatever")
    assert not is_amazon_sns_url("http://sns.us-east-1.amazonaws.com/whatever")
    assert not is_amazon_sns_url("https://sns.us-east-1.amazonaws.com.evil.com/x")
    assert not is_amazon_sns_url("https://example.com/sns.us-east-1.amazonaws.com")


def test_canonical_message_order():
    msg = {"Type": "Notification", "Message": "m", "MessageId": "id", "Timestamp": "t", "TopicArn": "arn"}
    assert (
        canonical_message(msg)
        == b"Message\nm\nMessageId\nid\nTimestamp\nt\nTopicArn\narn\nType\nNotification\n"
    )


def test_parse_ses_events_formats():
    bounce = {
        "eventType": "Bounce",
        "bounce": {
            "bounceType": "Permanent",
            "bounceSubType": "General",
            "bouncedRecipients": [{"emailAddress": "B@x.com"}],
        },
        "mail": {"messageId": "m1"},
    }
    events = parse_ses_events(json.dumps(bounce))
    assert events == [
        {
            "type": "bounce",
            "email": "b@x.com",
            "message_id": "m1",
            "permanent": True,
            "detail": "Permanent/General",
        }
    ]

    classic = {
        "notificationType": "Delivery",
        "delivery": {"recipients": ["a@x.com"]},
        "mail": {"messageId": "m2"},
    }
    assert parse_ses_events(json.dumps(classic))[0]["type"] == "delivery"

    assert parse_ses_events("not json") == []
    assert parse_ses_events(json.dumps({"eventType": "Open"})) == []


async def _sent_campaign(app, client, emails):
    for email in emails:
        await add_subscriber(client, email)
    campaign = await make_campaign(client)
    await client.post(
        f"/v1/campaigns/{campaign['id']}/send", json={"confirm": True, "skip_test": True}, headers=AUTH
    )
    loop = SenderLoop(app)
    for _ in range(10):
        if not await loop.tick():
            break
    return campaign["id"]


def _notification(event: dict) -> dict:
    return {"Type": "Notification", "Message": json.dumps(event)}


async def _recipient_rows(app, cid):
    async with app.state.db.execute(
        "SELECT * FROM campaign_recipients WHERE campaign_id = ? ORDER BY email", (cid,)
    ) as cur:
        return {r["email"]: r for r in await cur.fetchall()}


async def test_webhook_delivery_bounce_complaint(app_client):
    app, client = app_client
    cid = await _sent_campaign(app, client, ["a@x.com", "b@x.com", "c@x.com"])
    rows = await _recipient_rows(app, cid)

    resp = await client.post(
        "/webhooks/sns",
        json=_notification(
            {
                "eventType": "Delivery",
                "delivery": {"recipients": ["a@x.com"]},
                "mail": {"messageId": rows["a@x.com"]["ses_message_id"]},
            }
        ),
    )
    assert resp.json()["processed"] == 1

    await client.post(
        "/webhooks/sns",
        json=_notification(
            {
                "eventType": "Bounce",
                "bounce": {
                    "bounceType": "Permanent",
                    "bounceSubType": "General",
                    "bouncedRecipients": [{"emailAddress": "b@x.com"}],
                },
                "mail": {"messageId": rows["b@x.com"]["ses_message_id"]},
            }
        ),
    )
    await client.post(
        "/webhooks/sns",
        json=_notification(
            {
                "eventType": "Complaint",
                "complaint": {
                    "complainedRecipients": [{"emailAddress": "c@x.com"}],
                    "complaintFeedbackType": "abuse",
                },
                "mail": {"messageId": rows["c@x.com"]["ses_message_id"]},
            }
        ),
    )

    rows = await _recipient_rows(app, cid)
    assert rows["a@x.com"]["status"] == "delivered"
    assert rows["b@x.com"]["status"] == "bounced"
    assert rows["c@x.com"]["status"] == "complained"

    suppressions = (await client.get("/v1/suppressions", headers=AUTH)).json()
    assert {s["email"] for s in suppressions["suppressions"]} == {"b@x.com", "c@x.com"}

    subs = (await client.get("/v1/lists/default/subscribers", headers=AUTH)).json()["subscribers"]
    by_email = {s["email"]: s["status"] for s in subs}
    assert by_email["b@x.com"] == "bounced"
    assert by_email["c@x.com"] == "complained"

    stats = (await client.get(f"/v1/campaigns/{cid}/stats", headers=AUTH)).json()
    assert stats["delivered"] == 1 and stats["bounced"] == 1 and stats["complained"] == 1


async def test_transient_bounce_does_not_suppress(app_client):
    app, client = app_client
    cid = await _sent_campaign(app, client, ["t@x.com"])
    rows = await _recipient_rows(app, cid)
    await client.post(
        "/webhooks/sns",
        json=_notification(
            {
                "eventType": "Bounce",
                "bounce": {
                    "bounceType": "Transient",
                    "bounceSubType": "MailboxFull",
                    "bouncedRecipients": [{"emailAddress": "t@x.com"}],
                },
                "mail": {"messageId": rows["t@x.com"]["ses_message_id"]},
            }
        ),
    )
    assert (await client.get("/v1/suppressions", headers=AUTH)).json()["total"] == 0
    subs = (await client.get("/v1/lists/default/subscribers", headers=AUTH)).json()["subscribers"]
    assert subs[0]["status"] == "active", "transient bounce must not deactivate the subscriber"


async def test_webhook_rejects_bad_input(app_client):
    app, client = app_client
    resp = await client.post(
        "/webhooks/sns", content=b"not json", headers={"content-type": "application/json"}
    )
    assert resp.status_code == 400

    resp = await client.post(
        "/webhooks/sns",
        json={"Type": "SubscriptionConfirmation", "SubscribeURL": "https://evil.com/x"},
    )
    assert resp.status_code == 400

    resp = await client.post("/webhooks/sns", json={"Type": "Whatever"})
    assert resp.json()["ignored"] == "Whatever"
