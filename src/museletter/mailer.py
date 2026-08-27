"""Provider-neutral sending layer.

Every provider client exposes the same send_email coroutine and raises
SendError subclasses; the app talks to `app.state.mailer` and never to a
concrete client. SendResult carries the synchronous verdict: SES only ever
knows "sent", while Cloudflare can report delivery or a permanent bounce in
the send response itself.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from .config import Settings


class SendError(Exception):
    """A mail provider rejected a request. `code` is the provider's error code."""

    provider = "provider"

    def __init__(self, status: int, code: str, message: str):
        self.status = status
        self.code = code
        self.message = message
        super().__init__(f"{self.provider} {status} {code}: {message}")

    @property
    def throttled(self) -> bool:
        return self.status == 429 or "TooManyRequests" in self.code or "Throttling" in self.code


@dataclass
class SendResult:
    """What one send call established. 'sent' means accepted and in flight;
    'delivered' and 'bounced' are synchronous verdicts a provider may return.
    message_id is empty when the provider does not return one."""

    message_id: str = ""
    outcome: str = "sent"  # 'sent' | 'delivered' | 'bounced'
    detail: str = ""


class Mailer(Protocol):
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
    ) -> SendResult: ...


def create_mailer(settings: "Settings") -> Mailer:
    if settings.email_provider == "cloudflare":
        from .cloudflare import CloudflareEmail

        return CloudflareEmail(settings.cloudflare_account_id, settings.cloudflare_events_queue_id)
    from .ses import SESClient

    return SESClient(settings.aws_region, settings.ses_configuration_set)
