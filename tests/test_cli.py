import json

import httpx
import pytest
from typer.testing import CliRunner

import museletter.cli as cli_mod
from museletter import clientconf
from museletter.cli import app as cli_app

runner = CliRunner()


@pytest.fixture(autouse=True)
def isolate_config(monkeypatch, tmp_path):
    """Point client config at a temp file and clear env overrides for every test."""
    monkeypatch.setattr(clientconf, "CONFIG_PATH", tmp_path / "config.toml")
    for var in ("MUSELETTER_URL", "MUSELETTER_API_KEY", "MUSELETTER_PROFILE"):
        monkeypatch.delenv(var, raising=False)
    # Default: a saved profile so command tests resolve a server.
    clientconf.save_profile("default", "http://t.local", "testkey")


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
    monkeypatch.setattr(clientconf, "CONFIG_PATH", tmp_path / "missing.toml")
    result = runner.invoke(cli_app, ["health"])
    assert result.exit_code == 1
    assert "no server configured" in result.output


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


def test_subs_resolve_percent_encodes_plus_addressed_email(api):
    # A '+' in the email must reach the server as %2B, not a literal '+'
    # (which Starlette decodes to a space), or the lookup silently misses.
    captured = {}

    def route(request: httpx.Request) -> httpx.Response:
        captured["query"] = request.url.query.decode()
        return httpx.Response(200, json={"subscribers": [{"id": "sub_9", "email": "user+tag@x.com"}]})

    api[("GET", "/v1/lists/default/subscribers")] = route
    api[("DELETE", "/v1/subscribers/sub_9")] = {}
    result = runner.invoke(cli_app, ["subs", "rm", "user+tag@x.com"])
    assert result.exit_code == 0
    assert "q=user%2Btag%40x.com" in captured["query"]


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


# ---------- connect / status / print-token ----------


def _mock_server(monkeypatch, handler):
    """Force the connect/status ad-hoc httpx clients onto a MockTransport."""
    transport = httpx.MockTransport(handler)
    real = httpx.Client  # capture before patching to avoid recursing into the factory

    def factory(*, base_url="", **_):
        return real(base_url=base_url, transport=transport)

    monkeypatch.setattr(cli_mod.httpx, "Client", factory)


def test_connect_with_token_saves_profile(monkeypatch):
    def handler(request):
        if request.url.path == "/health":
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(200, json={"lists": []})

    _mock_server(monkeypatch, handler)
    token = clientconf.encode_token("https://news.example.com", "the-key")
    result = runner.invoke(cli_app, ["connect", token])
    assert result.exit_code == 0, result.output
    url, key, _ = clientconf.resolve()
    assert url == "https://news.example.com" and key == "the-key"


def test_connect_rejects_bad_key(monkeypatch):
    def handler(request):
        if request.url.path == "/health":
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(401, json={"detail": "invalid or missing API key"})

    _mock_server(monkeypatch, handler)
    result = runner.invoke(cli_app, ["connect", "--url", "https://x.example.com", "--api-key", "wrong"])
    assert result.exit_code == 1
    assert "API key was rejected" in result.output


def test_connect_rejects_malformed_token():
    result = runner.invoke(cli_app, ["connect", "ml_not-valid"])
    assert result.exit_code == 1


def test_status_reports_reachable_and_authed(monkeypatch):
    clientconf.save_profile("default", "https://news.example.com", "k")

    def handler(request):
        if request.url.path == "/health":
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(200, json={"lists": [{"active_subscribers": 5}, {"active_subscribers": 3}]})

    _mock_server(monkeypatch, handler)
    result = runner.invoke(cli_app, ["--json", "status"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["reachable"] and data["authenticated"]
    assert data["active_subscribers"] == 8


def test_print_token_roundtrips(monkeypatch):
    monkeypatch.setenv("MUSELETTER_BASE_URL", "https://news.example.com")
    monkeypatch.setenv("MUSELETTER_API_KEY", "abc123")
    result = runner.invoke(cli_app, ["print-token"])
    assert result.exit_code == 0
    url, key = clientconf.decode_token(result.output.strip())
    assert url == "https://news.example.com" and key == "abc123"


# ---------- skill install ----------


def test_docs_prints_readme():
    result = runner.invoke(cli_app, ["docs"])
    assert result.exit_code == 0
    assert "agent-first newsletter platform" in result.output
    assert "AWS SES setup" in result.output  # a section an agent needs
    assert "—" not in result.output and "–" not in result.output


# ---------- skill install ----------


def test_skill_install_copies_files(tmp_path):
    result = runner.invoke(cli_app, ["skill", "install", "--dir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    installed = tmp_path / "museletter"
    assert (installed / "SKILL.md").exists()
    assert (installed / "recipes" / "publish-new-issue.md").exists()

    # Refuses to clobber without --force, succeeds with it.
    assert runner.invoke(cli_app, ["skill", "install", "--dir", str(tmp_path)]).exit_code == 1
    assert runner.invoke(cli_app, ["skill", "install", "--dir", str(tmp_path), "--force"]).exit_code == 0


# ---------- init ----------


def test_init_non_interactive_writes_env_and_token(tmp_path):
    env_file = tmp_path / ".env"
    result = runner.invoke(
        cli_app,
        [
            "--json",
            "init",
            "--non-interactive",
            "--base-url",
            "https://news.example.com",
            "--from-email",
            "you@example.com",
            "--env-file",
            str(env_file),
        ],
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["env"]["MUSELETTER_BASE_URL"] == "https://news.example.com"
    assert len(data["env"]["MUSELETTER_API_KEY"]) >= 32
    url, key = clientconf.decode_token(data["connect_token"])
    assert url == "https://news.example.com" and key == data["env"]["MUSELETTER_API_KEY"]
    # --json must still write the env file (side effect), not just print JSON.
    assert env_file.read_text().count("MUSELETTER_API_KEY=") == 1
    assert data["env_file"] == str(env_file)


def test_init_requires_base_url_and_email():
    result = runner.invoke(cli_app, ["init", "--non-interactive", "--base-url", "https://x.example.com"])
    assert result.exit_code == 1
    assert "required" in result.output


def test_preview_writes_all_surfaces(tmp_path):
    result = runner.invoke(cli_app, ["preview", "--no-open", "--out", str(tmp_path)])
    assert result.exit_code == 0, result.output
    for name in (
        "index.html",
        "email-issue.html",
        "email-confirm.html",
        "page-subscribed.html",
        "page-invalid.html",
    ):
        assert (tmp_path / name).exists(), name
    # the email preview inlines the mark so it renders offline (no CDN dependency)
    assert "data:image/svg+xml" in (tmp_path / "email-issue.html").read_text()


def test_preview_forces_light_and_dark_variants(tmp_path):
    result = runner.invoke(cli_app, ["preview", "--no-open", "--out", str(tmp_path)])
    assert result.exit_code == 0, result.output
    light = (tmp_path / "email-issue.html").read_text()
    dark = (tmp_path / "email-issue.dark.html").read_text()
    # Each variant is forced (OS-independent): light drops the dark media block,
    # dark promotes it to always-on so it renders even under a light system theme.
    assert "prefers-color-scheme" not in light
    assert "prefers-color-scheme" not in dark
    assert "@media all" in dark
    # The gallery ships a light/dark toggle and a per-surface "open full page" link.
    index = (tmp_path / "index.html").read_text()
    assert '<button data-theme="dark">' in index
    assert 'class="open" data-base="email-issue"' in index


def test_preview_eject_copies_templates(tmp_path):
    result = runner.invoke(cli_app, ["preview", "--eject", str(tmp_path)])
    assert result.exit_code == 0
    for name in ("email.html", "email-system.html", "page.html"):
        assert (tmp_path / name).exists(), name


def test_lists_edit_renames(api):
    api[("PATCH", "/v1/lists/default")] = {"id": "list_1", "slug": "field-notes", "name": "Field Notes"}
    result = runner.invoke(
        cli_app, ["lists", "edit", "default", "--name", "Field Notes", "--slug", "field-notes"]
    )
    assert result.exit_code == 0
    assert "Field Notes" in result.output
    assert json.loads(api["calls"][-1].content) == {"name": "Field Notes", "slug": "field-notes"}
