"""Install Museletter as a per-user background service.

macOS -> a launchd LaunchAgent (~/Library/LaunchAgents).
Linux -> a systemd --user unit (~/.config/systemd/user).

Per-user (not system) on purpose: no sudo, and it matches the self-host /
Mac-mini use case where museletter runs as you.
"""

import shutil
import subprocess
import sys
from pathlib import Path

LABEL = "com.museletter.server"


class ServiceError(Exception):
    pass


def _museletter_bin() -> str:
    return shutil.which("museletter") or f"{sys.executable} -m museletter"


def _launch_agent_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def _systemd_unit_path() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / "museletter.service"


def _plist(host: str, port: int, env_file: str) -> str:
    # `serve --env-file` loads the .env itself, so we exec directly with no shell
    # (shell-sourcing an unquoted .env would split values with spaces).
    program = _museletter_bin().split()
    program += ["serve", "--host", host, "--port", str(port), "--env-file", env_file]
    args = "\n".join(f"    <string>{a}</string>" for a in program)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>{LABEL}</string>
    <key>ProgramArguments</key>
    <array>
{args}
    </array>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key><string>{Path.home()}/Library/Logs/museletter.log</string>
    <key>StandardErrorPath</key><string>{Path.home()}/Library/Logs/museletter.log</string>
</dict>
</plist>
"""  # noqa: E501 (args kept for readability of the template)


def _systemd_unit(host: str, port: int, env_file: str) -> str:
    program = " ".join([*_museletter_bin().split(), "serve", "--host", host, "--port", str(port)])
    return f"""[Unit]
Description=museletter newsletter server
After=network-online.target

[Service]
Type=simple
EnvironmentFile={env_file}
ExecStart={program}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
"""


def _run(cmd: list[str]) -> None:
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise ServiceError(f"{' '.join(cmd)} failed: {result.stderr.strip() or result.stdout.strip()}")


def install(*, host: str, port: int, env_file: str, start: bool) -> Path:
    if sys.platform == "darwin":
        path = _launch_agent_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        was_loaded = _is_loaded()
        path.write_text(_plist(host, port, env_file))
        if start:
            if was_loaded:
                _run(["launchctl", "unload", str(path)])
            _run(["launchctl", "load", str(path)])
        return path
    if sys.platform.startswith("linux"):
        path = _systemd_unit_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_systemd_unit(host, port, env_file))
        _run(["systemctl", "--user", "daemon-reload"])
        if start:
            _run(["systemctl", "--user", "enable", "--now", "museletter.service"])
        return path
    raise ServiceError(f"unsupported platform for service install: {sys.platform}")


def _is_loaded() -> bool:
    result = subprocess.run(["launchctl", "list"], capture_output=True, text=True)
    return LABEL in result.stdout


def uninstall() -> None:
    if sys.platform == "darwin":
        path = _launch_agent_path()
        if not path.exists():
            raise ServiceError("no launchd service installed")
        if _is_loaded():
            _run(["launchctl", "unload", str(path)])
        path.unlink()
        return
    if sys.platform.startswith("linux"):
        path = _systemd_unit_path()
        if not path.exists():
            raise ServiceError("no systemd service installed")
        _run(["systemctl", "--user", "disable", "--now", "museletter.service"])
        path.unlink()
        _run(["systemctl", "--user", "daemon-reload"])
        return
    raise ServiceError(f"unsupported platform: {sys.platform}")


def status() -> str:
    if sys.platform == "darwin":
        if not _launch_agent_path().exists():
            return "not installed"
        return "running" if _is_loaded() else "installed (not running)"
    if sys.platform.startswith("linux"):
        if not _systemd_unit_path().exists():
            return "not installed"
        result = subprocess.run(
            ["systemctl", "--user", "is-active", "museletter.service"], capture_output=True, text=True
        )
        return result.stdout.strip() or "unknown"
    return f"unsupported platform: {sys.platform}"
