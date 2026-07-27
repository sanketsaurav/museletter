# Deploy on Cloudflare

Two paths, one available today:

## Today: Cloudflare Containers

Cloudflare Containers runs the standard museletter image at the edge, attached
to a Worker that routes traffic to it. This works now with the unmodified
Dockerfile - follow Cloudflare's container quickstart, mount no volume (Containers
have ephemeral disks), and point `MUSELETTER_DB_PATH` at a persisted location…
which Containers don't offer yet. **Until Cloudflare containers have durable
disks, prefer Fly/Railway/VPS** - a newsletter platform cannot run on an
ephemeral database.

## Planned: native Workers + D1

museletter is deliberately built so a native Cloudflare port stays cheap:

- all SQL is SQLite-dialect (D1-compatible),
- the send queue is a database table, not Redis/Celery (a Workers cron trigger
  can drive the same loop),
- SES is called over signed HTTPS, not SMTP (Workers cannot open SMTP sockets),
- Python Workers now run FastAPI natively.

The port needs: a D1 driver behind `db.py`, the send loop hooked to a cron
trigger instead of a background task, and `pywrangler deploy` packaging.
Contributions welcome - see the tracking issue.
