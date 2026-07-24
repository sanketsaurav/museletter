import httpx
import pytest

from museletter.app import create_app
from museletter.config import Settings
from museletter.ses import SESError


class FakeSES:
    def __init__(self):
        self.sent = []
        self.fail_next: list = []

    async def send_email(
        self, to, subject, html, text, *, from_email, from_name="", headers=None, reply_to=""
    ):
        if self.fail_next:
            raise self.fail_next.pop(0)
        self.sent.append(
            {
                "to": to,
                "subject": subject,
                "html": html,
                "text": text,
                "from_email": from_email,
                "from_name": from_name,
                "headers": headers or {},
            }
        )
        return f"msg-{len(self.sent)}"

    async def get_account(self):
        return {
            "SendingEnabled": True,
            "ProductionAccessEnabled": True,
            "SendQuota": {"Max24HourSend": 50000, "MaxSendRate": 14, "SentLast24Hours": 100},
        }

    async def get_identity(self, identity):
        return {"VerifiedForSendingStatus": True, "DkimAttributes": {"Status": "SUCCESS"}}

    @staticmethod
    def has_credentials():
        return True


def make_settings(tmp_path) -> Settings:
    settings = Settings(
        api_key="testkey",
        db_path=str(tmp_path / "test.db"),
        base_url="http://test.local",
        from_email="news@test.local",
        from_name="Test News",
        postal_address="1 Test Street, Testville",
        opt_in="double",
        send_rate=10000,
    )
    settings.extra.update({"ses": FakeSES(), "disable_sender": True, "skip_sns_verify": True})
    return settings


AUTH = {"Authorization": "Bearer testkey"}


@pytest.fixture
async def app_client(tmp_path):
    app = create_app(make_settings(tmp_path))
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test.local") as client:
            yield app, client


async def add_subscriber(client, email, name="", tags=None, status="active"):
    resp = await client.post(
        "/v1/lists/default/subscribers",
        json={"email": email, "name": name, "tags": tags or [], "status": status},
        headers=AUTH,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def make_campaign(client, subject="Issue #1", body="Hello **world**", tag=None):
    payload = {"subject": subject, "body_markdown": body}
    if tag:
        payload["tag"] = tag
    resp = await client.post("/v1/lists/default/campaigns", json=payload, headers=AUTH)
    assert resp.status_code == 201, resp.text
    return resp.json()


__all__ = ["FakeSES", "SESError", "AUTH", "add_subscriber", "make_campaign", "make_settings"]
