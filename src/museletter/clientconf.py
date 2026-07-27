"""Client-side configuration: connect tokens and named server profiles.

The CLI talks to a remote (or local) museletter server. Config lives at
~/.config/museletter/config.toml as named profiles; a connect token is a single
copy-pasteable blob the server emits so wiring up a client is one command.
"""

import base64
import json
import os
import tomllib
from pathlib import Path

CONFIG_PATH = Path.home() / ".config" / "museletter" / "config.toml"
TOKEN_PREFIX = "ml_"


class ConfigError(Exception):
    pass


# ---------- connect tokens ----------


def encode_token(url: str, api_key: str) -> str:
    """A connect token packs {url, api_key} into one opaque, copy-pasteable blob."""
    payload = json.dumps({"u": url.rstrip("/"), "k": api_key}, separators=(",", ":")).encode()
    return TOKEN_PREFIX + base64.urlsafe_b64encode(payload).decode().rstrip("=")


def decode_token(token: str) -> tuple[str, str]:
    """Returns (url, api_key). Raises ConfigError on a malformed token."""
    token = token.strip()
    if not token.startswith(TOKEN_PREFIX):
        raise ConfigError("not a museletter connect token (should start with 'ml_')")
    raw = token[len(TOKEN_PREFIX) :]
    try:
        payload = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
        data = json.loads(payload)
        url, api_key = data["u"], data["k"]
    except (ValueError, TypeError, KeyError) as exc:
        raise ConfigError(f"malformed connect token: {exc}") from exc
    if not url or not api_key:
        raise ConfigError("connect token is missing a url or api key")
    return url, api_key


# ---------- profile store ----------


def _dumps(config: dict) -> str:
    """Minimal TOML writer for our known shape (a default key + string profiles)."""

    def q(s: str) -> str:
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'

    lines = []
    if config.get("default"):
        lines.append(f"default = {q(config['default'])}")
    for name, prof in config.get("profiles", {}).items():
        lines.append("")
        lines.append(f"[profiles.{name}]")
        for key in ("url", "api_key"):
            if prof.get(key):
                lines.append(f"{key} = {q(prof[key])}")
    return "\n".join(lines) + "\n"


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        return {"default": None, "profiles": {}}
    try:
        raw = tomllib.loads(CONFIG_PATH.read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return {"default": None, "profiles": {}}
    # Backward compat: an old flat {url, api_key} file is the implicit "default".
    if "url" in raw and "profiles" not in raw:
        return {
            "default": "default",
            "profiles": {"default": {"url": raw["url"], "api_key": raw.get("api_key", "")}},
        }
    raw.setdefault("profiles", {})
    raw.setdefault("default", None)
    return raw


def save_profile(name: str, url: str, api_key: str, *, make_default: bool = True) -> None:
    config = load_config()
    config["profiles"][name] = {"url": url.rstrip("/"), "api_key": api_key}
    if make_default or not config.get("default"):
        config["default"] = name
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(_dumps(config))
    CONFIG_PATH.chmod(0o600)


def resolve(profile: str | None = None) -> tuple[str, str, str]:
    """Resolve (url, api_key, source) for the active server.

    Precedence: explicit env (MUSELETTER_URL/_API_KEY) > requested profile >
    MUSELETTER_PROFILE > the config's default profile.
    """
    env_url = os.environ.get("MUSELETTER_URL")
    if env_url:
        return env_url.rstrip("/"), os.environ.get("MUSELETTER_API_KEY", ""), "env"

    config = load_config()
    name = profile or os.environ.get("MUSELETTER_PROFILE") or config.get("default")
    if not name:
        raise ConfigError(
            "no server configured; run `museletter connect <token>` "
            "(or set MUSELETTER_URL and MUSELETTER_API_KEY)"
        )
    prof = config.get("profiles", {}).get(name)
    if not prof:
        available = ", ".join(config.get("profiles", {})) or "none"
        raise ConfigError(f"unknown profile '{name}' (configured: {available})")
    return prof["url"].rstrip("/"), prof.get("api_key", ""), f"profile:{name}"
