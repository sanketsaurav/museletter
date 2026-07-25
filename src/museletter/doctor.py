"""Preflight checks: is this instance actually able to send a newsletter?"""

import dns.asyncresolver
import dns.exception

from .config import Settings
from .ses import SESClient, SESError


def _check(name: str, status: str, detail: str) -> dict:
    return {"name": name, "status": status, "detail": detail}


async def _resolve_txt(name: str) -> list[str]:
    resolver = dns.asyncresolver.Resolver()
    resolver.lifetime = 5
    try:
        answers = await resolver.resolve(name, "TXT")
        return [b"".join(r.strings).decode("utf-8", "replace") for r in answers]
    except (dns.exception.DNSException, OSError):
        return []


async def run_checks(settings: Settings, ses: SESClient, db) -> dict:
    checks: list[dict] = []

    problems = settings.missing_required()
    for problem in problems:
        checks.append(_check("config", "fail", problem))
    if not problems:
        checks.append(_check("config", "ok", "required configuration present"))
    if not settings.postal_address:
        checks.append(
            _check(
                "postal-address",
                "warn",
                "MUSELETTER_POSTAL_ADDRESS is empty; CAN-SPAM requires a physical address in every email",
            )
        )
    if settings.base_url.startswith("http://"):
        checks.append(_check("base-url", "warn", "MUSELETTER_BASE_URL is not https"))
    if not settings.sns_topic_arn:
        checks.append(
            _check(
                "sns-topic",
                "warn",
                "MUSELETTER_SNS_TOPIC_ARN is empty; the webhook accepts SES events from any "
                "SNS topic. Set it to your topic ARN so forged events are rejected.",
            )
        )

    if not SESClient.has_credentials():
        checks.append(
            _check("aws-credentials", "fail", "AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY are not set")
        )
    else:
        checks.append(_check("aws-credentials", "ok", "AWS credentials present"))
        try:
            account = await ses.get_account()
            if not account.get("SendingEnabled", False):
                checks.append(_check("ses-sending", "fail", "sending is disabled on this SES account"))
            elif not account.get("ProductionAccessEnabled", False):
                checks.append(
                    _check(
                        "ses-sandbox",
                        "fail",
                        "SES account is in the sandbox: you can only email verified addresses. "
                        "Request production access in the SES console.",
                    )
                )
            else:
                checks.append(_check("ses-sending", "ok", "SES production access enabled"))
            quota = account.get("SendQuota", {})
            max_rate = quota.get("MaxSendRate", 0)
            checks.append(
                _check(
                    "ses-quota",
                    "ok",
                    f"quota: {quota.get('SentLast24Hours', 0):.0f}/{quota.get('Max24HourSend', 0):.0f} "
                    f"in last 24h, max rate {max_rate:.0f}/sec",
                )
            )
            if max_rate and settings.send_rate > max_rate:
                checks.append(
                    _check(
                        "send-rate",
                        "warn",
                        f"MUSELETTER_SEND_RATE ({settings.send_rate}/sec) exceeds the SES account rate "
                        f"({max_rate:.0f}/sec); sends will be throttled",
                    )
                )
        except (SESError, OSError) as exc:
            checks.append(_check("ses-account", "fail", f"could not query SES: {exc}"))

        domain = settings.from_email.split("@")[-1] if "@" in settings.from_email else ""
        if domain:
            try:
                identity = await ses.get_identity(domain) or await ses.get_identity(settings.from_email)
                if identity is None:
                    checks.append(
                        _check(
                            "ses-identity",
                            "fail",
                            f"neither {domain} nor {settings.from_email} is a verified SES identity",
                        )
                    )
                else:
                    if identity.get("VerifiedForSendingStatus"):
                        checks.append(_check("ses-identity", "ok", "sender identity is verified"))
                    else:
                        checks.append(
                            _check("ses-identity", "fail", "sender identity exists but is not verified")
                        )
                    dkim = (identity.get("DkimAttributes") or {}).get("Status", "")
                    if dkim == "SUCCESS":
                        checks.append(_check("dkim", "ok", "DKIM is passing"))
                    else:
                        checks.append(_check("dkim", "warn", f"DKIM status is {dkim or 'unknown'}"))
            except (SESError, OSError) as exc:
                checks.append(_check("ses-identity", "warn", f"could not query identity: {exc}"))

            dmarc = await _resolve_txt(f"_dmarc.{domain}")
            if any(r.lower().startswith("v=dmarc1") for r in dmarc):
                checks.append(_check("dmarc", "ok", f"DMARC record found on {domain}"))
            else:
                checks.append(
                    _check(
                        "dmarc",
                        "warn",
                        f"no DMARC record on _dmarc.{domain}; Gmail/Yahoo require one for bulk senders "
                        '(minimal: "v=DMARC1; p=none")',
                    )
                )

    async with db.execute("SELECT COUNT(*) AS n FROM subscribers WHERE status = 'active'") as cur:
        active = (await cur.fetchone())["n"]
    checks.append(_check("database", "ok", f"database reachable, {active} active subscribers"))

    if any(c["status"] == "fail" for c in checks):
        overall = "fail"
    elif any(c["status"] == "warn" for c in checks):
        overall = "warn"
    else:
        overall = "ok"
    return {"status": overall, "checks": checks}
