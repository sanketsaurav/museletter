# Recipe: migrate from another platform

Goal: move an existing audience (Mailchimp, Buttondown, Substack, ConvertKit, …)
into museletter.

1. Have the user export their subscribers as CSV from the old platform.
2. Normalize it to museletter's format — columns: `email` (required), `name`,
   `tags` (semicolon-separated), `status`. Map the platform's states:
   subscribed/active → `active`; unsubscribed → `unsubscribed`; bounced/cleaned →
   `bounced`. **Never import unsubscribed or cleaned addresses as active** — that
   violates consent and will tank deliverability.
3. Also add cleaned/bounced addresses to the suppression list:
   ```bash
   museletter suppressions add <email>
   ```
4. Import: `museletter subs import subscribers.csv --list default`, then verify
   counts against the old platform (`museletter lists show default`).
5. Remind the user to send the first campaign to a small tag first if the list
   hasn't been mailed in months — cold lists bounce more, and SES watches
   bounce rates closely.
