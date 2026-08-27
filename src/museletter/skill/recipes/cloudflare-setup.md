# Recipe: first-time Cloudflare Email Service setup

Run once per Museletter install that sends through Cloudflare instead of SES.
Email Sending is in public beta and needs a Workers Paid plan. Most steps are
dashboard-side; substitute the user's domain and server URL throughout.

1. **Verify the sending domain.** Have the user open the Cloudflare dashboard,
   pick the zone, and enable Email Sending (Email > Email Sending) on the
   domain or a subdomain such as `news.example.com`, then add the DNS records
   it shows (SPF + DKIM) and wait for verification. `MUSELETTER_FROM_EMAIL`
   must be on that domain.

2. **Recommend DMARC** if missing: TXT record `_dmarc.example.com` with value
   `v=DMARC1; p=none` (Gmail/Yahoo require DMARC for bulk senders).

3. **Create an API token** (My Profile > API Tokens) with **Email Sending**
   write access on the account plus **Queues** read + write (for step 4).
   Collect the account id from the dashboard sidebar.

4. **Wire delivery events back to Museletter** (critical - without this,
   bounces and complaints are never suppressed). Events arrive on a Cloudflare
   Queue that Museletter polls; nothing else to host:
   ```bash
   npx wrangler queues create museletter-email-events
   ```
   Then in the dashboard, open the queue (Storage & Databases > Queues) and
   add an **event subscription** with source **Email Sending**, scoped to the
   sending domain, for the events `message.delivered`, `message.bounced`,
   `message.complained`, `message.failed`, `message.rejected`. Note the queue
   id shown on the queue's page.

5. **Set the environment** on the server and restart it:
   ```bash
   MUSELETTER_EMAIL_PROVIDER=cloudflare
   CLOUDFLARE_ACCOUNT_ID=<account id>
   CLOUDFLARE_API_TOKEN=<token>
   MUSELETTER_CLOUDFLARE_EVENTS_QUEUE_ID=<queue id>
   ```
   (`museletter init --provider cloudflare` writes all but the token.)

6. **Verify with `museletter doctor --json`**: expect `cloudflare-credentials`,
   `cloudflare-api`, and `cloudflare-events` to be `ok`. A `warn` on
   `cloudflare-events` means the queue id is missing; a `fail` usually means
   the token lacks Queues permissions or the queue id is wrong.

7. **Send a test** (`campaigns test --to <the user's address>`) and confirm it
   arrives out of spam. Cloudflare reports known-bad addresses synchronously,
   so a bounced test shows up immediately in `campaigns stats` and
   `suppressions list`.
