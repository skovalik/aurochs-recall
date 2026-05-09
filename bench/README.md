# bench/

I built the bench because the category had hyped options that didn't
hold up. Proof should be testable on your own machine.

## What this measures

The harness produces four kinds of numbers from one run:

1. **Retrieval quality** — precision@k, recall@k, mean reciprocal rank
   (MRR). Per-query: was the right drawer in the top-k window, and how
   far down the list?
2. **Indexing throughput** — drawers indexed per wall-clock second,
   including SQLite WAL fsync. The number you'd get if you `recall index`
   against your own corpus.
3. **Query latency** — end-to-end retrieval, reported as p50 / p95 / p99
   in milliseconds. Includes everything from FTS5 MATCH to row hydration.
4. **Configuration trace** — which retriever, which k, which dataset,
   which timestamp. Embedded in every report so old results are still
   readable months later.

## Datasets

The bundled, public-distribution-safe dataset is **synthetic** — see
[`datasets/README.md`](datasets/README.md) for the generation methodology.
Every byte in `datasets/` is regenerable from a seed:

```bash
python -m bench.scripts.generate_synthetic
```

Custom corpora drop into `datasets/<name>.jsonl` +
`datasets/<name>_queries.jsonl` and become accessible as `--dataset <name>`.
The schema is documented in the dataset README.

## How to run

```bash
# Default: synthetic dataset, k=10, both JSON and Markdown reports.
python -m bench.run

# A specific dataset and k.
python -m bench.run --dataset synthetic_chat --top-k 10

# JSON only (machine-readable; useful for CI diffing).
python -m bench.run --output-format json

# Markdown only (human-readable; pastes into commit messages).
python -m bench.run --output-format markdown
```

Reports land in `bench/results/<timestamp>-<dataset>.{json,md}`. The
directory is gitignored — runs are local artifacts, not commits.

## How to interpret

### Precision@k vs. recall@k

For our synthetic dataset every query has exactly **one relevant drawer**.
That collapses the formulas:

* **precision@k** = `1/k` if the answer is in top-k, `0` otherwise.
* **recall@k** = `1.0` if the answer is in top-k, `0.0` otherwise.

So **recall@k is the success rate** on the bundled dataset; **precision@k
is bounded by `1/k`** by construction (asking for ten and getting one
right is the best possible outcome). On a multi-relevant dataset the
formulas restore their usual meaning.

### MRR

`1/rank` of the expected drawer (averaged across queries; `0` for misses).
On the synthetic dataset MRR `== 1.0` means every query landed at rank 1.
**MRR < recall@k** is the signal that retrieval is finding the right thing
but ranking it below distractors.

### Latency

p50 / p95 / p99 are computed via inclusive linear interpolation. p99 on a
50-query run is sensitive to outliers; you'd expect noise around it. Run
twice and compare p50 to estimate baseline; trust p95 / p99 only when the
query count is in the hundreds.

### Indexing throughput

Drawers per wall-clock second, fsync included. Synthetic numbers will be
optimistic relative to a real corpus because the synthetic content is
short and heavily templated.

## What the bench does NOT measure

* **Generation quality.** Out of scope — the package indexes and
  retrieves, it doesn't generate. Those numbers belong to whatever
  layer consumes recall's output.
* **Cross-encoder rerank delta.** Wired in v0.2 — currently the bench
  reports `model_name = fts5_bm25` and that's the only retriever it
  exercises.
* **Operational behaviour under concurrent writers.** That's tested in
  `tests/integration/`, not here. The bench is single-process by design.
* **Adversarial-content safety.** See [`safety/`](safety/) for the
  multi-pass risk-score scanner bench (separate harness, separate goals).

## Reproducibility contract

Every result file includes:

* `timestamp` — ISO-8601 UTC.
* `model_name` — pinned to the retriever variant (`fts5_bm25` for v0.1).
* `dataset_name` + `dataset_size` + `query_count` + `top_k` — the inputs.
* The full metric set listed above.

The dataset generator is seeded; same seed in, byte-identical files out.
Re-running the bench against the same dataset on the same hardware
produces metric values that vary only on the latency dimension (run-to-run
noise).

## Why both bundled and personal-corpus?

A single number on a public benchmark is necessary but not sufficient.
Public benchmarks tell you "this package retrieves better than that
package on a corpus neither of you has seen." Your personal corpus
tells you "this package retrieves better than that package **on the
data you actually use it on**." If those two numbers disagree, the
public one is the marketing claim; the personal one is the operational
truth.

To run against your own corpus, drop a JSONL file shaped like
`datasets/synthetic_chat.jsonl` into `datasets/<your_name>.jsonl` and a
queries file shaped like `datasets/synthetic_chat_queries.jsonl` into
`datasets/<your_name>_queries.jsonl`, then `python -m bench.run --dataset
<your_name>`.

## Honesty disclosures

* **Recall@K means:** the relevance judgements come from the synthetic
  corpus's deterministic anchor labels (one anchor → one drawer), or from
  whatever labels you provide for a personal corpus. The package never
  grades itself.
* **What "p95 latency" means:** end-to-end FTS5 retrieval time inside
  the bench process, NOT the CLI startup overhead. CLI invocation time
  adds ~50-200 ms of Python startup on top.
* **What "indexing throughput" means:** raw drawer-insert rate against
  a fresh database. Real-world `recall index` adds source discovery,
  per-file mtime checks, and ingestor parsing overhead.
* **The bundled corpus is a smoke test, not a gold standard.** It's
  small (~600 drawers), the queries have unambiguous single-anchor
  ground truth, and it's English-only. Use it to detect regressions;
  use a real corpus to make ranking claims.

## Subdirectories

* [`datasets/`](datasets/) — bundled JSONL datasets + their generator
* [`scripts/`](scripts/) — helper scripts (synthetic generation, etc.)
* [`safety/`](safety/) — adversarial-content + benign-content evaluation
  for the risk-score scanner
* `results/` — output from your bench runs (gitignored)
* [`tests/`](tests/) — pytest suite for the bench harness itself
