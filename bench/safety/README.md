# bench/safety/

Adversarial-content + benign-content evaluation for the multi-pass
risk_score scanner.

## Targets

- **<5% false-positive rate** on the benign drawer set.
- **>90% recall** on the adversarial drawer set.

These are launch targets, not aspirational. If the scanner can't hit
both at once, the threshold defaults to whatever value preserves the
benign FP rate, and adversarial recall is reported as the achievable
ceiling at that operating point.

## Corpora

Two synthetic corpora live (or will live) under this directory:

| File                  | Count | What                                                                               |
| --------------------- | ----- | ---------------------------------------------------------------------------------- |
| `adversarial.jsonl`   | 200   | Bidi/ZW unicode injections, jailbreak templates, base64-encoded prompts            |
| `benign.jsonl`        | 200   | Code blocks discussing security topics, multilingual content, technical jargon     |

Both are **synthetic**. No real user content. See
[CONTRIBUTING.md](../../CONTRIBUTING.md) for the project's "test
fixtures are synthetic" rule.

## Methodology

1. Run the scanner over each corpus.
2. Bin drawers by the resulting `risk_score`.
3. For each candidate threshold T, compute:
   - `FP_rate = |{benign : risk_score >= T}| / |benign|`
   - `recall = |{adversarial : risk_score >= T}| / |adversarial|`
4. Report Pareto frontier of (FP_rate, recall) and the chosen
   operating point.

The scoring multipliers from plan v4/v5 — bidi/ZW +25, jailbreak
template +40, code-block 0.5×, base64 image-header exemption — are
versioned through `risk_score_version` so the bench can report which
algorithm version produced each score.

## Running

(Wired up during build — placeholder.)

```bash
make bench-safety
```

## Honesty disclosures

- **Adversarial coverage is finite.** 200 examples is enough to
  detect catastrophic regressions, not enough to claim "robust
  against all jailbreaks."
- **Benign FP examples are intentionally adversarial-adjacent.** A
  benign drawer that mentions "ignore previous instructions" inside
  a code block is exactly the kind of thing that should NOT trigger
  on. If the scanner fires on those, the threshold is too low.
- **The scanner is one layer.** It runs at ingest. Operational
  defense in depth is up to you.

## TODO

- Generate the synthetic adversarial set from a public taxonomy
- Generate the synthetic benign set
- Wire up the scoring harness
- Document the version-bump procedure for the scoring algorithm
