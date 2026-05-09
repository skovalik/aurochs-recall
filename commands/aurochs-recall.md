---
description: Full recall CLI surface — invoke any recall subcommand with arguments
argument-hint: [subcommand] [args...]
---

You are running aurochs-recall on behalf of the user. The user invoked `/aurochs-recall` with these arguments: $ARGUMENTS

Run the recall CLI with the provided arguments via the Bash tool:

```bash
recall $ARGUMENTS
```

If `$ARGUMENTS` is empty, run `recall --help` to show the user the available subcommands.

If recall is not installed (you see `recall: command not found` or equivalent), inform the user they need to `pip install aurochs-recall` first.

Show the user the output. If the output indicates an error, help them interpret it and suggest a fix.
