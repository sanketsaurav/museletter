import json
from datetime import datetime
from typing import Any

import httpx
import pytest

import museletter.ses as ses_mod
from museletter.ses import SESClient, SESError, derive_signing_key, sign_request

CREDS = {"access": "AKIDEXAMPLE", "secret": "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY"}


def test_derive_signing_key_matches_aws_documentation_example():
    # The worked example from AWS's SigV4 "deriving the signing key" docs.
    key = derive_signing_key(CREDS["secret"], "20150830", "us-east-1", "iam")
    assert key.hex() == "c4afb1cc5771d871763a393e44b703571b55cc28424d1a5e86da6ed3c154a4b9"


@pytest.fixture
def frozen_clock(monkeypatch):
    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2015, 8, 30, 12, 36, 0, tzinfo=tz)

    monkeypatch.setattr(ses_mod, "datetime", FrozenDatetime)


def _sign(**overrides: Any):
    kwargs: dict[str, Any] = {
        "method": "POST",
        "url": "https://email.us-east-1.amazonaws.com/v2/email/outbound-emails",
        "region": "us-east-1",
        "payload": b'{"a":1}',
        "access_key": CREDS["access"],
        "secret_key": CREDS["secret"],
    }
    kwargs.update(overrides)
    return sign_request(**kwargs)


def test_sign_request_shape_and_determinism(frozen_clock):
    headers = _sign()
    assert headers["x-amz-date"] == "20150830T123600Z"
    auth = headers["authorization"]
    assert auth.startswith("AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/20150830/us-east-1/ses/aws4_request, ")
    assert "SignedHeaders=host;x-amz-date, " in auth
    signature = auth.rsplit("Signature=", 1)[1]
    assert len(signature) == 64 and int(signature, 16) is not None
    assert _sign() == headers, "same inputs must produce the same signature"


def test_sign_request_includes_session_token(frozen_clock):
    headers = _sign(session_token="TOKEN")
    assert headers["x-amz-security-token"] == "TOKEN"
    assert "SignedHeaders=host;x-amz-date;x-amz-security-token, " in headers["authorization"]


def test_signature_is_sensitive_to_every_input(frozen_clock):
    base = _sign()["authorization"]
    assert _sign(payload=b'{"a":2}')["authorization"] != base
    assert _sign(region="eu-west-1")["authorization"] != base
    assert _sign(secret_key="other")["authorization"] != base
    assert _sign(url="https://email.us-east-1.amazonaws.com/v2/email/account")["authorization"] != base
    assert _sign(method="GET")["authorization"] != base


def make_client(monkeypatch, responder, region="us-east-1", configuration_set=""):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", CREDS["access"])
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", CREDS["secret"])
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return responder(request)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return SESClient(region, configuration_set=configuration_set, http=http), requests


async def test_send_email_request_and_response(monkeypatch):
    client, requests = make_client(
        monkeypatch,
        lambda r: httpx.Response(200, json={"MessageId": "msg-42"}),
        configuration_set="museletter",
    )
    message_id = await client.send_email(
        "reader@example.com",
        "Hello",
        "<p>hi</p>",
        "hi",
        from_email="news@example.com",
        from_name='Sanket "The" Writer',
        headers={
            "List-Unsubscribe": "<https://x/u/t>",
            "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
        },
        reply_to="replies@example.com",
    )
    assert message_id == "msg-42"

    request = requests[0]
    assert request.method == "POST"
    assert request.url == "https://email.us-east-1.amazonaws.com/v2/email/outbound-emails"
    assert request.headers["content-type"] == "application/json"
    assert request.headers["authorization"].startswith("AWS4-HMAC-SHA256 ")
    assert request.headers["x-amz-date"]

    body = json.loads(request.content)
    assert body["FromEmailAddress"] == '"Sanket The Writer" <news@example.com>', "quotes stripped from name"
    assert body["Destination"] == {"ToAddresses": ["reader@example.com"]}
    assert body["ReplyToAddresses"] == ["replies@example.com"]
    assert body["ConfigurationSetName"] == "museletter"
    simple = body["Content"]["Simple"]
    assert simple["Subject"] == {"Data": "Hello", "Charset": "UTF-8"}
    assert {"Name": "List-Unsubscribe-Post", "Value": "List-Unsubscribe=One-Click"} in simple["Headers"]


async def test_send_email_without_display_name(monkeypatch):
    client, requests = make_client(monkeypatch, lambda r: httpx.Response(200, json={"MessageId": "m"}))
    await client.send_email("a@x.com", "s", "<p></p>", "t", from_email="news@example.com")
    body = json.loads(requests[0].content)
    assert body["FromEmailAddress"] == "news@example.com"
    assert "ConfigurationSetName" not in body
    assert "Headers" not in body["Content"]["Simple"]


async def test_missing_credentials(monkeypatch):
    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("AWS_SECRET_ACCESS_KEY", raising=False)
    client = SESClient("us-east-1")
    with pytest.raises(SESError) as exc:
        await client.send_email("a@x.com", "s", "h", "t", from_email="n@x.com")
    assert exc.value.code == "NoCredentials"
    assert not SESClient.has_credentials()


async def test_error_mapping_and_throttling(monkeypatch):
    client, _ = make_client(
        monkeypatch,
        lambda r: httpx.Response(
            429,
            json={"__type": "TooManyRequestsException", "message": "slow down"},
            headers={"x-amzn-ErrorType": "TooManyRequestsException:http://internal"},
        ),
    )
    with pytest.raises(SESError) as exc:
        await client.send_email("a@x.com", "s", "h", "t", from_email="n@x.com")
    assert exc.value.status == 429
    assert exc.value.code == "TooManyRequestsException"
    assert exc.value.throttled

    assert SESError(400, "Throttling", "x").throttled
    assert not SESError(400, "MessageRejected", "x").throttled


async def test_error_with_non_json_body(monkeypatch):
    client, _ = make_client(monkeypatch, lambda r: httpx.Response(500, text="<html>boom</html>"))
    with pytest.raises(SESError) as exc:
        await client.get_account()
    assert exc.value.status == 500
    assert "boom" in exc.value.message


async def test_get_account_and_identity(monkeypatch):
    def responder(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/email/account":
            return httpx.Response(200, json={"ProductionAccessEnabled": True})
        if request.url.path == "/v2/email/identities/example.com":
            return httpx.Response(200, json={"VerifiedForSendingStatus": True})
        if request.url.path == "/v2/email/identities/missing.com":
            return httpx.Response(404, json={"__type": "NotFoundException", "message": "nope"})
        return httpx.Response(500, json={"message": "unexpected"})

    client, requests = make_client(monkeypatch, responder)
    assert (await client.get_account())["ProductionAccessEnabled"] is True
    assert requests[0].method == "GET"
    assert (await client.get_identity("example.com"))["VerifiedForSendingStatus"] is True
    assert await client.get_identity("missing.com") is None
    with pytest.raises(SESError):
        await client.get_identity("error.com")
