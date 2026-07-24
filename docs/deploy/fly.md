# Deploy on Fly.io

One shared-CPU machine and a 1GB volume is plenty for tens of thousands of
subscribers; that's roughly $3/month.

```bash
fly launch --no-deploy --name my-museletter
fly volumes create museletter_data --size 1
```

`fly.toml`:

```toml
app = "my-museletter"
primary_region = "iad"   # match your SES region for lower latency

[build]

[env]
  MUSELETTER_BASE_URL = "https://my-museletter.fly.dev"
  MUSELETTER_DB_PATH = "/data/museletter.db"
  AWS_REGION = "us-east-1"

[mounts]
  source = "museletter_data"
  destination = "/data"

[http_service]
  internal_port = 8000
  force_https = true
  auto_stop_machines = false    # the send loop must keep running mid-campaign
  min_machines_running = 1

[[vm]]
  size = "shared-cpu-1x"
```

Secrets, then deploy:

```bash
fly secrets set MUSELETTER_API_KEY=$(openssl rand -hex 32) \
  MUSELETTER_FROM_EMAIL=newsletter@example.com \
  MUSELETTER_FROM_NAME="Your Name" \
  MUSELETTER_POSTAL_ADDRESS="123 Main St, City, Country" \
  AWS_ACCESS_KEY_ID=... AWS_SECRET_ACCESS_KEY=...
fly deploy
```

Keep `auto_stop_machines = false`: sending 50k emails takes about an hour at
default SES rates, and a scaled-to-zero machine would pause mid-blast (the
ledger resumes safely on wake, but the campaign stalls until then).

Back up `/data/museletter.db` however you back up volumes; it is the entire state.
