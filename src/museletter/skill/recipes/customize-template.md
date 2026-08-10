# Recipe: design a custom look for issue emails

Goal: the user wants their issues styled differently (brand colors, logo, layout).

1. Start from the built-in and pull the HTML down to work on:
   ```bash
   museletter templates create <name> --from default
   museletter templates show <name> --out template.html
   ```
2. Restyle `template.html`. The contract: it is one self-contained HTML file
   using `string.Template` placeholders - `$content` (the rendered issue) and
   `$footer` (unsubscribe link + postal address) are required, `$subject` and
   `$header` (the publication name) optional. Use `$$` for a literal dollar
   sign. Email-client rules apply: inline styles, table layout, no external
   CSS/JS; keep the file well under Gmail's 102KB clip point. The server
   validates on every edit and rejects broken templates.
3. Push it back and email the user a sample issue rendered through it:
   ```bash
   museletter templates edit <name> --file template.html
   museletter templates test <name> --to <their address>
   ```
   Iterate until they approve it in their real inbox (dark mode too).
4. Wire it up: `museletter lists edit <slug> --template <name>` makes it the
   list's default; `campaigns create/edit --template <name>` pins one campaign
   (`--template default` forces the built-in, `--template none` un-pins).
5. Know the guardrails: editing a template's HTML clears the test-send state of
   drafts that render through it, so campaign test sends are required again; a
   template that is a list default or used by an unsent campaign cannot be
   deleted; `default` can never be edited or deleted.
