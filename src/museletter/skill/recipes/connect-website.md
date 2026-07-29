# Recipe: connect a website's signup form to Museletter

Goal: the user collects newsletter subscribers on their website and wants those
signups to land in Museletter. Pick the path by whether their site has a
backend, then hand them a copy-paste snippet.

## First, ask one question

"Does your website have a backend (a server you control), or is it a static
site (plain HTML, or a static-site generator / no-code builder)?" The answer
decides the path. If unsure, static-site is the safe default.

## Path A: they have a backend (more locked down)

Add subscribers server-side via the authenticated admin API, so the API key
stays on their server and never reaches the browser.

```
POST {base_url}/v1/lists/{list_slug}/subscribers
Authorization: Bearer {api_key}
Content-Type: application/json

{"email": "reader@example.com", "name": "Reader", "status": "unconfirmed"}
```

- `status: "unconfirmed"` sends the double opt-in confirmation email (the
  correct default). `status: "active"` adds them directly; only use it for
  people who genuinely opted in (an imported list they consented to).
- Wire this into wherever their site already handles the form POST.
- Since they are not using the public form, offer to disable the public
  endpoint entirely: set `MUSELETTER_PUBLIC_SUBSCRIBE=false` and restart. Then
  the only way to add subscribers is the authenticated API.

## Path B: static site (no backend)

Use the public `/subscribe/{slug}` endpoint. CORS is open, so browser
JavaScript can call it cross-origin. Give them this (substitute their base URL
and list slug; `default` is the built-in list):

```html
<form id="newsletter">
  <input type="email" name="email" required>
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
    body: JSON.stringify({ email: f.email.value, website: f.website.value }),
  });
  const data = await res.json();
  document.getElementById('newsletter-msg').textContent =
    res.ok ? data.message : (data.detail || 'Something went wrong.');
  if (res.ok) f.reset();
});
</script>
```

- The hidden `website` field is a honeypot; leave it empty. Real users never
  see it; bots fill it and are silently dropped.
- On success the response is `{"status": ..., "message": ...}` (HTTP 200);
  show `message`. Errors: 422 (bad email), 429 (rate limited), 404 (bad slug).
- The endpoint is already hardened (honeypot, per-IP rate limit, per-address
  confirmation cooldown, uniform response). If their form gets abused or is a
  spam target, add Cloudflare Turnstile: set `MUSELETTER_TURNSTILE_SECRET` on
  the server, add the Turnstile widget to the form, and the widget's
  `cf-turnstile-response` token is verified on each submit.

## Verify it works (either path)

```bash
curl -X POST {base_url}/subscribe/default \
  -H 'content-type: application/json' -d '{"email":"you@example.com"}'
museletter subs list --status unconfirmed    # the address should appear
# after clicking the confirmation link:
museletter subs list --status active
```

Remind the user that with double opt-in, a new subscriber stays `unconfirmed`
and receives nothing until they click the confirmation link.
