import html as html_mod
import re
from importlib.resources import files
from string import Template

import html2text
import mistune

_markdown = mistune.create_markdown(plugins=["strikethrough", "table", "url"], escape=False)

# {{name}}, {{email}}, with optional fallback: {{name|there}}
_TOKEN_RE = re.compile(r"\{\{\s*(name|email)\s*(?:\|([^}]*?)\s*)?\}\}")


def load_template(name: str) -> Template:
    """Templates ship inside the package (templates/*.html) and use $var
    placeholders — string.Template, so literal CSS/HTML braces need no escaping."""
    return Template((files("museletter") / "templates" / name).read_text(encoding="utf-8"))


# The email layout is a single boring, battle-tested table column with inline
# styles (what email clients actually support), kept under Gmail's 102KB clip.
_EMAIL_TEMPLATE = load_template("email.html")


def personalize(text: str, name: str, email: str, escape: bool = False) -> str:
    values = {"name": name, "email": email}

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
    """Render a campaign for one recipient. Returns (subject, html, text)."""
    subject = personalize(subject, name, email)
    content_html = personalize(markdown_to_html(markdown), name, email, escape=True)
    content_text = personalize(markdown_to_text(markdown), name, email)

    footer_parts = []
    if list_name:
        footer_parts.append(html_mod.escape(list_name))
    if postal_address:
        footer_parts.append(html_mod.escape(postal_address))
    if unsubscribe_url:
        footer_parts.append(
            f'<a href="{html_mod.escape(unsubscribe_url)}" style="color:#8a8a86;">Unsubscribe</a>'
        )
    footer_html = "<br>".join(footer_parts)

    html = _EMAIL_TEMPLATE.substitute(
        subject=html_mod.escape(subject), content=content_html, footer=footer_html
    )

    text_footer = ["—"]
    if list_name:
        text_footer.append(list_name)
    if postal_address:
        text_footer.append(postal_address)
    if unsubscribe_url:
        text_footer.append(f"Unsubscribe: {unsubscribe_url}")
    text = content_text + "\n\n" + "\n".join(text_footer) + "\n"

    return subject, html, text
