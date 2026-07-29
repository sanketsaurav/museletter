"""Cloudflare Turnstile verification for the public subscribe endpoint.

Opt-in: only used when MUSELETTER_TURNSTILE_SECRET is set. The client renders the
Turnstile widget, which posts a token; this verifies that token server-side.
"""

import httpx

SITEVERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"


async def verify(secret: str, token: str, remoteip: str = "", http: httpx.AsyncClient | None = None) -> bool:
    if not token:
        return False
    client = http or httpx.AsyncClient(timeout=10)
    try:
        payload = {"secret": secret, "response": token}
        if remoteip:
            payload["remoteip"] = remoteip
        resp = await client.post(SITEVERIFY_URL, data=payload)
        return bool(resp.json().get("success"))
    except (httpx.HTTPError, ValueError):
        return False
    finally:
        if http is None:
            await client.aclose()
