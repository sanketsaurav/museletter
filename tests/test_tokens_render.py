from museletter.render import build_email, personalize
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


def test_personalize_fallbacks_and_escaping():
    assert personalize("Hi {{name|there}},", "", "a@b.c") == "Hi there,"
    assert personalize("Hi {{name|there}},", "Ada", "a@b.c") == "Hi Ada,"
    assert personalize("{{email}}", "", "a@b.c") == "a@b.c"
    assert personalize("Hi {{name}}", "<b>x</b>", "a@b.c", escape=True) == "Hi &lt;b&gt;x&lt;/b&gt;"


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
