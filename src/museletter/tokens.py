import base64
import hashlib
import hmac

PURPOSES = ("confirm", "unsubscribe", "open")


def make_token(secret: str, purpose: str, subscriber_id: str) -> str:
    if purpose not in PURPOSES:
        raise ValueError(f"unknown token purpose: {purpose}")
    payload = f"{purpose}:{subscriber_id}".encode()
    sig = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()[:32]
    body = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    return f"{body}.{sig}"


def verify_token(secret: str, token: str, purpose: str) -> str | None:
    """Returns the signed identifier if the token is valid for this purpose, else None."""
    try:
        body, sig = token.split(".", 1)
        payload = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
    except (ValueError, TypeError):
        return None
    expected = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()[:32]
    # compare_digest raises TypeError on a non-ASCII signature; a malformed
    # token from a link scanner must return None, not 500 the public endpoint.
    if not sig.isascii() or not hmac.compare_digest(sig, expected):
        return None
    try:
        token_purpose, subscriber_id = payload.decode().split(":", 1)
    except (UnicodeDecodeError, ValueError):
        return None
    if token_purpose != purpose or not subscriber_id:
        return None
    return subscriber_id


def make_open_token(secret: str, campaign_id: str, subscriber_id: str) -> str:
    return make_token(secret, "open", f"{campaign_id}:{subscriber_id}")


def verify_open_token(secret: str, token: str) -> tuple[str, str] | None:
    identifier = verify_token(secret, token, "open")
    if identifier is None:
        return None
    parts = identifier.split(":")
    if len(parts) != 2 or not all(parts):
        return None
    return parts[0], parts[1]
