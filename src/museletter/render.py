import html as html_mod
import os
import re
from importlib.resources import files
from pathlib import Path
from string import Template
from typing import NamedTuple

import html2text
import mistune

_markdown = mistune.create_markdown(plugins=["strikethrough", "table", "url"], escape=False)

# {{name}}, {{first_name}}, {{email}}, with optional fallback: {{first_name|there}}
_TOKEN_RE = re.compile(r"\{\{\s*(first_name|name|email)\s*(?:\|([^}]*?)\s*)?\}\}")


def template_source(name: str) -> str:
    """Read a template, preferring an operator override in MUSELETTER_TEMPLATE_DIR
    over the packaged default. Eject the defaults with `museletter preview --eject`."""
    override = os.environ.get("MUSELETTER_TEMPLATE_DIR")
    if override:
        path = Path(override) / name
        if path.is_file():
            return path.read_text(encoding="utf-8")
    return (files("museletter") / "templates" / name).read_text(encoding="utf-8")


def load_template(name: str) -> Template:
    """Templates use $var placeholders (string.Template), so literal CSS/HTML
    braces need no escaping."""
    return Template(template_source(name))


# The email layout is a single boring, battle-tested table column with inline
# styles (what email clients actually support), kept under Gmail's 102KB clip.
_EMAIL_TEMPLATE = load_template("email.html")
# Transactional emails (confirmation) use a sans-serif variant with a button.
_SYSTEM_TEMPLATE = load_template("email-system.html")


def _footer_html(list_name: str, postal_address: str, unsubscribe_url: str = "") -> str:
    # Identity and the unsubscribe link share one line; the attribution sits on
    # its own line below, set apart with space above so it reads as separate.
    line = [html_mod.escape(p) for p in (list_name, postal_address) if p]
    if unsubscribe_url:
        line.append(f'<a href="{html_mod.escape(unsubscribe_url)}" style="color:#74747F;">Unsubscribe</a>')
    sent = (
        'Sent with <a href="https://github.com/sanketsaurav/museletter" style="color:#74747F;">Museletter</a>'
    )
    top = " · ".join(line)
    return f'{top}<div style="padding-top:12px;">{sent}</div>' if top else sent


def _footer_text(list_name: str, postal_address: str, unsubscribe_url: str = "") -> list[str]:
    lines = []
    identity = " · ".join(p for p in (list_name, postal_address) if p)
    if identity:
        lines.append(identity)
    if unsubscribe_url:
        lines.append(f"Unsubscribe: {unsubscribe_url}")
    if lines:
        lines.append("")  # blank line so the attribution reads as separate
    lines.append("Sent with Museletter")
    return lines


def personalize(text: str, name: str, email: str, escape: bool = False) -> str:
    parts = name.split()
    values = {"name": name, "first_name": parts[0] if parts else "", "email": email}

    def sub(m: re.Match) -> str:
        value = values.get(m.group(1), "") or (m.group(2) or "")
        return html_mod.escape(value) if escape else value

    return _TOKEN_RE.sub(sub, text)


def markdown_to_html(markdown: str) -> str:
    html = _markdown(markdown)
    assert isinstance(html, str)  # the HTML renderer always yields a string
    return html


def markdown_to_text(markdown: str) -> str:
    converter = html2text.HTML2Text()
    converter.body_width = 0
    converter.ignore_images = True
    converter.inline_links = True
    return converter.handle(markdown_to_html(markdown)).strip()


class CampaignBody(NamedTuple):
    """Markdown rendered once per campaign. Personalization tokens ({{name}})
    survive rendering as literal text and are substituted per recipient, so the
    Markdown parser runs once per campaign instead of once per recipient."""

    subject: str
    base_html: str
    base_text: str


def render_campaign(subject: str, markdown: str) -> CampaignBody:
    return CampaignBody(subject, markdown_to_html(markdown), markdown_to_text(markdown))


def personalize_email(
    body: CampaignBody,
    *,
    name: str = "",
    email: str = "",
    unsubscribe_url: str = "",
    list_name: str = "",
    postal_address: str = "",
) -> tuple[str, str, str]:
    """Personalize a pre-rendered campaign for one recipient. Returns (subject, html, text)."""
    subject = personalize(body.subject, name, email)
    content_html = personalize(body.base_html, name, email, escape=True)
    content_text = personalize(body.base_text, name, email)

    html = _EMAIL_TEMPLATE.substitute(
        subject=html_mod.escape(subject),
        header=html_mod.escape(list_name) if list_name else "Newsletter",
        content=content_html,
        footer=_footer_html(list_name, postal_address, unsubscribe_url),
    )
    text = content_text + "\n\n" + "\n".join(_footer_text(list_name, postal_address, unsubscribe_url)) + "\n"
    return subject, html, text


_BODY_STYLE = "margin:0 0 16px;font-size:16px;line-height:1.6;color:#3E3E45;"
_BTN_STYLE = (
    "display:inline-block;background:#0F7A6B;color:#FFFFFF;font-size:16px;font-weight:600;"
    "padding:13px 24px;border-radius:8px;text-decoration:none;margin-top:8px;"
)


def render_confirmation(
    *, list_name: str, confirm_url: str, postal_address: str = ""
) -> tuple[str, str, str]:
    """Render the double opt-in confirmation email: sans-serif, with the confirm
    button placed after the message. A transactional email, not an issue."""
    ln = html_mod.escape(list_name)
    subject = f"Confirm your subscription to {list_name}"
    content = (
        f'<h1 class="ml-h1" style="margin:0 0 16px;font-size:26px;font-weight:600;letter-spacing:-0.02em;'
        f'line-height:1.2;color:#1A1A1E;">Confirm your subscription</h1>'
        f'<p class="ml-body" style="{_BODY_STYLE}">Someone (hopefully you) asked to subscribe this address to '
        f'<strong style="color:#1A1A1E;">{ln}</strong>. Confirm below and the next issue lands in your inbox.</p>'
        f'<p class="ml-body" style="{_BODY_STYLE}">If this was not you, you can safely ignore this email. '
        f"Nothing happens until you confirm.</p>"
        f'<a class="ml-btn" href="{html_mod.escape(confirm_url)}" style="{_BTN_STYLE}">Confirm subscription</a>'
    )
    html = _SYSTEM_TEMPLATE.substitute(
        subject=html_mod.escape(subject),
        header=ln,
        content=content,
        footer=_footer_html(list_name, postal_address),
    )
    text = (
        f"Confirm your subscription\n\n"
        f"Someone (hopefully you) asked to subscribe this address to {list_name}. "
        f"Confirm here and the next issue lands in your inbox:\n{confirm_url}\n\n"
        f"If this was not you, you can safely ignore this email.\n\n"
        + "\n".join(_footer_text(list_name, postal_address))
        + "\n"
    )
    return subject, html, text


def build_email(
    subject: str,
    markdown: str,
    *,
    name: str = "",
    email: str = "",
    unsubscribe_url: str = "",
    list_name: str = "",
    postal_address: str = "",
) -> tuple[str, str, str]:
    """Render and personalize a campaign for a single recipient. Convenience
    wrapper for one-off sends (confirmation, test, preview); the bulk send loop
    calls render_campaign() once and personalize_email() per recipient."""
    return personalize_email(
        render_campaign(subject, markdown),
        name=name,
        email=email,
        unsubscribe_url=unsubscribe_url,
        list_name=list_name,
        postal_address=postal_address,
    )
