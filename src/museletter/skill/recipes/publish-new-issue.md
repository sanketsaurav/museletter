# Recipe: announce a new blog post

Goal: the user published a post and wants subscribers notified.

1. Fetch the post (URL or file the user points you at). Write a short issue in
   Markdown: a greeting (`Hi {{name|there}},`), 2-3 sentence summary in the
   author's voice, and a link to the full post. Keep it under ~300 words unless
   the user wants the full post inlined.
2. Save it to a file, then:
   ```bash
   museletter campaigns create --subject "<post title>" --file issue.md
   ```
3. Test-send to the user (`campaigns test <id> --to <their address>`) and ask them
   to check their inbox. Iterate on the draft (`campaigns edit`) until approved.
   Note: editing clears the test-send flag; test again after edits.
4. Dry-run, report the audience count, get explicit approval, then send with `--yes`.
5. Poll `campaigns stats <id>` until `pending` is 0 and report: sent, delivered,
   bounced, complained. Flag anything unusual (bounce rate over ~2%, any complaints).
