---
name: museletter
description: Operate a museletter newsletter server — manage subscribers and lists, draft and send campaigns, check delivery stats and account health. Use when the user asks to send a newsletter, announce a new post to subscribers, manage their mailing list, or check newsletter stats.
---

# Operating museletter

museletter is a headless newsletter platform. Everything is done through its CLI
(`pip install museletter`) or HTTP API. There is no web UI, by design: you are the interface.

## Setup

The CLI needs to know where the server is:

```bash
export MUSELETTER_URL=https://news.example.com
export MUSELETTER_API_KEY=...   # ask the user, or read from their secret store
# or persist: museletter config --url ... --api-key ...
```

Every CLI command accepts `--json` for machine-readable output. The raw API is
documented at `$MUSELETTER_URL/docs`; auth is `Authorization: Bearer $MUSELETTER_API_KEY`.

## Safety rules (non-negotiable)

1. **Never send without a dry run first.** `museletter campaigns send <id> --dry-run`
   shows the audience size and a sample. Show this to the user.
2. **Always test-send before the real send** (`campaigns test <id> --to <the user's address>`)
   and wait for the user to confirm it looks right. Only pass `--skip-test` if the
   user explicitly asks.
3. **Sends are confirmed and idempotent.** `campaigns send <id> --yes` sets an
   idempotency key, so a retried command cannot double-send. A campaign can only
   be sent once; edit a copy for resends.
4. Do not remove suppressions unless the user explicitly asks — they exist because
   an address bounced or complained, and sending to them damages deliverability.

## Core workflow: publish an issue

```bash
museletter doctor                       # is the instance healthy? fix fails first
museletter campaigns create --subject "New post: {title}" --file issue.md
museletter campaigns test cmp_xxx --to user@example.com
museletter campaigns send cmp_xxx --dry-run
museletter campaigns send cmp_xxx --yes # after the user approves
museletter campaigns stats cmp_xxx      # poll until pending reaches 0
```

Campaign bodies are Markdown. Personalization tokens: `{{name}}` and `{{email}}`,
with fallbacks like `{{name|there}}`. An unsubscribe footer is added automatically —
never add your own unsubscribe link.

Audience targeting: `--list <slug>` picks the list (default list slug: `default`);
`--tag <tag>` restricts to tagged subscribers.

## Recipes

- `recipes/publish-new-issue.md` — announce a new blog post end to end
- `recipes/aws-ses-setup.md` — first-time SES + SNS wiring (run once per install)
- `recipes/migrate-from-another-platform.md` — import from Mailchimp/Buttondown/etc.
- `recipes/health-check.md` — periodic deliverability and list hygiene review
