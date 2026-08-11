# Changelog

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
