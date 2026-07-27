# Self-host on a Mac mini (or any always-on machine)

museletter is one process and one SQLite file, so a Mac mini at home makes a
fine host. There's one thing to get right first.

## The one constraint: public endpoints must be reachable from the internet

museletter's HTTP surface has two audiences:

- **Public endpoints** — `/subscribe`, `/confirm`, `/unsubscribe`, `/webhooks/sns`.
  Your readers click confirm/unsubscribe links from their inbox, and Amazon SNS
  POSTs bounce/complaint events to the webhook. **These must be reachable from
  the public internet.** A Mac mini on your home LAN is not, by itself.
- **Admin API** — `/v1/*`. Only you (or your agent) call it; it can stay local.

So the Mac-mini recipe is: run the server locally, and expose it with a tunnel.
A [Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/)
is the cleanest (no open ports, free, gives you a stable hostname):

```bash
brew install cloudflared
cloudflared tunnel login
cloudflared tunnel create museletter
# route a hostname you control to the tunnel, then point it at the local server:
cloudflared tunnel route dns museletter news.example.com
cloudflared tunnel run --url http://127.0.0.1:8000 museletter
```

Set `MUSELETTER_BASE_URL=https://news.example.com` (the tunnel hostname), not the
LAN address — that URL goes into every confirm/unsubscribe link and the SNS
subscription. Tailscale Funnel or ngrok work the same way.

## Run museletter as a service

Generate config, then install it as a launchd agent so it starts on boot and
restarts on crash:

```bash
museletter init --base-url https://news.example.com --from-email you@example.com
# writes .env and prints a connect token

# put your AWS creds in the .env too, then:
museletter service install --env-file .env
museletter service status          # -> running
```

`service install` writes `~/Library/LaunchAgents/com.museletter.server.plist`
(launchd on macOS; a systemd `--user` unit on Linux) and loads it. Logs go to
`~/Library/Logs/museletter.log`. Remove it with `museletter service uninstall`.

## Use the CLI against your local server

On the same machine, connect to localhost — no tunnel needed for admin work:

```bash
museletter connect http://127.0.0.1:8000 --api-key "$(grep API_KEY .env | cut -d= -f2)"
museletter status
```

From your laptop on the same Tailscale network, connect to the mini's Tailscale
name instead (e.g. `http://minimac:8000`). The public tunnel hostname also works
from anywhere, but keep the admin API key private.

## Back up the database

The whole state is one file. A nightly cron/launchd job is a complete backup:

```bash
sqlite3 ~/museletter/museletter.db ".backup ~/backups/museletter-$(date +%F).db"
```
