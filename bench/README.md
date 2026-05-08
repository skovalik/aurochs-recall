# bench/

I built the bench because the category had hyped options that didn't
hold up. Proof should be testable on your own machine.

## What's in here

- **Public reproduction** — a runnable copy of LongMemEval (or whichever
  public benchmark this package ships against by version) with the
  scoring scripts, the prompt files, and the harness wired up.
- **Personal corpus eval** — a template you fill in with your own
  conversation corpus. The scoring is documented; the corpus stays on
  your disk.
- **Safety bench** — see [`safety/`](safety/). Adversarial-content
  recall + benign-content false-positive measurement.

## Why both

A single number on a public benchmark is necessary but not sufficient.
Public benchmarks tell you "this package retrieves better than that
package on a corpus neither of you has seen." Your personal corpus
tells you "this package retrieves better than that package **on the
data you actually use it on**." If those two numbers disagree, the
public one is the marketing claim; the personal one is the operational
truth. The bench is wired up so you can produce both.

## Running

(Wired up during build — placeholder.)

```bash
# Public reproduction
make bench-public

# Personal corpus
make bench-personal CORPUS=/path/to/your/corpus

# Safety
make bench-safety
```

## Scoring

(Wired up during build — placeholder.)

Each bench reports:

- **Recall@K** for K in {1, 5, 10}
- **MRR** (mean reciprocal rank)
- **nDCG@10**
- **Mean retrieval latency** (p50, p95, p99 in milliseconds)

The harness emits both human-readable and JSON-Lines output so you can
diff results across runs.

## Honesty disclosures

- **What "Recall@K" means here:** the relevance judgements come from the
  public benchmark's gold labels (public reproduction) or from your
  manual labelling (personal corpus). The package never grades itself.
- **What gets measured:** retrieval. Generation quality is downstream
  and not in scope for this bench.
- **What "p95 latency" means:** end-to-end CLI invocation time, not
  just the SQL query time. This is what you experience.
- **What's not measured:** anything cloud-vendor-backed. The bench is
  for tools you can run on your own machine.

## TODO

- Wire up the public benchmark fetcher
- Synthesize a small public corpus the personal-eval template can demo
  against
- Define the JSON-Lines output schema
- Add a bench-comparison utility for diffing two runs
