import museletter.render as render_mod
from museletter.render import build_email, personalize, personalize_email, render_campaign
from museletter.tokens import make_token, verify_token

SECRET = "s3cret"


def test_token_roundtrip():
    for purpose in ("confirm", "unsubscribe"):
        token = make_token(SECRET, purpose, "sub_abc123")
        assert verify_token(SECRET, token, purpose) == "sub_abc123"


def test_token_rejects_wrong_purpose_and_tampering():
    token = make_token(SECRET, "unsubscribe", "sub_abc123")
    assert verify_token(SECRET, token, "confirm") is None
    assert verify_token(SECRET, token + "x", "unsubscribe") is None
    assert verify_token("othersecret", token, "unsubscribe") is None
    assert verify_token(SECRET, "garbage", "unsubscribe") is None
    assert verify_token(SECRET, "", "unsubscribe") is None


def test_token_with_non_ascii_signature_returns_none():
    # A malformed token whose signature part is non-ASCII must return None,
    # not raise TypeError from hmac.compare_digest (500 on a public endpoint).
    assert verify_token(SECRET, "YQ.café", "confirm") is None
    assert verify_token(SECRET, "YQ.\udce9", "unsubscribe") is None


def test_personalize_fallbacks_and_escaping():
    assert personalize("Hi {{name|there}},", "", "a@b.c") == "Hi there,"
    assert personalize("Hi {{name|there}},", "Ada", "a@b.c") == "Hi Ada,"
    assert personalize("{{email}}", "", "a@b.c") == "a@b.c"
    assert personalize("Hi {{name}}", "<b>x</b>", "a@b.c", escape=True) == "Hi &lt;b&gt;x&lt;/b&gt;"


def test_personalize_first_name():
    assert personalize("Hi {{first_name}},", "Ada Lovelace", "a@b.c") == "Hi Ada,"
    assert personalize("Hi {{first_name}},", "Ada", "a@b.c") == "Hi Ada,"  # single-word name
    assert personalize("Hi {{first_name|there}},", "", "a@b.c") == "Hi there,"  # empty -> fallback
    assert personalize("{{name}} = {{first_name}}", "Ada Lovelace", "a@b.c") == "Ada Lovelace = Ada"


def test_build_email_renders_markdown_footer_and_unsub():
    subject, html, text = build_email(
        "Hello {{name|friend}}",
        "Hi {{name|there}},\n\nThis is **bold** and [a link](https://example.com).",
        name="Ada",
        email="ada@example.com",
        unsubscribe_url="http://x/unsubscribe/tok",
        list_name="My Letter",
        postal_address="1 Main St",
    )
    assert subject == "Hello Ada"
    assert "<strong>bold</strong>" in html
    assert "Hi Ada," in html
    assert 'href="http://x/unsubscribe/tok"' in html
    assert "1 Main St" in html
    assert "Unsubscribe: http://x/unsubscribe/tok" in text
    assert "bold" in text and "https://example.com" in text


def test_render_campaign_parses_markdown_once_for_many_recipients(monkeypatch):
    calls = {"n": 0}
    real = render_mod._markdown

    def counting_markdown(md):
        calls["n"] += 1
        return real(md)

    monkeypatch.setattr(render_mod, "_markdown", counting_markdown)

    body = render_campaign("New post for {{name|there}}", "Hi {{name|there}}, **read on**.")
    after_render = calls["n"]
    assert after_render >= 1

    # Personalizing for many recipients must not re-parse the Markdown.
    for i in range(50):
        subject, html, text = personalize_email(body, name=f"R{i}", email=f"r{i}@x.com")
        assert f"R{i}" in html and f"R{i}" in subject
    assert calls["n"] == after_render, "Markdown must be parsed once, not per recipient"


def test_build_email_matches_split_render_path():
    # The convenience wrapper must produce byte-identical output to the two-step path.
    args = {
        "name": "Ada",
        "email": "a@x.com",
        "unsubscribe_url": "http://x/u",
        "list_name": "L",
        "postal_address": "P",
    }
    combined = build_email("Hi {{name}}", "Body {{name}}", **args)
    split = personalize_email(render_campaign("Hi {{name}}", "Body {{name}}"), **args)
    assert combined == split
