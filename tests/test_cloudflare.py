"""Cloudflare Email client unit tests: REST contract, error mapping, queue
pull/ack, and event normalization. All HTTP goes through MockTransport."""

import base64
import json

import httpx
import pytest

from museletter.cloudflare import CloudflareEmail, CloudflareEmailError, parse_cloudflare_events
from museletter.config import Settings
from museletter.mailer import create_mailer
from museletter.ses import SESClient


def make_client(monkeypatch, responder, queue_id="q_1"):
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "cf-token")
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return responder(request)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return CloudflareEmail("acct1", queue_id, http=http), requests


def send_response(**arrays):
    result = {"delivered": [], "permanent_bounces": [], "queued": []}
    result.update(arrays)
    return httpx.Response(200, json={"success": True, "errors": [], "result": result})


def _envelope(etype: str, payload: dict) -> dict:
    return {
        "type": etype,
        "source": {"type": "email.sending", "domain": "example.com"},
        "payload": {"eventId": "evt_1", "sender": "news@example.com", "subject": "s", **payload},
        "metadata": {"accountId": "acct1", "eventSchemaVersion": 1},
    }


def _parse(etype: str, **payload):
    return parse_cloudflare_events(json.dumps(_envelope(etype, payload)))


async def test_send_email_request_shape(monkeypatch):
    client, requests = make_client(monkeypatch, lambda r: send_response(queued=["reader@example.com"]))
    result = await client.send_email(
        "reader@example.com",
        "Hello",
        "<p>hi</p>",
        "hi",
        from_email="news@example.com",
        from_name="Test News",
        headers={
            "List-Unsubscribe": "<https://x/u/t>",
            "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
        },
        reply_to="replies@example.com",
    )
    assert result.outcome == "sent"
    assert result.message_id == ""

    request = requests[0]
    assert request.method == "POST"
    assert str(request.url) == "https://api.cloudflare.com/client/v4/accounts/acct1/email/sending/send"
    assert request.headers["authorization"] == "Bearer cf-token"
    assert request.headers["content-type"] == "application/json"
    body = json.loads(request.content)
    assert body["from"] == {"address": "news@example.com", "name": "Test News"}
    assert body["to"] == "reader@example.com"
    assert body["reply_to"] == "replies@example.com"
    assert body["headers"]["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"


async def test_send_email_without_display_name(monkeypatch):
    client, requests = make_client(monkeypatch, lambda r: send_response(queued=["a@x.com"]))
    await client.send_email("a@x.com", "s", "<p></p>", "t", from_email="news@example.com")
    body = json.loads(requests[0].content)
    assert body["from"] == "news@example.com"
    assert "reply_to" not in body
    assert "headers" not in body


async def test_send_synchronous_outcomes(monkeypatch):
    client, _ = make_client(monkeypatch, lambda r: send_response(delivered=["Reader@Example.com"]))
    result = await client.send_email("reader@example.com", "s", "h", "t", from_email="n@x.com")
    assert result.outcome == "delivered", "case-insensitive recipient match"

    client, _ = make_client(monkeypatch, lambda r: send_response(permanent_bounces=["a@x.com"]))
    result = await client.send_email("a@x.com", "s", "h", "t", from_email="n@x.com")
    assert result.outcome == "bounced"
    assert result.detail


async def test_error_mapping_and_throttling(monkeypatch):
    client, _ = make_client(
        monkeypatch,
        lambda r: httpx.Response(
            429, json={"success": False, "errors": [{"code": 971, "message": "rate limited"}]}
        ),
    )
    with pytest.raises(CloudflareEmailError) as exc:
        await client.send_email("a@x.com", "s", "h", "t", from_email="n@x.com")
    assert exc.value.status == 429
    assert exc.value.code == "971"
    assert exc.value.throttled


async def test_success_false_is_an_error(monkeypatch):
    client, _ = make_client(
        monkeypatch,
        lambda r: httpx.Response(
            200,
            json={"success": False, "errors": [{"code": 1000, "message": "Sender domain not verified"}]},
        ),
    )
    with pytest.raises(CloudflareEmailError) as exc:
        await client.send_email("a@x.com", "s", "h", "t", from_email="n@x.com")
    assert exc.value.code == "1000"
    assert "not verified" in exc.value.message
    assert not exc.value.throttled


async def test_error_with_non_json_body(monkeypatch):
    client, _ = make_client(monkeypatch, lambda r: httpx.Response(500, text="<html>boom</html>"))
    with pytest.raises(CloudflareEmailError) as exc:
        await client.get_suppressions()
    assert exc.value.status == 500
    assert "boom" in exc.value.message


async def test_missing_credentials(monkeypatch):
    monkeypatch.delenv("CLOUDFLARE_API_TOKEN", raising=False)
    client = CloudflareEmail("acct1")
    with pytest.raises(CloudflareEmailError) as exc:
        await client.send_email("a@x.com", "s", "h", "t", from_email="n@x.com")
    assert exc.value.code == "NoCredentials"
    assert not CloudflareEmail.has_credentials()


async def test_pull_and_ack_events(monkeypatch):
    event = _envelope("cf.email.sending.message.delivered", {"recipient": "a@x.com", "messageId": "cf-1"})

    def responder(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/messages/pull"):
            body = json.loads(request.content)
            assert body["batch_size"] == 50
            assert body["visibility_timeout_ms"] == 60000
            encoded = base64.b64encode(json.dumps(event).encode()).decode()
            messages = [{"lease_id": "lease-1", "body": encoded}, {"body": "dropped: no lease id"}]
            return httpx.Response(200, json={"success": True, "result": {"messages": messages}})
        if request.url.path.endswith("/messages/ack"):
            assert json.loads(request.content) == {"acks": [{"lease_id": "lease-1"}]}
            return httpx.Response(200, json={"success": True, "result": {"ackCount": 1}})
        return httpx.Response(500)

    client, requests = make_client(monkeypatch, responder)
    messages = await client.pull_events()
    assert [m["lease_id"] for m in messages] == ["lease-1"]
    assert parse_cloudflare_events(messages[0]["body"])[0]["email"] == "a@x.com"
    await client.ack_events(["lease-1"])
    assert requests[0].url.path == "/client/v4/accounts/acct1/queues/q_1/messages/pull"
    assert requests[1].url.path == "/client/v4/accounts/acct1/queues/q_1/messages/ack"


async def test_pull_without_queue_configured(monkeypatch):
    client, requests = make_client(monkeypatch, lambda r: httpx.Response(500), queue_id="")
    assert await client.pull_events() == []
    await client.ack_events([])
    assert requests == [], "no API calls without a queue or with nothing to ack"


async def test_get_queue_absent(monkeypatch):
    client, _ = make_client(
        monkeypatch,
        lambda r: httpx.Response(
            404, json={"success": False, "errors": [{"code": 11000, "message": "queue not found"}]}
        ),
    )
    assert await client.get_queue() is None


def test_parse_delivered():
    [event] = _parse(
        "cf.email.sending.message.delivered",
        recipient="Reader@X.com",
        messageId="cf-9",
        delivery={"status": "delivered", "smtpStatusCode": 250},
    )
    assert event == {
        "type": "delivery",
        "email": "reader@x.com",
        "message_id": "cf-9",
        "permanent": False,
        "detail": "",
    }


def test_parse_hard_bounce():
    [event] = _parse(
        "cf.email.sending.message.bounced",
        recipient="a@x.com",
        messageId="cf-1",
        bounce={"type": "hard", "classification": "permanent_failure", "reason": "mailbox does not exist"},
    )
    assert event["type"] == "bounce"
    assert event["permanent"] is True
    assert event["detail"] == "hard/mailbox does not exist"


def test_parse_soft_bounce_is_not_permanent():
    [event] = _parse(
        "cf.email.sending.message.bounced",
        recipient="a@x.com",
        bounce={"type": "soft", "classification": "temporary_failure", "reason": "mailbox full"},
    )
    assert event["permanent"] is False


def test_parse_complaint():
    [event] = _parse(
        "cf.email.sending.message.complained",
        recipient="a@x.com",
        messageId="cf-2",
        complaint={"type": "abuse"},
    )
    assert event == {
        "type": "complaint",
        "email": "a@x.com",
        "message_id": "cf-2",
        "permanent": True,
        "detail": "abuse",
    }


def test_parse_failed_and_rejected_are_soft_bounces():
    [failed] = _parse("cf.email.sending.message.failed", recipient="a@x.com", failure={"reason": "expired"})
    assert (failed["type"], failed["permanent"], failed["detail"]) == ("bounce", False, "failed/expired")
    [rejected] = _parse(
        "cf.email.sending.message.rejected",
        recipient="a@x.com",
        rejection={"reason": "policy", "party": "cloudflare"},
    )
    assert (rejected["type"], rejected["permanent"], rejected["detail"]) == (
        "bounce",
        False,
        "rejected/policy",
    )


def test_parse_deferred_is_recorded_without_ledger_action():
    [event] = _parse(
        "cf.email.sending.message.deferred",
        recipient="a@x.com",
        bounce={"type": "soft", "reason": "greylisted"},
    )
    assert event["type"] == "deferred"
    assert event["permanent"] is False
    assert event["detail"] == "soft/greylisted"


def test_parse_rejects_garbage():
    assert parse_cloudflare_events("not json") == []
    assert parse_cloudflare_events(base64.b64encode(b"still not json").decode()) == []
    assert parse_cloudflare_events(json.dumps({"type": "cf.workers.build.completed"})) == []
    no_recipient = json.dumps(_envelope("cf.email.sending.message.delivered", {}))
    assert parse_cloudflare_events(no_recipient) == []


def test_parse_accepts_plain_and_base64_json():
    envelope = json.dumps(_envelope("cf.email.sending.message.delivered", {"recipient": "a@x.com"}))
    assert parse_cloudflare_events(envelope)[0]["email"] == "a@x.com"
    encoded = base64.b64encode(envelope.encode()).decode()
    assert parse_cloudflare_events(encoded)[0]["email"] == "a@x.com"


def test_create_mailer_picks_provider():
    ses = create_mailer(Settings(email_provider="ses", aws_region="eu-west-1"))
    assert isinstance(ses, SESClient)
    cf = create_mailer(
        Settings(email_provider="cloudflare", cloudflare_account_id="acct1", cloudflare_events_queue_id="q")
    )
    assert isinstance(cf, CloudflareEmail)
    assert cf.account_id == "acct1"
    assert cf.events_queue_id == "q"


def test_settings_validate_provider():
    ok = Settings(api_key="k", base_url="https://x", from_email="a@x.com")
    assert ok.missing_required() == []
    bad = Settings(api_key="k", base_url="https://x", from_email="a@x.com", email_provider="mailgun")
    assert any("MUSELETTER_EMAIL_PROVIDER" in p for p in bad.missing_required())
    no_acct = Settings(api_key="k", base_url="https://x", from_email="a@x.com", email_provider="cloudflare")
    assert any("CLOUDFLARE_ACCOUNT_ID" in p for p in no_acct.missing_required())
    cf = Settings(
        api_key="k",
        base_url="https://x",
        from_email="a@x.com",
        email_provider="cloudflare",
        cloudflare_account_id="acct1",
    )
    assert cf.missing_required() == []
