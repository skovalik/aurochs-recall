# Empirical receipts

This page is the audit trail behind the claims aurochs-recall makes
about itself. The framing is deliberate: in a category that ships
glossy benchmark numbers without the methodology, the only durable
trust signal is **a verifiable record of how the thing got built and
how it actually behaves**. So the receipts are kept here, in the repo,
where they don't drift from the code.

## Build receipts (T0)

T0 is the personal-MVP daily-use scope. By the time the public README
went up, the build had:

- **262 unit + integration tests passing** on the daily-use config.
- **199 MB recall.db** — the author's personal database at the time
  the public surface stabilized.
- **70,805 drawers indexed** across the full corpus (claude_code
  sessions + claude.ai conversation exports + ChatGPT exports +
  markdown vault).
- **Months in continuous daily use** as a working memory layer for
  Stefan's writing, debugging, and research before the package was
  factored out for public release.

The numbers above aren't a brag. They're the base-rate floor: the
package indexes a real-corpus-sized database and the test suite passes
on it.

## Validation trajectory (recall plan v3 → v5)

Confidence in the design got pulled up by an iterative validation
pipeline rather than by inspection. The trajectory is documented in
the recall plan history; the rough shape:

| Pass                          | Aggregate confidence | New issues surfaced | Outcome                                          |
| ----------------------------- | -------------------- | ------------------- | ------------------------------------------------ |
| Pre-validation (v1)           | 70%                  | n/a                 | baseline                                         |
| Ralph Loop pass 1             | 70% (down)           | 33                  | Architectural issues surfaced                    |
| Ralph Loop pass 2             | 80%                  | 9                   | Spec-tightening                                  |
| Blind-spots audit             | 86%                  | 6 fold-ins          | Self-discovery                                   |
| 5-agent QA pipeline           | 80% (down)           | 28 BLOCKERs + 50 MAJORs | The pipeline working as documented           |
| Plan v4 written               | 88%                  | n/a                 | All QA fixes folded                              |
| Ralph Loop pass 3             | **89%**              | 11 deltas           | **3-of-4 personas converged; v5 captures the lift** |

What this isn't: a confidence-inflation walk. Confidence dropped twice
in the trajectory — once after pass 1 surfaced architectural issues,
and again after the 5-agent QA pipeline turned up 28 BLOCKERs that
the author hadn't seen. Both drops were real; the lift back up
required actually folding fixes, not waving them away.

What this is: a record that the design has been challenged from
multiple independent angles (Ralph Loop personas, blind-spots
self-audit, 5-agent QA pipeline) before the public README went up,
and the failure modes the audits surfaced are now documented in
[failure-modes.md](failure-modes.md) rather than waiting to surprise
a downstream user.

## T0 daily-use receipts

What "daily use for months" actually looked like:

- **Authoring tasks.** The author drafts cold emails, chapter
  sections, audit reports, and pitch artifacts against recall — pull
  past phrasing on a topic, see who said what, route by recipient.
- **Debugging tasks.** Errors and stack traces are queried directly:
  recall surfaces the prior session that hit the same error and the
  fix that worked.
- **Research synthesis.** Multi-week research threads (Acme Corp
  guerrilla MVP, Wellspring Paradox, Cognograph competitive
  landscape) are indexed and queryable — the alternative was losing
  context every time a session crashed.
- **Voice model integration.** The `/stefan` voice prosthesis routes
  through recall for past-capture lookup; capture deltas land in the
  index and are searchable in the same query language as everything
  else.

The package didn't get factored into a release until the daily-use
shape was stable enough to be worth sharing. That's the prior on
"alpha but production-ready in the author's hands."

## Failure-mode taxonomy

The point of empirical receipts isn't a bug-count brag. It's a
**failure-mode taxonomy** — a record of how the system fails when it
fails, so users can plan around it. Headline categories:

1. **Schema drift** — handled by the migration runner with explicit
   `schema_version` tracking and per-migration `applied_at`. See
   [migrations.md](migrations.md).
2. **FTS rowid drift** — `recall verify --deep` checks for FTS5 rows
   without matching `drawer_meta` rows. Repair via FTS rebuild.
3. **Lockfile staleness** — Windows-hardened lockfile with
   stale-PID detection. See [concurrency.md](concurrency.md).
4. **Hash-input drift** — `hash_input_version` column tracks the
   normalization rule version. A future change to
   `normalize_whitespace()` is a controlled migration, not a silent
   drawer_uid break.
5. **BYOK provider failure** — extraction is crash-safe via
   `extract_pending`. A provider 500 mid-run is recovered by
   `recall extract --resume`.
6. **Adversarial content** — multi-pass safety scanning sanitizes
   ingest input that might contain injection attempts or PII before
   it lands in the index.

Each of these has a documented user-facing recovery path. The general
shape: recall surfaces the failure with a fix hint, the fix hint
points at the docs, and the docs describe the runtime command that
fixes it.

## Verification by runtime, not by reading

The whole point of the bench at [`bench/README.md`](../bench/README.md)
is that performance claims are testable on your own machine. If you
want to know whether recall's hybrid search is actually better than
BM25 alone on **your** corpus, run the bench against your corpus.
Numbers without methodology are vibes; the bench is the methodology.
