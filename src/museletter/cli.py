import json
import os
import sys
from pathlib import Path
from urllib.parse import quote

import httpx
import typer

app = typer.Typer(help="museletter — a headless, agent-first newsletter platform", no_args_is_help=True)
lists_app = typer.Typer(help="Manage lists", no_args_is_help=True)
subs_app = typer.Typer(help="Manage subscribers", no_args_is_help=True)
tags_app = typer.Typer(help="Manage tags", no_args_is_help=True)
campaigns_app = typer.Typer(help="Manage campaigns", no_args_is_help=True)
suppressions_app = typer.Typer(help="Manage the suppression list", no_args_is_help=True)
app.add_typer(lists_app, name="lists")
app.add_typer(subs_app, name="subs")
app.add_typer(tags_app, name="tags")
app.add_typer(campaigns_app, name="campaigns")
app.add_typer(suppressions_app, name="suppressions")

STATE = {"json": False}
CONFIG_PATH = Path.home() / ".config" / "museletter" / "config.toml"


def _load_file_config() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    import tomllib

    try:
        return tomllib.loads(CONFIG_PATH.read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return {}


@app.callback()
def _global(
    json_output: bool = typer.Option(False, "--json", help="Print raw JSON responses"),
):
    STATE["json"] = json_output


def _client() -> httpx.Client:
    file_config = _load_file_config()
    url = os.environ.get("MUSELETTER_URL") or file_config.get("url", "")
    api_key = os.environ.get("MUSELETTER_API_KEY") or file_config.get("api_key", "")
    if not url:
        typer.echo(
            "no server configured: set MUSELETTER_URL and MUSELETTER_API_KEY, "
            "or run `museletter config --url ... --api-key ...`",
            err=True,
        )
        raise typer.Exit(1)
    return httpx.Client(base_url=url.rstrip("/"), headers={"Authorization": f"Bearer {api_key}"}, timeout=120)


def _request(method: str, path: str, body: dict | None = None, headers: dict | None = None) -> httpx.Response:
    with _client() as client:
        resp = client.request(method, path, json=body, headers=headers)
    if resp.status_code >= 400:
        try:
            detail = resp.json().get("detail", resp.text)
        except ValueError:
            detail = resp.text
        typer.echo(f"error ({resp.status_code}): {detail}", err=True)
        raise typer.Exit(1)
    return resp


def _call(method: str, path: str, body: dict | None = None, headers: dict | None = None) -> dict:
    resp = _request(method, path, body, headers)
    return resp.json() if resp.content else {}


def _emit(data, human: str | None = None) -> None:
    if STATE["json"] or human is None:
        typer.echo(json.dumps(data, indent=2))
    else:
        typer.echo(human)


def _table(rows: list[dict], columns: list[str]) -> str:
    if not rows:
        return "(none)"
    widths = {c: max(len(c), *(len(str(r.get(c, ""))) for r in rows)) for c in columns}
    header = "  ".join(c.upper().ljust(widths[c]) for c in columns)
    lines = ["  ".join(str(r.get(c, "")).ljust(widths[c]) for c in columns) for r in rows]
    return "\n".join([header, *lines])


@app.command()
def config(
    url: str = typer.Option(..., help="museletter server URL"),
    api_key: str = typer.Option(..., help="admin API key"),
):
    """Save server URL and API key to ~/.config/museletter/config.toml."""
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(f'url = "{url}"\napi_key = "{api_key}"\n')
    CONFIG_PATH.chmod(0o600)
    typer.echo(f"saved to {CONFIG_PATH}")


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", help="bind address"),
    port: int = typer.Option(8000, help="bind port"),
):
    """Run the museletter server."""
    from .app import create_app
    from .config import Settings

    settings = Settings.from_env()
    problems = settings.missing_required()
    if problems:
        typer.echo("cannot start; fix configuration first:", err=True)
        for p in problems:
            typer.echo(f"  - {p}", err=True)
        raise typer.Exit(1)

    import uvicorn

    uvicorn.run(create_app(settings), host=host, port=port, log_level="info")


@app.command()
def doctor():
    """Check DNS, SES account status, and configuration."""
    data = _call("GET", "/v1/doctor")
    if STATE["json"]:
        typer.echo(json.dumps(data, indent=2))
        raise typer.Exit(0 if data["status"] != "fail" else 1)
    icons = {"ok": "✓", "warn": "!", "fail": "✗"}
    for check in data["checks"]:
        typer.echo(f"{icons[check['status']]} [{check['name']}] {check['detail']}")
    typer.echo(f"\noverall: {data['status']}")
    raise typer.Exit(0 if data["status"] != "fail" else 1)


@app.command()
def health():
    """Check that the server is up."""
    _emit(_call("GET", "/health"))


# ---------- lists ----------


@lists_app.command("list")
def lists_list():
    """Show all lists."""
    data = _call("GET", "/v1/lists")
    _emit(data, _table(data["lists"], ["id", "slug", "name", "active_subscribers"]))


@lists_app.command("create")
def lists_create(name: str, slug: str = typer.Option(None)):
    body = {"name": name}
    if slug:
        body["slug"] = slug
    data = _call("POST", "/v1/lists", body)
    _emit(data, f"created list {data['id']} (slug: {data['slug']})")


@lists_app.command("show")
def lists_show(ref: str):
    _emit(_call("GET", f"/v1/lists/{ref}"))


@lists_app.command("rm")
def lists_rm(ref: str, yes: bool = typer.Option(False, "--yes", "-y")):
    if not yes and not typer.confirm(f"delete list '{ref}' and all its subscribers?"):
        raise typer.Exit(1)
    _call("DELETE", f"/v1/lists/{ref}")
    typer.echo(f"deleted {ref}")


# ---------- subscribers ----------


def _resolve_subscriber(list_ref: str, ref: str) -> str:
    if ref.startswith("sub_"):
        return ref
    data = _call("GET", f"/v1/lists/{list_ref}/subscribers?q={quote(ref, safe='')}&limit=5")
    matches = [s for s in data["subscribers"] if s["email"] == ref.lower().strip()]
    if not matches:
        typer.echo(f"no subscriber found for '{ref}' in list '{list_ref}'", err=True)
        raise typer.Exit(1)
    return matches[0]["id"]


@subs_app.command("add")
def subs_add(
    email: str,
    list_ref: str = typer.Option("default", "--list"),
    name: str = typer.Option("", "--name"),
    tag: list[str] = typer.Option([], "--tag"),
):
    data = _call("POST", f"/v1/lists/{list_ref}/subscribers", {"email": email, "name": name, "tags": tag})
    note = f" ({data['warning']})" if "warning" in data else ""
    _emit(data, f"added {data['email']} [{data['id']}]{note}")


@subs_app.command("list")
def subs_list(
    list_ref: str = typer.Option("default", "--list"),
    status: str = typer.Option(None),
    tag: str = typer.Option(None),
    q: str = typer.Option(None),
    limit: int = typer.Option(100),
    offset: int = typer.Option(0),
):
    params = {"limit": limit, "offset": offset}
    if status:
        params["status"] = status
    if tag:
        params["tag"] = tag
    if q:
        params["q"] = q
    query = "&".join(f"{k}={quote(str(v), safe='')}" for k, v in params.items())
    data = _call("GET", f"/v1/lists/{list_ref}/subscribers?{query}")
    rows = [{**s, "tags": ",".join(s["tags"])} for s in data["subscribers"]]
    human = _table(rows, ["id", "email", "name", "status", "tags"]) + f"\n({data['total']} total)"
    _emit(data, human)


@subs_app.command("rm")
def subs_rm(ref: str, list_ref: str = typer.Option("default", "--list")):
    sub_id = _resolve_subscriber(list_ref, ref)
    _call("DELETE", f"/v1/subscribers/{sub_id}")
    typer.echo(f"deleted {sub_id}")


@subs_app.command("tag")
def subs_tag(ref: str, tag: str, list_ref: str = typer.Option("default", "--list")):
    sub_id = _resolve_subscriber(list_ref, ref)
    data = _call("POST", f"/v1/subscribers/{sub_id}/tags", {"name": tag})
    _emit(data, f"tagged {data['email']}: {', '.join(data['tags'])}")


@subs_app.command("untag")
def subs_untag(ref: str, tag: str, list_ref: str = typer.Option("default", "--list")):
    sub_id = _resolve_subscriber(list_ref, ref)
    data = _call("DELETE", f"/v1/subscribers/{sub_id}/tags/{tag}")
    _emit(data, f"untagged {data['email']}: {', '.join(data['tags']) or '(no tags)'}")


@subs_app.command("import")
def subs_import(file: Path, list_ref: str = typer.Option("default", "--list")):
    """Import subscribers from a CSV file (email[,name,tags,status])."""
    csv_text = sys.stdin.read() if str(file) == "-" else file.read_text()
    data = _call("POST", f"/v1/lists/{list_ref}/subscribers/import", {"csv": csv_text})
    _emit(
        data,
        f"imported {data['imported']}, skipped {data['skipped_existing']} existing, "
        f"{data['skipped_invalid']} invalid",
    )


@subs_app.command("export")
def subs_export(list_ref: str = typer.Option("default", "--list")):
    """Export subscribers as CSV to stdout."""
    typer.echo(_request("GET", f"/v1/lists/{list_ref}/subscribers/export").text, nl=False)


# ---------- tags ----------


@tags_app.command("list")
def tags_list(list_ref: str = typer.Option("default", "--list")):
    data = _call("GET", f"/v1/lists/{list_ref}/tags")
    _emit(data, _table(data["tags"], ["id", "name", "subscriber_count"]))


@tags_app.command("create")
def tags_create(name: str, list_ref: str = typer.Option("default", "--list")):
    data = _call("POST", f"/v1/lists/{list_ref}/tags", {"name": name})
    _emit(data, f"created tag {data['id']} ({data['name']})")


# ---------- campaigns ----------


@campaigns_app.command("list")
def campaigns_list(list_ref: str = typer.Option(None, "--list"), status: str = typer.Option(None)):
    params = []
    if list_ref:
        params.append(f"list={list_ref}")
    if status:
        params.append(f"status={status}")
    query = ("?" + "&".join(params)) if params else ""
    data = _call("GET", f"/v1/campaigns{query}")
    _emit(data, _table(data["campaigns"], ["id", "subject", "status", "recipient_count", "created_at"]))


@campaigns_app.command("create")
def campaigns_create(
    subject: str = typer.Option(..., "--subject"),
    file: Path = typer.Option(..., "--file", help="markdown file, or - for stdin"),
    list_ref: str = typer.Option("default", "--list"),
    tag: str = typer.Option(None, "--tag", help="only send to subscribers with this tag"),
):
    markdown = sys.stdin.read() if str(file) == "-" else file.read_text()
    body = {"subject": subject, "body_markdown": markdown}
    if tag:
        body["tag"] = tag
    data = _call("POST", f"/v1/lists/{list_ref}/campaigns", body)
    _emit(data, f'created draft {data["id"]}: "{data["subject"]}"')


@campaigns_app.command("show")
def campaigns_show(campaign_id: str):
    _emit(_call("GET", f"/v1/campaigns/{campaign_id}"))


@campaigns_app.command("edit")
def campaigns_edit(
    campaign_id: str,
    subject: str = typer.Option(None, "--subject"),
    file: Path = typer.Option(None, "--file"),
    tag: str = typer.Option(None, "--tag"),
):
    body = {}
    if subject is not None:
        body["subject"] = subject
    if file is not None:
        body["body_markdown"] = sys.stdin.read() if str(file) == "-" else file.read_text()
    if tag is not None:
        body["tag"] = tag
    if not body:
        typer.echo("nothing to update", err=True)
        raise typer.Exit(1)
    data = _call("PATCH", f"/v1/campaigns/{campaign_id}", body)
    _emit(data, f"updated {data['id']} (test send required again before sending)")


@campaigns_app.command("preview")
def campaigns_preview(campaign_id: str, html_out: Path = typer.Option(None, "--html")):
    data = _call("GET", f"/v1/campaigns/{campaign_id}/preview")
    if html_out:
        html_out.write_text(data["html"])
        typer.echo(f"wrote {html_out}")
    else:
        _emit(data, f"subject: {data['subject']}\n\n{data['text']}")


@campaigns_app.command("test")
def campaigns_test(campaign_id: str, to: str = typer.Option(..., "--to")):
    data = _call("POST", f"/v1/campaigns/{campaign_id}/test", {"to": to})
    _emit(data, f"test sent to {data['sent_to']}")


@campaigns_app.command("send")
def campaigns_send(
    campaign_id: str,
    dry_run: bool = typer.Option(False, "--dry-run"),
    yes: bool = typer.Option(False, "--yes", "-y"),
    skip_test: bool = typer.Option(False, "--skip-test"),
):
    """Send a campaign to its audience (asks for confirmation)."""
    preview = _call("POST", f"/v1/campaigns/{campaign_id}/send", {"dry_run": True})
    if dry_run:
        _emit(
            preview,
            f"would send to {preview['recipient_count']} subscribers\nsample: {', '.join(preview['sample'])}",
        )
        return
    if not yes and not typer.confirm(f"send to {preview['recipient_count']} subscribers?"):
        raise typer.Exit(1)
    data = _call(
        "POST",
        f"/v1/campaigns/{campaign_id}/send",
        {"confirm": True, "skip_test": skip_test},
        headers={"Idempotency-Key": f"send-{campaign_id}"},
    )
    _emit(
        data,
        f"sending to {data['recipient_count']} subscribers; check `museletter campaigns stats {campaign_id}`",
    )


@campaigns_app.command("stats")
def campaigns_stats(campaign_id: str):
    data = _call("GET", f"/v1/campaigns/{campaign_id}/stats")
    human = "\n".join(
        f"{k}: {data[k]}"
        for k in (
            "status",
            "recipient_count",
            "pending",
            "sent",
            "delivered",
            "bounced",
            "complained",
            "failed",
            "suppressed",
        )
    )
    _emit(data, human)


@campaigns_app.command("rm")
def campaigns_rm(campaign_id: str, yes: bool = typer.Option(False, "--yes", "-y")):
    if not yes and not typer.confirm(f"delete campaign {campaign_id}?"):
        raise typer.Exit(1)
    _call("DELETE", f"/v1/campaigns/{campaign_id}")
    typer.echo(f"deleted {campaign_id}")


# ---------- suppressions ----------


@suppressions_app.command("list")
def suppressions_list():
    data = _call("GET", "/v1/suppressions")
    _emit(
        data, _table(data["suppressions"], ["email", "reason", "created_at"]) + f"\n({data['total']} total)"
    )


@suppressions_app.command("add")
def suppressions_add(email: str):
    data = _call("POST", "/v1/suppressions", {"email": email})
    _emit(data, f"suppressed {data['email']}")


@suppressions_app.command("rm")
def suppressions_rm(email: str):
    _call("DELETE", f"/v1/suppressions/{email}")
    typer.echo(f"removed {email} from suppressions")


def main():
    app()


if __name__ == "__main__":
    main()
