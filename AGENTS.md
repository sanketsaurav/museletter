# museletter — agent guide

Headless, agent-first newsletter platform: one FastAPI + SQLite server, Amazon
SES for delivery, a typer CLI (`museletter`) as the only client. No web UI by
design. Read `README.md` for the product story; this file is for working on
the code.

## Commands

```sh
uv venv && uv pip install -e ".[dev]"   # setup
.venv/bin/pytest -q                     # tests (-k name for one; --cov=museletter for coverage)
.venv/bin/ruff check src tests          # lint (--fix to autofix)
.venv/bin/ruff format src tests         # format (--check in CI)
.venv/bin/ty check                      # typecheck
```

All four gates must pass before any commit/PR — CI (`.github/workflows/ci.yml`)
enforces them on Python 3.11–3.14.

Run locally: `museletter serve` needs `MUSELETTER_API_KEY`, `MUSELETTER_BASE_URL`,
`MUSELETTER_FROM_EMAIL` (full table in README). `museletter doctor` diagnoses a
running instance.

## Map (src/museletter/)

- `app.py` — FastAPI factory; lifespan owns the DB, link-signing secret, SES
  client, and sender task; idempotency middleware for `/v1/*` mutations
- `config.py` — env-based `Settings` (`MUSELETTER_*`); `extra` dict carries
  test injection points (`ses`, `sns_verifier`, `disable_sender`, `skip_sns_verify`)
- `db.py` — schema + helpers; the whole state is one SQLite file
- `api/admin.py` — `/v1` bearer-auth CRUD + campaign lifecycle + doctor
- `api/public.py` — subscribe/confirm/unsubscribe pages + SNS webhook
- `sender.py` — background loop draining the campaign_recipients ledger
- `ses.py` — hand-rolled SigV4 + SES v2 JSON API over httpx
- `sns.py` — SNS signature verification + bounce/complaint/delivery parsing
- `render.py` — markdown → email HTML/text; templates in `templates/*.html`
  use `string.Template` `$vars` (never str.format — CSS braces)
- `tokens.py` — HMAC tokens for confirm/unsubscribe links
- `doctor.py` — DNS/SES/config preflight checks
- `clientconf.py` — CLIENT-side config: connect-token (`ml_...`) encode/decode
  and named profiles in `~/.config/museletter/config.toml` (env overrides)
- `service.py` — launchd (macOS) / systemd `--user` (Linux) service install
- `cli.py` — typer app. Server: `serve`, `init`, `print-token`, `service *`.
  Client: `connect`, `status`, `skill install`, and the API-mirroring commands
- `skill/` — the bundled agent skill (SKILL.md + recipes), shipped in the wheel
  and installed via `museletter skill install`

## Invariants — do not weaken

- **Send guardrails**: sending requires `confirm=true`, a prior test send
  (unless `skip_test`), and supports `dry_run`; a campaign sends at most once.
  Editing a draft clears `test_sent_at` on purpose.
- **GET `/unsubscribe/...` must never mutate** — mail scanners prefetch links;
  only POST unsubscribes (RFC 8058 one-click).
- **Suppressions** are checked both at audience materialization and again at
  send time; permanent bounces/complaints auto-suppress. Never auto-remove.
- **Ledger is the source of truth**: recipient rows go pending → sent →
  delivered/bounced/complained (via SNS); suppressed/failed are terminal.
  Resume-after-crash and no-double-send both depend on row state — any sender
  change must preserve at-least-once + state-dedupe semantics.
- **Portability rules** (Cloudflare Workers/D1 is a planned second target):
  SQLite-dialect SQL only; the DB is the queue (no Redis/Celery); SES over
  signed HTTPS only (no boto3, no SMTP).
- **Tests never touch the network**: `FakeSES` from `tests/conftest.py` for
  the app, `httpx.MockTransport` for client/verifier tests, in-test generated
  certs for SNS crypto.

## Conventions

- ruff-formatted, line length 110; comments only for constraints the code
  can't show; ids are `prefix_hex` (`sub_`, `cmp_`, `list_`, `tag_`)
- API errors are FastAPI `{"detail": "..."}` with accurate status codes; CLI
  exits 1 on any HTTP error and prints `error (<code>): <detail>` to stderr
- **Commits follow [Conventional Commits](https://www.conventionalcommits.org):**
  `<type>[optional scope]: <description>`, imperative and lowercase. Types:
  `feat`, `fix`, `docs`, `refactor`, `perf`, `test`, `build`, `ci`, `chore`.
  Use a scope when it sharpens intent (`fix(ci):`, `feat(cli):`). Breaking
  changes get a `!` before the colon and a `BREAKING CHANGE:` body footer.
- No AI/tool attribution in commits, tags, or the changelog
- Timestamps are ISO-8601 UTC strings via `db.utcnow()`

## Releasing

Use the `tag-release` skill (`.claude/skills/tag-release/SKILL.md`):
bump `pyproject.toml` + `__init__.py` together, changelog, tag `vX.Y.Z` on
master → CI publishes to PyPI (trusted publishing) and GHCR.
