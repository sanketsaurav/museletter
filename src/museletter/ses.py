"""Minimal Amazon SES v2 client over signed HTTPS.

Deliberately not boto3: the dependency is heavy, sync, and unavailable on
edge runtimes. SigV4 is ~40 lines and SES v2 is a plain JSON API, which keeps
this module portable to Cloudflare Workers later.
"""

import hashlib
import hmac
import json
import os
from datetime import UTC, datetime
from urllib.parse import quote, urlparse

import httpx


class SESError(Exception):
    def __init__(self, status: int, code: str, message: str):
        self.status = status
        self.code = code
        self.message = message
        super().__init__(f"SES {status} {code}: {message}")

    @property
    def throttled(self) -> bool:
        return self.status == 429 or "TooManyRequests" in self.code or "Throttling" in self.code


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def derive_signing_key(secret_key: str, datestamp: str, region: str, service: str) -> bytes:
    key = _hmac(("AWS4" + secret_key).encode(), datestamp)
    for part in (region, service, "aws4_request"):
        key = _hmac(key, part)
    return key


def sign_request(
    method: str,
    url: str,
    region: str,
    payload: bytes,
    access_key: str,
    secret_key: str,
    session_token: str = "",
    service: str = "ses",
) -> dict[str, str]:
    parsed = urlparse(url)
    now = datetime.now(UTC)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    datestamp = now.strftime("%Y%m%d")

    headers = {"host": parsed.netloc, "x-amz-date": amz_date}
    if session_token:
        headers["x-amz-security-token"] = session_token
    signed_headers = ";".join(sorted(headers))
    canonical_headers = "".join(f"{k}:{headers[k]}\n" for k in sorted(headers))
    payload_hash = hashlib.sha256(payload).hexdigest()
    # AWS builds the canonical URI from the percent-encoded path, so an
    # unencoded reserved char (e.g. '@' in an email-address identity) would
    # otherwise yield SignatureDoesNotMatch. quote keeps unreserved chars and '/'.
    canonical_uri = quote(parsed.path or "/", safe="/-._~")
    canonical_request = "\n".join(
        [method, canonical_uri, parsed.query, canonical_headers, signed_headers, payload_hash]
    )

    scope = f"{datestamp}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join(
        ["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical_request.encode()).hexdigest()]
    )
    key = derive_signing_key(secret_key, datestamp, region, service)
    signature = hmac.new(key, string_to_sign.encode(), hashlib.sha256).hexdigest()

    out = {
        "x-amz-date": amz_date,
        "authorization": (
            f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        ),
    }
    if session_token:
        out["x-amz-security-token"] = session_token
    return out


class SESClient:
    def __init__(self, region: str, configuration_set: str = "", http: httpx.AsyncClient | None = None):
        self.region = region
        self.configuration_set = configuration_set
        self.endpoint = f"https://email.{region}.amazonaws.com"
        self._http = http

    def _credentials(self) -> tuple[str, str, str]:
        access = os.environ.get("AWS_ACCESS_KEY_ID", "")
        secret = os.environ.get("AWS_SECRET_ACCESS_KEY", "")
        token = os.environ.get("AWS_SESSION_TOKEN", "")
        if not access or not secret:
            raise SESError(0, "NoCredentials", "AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY are not set")
        return access, secret, token

    async def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=30)
        url = self.endpoint + path
        payload = json.dumps(body).encode() if body is not None else b""
        access, secret, token = self._credentials()
        headers = sign_request(method, url, self.region, payload, access, secret, token)
        if body is not None:
            headers["content-type"] = "application/json"
        resp = await self._http.request(method, url, content=payload or None, headers=headers)
        if resp.status_code >= 400:
            try:
                data = resp.json()
            except ValueError:
                data = {}
            code = resp.headers.get("x-amzn-ErrorType", data.get("__type", "UnknownError"))
            code = code.split(":")[0].split("#")[-1]
            raise SESError(resp.status_code, code, data.get("message", resp.text[:500]))
        return resp.json() if resp.content else {}

    async def send_email(
        self,
        to: str,
        subject: str,
        html: str,
        text: str,
        *,
        from_email: str,
        from_name: str = "",
        headers: dict[str, str] | None = None,
        reply_to: str = "",
    ) -> str:
        """Send one email, returns the SES message id."""
        sender = f'"{from_name.replace(chr(34), "")}" <{from_email}>' if from_name else from_email
        simple: dict = {
            "Subject": {"Data": subject, "Charset": "UTF-8"},
            "Body": {
                "Html": {"Data": html, "Charset": "UTF-8"},
                "Text": {"Data": text, "Charset": "UTF-8"},
            },
        }
        if headers:
            simple["Headers"] = [{"Name": k, "Value": v} for k, v in headers.items()]
        body: dict = {
            "FromEmailAddress": sender,
            "Destination": {"ToAddresses": [to]},
            "Content": {"Simple": simple},
        }
        if reply_to:
            body["ReplyToAddresses"] = [reply_to]
        if self.configuration_set:
            body["ConfigurationSetName"] = self.configuration_set
        data = await self._request("POST", "/v2/email/outbound-emails", body)
        return data.get("MessageId", "")

    async def get_account(self) -> dict:
        return await self._request("GET", "/v2/email/account")

    async def get_identity(self, identity: str) -> dict | None:
        try:
            return await self._request("GET", f"/v2/email/identities/{identity}")
        except SESError as exc:
            if exc.status == 404 or "NotFound" in exc.code:
                return None
            raise

    @staticmethod
    def has_credentials() -> bool:
        return bool(os.environ.get("AWS_ACCESS_KEY_ID") and os.environ.get("AWS_SECRET_ACCESS_KEY"))
