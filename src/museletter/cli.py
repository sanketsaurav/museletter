import json
import sys
from pathlib import Path
from urllib.parse import quote

import httpx
import typer

from . import clientconf

app = typer.Typer(help="Museletter: a headless, agent-first newsletter platform", no_args_is_help=True)
lists_app = typer.Typer(help="Manage lists", no_args_is_help=True)
subs_app = typer.Typer(help="Manage subscribers", no_args_is_help=True)
tags_app = typer.Typer(help="Manage tags", no_args_is_help=True)
campaigns_app = typer.Typer(help="Manage campaigns", no_args_is_help=True)
suppressions_app = typer.Typer(help="Manage the suppression list", no_args_is_help=True)
skill_app = typer.Typer(help="Install the museletter agent skill", no_args_is_help=True)
service_app = typer.Typer(help="Run Museletter as a background service", no_args_is_help=True)
app.add_typer(lists_app, name="lists")
app.add_typer(subs_app, name="subs")
app.add_typer(tags_app, name="tags")
app.add_typer(campaigns_app, name="campaigns")
app.add_typer(suppressions_app, name="suppressions")
app.add_typer(skill_app, name="skill")
app.add_typer(service_app, name="service")

STATE: dict = {"json": False, "profile": None}


@app.callback()
def _global(
    json_output: bool = typer.Option(False, "--json", help="Print raw JSON responses"),
    profile: str = typer.Option(None, "--profile", "-p", help="Server profile to use"),
):
    STATE["json"] = json_output
    STATE["profile"] = profile


def _resolve():
    try:
        return clientconf.resolve(STATE["profile"])
    except clientconf.ConfigError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc


def _client() -> httpx.Client:
    url, api_key, _ = _resolve()
    return httpx.Client(base_url=url, headers={"Authorization": f"Bearer {api_key}"}, timeout=120)


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
def connect(
    token: str = typer.Argument(None, help="connect token (ml_...) from `museletter print-token`"),
    url: str = typer.Option(None, help="server URL (instead of a token)"),
    api_key: str = typer.Option(None, help="admin API key (with --url)"),
    name: str = typer.Option("default", "--name", help="profile name to save under"),
):
    """Connect this machine to a Museletter server and save it as a profile."""
    if token:
        try:
            url, api_key = clientconf.decode_token(token)
        except clientconf.ConfigError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(1) from exc
    elif not url or not api_key:
        typer.echo("provide a connect token, or both --url and --api-key", err=True)
        raise typer.Exit(1)

    # Verify reachability and auth before saving, so a bad token fails loudly here.
    try:
        with httpx.Client(base_url=url.rstrip("/"), timeout=15) as client:
            health = client.get("/health")
            health.raise_for_status()
            authed = client.get("/v1/lists", headers={"Authorization": f"Bearer {api_key}"})
    except httpx.HTTPError as exc:
        typer.echo(f"could not reach a Museletter server at {url}: {exc}", err=True)
        raise typer.Exit(1) from exc
    if authed.status_code == 401:
        typer.echo(f"reached {url} but the API key was rejected", err=True)
        raise typer.Exit(1)
    authed.raise_for_status()

    clientconf.save_profile(name, url, api_key)
    typer.echo(f"connected to {url} (profile '{name}', saved to {clientconf.CONFIG_PATH})")


@app.command("print-token")
def print_token():
    """Print a connect token for this server's configured URL + API key (run on the server)."""
    from .config import Settings

    settings = Settings.from_env()
    if not settings.base_url or not settings.api_key:
        typer.echo(
            "MUSELETTER_BASE_URL and MUSELETTER_API_KEY must be set to print a connect token",
            err=True,
        )
        raise typer.Exit(1)
    typer.echo(clientconf.encode_token(settings.base_url, settings.api_key))


@app.command()
def status():
    """Show which server this machine is pointed at and whether it's reachable."""
    try:
        url, api_key, source = clientconf.resolve(STATE["profile"])
    except clientconf.ConfigError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc

    info: dict = {"url": url, "source": source, "reachable": False, "authenticated": False}
    try:
        with httpx.Client(base_url=url, timeout=15) as client:
            info["reachable"] = client.get("/health").status_code == 200
            if info["reachable"]:
                lists = client.get("/v1/lists", headers={"Authorization": f"Bearer {api_key}"})
                info["authenticated"] = lists.status_code == 200
                if info["authenticated"]:
                    data = lists.json()["lists"]
                    info["lists"] = len(data)
                    info["active_subscribers"] = sum(item.get("active_subscribers", 0) for item in data)
    except httpx.HTTPError as exc:
        info["error"] = str(exc)

    if STATE["json"]:
        typer.echo(json.dumps(info, indent=2))
    else:
        typer.echo(f"server:        {info['url']}  ({source})")
        typer.echo(f"reachable:     {'yes' if info['reachable'] else 'no'}")
        typer.echo(f"authenticated: {'yes' if info['authenticated'] else 'no'}")
        if info.get("authenticated"):
            typer.echo(f"lists:         {info['lists']}")
            typer.echo(f"active subs:   {info['active_subscribers']}")
    raise typer.Exit(0 if info["authenticated"] else 1)


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", help="bind address"),
    port: int = typer.Option(8000, help="bind port"),
):
    """Run the Museletter server."""
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


def _read_readme() -> str:
    from importlib.resources import files

    bundled = files("museletter") / "README.md"  # present in an installed wheel
    if bundled.is_file():
        return bundled.read_text(encoding="utf-8")
    repo_readme = Path(__file__).resolve().parent.parent.parent / "README.md"  # editable checkout
    if repo_readme.is_file():
        return repo_readme.read_text(encoding="utf-8")
    return "README not found; see https://github.com/sanketsaurav/museletter"


@app.command()
def docs():
    """Print the full Museletter manual (the README), for humans and agents alike."""
    text = _read_readme()
    if sys.stdout.isatty():
        import pydoc

        pydoc.pager(text)
    else:
        typer.echo(text)


_TEMPLATE_FILES = ["email.html", "email-system.html", "page.html"]

_PREVIEW_INDEX = """<!doctype html><html lang="en" data-theme="light"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Museletter surfaces</title>
<style>
:root{--bg:#f5f5f7;--fg:#1a1a1e;--sub:#6b6b74;--h2:#8a8a92;--frame:#e1e1e6;--card:#fff}
html[data-theme="dark"]{--bg:#0b0b0d;--fg:#f5f5f7;--sub:#9b9ba6;--h2:#9b9ba6;--frame:#2e2e35;--card:#111114}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:var(--bg);color:var(--fg);margin:0 auto;max-width:1000px;padding:0 40px 48px}
.bar{position:sticky;top:0;z-index:5;display:flex;align-items:center;justify-content:space-between;gap:16px;background:var(--bg);padding:20px 0 14px;border-bottom:1px solid var(--frame)}
h1{font-size:20px;margin:0}.sub{color:var(--sub);margin:16px 0 0;font-size:14px}
h2{font-size:13px;color:var(--h2);margin:32px 0 10px;font-weight:600}
.frame{border:1px solid var(--frame);border-radius:10px;overflow:hidden;background:var(--card)}
iframe{width:100%;border:0;display:block;background:var(--card)}.email iframe{height:640px}.page iframe{height:430px}
.seg{display:inline-flex;flex:none;border:1px solid var(--frame);border-radius:8px;overflow:hidden}
.seg button{appearance:none;-webkit-appearance:none;border:0;background:transparent;color:var(--sub);font:inherit;font-size:13px;padding:7px 15px;cursor:pointer}
.seg button.on{background:var(--fg);color:var(--bg)}</style>
</head><body>
<div class="bar"><h1>Museletter public surfaces</h1>
<div class="seg"><button data-theme="light">Light</button><button data-theme="dark">Dark</button></div></div>
<p class="sub">Rendered from the current templates, forced to each mode so you can review both regardless of your system theme.</p>
$rows
<script>
(function(){
  var root=document.documentElement;
  var btns=document.querySelectorAll('.seg button');
  var frames=document.querySelectorAll('iframe[data-base]');
  function apply(theme){
    root.setAttribute('data-theme',theme);
    btns.forEach(function(b){b.classList.toggle('on',b.getAttribute('data-theme')===theme)});
    frames.forEach(function(f){
      var src=f.getAttribute('data-base')+(theme==='dark'?'.dark.html':'.html');
      if(f.getAttribute('src')!==src)f.setAttribute('src',src);
    });
  }
  btns.forEach(function(b){b.addEventListener('click',function(){apply(b.getAttribute('data-theme'))})});
  var dark=window.matchMedia&&window.matchMedia('(prefers-color-scheme: dark)').matches;
  apply(dark?'dark':'light');
})();
</script>
</body></html>"""


_DARK_MEDIA = "@media (prefers-color-scheme: dark)"


def _theme_variants(html: str) -> tuple[str, str]:
    """Split a rendered surface into OS-independent light and dark renderings so
    the preview can force either mode. Dark promotes the prefers-color-scheme
    block to always-on (@media all); light removes it. The email templates guard
    their dark rules with !important, so forcing the block overrides the inline
    light styles."""
    dark = html.replace(_DARK_MEDIA, "@media all")
    light = html
    while (start := light.find(_DARK_MEDIA)) != -1:
        brace = light.find("{", start)
        if brace == -1:
            break
        depth, end = 0, brace
        while end < len(light):
            if light[end] == "{":
                depth += 1
            elif light[end] == "}":
                depth -= 1
                if depth == 0:
                    break
            end += 1
        light = light[:start] + light[end + 1 :]
    return light, dark


@app.command()
def preview(
    out: str = typer.Option(None, "--out", help="directory to write previews to (default: a temp dir)"),
    open_browser: bool = typer.Option(True, "--open/--no-open", help="open the previews in a browser"),
    eject: str = typer.Option(None, "--eject", help="copy the templates to this dir to customize them"),
):
    """Preview every public surface (emails and pages), or eject the templates to customize."""
    import tempfile
    import webbrowser
    from pathlib import Path
    from urllib.parse import quote

    from .render import template_source

    if eject:
        dest = Path(eject)
        dest.mkdir(parents=True, exist_ok=True)
        for f in _TEMPLATE_FILES:
            (dest / f).write_text(template_source(f), encoding="utf-8")
        typer.echo(f"ejected {len(_TEMPLATE_FILES)} templates to {dest}")
        typer.echo(f"edit them, then run the server (or preview) with MUSELETTER_TEMPLATE_DIR={dest}")
        return

    from .api.public import _page
    from .render import build_email, render_confirmation

    def page_html(*a, **k) -> str:
        # HTMLResponse.body is typed bytes | memoryview; coerce before decode.
        return bytes(_page(*a, **k).body).decode()

    def mark(fill: str) -> str:
        svg = (
            f"<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32' fill='{fill}'>"
            "<rect x='2.40' y='13.25' width='27.20' height='5.5' rx='2.75' transform='rotate(0 16 16)'/>"
            "<rect x='2.40' y='13.25' width='27.20' height='5.5' rx='2.75' transform='rotate(60 16 16)'/>"
            "<rect x='2.40' y='13.25' width='27.20' height='5.5' rx='2.75' transform='rotate(120 16 16)'/></svg>"
        )
        return "data:image/svg+xml," + quote(svg)

    def offline(html: str) -> str:
        base = "https://cdn.jsdelivr.net/gh/sanketsaurav/museletter@master/assets/email"
        return html.replace(f"{base}/mark-light.png", mark("#0F7A6B")).replace(
            f"{base}/mark-dark.png", mark("#5FC9B6")
        )

    ln, addr = "Your Newsletter", "123 Main St, Your City"
    issue = (
        "## A sample issue\n\nHi {{first_name|there}}, this is what an issue looks like: a "
        "[link](https://example.com), some *emphasis*, and a short list.\n\n"
        "- the first point\n- the second point\n\n> A pull quote, to show the blockquote style.\n\n"
        "Inline `code` renders like this. That is the whole system."
    )
    _, issue_html, _ = build_email(
        "A sample issue",
        issue,
        name="Ada Lovelace",
        email="ada@example.com",
        unsubscribe_url="#",
        list_name=ln,
        postal_address=addr,
    )
    _, confirm_html, _ = render_confirmation(list_name=ln, confirm_url="#", postal_address=addr)
    unsub_action = '<form method="post" action="#"><button class="btn btn-danger" type="submit">Unsubscribe</button></form>'
    surfaces = [
        ("email-issue", "Campaign / issue email", offline(issue_html)),
        ("email-confirm", "Confirmation email", offline(confirm_html)),
        (
            "page-subscribed",
            "Page: subscribed",
            page_html(
                "You're subscribed",
                f"<p>Welcome to <strong>{ln}</strong>. Your first issue is on its way.</p>",
                list_name=ln,
            ),
        ),
        (
            "page-unsubscribe",
            "Page: unsubscribe confirm",
            page_html(
                "Unsubscribe?",
                f"<p>You will stop receiving emails from <strong>{ln}</strong>. This takes effect immediately.</p>",
                variant="gray",
                list_name=ln,
                action=unsub_action,
            ),
        ),
        (
            "page-unsubscribed",
            "Page: unsubscribed",
            page_html(
                "Unsubscribed",
                "<p>You won't receive these emails anymore. You can re-subscribe any time from the website.</p>",
                list_name=ln,
            ),
        ),
        (
            "page-closed",
            "Page: subscription closed",
            page_html(
                "Subscription closed",
                f"<p>This address is not subscribed to {ln}. Sign up again from the website if that is a mistake.</p>",
                variant="muted",
                list_name=ln,
            ),
        ),
        (
            "page-invalid",
            "Page: invalid link",
            page_html(
                "This link has gone stale",
                "<p>Confirmation links last 48 hours and work once. Sign up again from the website to get a fresh one.</p>",
                variant="muted",
                list_name="Museletter",
                code="link_invalid",
            ),
        ),
    ]
    out_dir = Path(out) if out else Path(tempfile.mkdtemp(prefix="museletter-preview-"))
    out_dir.mkdir(parents=True, exist_ok=True)
    for slug, _, html in surfaces:
        light, dark = _theme_variants(html)
        (out_dir / f"{slug}.html").write_text(light, encoding="utf-8")
        (out_dir / f"{slug}.dark.html").write_text(dark, encoding="utf-8")
    rows = "".join(
        f'<h2>{title}</h2><div class="frame {"email" if slug.startswith("email") else "page"}">'
        f'<iframe data-base="{slug}" title="{title}"></iframe></div>'
        for slug, title, _ in surfaces
    )
    index = out_dir / "index.html"
    index.write_text(_PREVIEW_INDEX.replace("$rows", rows), encoding="utf-8")
    typer.echo(f"wrote {len(surfaces)} surfaces (light + dark) to {out_dir}")
    if open_browser:
        webbrowser.open(index.as_uri())


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


@lists_app.command("edit")
def lists_edit(
    ref: str,
    name: str = typer.Option(None, "--name", help="new display name (the publication name)"),
    slug: str = typer.Option(None, "--slug", help="new URL slug"),
):
    """Rename a list or change its slug."""
    body = {}
    if name is not None:
        body["name"] = name
    if slug is not None:
        body["slug"] = slug
    if not body:
        typer.echo("nothing to update (pass --name and/or --slug)", err=True)
        raise typer.Exit(1)
    data = _call("PATCH", f"/v1/lists/{ref}", body)
    _emit(data, f"updated list {data['id']} (name: {data['name']}, slug: {data['slug']})")


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


# ---------- skill ----------


@skill_app.command("install")
def skill_install(
    project: bool = typer.Option(
        False, "--project", help="install into ./.claude/skills instead of ~/.claude"
    ),
    directory: str = typer.Option(None, "--dir", help="explicit skills directory to install into"),
    force: bool = typer.Option(False, "--force", help="overwrite an existing install"),
):
    """Install the bundled agent skill into a Claude Code skills directory."""
    import shutil
    from importlib.resources import as_file, files

    if directory:
        base = Path(directory)
    elif project:
        base = Path.cwd() / ".claude" / "skills"
    else:
        base = Path.home() / ".claude" / "skills"
    target = base / "museletter"

    if target.exists() and not force:
        typer.echo(f"{target} already exists; pass --force to overwrite", err=True)
        raise typer.Exit(1)

    source = files("museletter") / "skill"
    with as_file(source) as skill_dir:
        if target.exists():
            shutil.rmtree(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(skill_dir, target)
    typer.echo(f"installed skill to {target}")


# ---------- init ----------


@app.command()
def init(
    base_url: str = typer.Option(None, "--base-url", help="public URL of the server"),
    from_email: str = typer.Option(None, "--from-email", help="sender address"),
    from_name: str = typer.Option("", "--from-name", help="sender display name"),
    postal_address: str = typer.Option("", "--postal-address", help="footer postal address"),
    region: str = typer.Option("us-east-1", "--region", help="AWS region"),
    api_key: str = typer.Option(None, "--api-key", help="admin API key (generated if omitted)"),
    env_file: str = typer.Option(".env", "--env-file", help="path to write env to; '-' to print only"),
    non_interactive: bool = typer.Option(False, "--non-interactive", help="never prompt; use flags/defaults"),
):
    """Bootstrap a server: generate an API key, write an env file, print a connect token."""
    import secrets

    if not non_interactive:
        base_url = base_url or typer.prompt("Public base URL (e.g. https://news.example.com)")
        from_email = from_email or typer.prompt("Sender email (SES-verified)")
        from_name = from_name or typer.prompt("Sender name", default="")
        postal_address = postal_address or typer.prompt("Postal address (CAN-SPAM)", default="")
    if not base_url or not from_email:
        typer.echo("--base-url and --from-email are required", err=True)
        raise typer.Exit(1)
    api_key = api_key or secrets.token_hex(32)

    env = {
        "MUSELETTER_API_KEY": api_key,
        "MUSELETTER_BASE_URL": base_url.rstrip("/"),
        "MUSELETTER_FROM_EMAIL": from_email,
        "MUSELETTER_FROM_NAME": from_name,
        "MUSELETTER_POSTAL_ADDRESS": postal_address,
        "AWS_REGION": region,
    }
    env_text = "".join(f"{k}={v}\n" for k, v in env.items())
    token = clientconf.encode_token(base_url, api_key)

    # Write the env file in every mode (it's the command's side effect); only the
    # rendering differs between --json and human output.
    wrote_path = None
    if env_file != "-":
        path = Path(env_file)
        path.write_text(env_text)
        path.chmod(0o600)
        wrote_path = str(path)

    if STATE["json"]:
        typer.echo(json.dumps({"env": env, "connect_token": token, "env_file": wrote_path}))
        return

    if env_file == "-":
        typer.echo(env_text)
    else:
        typer.echo(f"wrote {wrote_path} (keep it secret)")
    typer.echo("\nStart the server with this env, then connect a client with:\n")
    typer.echo(f"  museletter connect {token}\n")
    typer.echo(
        "Set your AWS credentials (AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY) too, then run `museletter doctor`."
    )


# ---------- service ----------


@service_app.command("install")
def service_install(
    host: str = typer.Option("127.0.0.1", help="bind address"),
    port: int = typer.Option(8000, help="bind port"),
    env_file: str = typer.Option(".env", "--env-file", help="env file the service loads"),
    start: bool = typer.Option(True, "--start/--no-start", help="start the service after installing"),
):
    """Install Museletter as a launchd (macOS) or systemd (Linux) user service."""
    from . import service

    try:
        path = service.install(host=host, port=port, env_file=str(Path(env_file).resolve()), start=start)
    except service.ServiceError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"installed service unit at {path}" + (" and started it" if start else ""))


@service_app.command("uninstall")
def service_uninstall():
    """Stop and remove the Museletter user service."""
    from . import service

    try:
        service.uninstall()
    except service.ServiceError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    typer.echo("service removed")


@service_app.command("status")
def service_status():
    """Show whether the Museletter user service is installed and running."""
    from . import service

    typer.echo(service.status())


def main():
    app()


if __name__ == "__main__":
    main()
