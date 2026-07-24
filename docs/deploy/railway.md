# Deploy on Railway / Render

Both detect the Dockerfile automatically. The one thing to get right on any
PaaS: **attach a persistent volume** mounted at `/data` — the SQLite database is
the entire state, and an ephemeral filesystem loses your subscribers on redeploy.

1. Create a service from this repo (Dockerfile build).
2. Attach a volume, mount path `/data`.
3. Set the environment variables (see the README table): `MUSELETTER_API_KEY`,
   `MUSELETTER_BASE_URL` (the public URL the platform assigns), `MUSELETTER_FROM_EMAIL`,
   `MUSELETTER_FROM_NAME`, `MUSELETTER_POSTAL_ADDRESS`, `AWS_REGION`,
   `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`.
4. Disable scale-to-zero / sleeping if the platform does it by default — the
   send loop runs in-process, and a sleeping instance pauses mid-campaign.
5. `museletter doctor` against the deployed URL to verify.
