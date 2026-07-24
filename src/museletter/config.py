import os
from dataclasses import dataclass, field


@dataclass
class Settings:
    api_key: str = ""
    db_path: str = "museletter.db"
    base_url: str = ""
    from_email: str = ""
    from_name: str = ""
    postal_address: str = ""
    opt_in: str = "double"  # "double" or "single"
    send_rate: float = 10.0  # emails per second, must stay under the SES account rate
    aws_region: str = "us-east-1"
    ses_configuration_set: str = ""
    secret: str = ""  # HMAC key for public links; auto-generated into the DB if empty
    extra: dict = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> "Settings":
        env = os.environ
        return cls(
            api_key=env.get("MUSELETTER_API_KEY", ""),
            db_path=env.get("MUSELETTER_DB_PATH", "museletter.db"),
            base_url=env.get("MUSELETTER_BASE_URL", "").rstrip("/"),
            from_email=env.get("MUSELETTER_FROM_EMAIL", ""),
            from_name=env.get("MUSELETTER_FROM_NAME", ""),
            postal_address=env.get("MUSELETTER_POSTAL_ADDRESS", ""),
            opt_in=env.get("MUSELETTER_OPT_IN", "double"),
            send_rate=float(env.get("MUSELETTER_SEND_RATE", "10")),
            aws_region=env.get("AWS_REGION", env.get("AWS_DEFAULT_REGION", "us-east-1")),
            ses_configuration_set=env.get("MUSELETTER_SES_CONFIGURATION_SET", ""),
            secret=env.get("MUSELETTER_SECRET", ""),
        )

    def missing_required(self) -> list[str]:
        problems = []
        if not self.api_key:
            problems.append(
                "MUSELETTER_API_KEY is not set (any long random string; it is the admin credential)"
            )
        if not self.base_url:
            problems.append(
                "MUSELETTER_BASE_URL is not set (public URL of this server, e.g. https://news.example.com)"
            )
        if not self.from_email:
            problems.append("MUSELETTER_FROM_EMAIL is not set (the address newsletters are sent from)")
        if self.opt_in not in ("double", "single"):
            problems.append(f"MUSELETTER_OPT_IN must be 'double' or 'single', got '{self.opt_in}'")
        return problems
