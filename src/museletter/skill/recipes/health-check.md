# Recipe: periodic health check

Run monthly, or when the user asks "how's the newsletter doing?"

1. `museletter doctor --json` - report and help fix any `fail`/`warn`
   (expired DKIM, missing DMARC, sandbox regressions, rate mismatches).
2. `museletter lists show <each list> --json` - report subscriber counts by
   status and growth since last check.
3. For campaigns since the last check (`museletter campaigns list --json`):
   compare `bounced + complained` against `recipient_count`. Over ~2% bounces or
   any complaint cluster deserves a callout.
4. `museletter suppressions list --json` - report how many addresses were
   auto-suppressed and why. Do not remove any without explicit instruction.
5. Summarize in 5 lines or fewer unless something needs action.
