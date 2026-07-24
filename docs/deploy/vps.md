# Deploy on any VPS

Any $4/month box works. Docker + Caddy (for automatic HTTPS) is the least effort.

```bash
docker run -d --name museletter --restart unless-stopped \
  -p 127.0.0.1:8000:8000 \
  -v /srv/museletter:/data \
  -e MUSELETTER_API_KEY=$(openssl rand -hex 32) \
  -e MUSELETTER_BASE_URL=https://news.example.com \
  -e MUSELETTER_FROM_EMAIL=newsletter@example.com \
  -e MUSELETTER_FROM_NAME="Your Name" \
  -e MUSELETTER_POSTAL_ADDRESS="123 Main St, City, Country" \
  -e AWS_REGION=us-east-1 \
  -e AWS_ACCESS_KEY_ID=... \
  -e AWS_SECRET_ACCESS_KEY=... \
  ghcr.io/sanketsaurav/museletter:latest
```

`/etc/caddy/Caddyfile`:

```
news.example.com {
    reverse_proxy 127.0.0.1:8000
}
```

Without Docker: `pip install museletter`, export the same variables, run
`museletter serve` under systemd.

Backups: the whole state is one SQLite file. A nightly
`sqlite3 /srv/museletter/museletter.db ".backup /srv/backups/museletter-$(date +%F).db"`
in cron is a complete backup strategy.
