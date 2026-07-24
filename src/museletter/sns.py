"""Verification and parsing of SES event notifications delivered via SNS."""

import base64
import json
import re
from urllib.parse import urlparse

import httpx
from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

_CERT_HOST_RE = re.compile(r"^sns\.[a-z0-9\-]+\.amazonaws\.com(\.cn)?$")


def is_amazon_sns_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme == "https" and bool(_CERT_HOST_RE.match(parsed.netloc))


def canonical_message(msg: dict) -> bytes:
    if msg.get("Type") == "Notification":
        keys = ["Message", "MessageId", "Subject", "Timestamp", "TopicArn", "Type"]
    else:
        keys = ["Message", "MessageId", "SubscribeURL", "Timestamp", "Token", "TopicArn", "Type"]
    parts = []
    for key in keys:
        if msg.get(key) is not None:
            parts.append(key)
            parts.append(str(msg[key]))
    return ("\n".join(parts) + "\n").encode()


class SNSVerifier:
    def __init__(self, http: httpx.AsyncClient | None = None):
        self._http = http
        self._cert_cache: dict[str, bytes] = {}

    async def _fetch_cert(self, url: str) -> bytes | None:
        parsed = urlparse(url)
        if (
            parsed.scheme != "https"
            or not _CERT_HOST_RE.match(parsed.netloc)
            or not parsed.path.endswith(".pem")
        ):
            return None
        if url not in self._cert_cache:
            if self._http is None:
                self._http = httpx.AsyncClient(timeout=10)
            resp = await self._http.get(url)
            resp.raise_for_status()
            self._cert_cache[url] = resp.content
        return self._cert_cache[url]

    async def verify(self, msg: dict) -> bool:
        cert_url = msg.get("SigningCertURL") or msg.get("SigningCertUrl") or ""
        signature = msg.get("Signature", "")
        if not cert_url or not signature:
            return False
        try:
            pem = await self._fetch_cert(cert_url)
            if pem is None:
                return False
            public_key = x509.load_pem_x509_certificate(pem).public_key()
            if not isinstance(public_key, rsa.RSAPublicKey):
                return False  # SNS signing certificates are RSA
            digest = hashes.SHA1() if msg.get("SignatureVersion", "1") == "1" else hashes.SHA256()
            public_key.verify(base64.b64decode(signature), canonical_message(msg), padding.PKCS1v15(), digest)
            return True
        except (InvalidSignature, ValueError, httpx.HTTPError):
            return False


def parse_ses_events(message_json: str) -> list[dict]:
    """Normalize an SES notification (event publishing or classic feedback format)
    into a flat list of {type, email, message_id, permanent, detail} dicts."""
    try:
        data = json.loads(message_json)
    except (ValueError, TypeError):
        return []
    if not isinstance(data, dict):
        return []

    event_type = (data.get("eventType") or data.get("notificationType") or "").lower()
    message_id = (data.get("mail") or {}).get("messageId", "")
    events = []

    if event_type == "bounce":
        bounce = data.get("bounce") or {}
        permanent = bounce.get("bounceType") == "Permanent"
        for recipient in bounce.get("bouncedRecipients", []):
            email = (recipient.get("emailAddress") or "").lower().strip()
            if email:
                events.append(
                    {
                        "type": "bounce",
                        "email": email,
                        "message_id": message_id,
                        "permanent": permanent,
                        "detail": f"{bounce.get('bounceType', '')}/{bounce.get('bounceSubType', '')}",
                    }
                )
    elif event_type == "complaint":
        complaint = data.get("complaint") or {}
        for recipient in complaint.get("complainedRecipients", []):
            email = (recipient.get("emailAddress") or "").lower().strip()
            if email:
                events.append(
                    {
                        "type": "complaint",
                        "email": email,
                        "message_id": message_id,
                        "permanent": True,
                        "detail": complaint.get("complaintFeedbackType", ""),
                    }
                )
    elif event_type == "delivery":
        for email in (data.get("delivery") or {}).get("recipients", []):
            events.append(
                {
                    "type": "delivery",
                    "email": str(email).lower().strip(),
                    "message_id": message_id,
                    "permanent": False,
                    "detail": "",
                }
            )
    return events
