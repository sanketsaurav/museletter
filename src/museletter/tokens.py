import base64
import hashlib
import hmac

PURPOSES = ("confirm", "unsubscribe")


def make_token(secret: str, purpose: str, subscriber_id: str) -> str:
    if purpose not in PURPOSES:
        raise ValueError(f"unknown token purpose: {purpose}")
    payload = f"{purpose}:{subscriber_id}".encode()
    sig = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()[:32]
    body = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    return f"{body}.{sig}"


def verify_token(secret: str, token: str, purpose: str) -> str | None:
    """Returns the subscriber id if the token is valid for this purpose, else None."""
    try:
        body, sig = token.split(".", 1)
        payload = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
    except (ValueError, TypeError):
        return None
    expected = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        token_purpose, subscriber_id = payload.decode().split(":", 1)
    except (UnicodeDecodeError, ValueError):
        return None
    if token_purpose != purpose or not subscriber_id:
        return None
    return subscriber_id
