# Museletter

A headless, agent-first newsletter platform. One container, one SQLite
database, Amazon SES. There is no web UI: you (or your AI agent) operate it
through a CLI and an HTTP API.

## Why

Newsletter platforms charge per subscriber. Amazon SES charges about **$0.10
per 1,000 emails**, so a 50,000-subscriber issue costs $5 and hosting
Museletter costs a few dollars a month on any container host. If you send the
occasional email to people who subscribed on your website, you don't need a
marketing suite. You need the primitives, and nothing else:

- subscribers, lists, and tags (with CSV import/export)
- a public subscribe endpoint with double opt-in
- Markdown campaigns rendered into a clean, battle-tested email template
- RFC 8058 one-click unsubscribe, injected automatically on every send
- automatic bounce/complaint suppression via SES to SNS webhooks
- a crash-safe send ledger that respects SES rate limits and resumes mid-blast
- per-campaign delivery stats
- `museletter doctor`, which checks DNS, DKIM, DMARC, SES sandbox/quota, config

No automations, no A/B tests, no landing pages, no tracking pixels. Want
RSS-to-email? That is your agent's job, and it ships as a skill recipe.

## How it works

Three layers:

1. **The server** (`museletter serve`): a FastAPI app over one SQLite file.
   Its HTTP surface has two audiences. The **public endpoints**
   (`/subscribe`, `/confirm`, `/unsubscribe`, `/webhooks/sns`) must be
   reachable from the internet, because readers click links in their inbox and
   Amazon SNS posts delivery events to the webhook. The **admin API**
   (`/v1/*`, bearer-authenticated) only needs to be reachable by you.
2. **The CLI** (`museletter`): the same binary runs the server *and* is the
   admin client for a running server, local or remote.
3. **The skill**: a bundled set of recipes so an agent can drive the CLI. See
   [Agent-first design](#agent-first-design).

Sending never happens inline. A campaign materializes one row per recipient in
a ledger table; a background loop drains that ledger through SES at your
configured rate, marking each row with its SES message id. A crash or redeploy
resumes from the pending rows, and retries cannot double-send.

## Install

```bash
pip install museletter          # the CLI and server
```

Or run the server as a container (see [Deployment](#deployment)):

```bash
docker run ghcr.io/sanketsaurav/museletter:latest
```

## Quickstart

Museletter is two installs: the **server** on an always-on host, and the
**CLI** on your machine pointed at that server. On a single machine (a Mac
mini, say) they are the same install talking to `localhost`.

### 1. Bootstrap and run the server

On the host, generate config (this writes a `.env` and prints a connect
token):

```bash
museletter init --base-url https://news.example.com --from-email you@example.com
```

Add your AWS credentials to the `.env` (see [AWS SES setup](#aws-ses-setup-once)),
then start the server with that environment. Any container host works; the
simplest is Docker:

```bash
docker run -d --env-file .env -v museletter:/data -p 8000:8000 \
  ghcr.io/sanketsaurav/museletter:latest
```

`museletter init` prints a **connect token**: one `ml_...` blob that encodes
the server URL and admin API key. Copy it.

### 2. Point your CLI at the server

On your laptop:

```bash
pip install museletter
museletter connect ml_...        # paste the token; verifies reachability + auth
museletter skill install         # drop the agent skill into ~/.claude/skills
museletter doctor                # confirm SES, DNS, and config are healthy
museletter status                # server, reachability, subscriber counts
```

`connect` saves a named profile in `~/.config/museletter/config.toml`. Manage
several servers with `--name` on connect and `--profile` on any command.

### 3. Send your first issue

```bash
museletter subs add reader@example.com --name "First Reader"
museletter campaigns create --subject "Hello" --file issue.md
museletter campaigns test cmp_xxx --to you@example.com   # test send to yourself
museletter campaigns send cmp_xxx --dry-run              # show the audience
museletter campaigns send cmp_xxx                        # asks to confirm
museletter campaigns stats cmp_xxx                       # sent/delivered/bounced
```

Every command accepts `--json`. The HTTP API is browsable at `/docs` and
authenticates with `Authorization: Bearer <api key>`.

## AWS SES setup (once)

Museletter sends through Amazon SES, so SES has to be set up once. All of this
is scriptable, and the bundled skill has a copy-paste recipe
(`museletter skill install`, then see `recipes/aws-ses-setup.md`).

1. **Verify your sending domain** (creates DKIM keys):

   ```bash
   aws sesv2 create-email-identity --email-identity example.com
   aws sesv2 get-email-identity --email-identity example.com \
     --query 'DkimAttributes.Tokens'
   ```

   Add each returned token as a CNAME:
   `<token>._domainkey.example.com -> <token>.dkim.amazonses.com`.

2. **Add a DMARC record.** Gmail and Yahoo require one for bulk senders. A
   minimal TXT record on `_dmarc.example.com`: `v=DMARC1; p=none`.

3. **Wire bounce/complaint events back to Museletter.** Without this, bounces
   are never suppressed and SES will eventually suspend your account.

   ```bash
   aws sesv2 create-configuration-set --configuration-set-name museletter
   aws sns create-topic --name museletter-events   # note the TopicArn
   aws sesv2 create-configuration-set-event-destination \
     --configuration-set-name museletter \
     --event-destination-name sns \
     --event-destination '{"Enabled":true,"MatchingEventTypes":["BOUNCE","COMPLAINT","DELIVERY"],"SnsDestination":{"TopicArn":"<TopicArn>"}}'
   aws sns subscribe --topic-arn <TopicArn> --protocol https \
     --notification-endpoint https://news.example.com/webhooks/sns
   ```

   Museletter auto-confirms the SNS subscription. Then set
   `MUSELETTER_SES_CONFIGURATION_SET=museletter` and
   `MUSELETTER_SNS_TOPIC_ARN=<TopicArn>` in your environment. Setting the topic
   ARN is important: it makes the webhook reject events from any other topic.

4. **Request production access.** New SES accounts are sandboxed (only verified
   recipients, 200/day). This is a support form in the SES console.

5. **Minimal IAM policy** for the server's credentials: `ses:SendEmail`,
   `ses:GetAccount`, `ses:GetEmailIdentity`.

Run `museletter doctor` at any point; it reports exactly which of these is
missing.

## Deployment

The server is a single stateless process plus one SQLite file. Run the
**Docker image** anywhere and mount a volume at `/data`. That is the primary
and best-supported path; the platform notes below are thin wrappers around it.

The one hard requirement on every platform: the **public endpoints must be
reachable from the internet** over HTTPS, and `MUSELETTER_BASE_URL` must be
that public URL (it goes into every confirm/unsubscribe link and the SNS
subscription).

### Docker (primary)

```bash
docker run -d --name museletter --restart unless-stopped \
  --env-file .env \
  -v museletter-data:/data \
  -p 8000:8000 \
  ghcr.io/sanketsaurav/museletter:latest
```

Build it yourself instead of pulling: `docker build -t museletter .`. The
database lives at `/data/museletter.db` (set by the image); back up that one
file and you have backed up everything. Put the container behind a reverse
proxy (Caddy, nginx, your platform's router) for TLS.

### Render (or any PaaS with a Dockerfile)

Create a service from this repo; Render detects the Dockerfile. Then:

1. Attach a **persistent disk** mounted at `/data`. Without it, a redeploy
   wipes your subscribers.
2. Set the environment variables from the table below.
3. Disable scale-to-zero / sleeping. The send loop runs in-process, so a
   sleeping instance pauses mid-campaign (it resumes safely, just late).

### A plain VPS

Any small box works. Run the container as above, or `pip install museletter`
and run `museletter serve` under systemd. Front it with Caddy for automatic
HTTPS:

```
news.example.com {
    reverse_proxy 127.0.0.1:8000
}
```

### Self-host (Mac mini or any always-on machine)

Run the server locally and expose only the public endpoints through a tunnel
(a home LAN is not internet-reachable on its own). A
[Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/)
is the cleanest (no open ports, free, stable hostname); Tailscale Funnel and
ngrok work the same way. Point the tunnel at `http://127.0.0.1:8000` and set
`MUSELETTER_BASE_URL` to the tunnel's public hostname.

Keep it running across reboots with the built-in service installer (launchd on
macOS, a systemd user unit on Linux):

```bash
museletter service install --env-file .env    # starts on boot, restarts on crash
museletter service status
museletter service uninstall
```

Drive it from the same machine over localhost:
`museletter connect http://127.0.0.1:8000 --api-key <key>`.

## Configuration

Set these in the server's environment (`museletter init` writes most of them).

| Variable | Required | Meaning |
|---|---|---|
| `MUSELETTER_API_KEY` | yes | admin credential (any long random string) |
| `MUSELETTER_BASE_URL` | yes | public URL used in confirm/unsubscribe links |
| `MUSELETTER_FROM_EMAIL` | yes | sender address (on an SES-verified domain) |
| `MUSELETTER_FROM_NAME` | no | sender display name |
| `MUSELETTER_POSTAL_ADDRESS` | no* | postal address in the footer (*required by CAN-SPAM) |
| `MUSELETTER_OPT_IN` | no | `double` (default) or `single` |
| `MUSELETTER_SEND_RATE` | no | emails/sec, default 10; keep under your SES rate |
| `MUSELETTER_SES_CONFIGURATION_SET` | no | configuration set for event feedback |
| `MUSELETTER_SNS_TOPIC_ARN` | recommended | your SNS topic ARN; the webhook rejects events from any other topic |
| `MUSELETTER_TRUST_PROXY` | no | `true` when behind a proxy, so rate limiting uses `X-Forwarded-For` not the proxy IP |
| `MUSELETTER_PUBLIC_SUBSCRIBE` | no | `false` disables the public `/subscribe` endpoint (add subscribers via the admin API instead) |
| `MUSELETTER_TURNSTILE_SECRET` | no | Cloudflare Turnstile secret; when set, `/subscribe` requires a valid Turnstile token |
| `MUSELETTER_CONFIRMATION_COOLDOWN` | no | min seconds between confirmation emails to one address, default 3600 |
| `MUSELETTER_DB_PATH` | no | SQLite path, default `museletter.db` (the image uses `/data/museletter.db`) |
| `AWS_REGION`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | yes | SES credentials |

The client CLI reads its server from `~/.config/museletter/config.toml`
(written by `museletter connect`), or from `MUSELETTER_URL` +
`MUSELETTER_API_KEY` when set.

## Connect your website

There are two ways to collect subscribers, depending on whether your site has a
backend. Both feed the same `default` list (or any list slug).

Either way, double opt-in is the default (`MUSELETTER_OPT_IN=single` to skip the
confirmation email), and unsubscribes are one-click (RFC 8058), immediate, and
handled for you.

### If you have a backend (recommended when you can)

Add subscribers server-side through the authenticated admin API, so your API
key never touches the browser:

```bash
curl -X POST https://news.example.com/v1/lists/default/subscribers \
  -H "Authorization: Bearer $MUSELETTER_API_KEY" \
  -H "content-type: application/json" \
  -d '{"email":"reader@example.com","name":"Reader","status":"unconfirmed"}'
```

Use `status:"unconfirmed"` to trigger the double opt-in email, or `"active"` to
add them directly (only for people who genuinely opted in). Since you are not
using the public form, you can turn the public endpoint off entirely:

```bash
MUSELETTER_PUBLIC_SUBSCRIBE=false
```

### If you have a static site (no backend)

Point a form at the public `/subscribe/{slug}` endpoint. CORS is open, so
client-side JavaScript can call it directly from any domain. This keeps the
reader on your page and shows the result inline:

```html
<form id="newsletter">
  <input type="email" name="email" placeholder="you@example.com" required>
  <!-- honeypot: hidden from humans; bots fill it and are silently dropped -->
  <input type="text" name="website" tabindex="-1" autocomplete="off"
         style="position:absolute;left:-9999px" aria-hidden="true">
  <button type="submit">Subscribe</button>
  <p id="newsletter-msg"></p>
</form>

<script>
document.getElementById('newsletter').addEventListener('submit', async (e) => {
  e.preventDefault();
  const form = e.target;
  const res = await fetch('https://news.example.com/subscribe/default', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email: form.email.value, website: form.website.value }),
  });
  const data = await res.json();
  document.getElementById('newsletter-msg').textContent =
    res.ok ? data.message : (data.detail || 'Something went wrong.');
  if (res.ok) form.reset();
});
</script>
```

A plain `<form action="..." method="post">` works too, but without JavaScript
the browser navigates to the endpoint's JSON response, so the reader lands on a
raw JSON page. Use the fetch version above for real visitors.

The public endpoint is hardened against abuse: a honeypot field, a per-IP rate
limit, a per-address cooldown so it cannot be used to flood a victim with
confirmation emails, and a uniform response that does not reveal who is already
subscribed. For a high-traffic or targeted form, turn on
[Cloudflare Turnstile](https://developers.cloudflare.com/turnstile/) (free): set
`MUSELETTER_TURNSTILE_SECRET`, add the Turnstile widget to your form, and the
widget's `cf-turnstile-response` token is verified on every submit.

## Sending a campaign

Campaign bodies are Markdown. Personalization tokens are `{{name}}` and
`{{email}}`, with fallbacks like `{{name|there}}`. The unsubscribe footer and
postal address are added automatically; never write your own unsubscribe link.

Sending is guarded so an automated caller cannot blast the wrong thing:

- `--dry-run` reports the audience size and a sample without sending.
- A **test send** is required before the real send (or pass `--skip-test`).
- The real send needs explicit confirmation (`--yes` for automation).
- The send is idempotent: re-running `campaigns send` for a campaign that is
  already sending does nothing.

Target a subset with tags: `campaigns create ... --tag vip` sends only to
subscribers carrying that tag. Pick a list with `--list <slug>` (a `default`
list exists out of the box).

## CLI reference

```
museletter serve                 run the server
museletter init                  bootstrap server config (.env + connect token)
museletter print-token           print a connect token for a configured server
museletter service <cmd>         install|uninstall|status (launchd/systemd)

museletter connect <token|--url> point the CLI at a server, save a profile
museletter status                server, reachability, auth, counts
museletter doctor                DNS/DKIM/DMARC/SES/config health checks
museletter health                liveness of the configured server
museletter docs                  print this README (offline, agent-readable)
museletter skill install         install the agent skill into .claude/skills

museletter lists <cmd>           list|create|show|rm
museletter subs <cmd>            add|list|rm|tag|untag|import|export
museletter tags <cmd>            list|create
museletter campaigns <cmd>       create|show|edit|preview|test|send|stats|rm
museletter suppressions <cmd>    list|add|rm
```

Run any command with `--help` for its flags, or `--json` for machine output.

## Troubleshooting

Start with `museletter doctor` (server health) and `museletter status` (can
the CLI reach and authenticate). Common cases:

- **`doctor` says the account is in the SES sandbox:** you can only email
  verified addresses until you request production access (SES console).
- **Emails land in spam / DKIM or DMARC failing:** re-check the CNAME and TXT
  records from [AWS SES setup](#aws-ses-setup-once); `doctor` reports each.
- **Bounces or complaints are not being suppressed:** the SNS webhook is not
  wired. Confirm the configuration set, the HTTPS subscription to
  `/webhooks/sns`, and that `MUSELETTER_SNS_TOPIC_ARN` matches your topic.
- **The subscribe form returns 429 under load:** you are behind a proxy and
  rate limiting sees the proxy IP as one client. Set `MUSELETTER_TRUST_PROXY=true`.
- **`campaigns send` refuses with 412:** do a test send first, or pass
  `--skip-test`.
- **`connect` reports "API key was rejected":** the token or key is stale;
  regenerate one on the server with `museletter print-token`.
- **The database:** everything is in one SQLite file (`MUSELETTER_DB_PATH`, or
  `/data/museletter.db` in the image). Copy it to back up; delete it to reset.

## Agent-first design

Museletter ships with a ready-made skill for Claude Code and any agent that
reads Markdown. Install it into a skills directory with:

```bash
museletter skill install            # ~/.claude/skills/museletter (all projects)
museletter skill install --project  # ./.claude/skills/museletter (this repo)
```

The skill's recipes cover publishing an issue, first-time SES setup, migrating
from another platform, and a periodic health check. The skill source lives at
[`src/museletter/skill/`](src/museletter/skill/).

The whole tool is built for agents: idempotency keys on mutations, dry runs,
mandatory confirm-to-send, test-send-before-send guardrails, machine-readable
JSON on every command, `doctor` for self-diagnosis, and `museletter docs` so
the full manual is available offline from the CLI itself.

## Development

```bash
uv venv && uv pip install -e ".[dev]"
.venv/bin/pytest            # tests
.venv/bin/ruff check .      # lint (--fix to autofix)
.venv/bin/ruff format .     # format
.venv/bin/ty check          # typecheck
```

All four must pass before a PR. See [AGENTS.md](AGENTS.md) for architecture and
conventions.

## License

MIT
