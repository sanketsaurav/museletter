# Changelog

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
