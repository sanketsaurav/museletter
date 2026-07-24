import base64
from datetime import UTC, datetime, timedelta

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.x509.oid import NameOID

from museletter.sns import SNSVerifier, canonical_message

CERT_URL = "https://sns.us-east-1.amazonaws.com/SimpleNotificationService-test.pem"


def _self_signed_cert(private_key):
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "sns.us-east-1.amazonaws.com")])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .sign(private_key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM)


def _verifier_for(pem: bytes, calls: list | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request)
        return httpx.Response(200, content=pem)

    return SNSVerifier(http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def _signed_notification(private_key, digest, version: str, tamper: bool = False) -> dict:
    msg = {
        "Type": "Notification",
        "Message": '{"eventType":"Delivery"}',
        "MessageId": "mid-1",
        "Timestamp": "2026-07-24T00:00:00.000Z",
        "TopicArn": "arn:aws:sns:us-east-1:123:museletter-events",
        "SignatureVersion": version,
        "SigningCertURL": CERT_URL,
    }
    signature = private_key.sign(canonical_message(msg), padding.PKCS1v15(), digest)
    if tamper:
        msg["Message"] = '{"eventType":"Bounce"}'
    msg["Signature"] = base64.b64encode(signature).decode()
    return msg


async def test_valid_signature_v1_and_v2():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    verifier = _verifier_for(_self_signed_cert(key))
    assert await verifier.verify(_signed_notification(key, hashes.SHA1(), "1"))
    assert await verifier.verify(_signed_notification(key, hashes.SHA256(), "2"))


async def test_tampered_message_rejected():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    verifier = _verifier_for(_self_signed_cert(key))
    assert not await verifier.verify(_signed_notification(key, hashes.SHA1(), "1", tamper=True))


async def test_signature_from_wrong_key_rejected():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    verifier = _verifier_for(_self_signed_cert(other))  # cert doesn't match signing key
    assert not await verifier.verify(_signed_notification(key, hashes.SHA1(), "1"))


async def test_non_rsa_certificate_rejected():
    ec_key = ec.generate_private_key(ec.SECP256R1())
    rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    verifier = _verifier_for(_self_signed_cert(ec_key))
    assert not await verifier.verify(_signed_notification(rsa_key, hashes.SHA1(), "1"))


async def test_cert_url_must_be_https_on_amazon_sns():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    verifier = _verifier_for(_self_signed_cert(key))
    for bad_url in (
        "http://sns.us-east-1.amazonaws.com/cert.pem",
        "https://evil.example.com/cert.pem",
        "https://sns.us-east-1.amazonaws.com.evil.com/cert.pem",
        "https://sns.us-east-1.amazonaws.com/cert.txt",
    ):
        msg = _signed_notification(key, hashes.SHA1(), "1")
        msg["SigningCertURL"] = bad_url
        assert not await verifier.verify(msg), bad_url


async def test_missing_fields_rejected():
    verifier = _verifier_for(b"")
    assert not await verifier.verify({"Type": "Notification"})
    assert not await verifier.verify({"Type": "Notification", "SigningCertURL": CERT_URL})


async def test_cert_fetch_failure_rejected():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    verifier = SNSVerifier(http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert not await verifier.verify(_signed_notification(key, hashes.SHA1(), "1"))


async def test_certificate_is_cached_per_url():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    calls: list = []
    verifier = _verifier_for(_self_signed_cert(key), calls)
    assert await verifier.verify(_signed_notification(key, hashes.SHA1(), "1"))
    assert await verifier.verify(_signed_notification(key, hashes.SHA1(), "1"))
    assert len(calls) == 1, "second verify must hit the cache"
