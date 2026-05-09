---
description: Hide a drawer from recall — accepts drawer_uid prefix (like git short-SHA)
argument-hint: [drawer_uid_prefix]
---

You are forgetting a drawer on behalf of the user. The user invoked `/recall-forget` with: $ARGUMENTS

If `$ARGUMENTS` is empty, ask the user which drawer to forget (suggest they use `/recall <query>` first to find one).

Default to `--dry-run` mode for safety. Run via the Bash tool:

```bash
recall forget $ARGUMENTS --dry-run
```

Show the user what would be hidden and confirm before running without `--dry-run`.

After confirmation:

```bash
recall forget $ARGUMENTS
```

If the prefix matches multiple drawers (ambiguous), recall will print a disambiguation list — relay it to the user and ask which one they meant.
