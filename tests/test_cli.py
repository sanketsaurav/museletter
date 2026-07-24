import json

import httpx
import pytest
from typer.testing import CliRunner

import museletter.cli as cli_mod
from museletter.cli import app as cli_app

runner = CliRunner()


@pytest.fixture
def api(monkeypatch):
    """Route table for the CLI's HTTP calls: {(method, path): dict | callable}."""
    routes: dict = {}
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        route = routes.get((request.method, request.url.path))
        if route is None:
            return httpx.Response(404, json={"detail": f"no route: {request.url.path}"})
        if callable(route):
            return route(request)
        return httpx.Response(200, json=route)

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        cli_mod, "_client", lambda: httpx.Client(transport=transport, base_url="http://t.local")
    )
    routes["calls"] = calls  # convenience handle for tests
    return routes


def test_unconfigured_cli_exits_with_guidance(monkeypatch, tmp_path):
    monkeypatch.delenv("MUSELETTER_URL", raising=False)
    monkeypatch.setattr(cli_mod, "CONFIG_PATH", tmp_path / "missing.toml")
    result = runner.invoke(cli_app, ["health"])
    assert result.exit_code == 1
    assert "no server configured" in result.output


def test_config_writes_file(monkeypatch, tmp_path):
    config_path = tmp_path / "config.toml"
    monkeypatch.setattr(cli_mod, "CONFIG_PATH", config_path)
    result = runner.invoke(cli_app, ["config", "--url", "http://x.local", "--api-key", "k123"])
    assert result.exit_code == 0
    assert cli_mod._load_file_config() == {"url": "http://x.local", "api_key": "k123"}


def test_lists_table_and_json(api):
    api[("GET", "/v1/lists")] = {
        "lists": [{"id": "list_1", "slug": "default", "name": "Newsletter", "active_subscribers": 7}]
    }
    human = runner.invoke(cli_app, ["lists", "list"])
    assert human.exit_code == 0
    assert "default" in human.output and "Newsletter" in human.output

    as_json = runner.invoke(cli_app, ["--json", "lists", "list"])
    assert json.loads(as_json.output)["lists"][0]["slug"] == "default"


def test_subs_add_posts_body(api):
    api[("POST", "/v1/lists/default/subscribers")] = {
        "id": "sub_1",
        "email": "a@x.com",
        "tags": ["vip"],
    }
    result = runner.invoke(cli_app, ["subs", "add", "a@x.com", "--name", "Ada", "--tag", "vip"])
    assert result.exit_code == 0
    assert "added a@x.com [sub_1]" in result.output
    body = json.loads(api["calls"][-1].content)
    assert body == {"email": "a@x.com", "name": "Ada", "tags": ["vip"]}


def test_subs_list_and_export(api):
    api[("GET", "/v1/lists/default/subscribers")] = {
        "subscribers": [{"id": "sub_1", "email": "a@x.com", "name": "", "status": "active", "tags": []}],
        "total": 1,
    }
    result = runner.invoke(cli_app, ["subs", "list"])
    assert "a@x.com" in result.output and "(1 total)" in result.output

    api[("GET", "/v1/lists/default/subscribers/export")] = lambda r: httpx.Response(
        200, text="email,name\na@x.com,\n", headers={"content-type": "text/csv"}
    )
    result = runner.invoke(cli_app, ["subs", "export"])
    assert result.exit_code == 0
    assert result.output.startswith("email,name")


def test_subs_import_reads_file(api, tmp_path):
    csv_file = tmp_path / "subs.csv"
    csv_file.write_text("email\na@x.com\n")
    api[("POST", "/v1/lists/default/subscribers/import")] = {
        "imported": 1,
        "skipped_existing": 0,
        "skipped_invalid": 0,
    }
    result = runner.invoke(cli_app, ["subs", "import", str(csv_file)])
    assert "imported 1" in result.output
    assert json.loads(api["calls"][-1].content) == {"csv": "email\na@x.com\n"}


def test_campaigns_create_from_stdin(api):
    api[("POST", "/v1/lists/default/campaigns")] = {"id": "cmp_1", "subject": "Hi"}
    result = runner.invoke(
        cli_app,
        ["campaigns", "create", "--subject", "Hi", "--file", "-", "--tag", "vip"],
        input="Hello **world**\n",
    )
    assert result.exit_code == 0
    assert 'created draft cmp_1: "Hi"' in result.output
    body = json.loads(api["calls"][-1].content)
    assert body == {"subject": "Hi", "body_markdown": "Hello **world**\n", "tag": "vip"}


def _send_route(sent: list):
    def route(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body.get("dry_run"):
            return httpx.Response(200, json={"dry_run": True, "recipient_count": 3, "sample": ["a@x.com"]})
        sent.append(request)
        return httpx.Response(200, json={"id": "cmp_1", "status": "sending", "recipient_count": 3})

    return route


def test_campaigns_send_dry_run_only(api):
    sent: list = []
    api[("POST", "/v1/campaigns/cmp_1/send")] = _send_route(sent)
    result = runner.invoke(cli_app, ["campaigns", "send", "cmp_1", "--dry-run"])
    assert "would send to 3 subscribers" in result.output
    assert sent == []


def test_campaigns_send_confirmation_declined(api):
    sent: list = []
    api[("POST", "/v1/campaigns/cmp_1/send")] = _send_route(sent)
    result = runner.invoke(cli_app, ["campaigns", "send", "cmp_1"], input="n\n")
    assert result.exit_code == 1
    assert sent == []


def test_campaigns_send_with_yes_sets_idempotency_key(api):
    sent: list = []
    api[("POST", "/v1/campaigns/cmp_1/send")] = _send_route(sent)
    result = runner.invoke(cli_app, ["campaigns", "send", "cmp_1", "--yes"])
    assert result.exit_code == 0
    assert "sending to 3 subscribers" in result.output
    assert len(sent) == 1
    assert sent[0].headers["Idempotency-Key"] == "send-cmp_1"
    assert json.loads(sent[0].content) == {"confirm": True, "skip_test": False}


def test_campaigns_stats_human_output(api):
    api[("GET", "/v1/campaigns/cmp_1/stats")] = {
        "id": "cmp_1",
        "status": "sent",
        "recipient_count": 3,
        "pending": 0,
        "sent": 3,
        "delivered": 2,
        "bounced": 1,
        "complained": 0,
        "failed": 0,
        "suppressed": 0,
        "started_at": "t",
        "completed_at": "t",
    }
    result = runner.invoke(cli_app, ["campaigns", "stats", "cmp_1"])
    assert "status: sent" in result.output
    assert "delivered: 2" in result.output


def test_campaigns_test_send(api):
    api[("POST", "/v1/campaigns/cmp_1/test")] = {"sent_to": "me@x.com", "ses_message_id": "m1"}
    result = runner.invoke(cli_app, ["campaigns", "test", "cmp_1", "--to", "me@x.com"])
    assert "test sent to me@x.com" in result.output


def test_doctor_exit_codes(api):
    api[("GET", "/v1/doctor")] = {
        "status": "warn",
        "checks": [{"name": "dmarc", "status": "warn", "detail": "no record"}],
    }
    ok = runner.invoke(cli_app, ["doctor"])
    assert ok.exit_code == 0
    assert "! [dmarc] no record" in ok.output

    api[("GET", "/v1/doctor")] = {
        "status": "fail",
        "checks": [{"name": "aws-credentials", "status": "fail", "detail": "missing"}],
    }
    fail = runner.invoke(cli_app, ["doctor"])
    assert fail.exit_code == 1
    assert "✗ [aws-credentials] missing" in fail.output


def test_http_error_reports_detail_and_exits(api):
    api[("GET", "/v1/campaigns/cmp_404")] = lambda r: httpx.Response(
        404, json={"detail": "campaign not found"}
    )
    result = runner.invoke(cli_app, ["campaigns", "show", "cmp_404"])
    assert result.exit_code == 1
    assert "error (404): campaign not found" in result.output


def test_suppressions_roundtrip(api):
    api[("GET", "/v1/suppressions")] = {
        "suppressions": [{"email": "b@x.com", "reason": "bounce", "created_at": "t"}],
        "total": 1,
    }
    api[("POST", "/v1/suppressions")] = {"email": "c@x.com", "reason": "manual"}
    api[("DELETE", "/v1/suppressions/c@x.com")] = {}
    assert "b@x.com" in runner.invoke(cli_app, ["suppressions", "list"]).output
    assert "suppressed c@x.com" in runner.invoke(cli_app, ["suppressions", "add", "c@x.com"]).output
    assert "removed c@x.com" in runner.invoke(cli_app, ["suppressions", "rm", "c@x.com"]).output


def test_serve_refuses_missing_config(monkeypatch):
    for var in ("MUSELETTER_API_KEY", "MUSELETTER_BASE_URL", "MUSELETTER_FROM_EMAIL"):
        monkeypatch.delenv(var, raising=False)
    result = runner.invoke(cli_app, ["serve"])
    assert result.exit_code == 1
    assert "MUSELETTER_API_KEY" in result.output
