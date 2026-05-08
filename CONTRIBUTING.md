# Contributing to aurochs-recall

Thanks for considering a contribution. This project has a few hard rules
that exist for safety reasons — please read this before your first commit.

## TL;DR

1. **All test fixtures are synthetic.** Never check in real conversation data,
   even your own.
2. **Pre-commit hooks block PII and secrets.** They are advisory in spirit
   but enforced in practice — `git commit` will fail if your staged files
   match any pattern in `.githooks/pii-rules-generic.txt` or your local
   `.pii-rules.local`.
3. **Conventional Commits** for the subject line: `feat:`, `fix:`, `docs:`,
   `test:`, `refactor:`, `chore:`, `perf:`. Body wraps at 72 columns.
4. **No code that emits outbound HTTP from `core/`** unless the destination
   is whitelisted in the CI lint config. CLI extras can hit BYOK providers;
   `core/` cannot.

## Setup

```bash
git clone https://github.com/skovalik/aurochs-recall.git
cd aurochs-recall
python -m venv .venv && source .venv/bin/activate     # or .venv\Scripts\activate on Windows
pip install -e ".[dev,docs]"
git config core.hooksPath .githooks
cp .githooks/pii-rules.example .pii-rules.local       # add your personal patterns; this file is gitignored
```

The `core.hooksPath` step wires the repo's `.githooks/` directory in as
your hook source. Without it, `git commit` won't run the PII / secret
scanners. Verify with:

```bash
git config core.hooksPath
# should print: .githooks
```

## Pre-commit hooks

Two scanners run on every staged file before a commit lands:

- **`.githooks/pre-commit-pii`** — walks staged file *contents* (not just
  diff lines) for the union of `.githooks/pii-rules-generic.txt` (public,
  generic) and `.pii-rules.local` (personal, gitignored). **Blocks the
  commit** on any hit. The matched file and pattern are printed.
- **`.githooks/pre-commit-secrets`** — gitleaks-seeded patterns (AWS, GH,
  OpenAI, Anthropic, Slack, JWT, PEM headers). Same enforcement.

Files >10MB and binary files are skipped.

If you genuinely need to bypass a hook for a one-off — say you've already
verified the match is a false positive — `--no-verify` is the documented
escape hatch. **Use it sparingly.** A bypass is logged in your local
reflog; a leak is permanent.

## Personal pattern file

`.pii-rules.local` (gitignored, `binary` per `.gitattributes`) holds
patterns specific to *your* commits — your client names, family names,
internal project codenames. The example file at
`.githooks/pii-rules.example` shows the format. Copy it to
`.pii-rules.local` and edit before your first commit.

## Test fixtures — synthetic only

Every fixture under `tests/fixtures/` is synthetic. There is **no real
conversation data** in the repo, ever. If you need a fixture that looks
like a `claude_code` jsonl session, write one from scratch. If you need
one that looks like a `chatgpt` conversations.json export, write one
from scratch. The bench corpus is the same way: public reproductions of
public benchmarks plus a template the user fills in locally.

If you find yourself wanting to "just commit this small real example,"
stop and synthesize an equivalent one instead. There is no scenario
where committing real data is the right call.

## Style

- **ruff** for lint + format. `ruff check` and `ruff format` should both
  pass cleanly.
- **mypy strict** for `core/` and `cli/`. Tests are exempted from
  `disallow_untyped_defs`.
- Docstrings: imperative mood, three-line minimum for public functions
  (one-line summary, blank, details + arg/return semantics).

## Tests

```bash
pytest                                  # full suite
pytest -m unit                          # fast unit tests only
pytest -m "not slow"                    # everything except >1s tests
pytest --cov=core --cov=cli --cov-report=term-missing
```

Pytest is configured with `filterwarnings = ["error"]` — warnings turn
into failures. If you're surfacing a real DeprecationWarning from a
dependency, file it as an issue rather than silencing it.

## Submitting

- Branch from `main`.
- One concern per PR. If you're tempted to slip in a "small unrelated
  cleanup," open a second PR.
- The PR description explains *why*, not *what*. The diff covers what.
- Public CI must be green before merge.

## Questions

Open an issue. Tag it `question` if you're unsure of the right path
forward — it's better to discuss before writing 500 lines that get
rejected.
