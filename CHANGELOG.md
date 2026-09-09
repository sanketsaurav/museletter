# Changelog

## Unreleased

### Features

- Authenticated website signup at `POST /v1/lists/{ref}/subscribe` accepts an
  email and optional name, always sends new readers through double opt-in,
  and works with public signup disabled. The admin import endpoint remains
  separate and does not send confirmation emails.

### Fixes

- Public and authenticated signup share a confirmation cooldown claimed
  before sending, so concurrent requests cannot send duplicate emails. A
  failed send releases its claim for a retry. Synchronous permanent bounces
  suppress the address just like campaign bounces.
- Signup no longer sends unusable confirmation emails to opted-out readers
  or reactivates them in single opt-in mode. Existing opt-outs and
  suppressions stay closed, including when an old confirmation link is used.

### Upgrade notes

- No schema changes or new required server settings. Deploy the new server
  before switching a website to the authenticated endpoint, then optionally
  set `MUSELETTER_PUBLIC_SUBSCRIBE=false`. The website backend must validate
  submissions and apply its own bot protection and rate limiting.

## v1.4.0 - 2026-09-08

### Features

- **Campaign open tracking** works with SES, Cloudflare, and custom templates.
  Signed, per-recipient pixels record unique opens, total opens, and an
  estimated open rate in `campaigns stats`, `campaigns show`, and API/JSON
  responses. Tracking is on by default; use `--no-track-opens` when creating
  or editing a draft, or set `track_opens: false` through the API. Preview,
  test, and confirmation emails have no tracking pixels. Opens measure image
  loads and can be affected by mail privacy features, image blocking, and
  caching; Museletter stores counts and timestamps, without IP addresses or
  user agents.
- **Server version and clearer CLI help.** `museletter status` now reports
  the running server's version, including in JSON output. `museletter help`
  and `--help` group commands into client and server commands.

### Fixes

- An explicit `--profile` or `-p` now uses that profile's saved URL, API key,
  and pinned list even when environment credentials are set. Unknown explicit
  profiles fail instead of silently selecting the environment's server.
- Campaign delivery reports show a cumulative sent total with delivered,
  awaiting confirmation, bounced, and complained counts beneath it. Pending,
  failed, and suppressed recipients appear separately, and small nonzero
  rates no longer round to zero. API/JSON delivery counts keep their existing
  meanings.

### Upgrade notes

- The database migrates automatically at startup, adding campaign tracking
  settings and recipient open counts and timestamps. There are no new required
  environment variables.
- Existing drafts enable open tracking on upgrade; campaigns already queued
  or sent remain untracked. Disable tracking on a draft with
  `museletter campaigns edit <id> --no-track-opens` before sending if needed.
- Allow public GET requests to `/open/<token>.gif` under
  `MUSELETTER_BASE_URL` through your reverse proxy or tunnel, and disable
  caching for this route. HEAD requests do not record opens.

## v1.3.0 - 2026-09-08

### Features

- **The "Sent with Museletter" footer line is now optional.** Every email
  footer ended with a small attribution line that neither a custom template
  nor any setting could remove. Set `MUSELETTER_ATTRIBUTION=false` on the
  server to drop it from both the HTML and plain-text footers on every render
  path: campaign sends, campaign and template test sends, campaign previews,
  and the double opt-in confirmation email. The default is `true`, so existing
  installs are unchanged (`0` and `no` also turn it off). The unsubscribe link
  and postal address are untouched, and templates still cannot strip the line
  themselves; it stays an operator decision. `museletter preview` reads the
  flag from the environment, so a local preview matches what the server sends.

### Upgrade notes

- Nothing to do: no schema changes and no new required config. The new
  `MUSELETTER_ATTRIBUTION` setting is optional and defaults to the previous
  behavior.

## v1.2.0 - 2026-08-26

### Features

- **Cloudflare Email Service is now a second sending provider.** Set
  `MUSELETTER_EMAIL_PROVIDER=cloudflare` to send through Cloudflare's Email
  Sending REST API instead of Amazon SES (which stays the default and is
  unchanged). Delivery, bounce, and complaint events arrive on a Cloudflare
  Queue that Museletter polls over HTTPS, so there is no Worker to deploy and
  no inbound webhook to expose; hard bounces and complaints auto-suppress
  exactly as they do on SES, including addresses Cloudflare rejects
  synchronously in the send response. `museletter doctor` gains a full
  Cloudflare preflight (token, Email Sending API access, events queue, and
  the queue's HTTP pull consumer), `museletter init --provider cloudflare`
  writes the matching env scaffold, and the bundled agent skill ships a
  `cloudflare-setup.md` recipe. Note: Email Sending is a Cloudflare public
  beta and needs a Workers Paid plan; treat the provider as experimental
  until Cloudflare declares it GA.
- **Optional Reply-To on all outgoing email.** Set `MUSELETTER_REPLY_TO` and
  campaign sends, test sends, and confirmation emails carry it as the
  Reply-To header on either provider. When unset, behavior is unchanged and
  replies go to the from address.

### Upgrade notes

- SES users have nothing to do: no schema changes, no new required config,
  and `ses` remains the default provider.
- Switching to Cloudflare needs a verified sending domain, an API token with
  Email Sending + Queues Edit permissions (`CLOUDFLARE_API_TOKEN`,
  `CLOUDFLARE_ACCOUNT_ID`), and a queue with an HTTP pull consumer and an
  Email Sending event subscription (`MUSELETTER_CLOUDFLARE_EVENTS_QUEUE_ID`).
  The README's "Cloudflare Email Service setup" section walks through it, and
  `doctor` verifies every link in the chain.

### Internal

- GitHub release notes are now built from the matching changelog section, and
  the Homebrew tap formula is regenerated by the release pipeline.

## v1.1.0 - 2026-08-10

### Features

- **Issue templates now live on the server**, managed entirely through the CLI
  and API, so restyling a running instance never needs filesystem access:
  `museletter templates list|create|show|edit|test|rm`, backed by a new
  `/v1/templates` endpoint group. The packaged issue template is exposed as a
  virtual `default` you can copy but never edit or delete, and
  `museletter templates test` mails a sample issue to a real inbox so you can
  judge it where it will actually be read.
- **Pick a template per list or per campaign.** A campaign's own template wins,
  then its list's default, then the built-in:
  `museletter lists edit <slug> --template mine` and
  `museletter campaigns create|edit ... --template mine` (`--template none`
  clears a campaign back to the list's choice).
- **Templates are validated before they can reach subscribers.** Every create
  and edit rejects unknown placeholders, a missing `$content` or `$footer`, or
  HTML past Gmail's clip point, and the send path re-checks the template as a
  preflight. Deleting a template a list or unsent campaign still references is
  refused, as is editing one mid-send.
- **Homebrew install on macOS**: `brew install sanketsaurav/tap/museletter`.
  The tap formula is regenerated automatically on every release.

### Upgrade notes

- The schema gains a `templates` table and a `template_id` column on `lists`
  and `campaigns`. The migration runs automatically at startup; there is
  nothing to run and no new config to set.
- Editing a template's HTML clears the test-send state of every draft that
  renders through it, on purpose: the test you approved is always the email
  that goes out. Those drafts need a fresh `campaigns test` before they can
  send.
- `MUSELETTER_TEMPLATE_DIR` still overrides the packaged templates (the
  confirmation email and public pages), but issue templates are better managed
  with `museletter templates`.

## v1.0.1 - 2026-07-29

### Fixes

- The logo now renders on the PyPI project page. The README's relative SVG
  worked on GitHub but not on PyPI (its image proxy needs an absolute URL and
  does not render SVG), so it now falls back to an absolute PNG.

## v1.0.0 - 2026-07-29

The first public release. Museletter is a headless, agent-first newsletter
engine: one container, one SQLite file, Amazon SES for delivery, run entirely
from the CLI and HTTP API.

### What's in it

- Subscribers, lists, and tags, with CSV import/export and a public double
  opt-in subscribe endpoint (honeypot, rate limits, optional Turnstile).
- Markdown campaigns rendered to a clean email, with `{{name}}` /
  `{{first_name}}` personalization and tag targeting.
- Guarded sending: dry run, a required test send, confirm-to-send, and an
  idempotent, crash-safe ledger that respects your SES rate and resumes if
  interrupted.
- One-click RFC 8058 unsubscribe on every email, and automatic
  bounce/complaint suppression through SES and SNS.
- Delivery reporting in the CLI (per-campaign funnels, per-list subscriber
  breakdowns), with `--json` on every command for agents.
- `museletter doctor` checks DNS, DKIM, DMARC, SES sandbox/quota, and config.
- Run more than one newsletter with lists (`lists use`) or servers
  (`profiles`), and restyle every surface with ejectable templates you can
  preview locally.
- A bundled agent skill, and the full manual offline via `museletter docs`.

### Getting it

`pip install museletter`, or the container at
`ghcr.io/sanketsaurav/museletter`. See the README for AWS SES setup and
deployment. The whole state is one SQLite file, so a volume or Litestream is
all you need to back it up.
