# BYOK (Bring Your Own Key)

aurochs-recall's LLM extraction step is **BYOK**: you supply the API
key, you pay the bill, you pick the provider. The package does not
bundle a managed key, an organizational quota, or a hidden upstream
relationship. Extraction calls go directly from your machine to the
provider you configured.

## Why BYOK over plugin-managed

Three reasons, in order of how much they matter:

1. **Privacy.** Your conversations are the input to extraction. Every
   prompt is text you wrote (or text someone wrote to you). A
   plugin-managed key path would route that text through a server we
   operate, and "we don't log it" is a worse trust posture than "the
   call doesn't go through us at all."
2. **Cost transparency.** With BYOK you see the line items in your
   provider's dashboard. With plugin-managed keys you'd see whatever
   we charge — which would either be markup or break-even billing,
   neither of which beats sending the call directly.
3. **Provider choice.** You can route extraction through Anthropic,
   OpenAI, a self-hosted vLLM, an Ollama OpenAI-compatible endpoint,
   or a Cloudflare AI Gateway in front of any of those. Same package,
   different envvar — no plugin update needed when you change your
   mind.

## Configuration

Recall reads provider keys from the environment. There is no key file,
no first-run prompt, no key cache.

```bash
# Anthropic
export ANTHROPIC_API_KEY=sk-ant-...

# OpenAI
export OPENAI_API_KEY=sk-...
```

Set whichever you'll use. If both are set, the `--provider` flag (or
the chosen model) decides which gets called.

## Provider + model selection

```bash
recall extract --provider anthropic --model claude-haiku-4.5
recall extract --provider openai --model gpt-4-mini
```

Defaults are tuned for cost-per-quality on the extraction task, not
for the latest-frontier model — the workload is structured-output
entity/relationship extraction over short drawers, not deep
reasoning. The defaults:

| Provider   | Default model        | Notes                                          |
| ---------- | -------------------- | ---------------------------------------------- |
| anthropic  | `claude-haiku-4.5`   | Cheapest tier with reliable JSON output        |
| openai     | `gpt-4-mini`         | Equivalent OpenAI tier                         |

You can override with any model your account has access to.

## Cost cap controls

```bash
recall extract --budget 1.0       # stop after spending USD 1.00
recall extract --max-drawers 500  # stop after extracting from 500 drawers
recall extract --dry-run          # show estimated cost; don't call
```

The cost ledger is persisted in `extract_runs`. A run that hits
`--budget` exits cleanly; a follow-up `recall extract --resume` picks
up where it left off without re-paying for already-extracted drawers.

## Cloudflare AI Gateway

Cloudflare AI Gateway gives you logging, caching, and rate limiting in
front of either provider. To route through it, point the provider
base URL at the gateway:

```bash
# Anthropic via Cloudflare AI Gateway
export ANTHROPIC_API_KEY=sk-ant-...
export ANTHROPIC_BASE_URL="https://gateway.ai.cloudflare.com/v1/<account>/<gateway>/anthropic"

# OpenAI via Cloudflare AI Gateway
export OPENAI_API_KEY=sk-...
export OPENAI_BASE_URL="https://gateway.ai.cloudflare.com/v1/<account>/<gateway>/openai"
```

Replace `<account>` with your Cloudflare account id and `<gateway>`
with the gateway slug you configured in the Cloudflare dashboard.
**No plugin config change is needed** — recall's extraction uses the
provider SDKs directly, and the SDKs respect their own
`*_BASE_URL` envvars. The gateway's caching layer can substantially
cut bills on re-extracts of the same corpus.

## Self-hosted vLLM / Ollama

Anything that exposes an OpenAI-compatible HTTP API works. Point
`OPENAI_BASE_URL` at it and pick a model the server actually serves:

```bash
# vLLM serving Llama-3.1-70B
export OPENAI_API_KEY="not-required-but-must-be-set"
export OPENAI_BASE_URL="http://localhost:8000/v1"
recall extract --provider openai --model meta-llama/Llama-3.1-70B-Instruct

# Ollama (OpenAI-compatible endpoint at port 11434)
export OPENAI_API_KEY="ollama"
export OPENAI_BASE_URL="http://localhost:11434/v1"
recall extract --provider openai --model llama3.1
```

The OpenAI Python SDK requires `OPENAI_API_KEY` to be non-empty even
when the upstream doesn't actually check it; any non-empty string
works for self-hosted endpoints.

## What gets sent

Each extraction call sends the **drawer text** plus a system prompt
describing the entity and predicate seed lists from
`seed-entities.toml` / `seed-predicates.toml`. The model is asked to
return structured JSON: entity mentions, relationships between them,
and confidence scores.

What is **not** sent:

- The full database contents.
- Any other drawer than the one being extracted.
- Any access-log data, query history, or user identifiers.
- Telemetry or usage signals.

The call is per-drawer and stateless. If you stop extraction
mid-corpus, only drawers up to that point have been transmitted.

## What's NOT covered by BYOK

- The seed-list linker runs **without** any LLM provider configured.
  Pure pattern matching against `seed-entities.toml`. Day-one entity
  graph with no API keys needed.
- The cross-encoder rerank stage runs **locally** via
  sentence-transformers. No provider call. Default model is
  `cross-encoder/ms-marco-MiniLM-L-6-v2` and ships in the
  `[embeddings]` extra.
- BM25 search runs on the local SQLite FTS5 index. No provider call.

The point: only the extraction step calls out, and only when you ask
it to.
