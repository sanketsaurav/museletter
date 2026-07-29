import html as html_mod
import re
from importlib.resources import files
from string import Template
from typing import NamedTuple

import html2text
import mistune

_markdown = mistune.create_markdown(plugins=["strikethrough", "table", "url"], escape=False)

# {{name}}, {{first_name}}, {{email}}, with optional fallback: {{first_name|there}}
_TOKEN_RE = re.compile(r"\{\{\s*(first_name|name|email)\s*(?:\|([^}]*?)\s*)?\}\}")


def load_template(name: str) -> Template:
    """Templates ship inside the package (templates/*.html) and use $var
    placeholders (string.Template), so literal CSS/HTML braces need no escaping."""
    return Template((files("museletter") / "templates" / name).read_text(encoding="utf-8"))


# The email layout is a single boring, battle-tested table column with inline
# styles (what email clients actually support), kept under Gmail's 102KB clip.
_EMAIL_TEMPLATE = load_template("email.html")


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

    # Footer: identity line (list + postal address), then a rule-thin line of
    # actions. "Sent with Museletter" is the platform attribution.
    identity = " · ".join(html_mod.escape(p) for p in (list_name, postal_address) if p)
    actions = []
    if unsubscribe_url:
        actions.append(f'<a href="{html_mod.escape(unsubscribe_url)}" style="color:#74747F;">Unsubscribe</a>')
    actions.append(
        'Sent with <a href="https://github.com/sanketsaurav/museletter" style="color:#74747F;">Museletter</a>'
    )
    footer_html = (identity + "<br>" if identity else "") + " · ".join(actions)
    header_html = html_mod.escape(list_name) if list_name else "Newsletter"

    html = _EMAIL_TEMPLATE.substitute(
        subject=html_mod.escape(subject),
        header=header_html,
        content=content_html,
        footer=footer_html,
    )

    text_footer = [p for p in (list_name, postal_address) if p]
    if unsubscribe_url:
        text_footer.append(f"Unsubscribe: {unsubscribe_url}")
    text_footer.append("Sent with Museletter")
    text = content_text + "\n\n" + "\n".join(text_footer) + "\n"

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
