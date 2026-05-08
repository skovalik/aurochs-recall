# aurochs-recall

**Memory architecture for your AI conversations.**

Drawers preserve the verbatim text. The index makes it instantly findable.
The knowledge graph remembers what's connected to what. Local SQLite. No
paraphrasing. Citations everywhere.

*MIT · v0.1 · public from commit 1 · Stefan Kovalik <stefan@aurochs.agency>*

**Verify, back up, restore.** Multi-pass safety scanning · Versioned
extractions · Stable citations across DB ops.

---

## What this is

A four-layer memory system that ingests your AI conversation history,
stores it verbatim, indexes it with FTS5, links its entities into a
knowledge graph, and tracks what you recall so the system can learn how
you actually use it.

- **[Concepts](concepts.md)** — the four layers explained.
- **[Install](install.md)** — `pip install aurochs-recall` and the
  optional extras.
- **[CLI](cli.md)** — every command, every flag.
- **[Privacy](privacy.md)** — explicit no-telemetry posture.
- **[Comparison](comparison.md)** — where this sits in the
  memory-tooling category.

## What this isn't

- It doesn't summarize your conversations.
- It doesn't paraphrase what you said.
- It doesn't decide what's "important" and forget the rest.
- It doesn't auto-cluster, auto-tag, or auto-anything you didn't ask for.
- It doesn't claim to "understand" your memory. It indexes it. There's a
  difference.
- It doesn't run in the cloud. The database file lives on your disk and
  you can read it with the standard SQLite CLI.
- It doesn't promise you a benchmark number. The bench is published with
  full methodology so you can run it yourself.

## 30-second quickstart

```bash
pip install aurochs-recall
recall init                          # discovers your sources, writes starter config
recall "your first query"
```

Continue with [Install](install.md) for the full setup, or
[Concepts](concepts.md) if you want the architecture overview first.
