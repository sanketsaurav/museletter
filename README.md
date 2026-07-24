# museletter

A headless, agent-first newsletter platform. One container, one SQLite database,
Amazon SES. No web UI — you (or your AI agent) operate it through a CLI and an HTTP API.

## Why

Newsletter platforms charge per subscriber. Amazon SES charges **$0.10 per 1,000
emails** — a 50,000-subscriber issue costs $5, and hosting museletter costs a few
dollars a month on any container platform. If you send occasional emails to people
who subscribed on your website, you don't need a marketing suite; you need the
primitives:

- subscribers, lists, and tags (CSV import/export)
- a public subscribe endpoint with double opt-in
- Markdown campaigns rendered into a clean, battle-tested email template
- RFC 8058 one-click unsubscribe, injected automatically on every send
- automatic bounce/complaint suppression (via SES → SNS webhooks)
- a crash-safe send ledger that respects SES rate limits and resumes mid-blast
- per-campaign delivery stats
- `museletter doctor` — checks DNS, DKIM, DMARC, SES sandbox/quota, and config

And nothing more. No automations, no A/B tests, no landing pages, no tracking
pixels. RSS-to-email? That's your agent's job — see `skill/`.

## Quickstart

```bash
pip install museletter

export MUSELETTER_API_KEY=$(openssl rand -hex 32)
export MUSELETTER_BASE_URL=https://news.example.com   # public URL of this server
export MUSELETTER_FROM_EMAIL=newsletter@example.com
export MUSELETTER_FROM_NAME="Your Name"
export MUSELETTER_POSTAL_ADDRESS="123 Main St, City"  # CAN-SPAM requires one
export AWS_REGION=us-east-1
export AWS_ACCESS_KEY_ID=...
export AWS_SECRET_ACCESS_KEY=...

museletter serve
```

A `default` list exists out of the box. From another shell (or machine):

```bash
export MUSELETTER_URL=https://news.example.com MUSELETTER_API_KEY=...

museletter doctor                       # verify SES + DNS before anything else
museletter subs add reader@example.com --name "First Reader"
museletter campaigns create --subject "Hello" --file issue.md
museletter campaigns test cmp_xxx --to you@example.com
museletter campaigns send cmp_xxx      # asks for confirmation
museletter campaigns stats cmp_xxx
```

Every command takes `--json`. The HTTP API is browsable at `/docs` and uses
`Authorization: Bearer $MUSELETTER_API_KEY`.

Docker instead of pip:

```bash
docker build -t museletter . && docker run -p 8000:8000 -v museletter:/data \
  -e MUSELETTER_API_KEY=... [...] museletter
```

Deployment guides for [Fly.io](docs/deploy/fly.md), [Railway/Render](docs/deploy/railway.md),
[any VPS](docs/deploy/vps.md), and [Cloudflare](docs/deploy/cloudflare.md).

## AWS SES setup (once)

1. Verify your domain in SES (adds DKIM CNAMEs), add a DMARC record.
2. Request production access (new accounts are sandboxed).
3. Wire bounce/complaint events to museletter: configuration set → SNS topic →
   HTTPS subscription to `https://your-server/webhooks/sns` (museletter
   auto-confirms it), then set `MUSELETTER_SES_CONFIGURATION_SET`.

Step-by-step commands: [skill/recipes/aws-ses-setup.md](skill/recipes/aws-ses-setup.md).
This wiring is not optional — without it bounces are never suppressed and SES
will eventually suspend the account.

## Subscribing readers

Point your website's form at the public endpoint (no auth):

```html
<form action="https://news.example.com/subscribe/default" method="post">
  <input type="email" name="email" required>
  <input type="text" name="website" style="display:none">  <!-- honeypot -->
  <button>Subscribe</button>
</form>
```

JSON works too. Double opt-in by default (`MUSELETTER_OPT_IN=single` to disable).
Unsubscribes are one-click (RFC 8058) and immediate — even mid-campaign.

## Configuration

| Variable | Required | Meaning |
|---|---|---|
| `MUSELETTER_API_KEY` | yes | admin credential (any long random string) |
| `MUSELETTER_BASE_URL` | yes | public URL used in confirm/unsubscribe links |
| `MUSELETTER_FROM_EMAIL` | yes | sender address (SES-verified domain) |
| `MUSELETTER_FROM_NAME` | no | sender display name |
| `MUSELETTER_POSTAL_ADDRESS` | no* | postal address in the footer (*legally required) |
| `MUSELETTER_OPT_IN` | no | `double` (default) or `single` |
| `MUSELETTER_SEND_RATE` | no | emails/sec, default 10 — keep under your SES rate |
| `MUSELETTER_SES_CONFIGURATION_SET` | no | configuration set for event feedback |
| `MUSELETTER_DB_PATH` | no | SQLite path, default `museletter.db` |
| `AWS_REGION`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | yes | SES credentials |

## How a 50k blast works

Sending materializes one ledger row per recipient. A background loop drains
pending rows through SES at `MUSELETTER_SEND_RATE`, marking each with its SES
message id — so a crash or redeploy resumes exactly where it stopped, and
retries can't double-send. At SES's default 14/sec production rate, 50k emails
take about an hour. SNS webhooks then flip rows to delivered/bounced/complained,
and hard bounces/complaints are auto-added to the global suppression list.

## Agent-first design

The `skill/` directory is a ready-made skill for Claude Code (and any agent that
reads Markdown): drop it into `.claude/skills/museletter/`. The API is built for
agents — idempotency keys on mutations, dry runs, mandatory confirm-to-send,
test-send-before-send guardrails, machine-readable errors, and `doctor` for
self-diagnosis.

## Development

```bash
uv venv && uv pip install -e ".[dev]"
.venv/bin/pytest            # tests
.venv/bin/ruff check .      # lint (--fix to autofix)
.venv/bin/ruff format .     # format
.venv/bin/ty check          # typecheck
```

All four must pass before a PR.

## License

MIT
