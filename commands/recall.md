---
description: Search your recall memory — shortcut for `recall search`
argument-hint: [query]
---

You are running a recall search on behalf of the user. The user invoked `/recall` with these arguments: $ARGUMENTS

If `$ARGUMENTS` is empty, prompt the user for a query, then proceed once they provide one.

Otherwise, run via the Bash tool:

```bash
recall search "$ARGUMENTS"
```

Show the user the top results with their `drawer_uid`s and snippets. If a result looks particularly relevant, ask the user if they want to fetch the full drawer (use `recall drawer <uid>`).
