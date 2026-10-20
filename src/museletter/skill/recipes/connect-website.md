# Recipe: connect a website's signup form to Museletter

Goal: the user collects newsletter subscribers on their website and wants those
signups to land in Museletter. Pick the path by whether their site has a
backend, then hand them a copy-paste snippet.

## First, ask one question

"Does your website have a backend (a server you control), or is it a static
site (plain HTML, or a static-site generator / no-code builder)?" The answer
decides the path. If unsure, static-site is the safe default.

## Path A: they have a backend (more locked down)

Request signup server-side via the authenticated confirmation API, so the API
key stays on their server and never reaches the browser. Check the server's
`/docs` for this route first: versions through 1.4.0 do not support it.

```
POST {base_url}/v1/lists/{list_slug}/subscribe
Authorization: Bearer {api_key}
Content-Type: application/json

{"email": "reader@example.com", "first_name": "Ada", "last_name": "Lovelace"}
```

- `first_name` and `last_name` are optional and stored joined as the
  subscriber's name. A single `name` also works and wins if both are sent.
  Servers through 1.5.0 reject the separate fields with 422; join them into
  `name` in the backend there. The site's form can also stay email-only:
  readers who confirm without a name can add one on the confirmation page.
- HTTP 202 returns `status: "pending_confirmation"`. New subscribers remain
  unconfirmed until they click the email link, even with server single opt-in.
  A status override is rejected. The separate `/subscribers` admin import
  endpoint never sends confirmation emails, including for unconfirmed imports.
- Pending signups share the public form's confirmation cooldown. Active,
  opted-out, and suppressed addresses get the same response without an email
  or status change. Opt-outs cannot be reopened by replaying old confirm links.
- Wire this into the site's form handler, with email validation and bot
  protection/rate limiting before calling Museletter. Authenticated signup
  does not need the public form's Turnstile token or use its per-IP rate limit.
- Handle 401 (missing/invalid API key), 404 (unsupported route or missing list),
  422 (bad input), and 502 (confirmation delivery failed, safe to retry).
  Never fall back to unauthenticated signup on an auth or version error.
- Since they are not using the public form, offer to disable the public
  endpoint entirely: set `MUSELETTER_PUBLIC_SUBSCRIBE=false` and restart. Then
  the only way to add subscribers is the authenticated API.

## Path B: static site (no backend)

Use the public `/subscribe/{slug}` endpoint. CORS is open, so browser
JavaScript can call it cross-origin. Give them this (substitute their base URL
and list slug; `default` is the built-in list):

```html
<form id="newsletter">
  <input type="email" name="email" placeholder="you@example.com" required>
  <input type="text" name="website" tabindex="-1" autocomplete="off"
         style="position:absolute;left:-9999px" aria-hidden="true">
  <button type="submit">Subscribe</button>
  <p id="newsletter-msg"></p>
</form>
<script>
document.getElementById('newsletter').addEventListener('submit', async (e) => {
  e.preventDefault();
  const f = e.target;
  const res = await fetch('{base_url}/subscribe/default', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(Object.fromEntries(new FormData(f))),
  });
  const data = await res.json();
  document.getElementById('newsletter-msg').textContent =
    res.ok ? data.message : (data.detail || 'Something went wrong.');
  if (res.ok) f.reset();
});
</script>
```

- An email-only form is enough: with double opt-in, readers who confirm
  without a name get optional first and last name fields on the confirmation
  page, so campaigns can greet them with `{{first_name}}`. If the user wants
  names up front (or runs single opt-in, which has no confirmation page), add
  `first_name` and `last_name` text inputs; the script sends every form field.
  Servers through 1.5.0 ignore these fields and have no confirmation-page form.
- The hidden `website` field is a honeypot; leave it empty. Real users never
  see it; bots fill it and are silently dropped.
- On success the response is `{"status": ..., "message": ...}` (HTTP 200);
  show `message`. Errors: 422 (bad email), 429 (rate limited), 404 (bad slug).
- The endpoint is already hardened (honeypot, per-IP rate limit, per-address
  confirmation cooldown, uniform response). If their form gets abused or is a
  spam target, add Cloudflare Turnstile: set `MUSELETTER_TURNSTILE_SECRET` on
  the server and place the Turnstile widget inside the form. The script sends
  every form field, so the widget's `cf-turnstile-response` token is verified
  on each submit without script changes.

## Verify it works

Use an address the user controls. For Path A, submit through the website's
backend or call `/v1/lists/{slug}/subscribe` with the Bearer key as above.
For Path B only:

```bash
curl -X POST {base_url}/subscribe/default \
  -H 'content-type: application/json' -d '{"email":"you@example.com"}'
```

Then check the chosen list:

```bash
museletter subs list --status unconfirmed    # the address should appear
# after clicking the confirmation link:
museletter subs list --status active
```

Remind the user that with double opt-in, a new subscriber stays `unconfirmed`
and receives nothing until they click the confirmation link.
