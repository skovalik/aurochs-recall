"""BYOK LLM entity extraction layer.

Plan v5 reference: extraction is a separate offline pass that lifts
entities + relationships out of drawer content using a frontier LLM.
The user supplies their own API key (BYOK); we never proxy traffic
through aurochs.agency or any other endpoint we control. SDKs respect
``ANTHROPIC_BASE_URL`` / ``OPENAI_BASE_URL`` so users can route through
gateways (Cloudflare AI Gateway, vLLM, Ollama) without code changes.

Pipeline:

    indexer       → enqueue drawer_uid into ``extract_pending``
                    (crash-safe staging — survives mid-run kill)

    recall extract → for each pending row:
        1. pre-flight token estimate
        2. budget check (running tally)
        3. provider call (anthropic | openai)
        4. parse response → entities[]
        5. INSERT extraction_runs row, DELETE pending row
           (single transaction)

The extraction_runs ledger is append-only; re-running an extraction on
the same drawer creates a NEW row rather than updating the previous one.
This preserves the audit trail when prompt versions change.

Optional dependency: ``anthropic + openai`` (the ``[rerank-llm]`` extra).
Lazy imports keep cold-start fast for users who never extract; missing
deps raise ``BYOKExtractionUnavailableError`` with an install hint.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from aurochs_recall.core.db import connect

# ---------------------------------------------------------------------------
# Constants & contracts
# ---------------------------------------------------------------------------

# Locked default in plan v5. Override per call or via RECALL_EXTRACT_MODEL.
DEFAULT_MODEL: str = "claude-haiku-4.5"

# Default per-run budget cap. Soft default — production runs should be
# explicit, but a tight default protects against accidental mass-runs.
DEFAULT_BUDGET_USD: float = 1.0

# Prompt version is the contract between this code and the prompt template
# the runner is using. Bump when the prompt changes in a way that should
# invalidate prior extractions (or when you want to trigger a re-extract).
DEFAULT_PROMPT_VERSION: str = "1.0.0"

# Default extraction prompt. Production runs should pass an explicit
# template via ``ExtractionRunner.extract_drawer(prompt=...)`` or the
# CLI ``--prompt`` flag — this default is intentionally small so unit
# tests + first-run smoke tests are deterministic.
DEFAULT_PROMPT_TEMPLATE: str = (
    "Extract named entities and key relationships from the following "
    "conversation drawer. Return JSON with shape "
    '{"entities": [{"name": str, "type": str}], '
    '"relationships": [{"subject": str, "predicate": str, "object": str}]}.\n\n'
    "Drawer content:\n{content}"
)

# Rough cost estimates ($/1M tokens). These are NOT ground truth — they
# exist to seed the budget gate. Operators can override per-call. Keep
# the table small + literal so a price change is one diff.
#
# Sources: Anthropic + OpenAI pricing pages (best-effort snapshot).
# Used only for the pre-flight gate, not for billing.
PRICE_TABLE_USD_PER_1M: dict[str, tuple[float, float]] = {
    # model_name : (input_per_1M, output_per_1M)
    "claude-haiku-4.5":         (1.00, 5.00),
    "claude-haiku-3.5":         (0.80, 4.00),
    "claude-sonnet-4.5":        (3.00, 15.00),
    "claude-opus-4.5":          (15.00, 75.00),
    "gpt-4-mini":               (0.15, 0.60),
    "gpt-4o-mini":              (0.15, 0.60),
    "gpt-4o":                   (2.50, 10.00),
}
# Fallback when a model isn't in the table. Conservative high estimate
# so the budget gate fires earlier rather than later.
FALLBACK_PRICE_USD_PER_1M: tuple[float, float] = (3.00, 15.00)

# Char→token heuristic: ~4 chars ≈ 1 token for English; multilingual content
# can be 2-3 chars/token. We use 3 as a conservative middle ground for the
# pre-flight gate.
CHARS_PER_TOKEN_ESTIMATE: float = 3.0

# Output token estimate for the pre-flight gate. The model can emit more,
# but extraction outputs are typically small structured JSON. Used only for
# budget pre-flight; actual cost is calculated from real API response.
ESTIMATED_OUTPUT_TOKENS: int = 512


ExtractionStatusLiteral = Literal["success", "partial", "failed", "budget_exhausted"]


class BYOKExtractionUnavailableError(RuntimeError):
    """Raised when neither anthropic nor openai SDKs are importable.

    Caller should hint:
        pip install aurochs-recall[rerank-llm]
    """


class BYOKConfigurationError(RuntimeError):
    """Raised when BYOK envvars are missing or the model can't be routed.

    Distinct from the unavailable error so a CLI handler can prompt for
    ``ANTHROPIC_API_KEY`` / ``OPENAI_API_KEY`` rather than instructing
    the user to install something.
    """


class BudgetExhaustedError(RuntimeError):
    """Raised internally when the running tally exceeds ``budget_usd``.

    Surfaced to callers as a row in ``extraction_runs`` with
    ``status='budget_exhausted'``, never as an unhandled exception.
    """


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ExtractionResult:
    """Outcome of a single drawer extraction.

    Mirrors the ``extraction_runs`` table 1:1 with two extras: the
    ``entities`` field is the parsed payload (not the raw JSON string),
    and ``error_message`` carries any human-readable failure detail.
    """

    drawer_uid: str
    extraction_run_id: int
    status: ExtractionStatusLiteral
    model: str
    prompt_version: str
    tokens_input: int
    tokens_output: int
    cost_usd: float
    entities: list[dict[str, Any]] = field(default_factory=list)
    error_message: str | None = None


@dataclass(frozen=True, slots=True)
class ExtractionStatus:
    """Aggregate status for a single drawer across all of its runs."""

    drawer_uid: str
    is_pending: bool
    latest_run_id: int | None
    latest_status: ExtractionStatusLiteral | None
    latest_run_at: int | None
    total_runs: int
    total_cost_usd: float


# ---------------------------------------------------------------------------
# Provider abstraction
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class _ProviderResponse:
    """Shape returned by every provider client adapter."""

    text: str
    tokens_input: int
    tokens_output: int


class _ProviderClient:
    """Abstract base for provider adapters.

    Concrete subclasses live below. We intentionally keep the surface
    tiny — a single ``complete(prompt) -> _ProviderResponse`` method —
    so adding new providers is one new subclass and one entry in
    ``_resolve_provider()``.
    """

    name: str = ""

    def complete(self, prompt: str) -> _ProviderResponse:
        raise NotImplementedError


class _AnthropicClient(_ProviderClient):
    name: str = "anthropic"

    def __init__(self, model: str) -> None:
        try:
            import anthropic
        except ImportError as exc:
            raise BYOKExtractionUnavailableError(
                "anthropic SDK not installed. "
                "Install with: pip install aurochs-recall[rerank-llm]"
            ) from exc
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise BYOKConfigurationError(
                "ANTHROPIC_API_KEY environment variable is not set."
            )
        # SDK respects ANTHROPIC_BASE_URL automatically.
        self._client = anthropic.Anthropic(api_key=api_key)
        self._model = model

    def complete(self, prompt: str) -> _ProviderResponse:
        response = self._client.messages.create(
            model=self._model,
            max_tokens=ESTIMATED_OUTPUT_TOKENS * 2,
            messages=[{"role": "user", "content": prompt}],
        )
        # Anthropic returns content as a list of blocks; we want the text.
        text_parts: list[str] = []
        for block in response.content:
            block_text = getattr(block, "text", None)
            if block_text:
                text_parts.append(block_text)
        text = "".join(text_parts)
        usage = response.usage
        return _ProviderResponse(
            text=text,
            tokens_input=int(getattr(usage, "input_tokens", 0)),
            tokens_output=int(getattr(usage, "output_tokens", 0)),
        )


class _OpenAIClient(_ProviderClient):
    name: str = "openai"

    def __init__(self, model: str) -> None:
        try:
            import openai
        except ImportError as exc:
            raise BYOKExtractionUnavailableError(
                "openai SDK not installed. "
                "Install with: pip install aurochs-recall[rerank-llm]"
            ) from exc
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise BYOKConfigurationError(
                "OPENAI_API_KEY environment variable is not set."
            )
        # SDK respects OPENAI_BASE_URL automatically.
        self._client = openai.OpenAI(api_key=api_key)
        self._model = model

    def complete(self, prompt: str) -> _ProviderResponse:
        response = self._client.chat.completions.create(
            model=self._model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=ESTIMATED_OUTPUT_TOKENS * 2,
        )
        choice = response.choices[0]
        text = choice.message.content or ""
        usage = response.usage
        return _ProviderResponse(
            text=text,
            tokens_input=int(getattr(usage, "prompt_tokens", 0)) if usage else 0,
            tokens_output=int(getattr(usage, "completion_tokens", 0)) if usage else 0,
        )


def _resolve_provider(model: str) -> str:
    """Map a model name to its provider string.

    Anthropic models start with ``claude-`` (covers haiku/sonnet/opus +
    versioned forms). Everything else routes to OpenAI by default. The
    ``RECALL_EXTRACT_PROVIDER`` envvar can force a route when a custom
    model name doesn't follow either convention (e.g. self-hosted via
    OPENAI_BASE_URL).
    """
    forced = os.environ.get("RECALL_EXTRACT_PROVIDER")
    if forced in ("anthropic", "openai"):
        return forced
    if model.lower().startswith("claude-"):
        return "anthropic"
    return "openai"


def _instantiate_client(model: str) -> _ProviderClient:
    provider = _resolve_provider(model)
    if provider == "anthropic":
        return _AnthropicClient(model=model)
    return _OpenAIClient(model=model)


# ---------------------------------------------------------------------------
# Cost estimation
# ---------------------------------------------------------------------------

def estimate_input_tokens(prompt: str) -> int:
    """Rough char/token heuristic for the budget pre-flight gate.

    Not a real tokenizer — just a structurally-conservative estimate so
    we can decline to make a call when we'd blow the budget. Real cost
    is computed from the actual API response.
    """
    return max(1, int(len(prompt) / CHARS_PER_TOKEN_ESTIMATE))


def estimate_cost_usd(
    *,
    model: str,
    tokens_input: int,
    tokens_output: int,
) -> float:
    """Compute USD cost from token counts using ``PRICE_TABLE_USD_PER_1M``.

    Returns 0.0 for free models (in_price=0 and out_price=0). Unknown
    models use ``FALLBACK_PRICE_USD_PER_1M`` — conservative-high so the
    pre-flight gate fires sooner rather than later.
    """
    in_price, out_price = PRICE_TABLE_USD_PER_1M.get(model, FALLBACK_PRICE_USD_PER_1M)
    in_cost = (tokens_input / 1_000_000.0) * in_price
    out_cost = (tokens_output / 1_000_000.0) * out_price
    return float(in_cost + out_cost)


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------

def parse_extraction_response(text: str) -> tuple[list[dict[str, Any]], bool]:
    """Parse the LLM's text response into an entities list.

    The default prompt asks for a JSON object with shape
    ``{"entities": [...], "relationships": [...]}``. We accept either:

      * Pure JSON (preferred).
      * JSON wrapped in `````json`` fences (common for chat models).
      * JSON with leading/trailing prose (extract first balanced object).

    Returns
    -------
    (entities, is_partial)
        ``entities`` is the parsed list (empty if nothing extractable).
        ``is_partial`` is True when we got SOMETHING but not in the expected
        shape — caller marks the run ``partial`` rather than ``success``.
    """
    if not text or not text.strip():
        return [], False

    candidate = text.strip()

    # Strip ```json fences if present.
    if candidate.startswith("```"):
        # Drop opening fence + optional language tag.
        first_newline = candidate.find("\n")
        if first_newline != -1:
            candidate = candidate[first_newline + 1 :]
        if candidate.endswith("```"):
            candidate = candidate[:-3]
        candidate = candidate.strip()

    # Find the first balanced JSON object/list if the text contains prose
    # surrounding it. Bare lists are allowed (some prompts ask for a list);
    # we check { and [ and use whichever appears first.
    if not (candidate.startswith("{") or candidate.startswith("[")):
        first_obj = candidate.find("{")
        first_arr = candidate.find("[")
        if first_obj == -1 and first_arr == -1:
            pass  # nothing structured — falls through to json.loads which fails
        else:
            # Pick whichever opener is earlier (or the only one present).
            if first_obj == -1:
                start, opener, closer = first_arr, "[", "]"
            elif first_arr == -1 or first_obj < first_arr:
                start, opener, closer = first_obj, "{", "}"
            else:
                start, opener, closer = first_arr, "[", "]"
            depth = 0
            end_idx = -1
            for i in range(start, len(candidate)):
                ch = candidate[i]
                if ch == opener:
                    depth += 1
                elif ch == closer:
                    depth -= 1
                    if depth == 0:
                        end_idx = i + 1
                        break
            if end_idx != -1:
                candidate = candidate[start:end_idx]

    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        # We got *something* but it didn't parse — return partial.
        return [], True

    if isinstance(parsed, dict):
        entities = parsed.get("entities", [])
        if isinstance(entities, list):
            return entities, False
        return [], True
    if isinstance(parsed, list):
        # Some prompts ask for a bare list; accept it.
        return parsed, False

    return [], True


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

class ExtractionRunner:
    """Drives the BYOK extraction pipeline against a recall database.

    Construct once per ``recall extract`` invocation; the constructor opens
    a connection (or accepts one) and verifies the schema. The actual LLM
    client is instantiated lazily on first call so a failure to pick up
    BYOK credentials doesn't crash the runner before it's needed (e.g.
    when only ``get_extraction_status`` is being called).

    Parameters
    ----------
    db_path:
        Path to recall.db. The database must already have schema v2
        (extract_pending + extraction_runs).
    model:
        LLM model identifier. Provider routing is automatic from the
        prefix — see ``_resolve_provider``.
    budget_usd:
        Hard cap on cumulative cost across this runner instance. Each
        call to ``extract_drawer`` first runs a pre-flight estimate; if
        accepting it would push the running tally over budget, the run
        records ``status='budget_exhausted'`` and skips the API call.
    prompt_version:
        Semver of the prompt template currently in use. Recorded on every
        ``extraction_runs`` row.
    """

    def __init__(
        self,
        db_path: Path | str,
        *,
        model: str = DEFAULT_MODEL,
        budget_usd: float = DEFAULT_BUDGET_USD,
        prompt_version: str = DEFAULT_PROMPT_VERSION,
        client: _ProviderClient | None = None,
    ) -> None:
        if budget_usd < 0:
            raise ValueError(f"budget_usd must be >= 0, got {budget_usd}")
        if not model:
            raise ValueError("model must be non-empty")
        if not prompt_version:
            raise ValueError("prompt_version must be non-empty")

        self.db_path: Path = Path(db_path)
        self.model: str = model
        self.budget_usd: float = float(budget_usd)
        self.prompt_version: str = prompt_version
        self._spent_usd: float = 0.0
        self._client_override: _ProviderClient | None = client
        self._client_cached: _ProviderClient | None = client

    # ------------------------------------------------------------------
    # Lifecycle helpers
    # ------------------------------------------------------------------

    def _get_client(self) -> _ProviderClient:
        if self._client_cached is not None:
            return self._client_cached
        self._client_cached = _instantiate_client(self.model)
        return self._client_cached

    @property
    def spent_usd(self) -> float:
        return self._spent_usd

    @property
    def remaining_budget_usd(self) -> float:
        return max(0.0, self.budget_usd - self._spent_usd)

    # ------------------------------------------------------------------
    # Public API — staging
    # ------------------------------------------------------------------

    def enqueue(
        self,
        drawer_uid: str,
        *,
        prompt_template: str = DEFAULT_PROMPT_TEMPLATE,
        prompt_version: str | None = None,
    ) -> None:
        """Stage ``drawer_uid`` for later extraction.

        Idempotent: calling enqueue twice on the same drawer keeps the
        original ``enqueued_at`` (we use ``INSERT OR IGNORE`` against the
        PRIMARY KEY). Pass an updated ``prompt_template`` only via a
        manual delete + re-enqueue.
        """
        if not drawer_uid:
            raise ValueError("drawer_uid must be non-empty")
        version = prompt_version or self.prompt_version
        conn = connect(self.db_path)
        try:
            conn.execute(
                "INSERT OR IGNORE INTO extract_pending "
                "(drawer_uid, enqueued_at, prompt_template, prompt_version) "
                "VALUES (?, ?, ?, ?)",
                (drawer_uid, _now(), prompt_template, version),
            )
        finally:
            conn.close()

    def list_pending(self, *, limit: int = 100) -> list[tuple[str, str, str, int]]:
        """Return up to ``limit`` pending rows in enqueue order.

        Returns
        -------
        list of (drawer_uid, prompt_template, prompt_version, enqueued_at).
        """
        conn = connect(self.db_path)
        try:
            rows = conn.execute(
                "SELECT drawer_uid, prompt_template, prompt_version, enqueued_at "
                "FROM extract_pending "
                "ORDER BY enqueued_at ASC "
                "LIMIT ?",
                (max(1, limit),),
            ).fetchall()
            return [
                (str(r["drawer_uid"]), str(r["prompt_template"]),
                 str(r["prompt_version"]), int(r["enqueued_at"]))
                for r in rows
            ]
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Public API — extraction
    # ------------------------------------------------------------------

    def extract_drawer(
        self,
        drawer_uid: str,
        prompt: str | None = None,
        *,
        prompt_version: str | None = None,
    ) -> ExtractionResult:
        """Run extraction on a single drawer.

        Workflow:
          1. Load drawer content.
          2. Render the prompt (default template if not supplied).
          3. Pre-flight token estimate + budget check.
          4. Call the provider; record the result in ``extraction_runs``.
          5. On success/partial, remove the matching ``extract_pending``
             row in the same transaction.

        Errors short-circuit through the ``extraction_runs`` ledger as
        non-success rows; the caller never sees a raw provider exception
        unless the database itself fails.
        """
        if not drawer_uid:
            raise ValueError("drawer_uid must be non-empty")

        version_to_record = prompt_version or self.prompt_version
        conn = connect(self.db_path)
        try:
            content_row = conn.execute(
                "SELECT f.content AS content "
                "FROM drawer_meta AS m "
                "JOIN drawers_fts AS f ON f.rowid = m.rowid "
                "WHERE m.drawer_uid = ?",
                (drawer_uid,),
            ).fetchone()
            if content_row is None:
                # Recording 'failed' for an unknown drawer would cascade
                # via FK on drawer_meta, so just raise — this is a
                # programmer error, not an extraction failure.
                raise ValueError(f"drawer_uid not found: {drawer_uid!r}")

            content = content_row["content"] or ""
            if prompt is None:
                # If the drawer is staged with a custom template, prefer that.
                pending = conn.execute(
                    "SELECT prompt_template, prompt_version "
                    "FROM extract_pending WHERE drawer_uid = ?",
                    (drawer_uid,),
                ).fetchone()
                if pending is not None:
                    prompt_template = str(pending["prompt_template"])
                    if prompt_version is None:
                        version_to_record = str(pending["prompt_version"])
                else:
                    prompt_template = DEFAULT_PROMPT_TEMPLATE
                # str.replace, not str.format — the templates contain literal
                # JSON braces that .format() would mis-interpret as fields.
                rendered_prompt = prompt_template.replace("{content}", content)
            else:
                # Caller passed a literal prompt; still allow {content} substitution.
                if "{content}" in prompt:
                    rendered_prompt = prompt.replace("{content}", content)
                else:
                    rendered_prompt = prompt

            return self._run_one(
                conn,
                drawer_uid=drawer_uid,
                prompt=rendered_prompt,
                prompt_version=version_to_record,
            )
        finally:
            conn.close()

    def extract_pending(self, *, batch_size: int = 50) -> list[ExtractionResult]:
        """Drain up to ``batch_size`` pending rows, oldest-first.

        Stops early when the budget is exhausted (the budget_exhausted
        result for the offending row is included in the return list so
        the caller can see exactly where the run stopped).
        """
        if batch_size <= 0:
            return []
        results: list[ExtractionResult] = []
        pending = self.list_pending(limit=batch_size)
        if not pending:
            return results

        for drawer_uid, prompt_template, prompt_version, _enqueued_at in pending:
            result = self.extract_drawer(
                drawer_uid,
                prompt_template,
                prompt_version=prompt_version,
            )
            results.append(result)
            if result.status == "budget_exhausted":
                break
        return results

    def get_extraction_status(self, drawer_uid: str) -> ExtractionStatus:
        """Aggregate the latest status across all runs for a drawer."""
        conn = connect(self.db_path)
        try:
            pending_row = conn.execute(
                "SELECT 1 FROM extract_pending WHERE drawer_uid = ?",
                (drawer_uid,),
            ).fetchone()
            is_pending = pending_row is not None

            latest = conn.execute(
                "SELECT id, status, started_at FROM extraction_runs "
                "WHERE drawer_uid = ? ORDER BY started_at DESC LIMIT 1",
                (drawer_uid,),
            ).fetchone()
            agg = conn.execute(
                "SELECT COUNT(*) AS cnt, COALESCE(SUM(cost_usd), 0.0) AS total_cost "
                "FROM extraction_runs WHERE drawer_uid = ?",
                (drawer_uid,),
            ).fetchone()

            return ExtractionStatus(
                drawer_uid=drawer_uid,
                is_pending=is_pending,
                latest_run_id=int(latest["id"]) if latest else None,
                latest_status=(
                    _coerce_status(latest["status"]) if latest else None
                ),
                latest_run_at=int(latest["started_at"]) if latest else None,
                total_runs=int(agg["cnt"]) if agg else 0,
                total_cost_usd=float(agg["total_cost"]) if agg else 0.0,
            )
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _run_one(
        self,
        conn: sqlite3.Connection,
        *,
        drawer_uid: str,
        prompt: str,
        prompt_version: str,
    ) -> ExtractionResult:
        """Execute a single extraction inside an open connection.

        Records exactly one row in ``extraction_runs`` per call. Removes
        the matching ``extract_pending`` row only on success/partial.
        """
        started_at = _now()

        # Pre-flight budget gate.
        est_input = estimate_input_tokens(prompt)
        est_output = ESTIMATED_OUTPUT_TOKENS
        est_cost = estimate_cost_usd(
            model=self.model,
            tokens_input=est_input,
            tokens_output=est_output,
        )
        if self._spent_usd + est_cost > self.budget_usd:
            run_id = _record_run(
                conn,
                drawer_uid=drawer_uid,
                started_at=started_at,
                ended_at=_now(),
                status="budget_exhausted",
                model=self.model,
                prompt_version=prompt_version,
                tokens_input=0,
                tokens_output=0,
                cost_usd=0.0,
                entities=[],
                error_message=(
                    f"pre-flight cost ${est_cost:.4f} would push spend over "
                    f"budget ${self.budget_usd:.2f} (already spent ${self._spent_usd:.4f})"
                ),
            )
            return ExtractionResult(
                drawer_uid=drawer_uid,
                extraction_run_id=run_id,
                status="budget_exhausted",
                model=self.model,
                prompt_version=prompt_version,
                tokens_input=0,
                tokens_output=0,
                cost_usd=0.0,
                entities=[],
                error_message="budget exhausted",
            )

        # Provider call.
        try:
            response = self._get_client().complete(prompt)
        except (BYOKExtractionUnavailableError, BYOKConfigurationError):
            raise  # programmer/config errors — surface immediately
        except Exception as exc:
            run_id = _record_run(
                conn,
                drawer_uid=drawer_uid,
                started_at=started_at,
                ended_at=_now(),
                status="failed",
                model=self.model,
                prompt_version=prompt_version,
                tokens_input=0,
                tokens_output=0,
                cost_usd=0.0,
                entities=[],
                error_message=f"{type(exc).__name__}: {exc}",
            )
            return ExtractionResult(
                drawer_uid=drawer_uid,
                extraction_run_id=run_id,
                status="failed",
                model=self.model,
                prompt_version=prompt_version,
                tokens_input=0,
                tokens_output=0,
                cost_usd=0.0,
                entities=[],
                error_message=f"{type(exc).__name__}: {exc}",
            )

        cost = estimate_cost_usd(
            model=self.model,
            tokens_input=response.tokens_input,
            tokens_output=response.tokens_output,
        )
        self._spent_usd += cost

        entities, is_partial = parse_extraction_response(response.text)
        status: ExtractionStatusLiteral = "partial" if is_partial else "success"

        run_id = _record_run(
            conn,
            drawer_uid=drawer_uid,
            started_at=started_at,
            ended_at=_now(),
            status=status,
            model=self.model,
            prompt_version=prompt_version,
            tokens_input=response.tokens_input,
            tokens_output=response.tokens_output,
            cost_usd=cost,
            entities=entities,
            error_message=None,
        )

        # On success/partial we drop the pending row; the run is the new
        # source of truth. On failure we leave it for retry.
        conn.execute(
            "DELETE FROM extract_pending WHERE drawer_uid = ?", (drawer_uid,)
        )

        return ExtractionResult(
            drawer_uid=drawer_uid,
            extraction_run_id=run_id,
            status=status,
            model=self.model,
            prompt_version=prompt_version,
            tokens_input=response.tokens_input,
            tokens_output=response.tokens_output,
            cost_usd=cost,
            entities=entities,
            error_message=None,
        )


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _now() -> int:
    return int(time.time())


def _coerce_status(value: object) -> ExtractionStatusLiteral:
    s = str(value)
    if s in ("success", "partial", "failed", "budget_exhausted"):
        return s  # type: ignore[return-value]
    # Unknown legacy value — treat as failed for the aggregate view.
    return "failed"


def _record_run(
    conn: sqlite3.Connection,
    *,
    drawer_uid: str,
    started_at: int,
    ended_at: int,
    status: ExtractionStatusLiteral,
    model: str,
    prompt_version: str,
    tokens_input: int,
    tokens_output: int,
    cost_usd: float,
    entities: list[dict[str, Any]],
    error_message: str | None,
) -> int:
    """Insert one ``extraction_runs`` row; return the inserted PK."""
    cur = conn.execute(
        "INSERT INTO extraction_runs ("
        "drawer_uid, started_at, ended_at, status, model, prompt_version, "
        "tokens_input, tokens_output, cost_usd, entities_json, error_message"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            drawer_uid,
            started_at,
            ended_at,
            status,
            model,
            prompt_version,
            tokens_input,
            tokens_output,
            cost_usd,
            json.dumps(entities, ensure_ascii=False),
            error_message,
        ),
    )
    rowid = cur.lastrowid
    if rowid is None:  # pragma: no cover — sqlite always returns rowid here
        raise RuntimeError("extraction_runs INSERT did not yield a rowid")
    return int(rowid)


# ---------------------------------------------------------------------------
# Index-time hook
# ---------------------------------------------------------------------------

def enqueue_for_extraction(
    conn: sqlite3.Connection,
    drawer_uid: str,
    *,
    prompt_template: str = DEFAULT_PROMPT_TEMPLATE,
    prompt_version: str = DEFAULT_PROMPT_VERSION,
) -> bool:
    """Insert a row into ``extract_pending`` if missing.

    This is the index-time hook ``index.py`` calls when a fresh drawer is
    inserted. It uses the caller's already-open connection so the enqueue
    is part of the same transaction as the drawer insert (when caller
    wraps it in BEGIN).

    Returns
    -------
    True if a row was inserted, False if one already existed (or the table
    is missing on a pre-T1 schema).
    """
    if not drawer_uid:
        return False
    try:
        cur = conn.execute(
            "INSERT OR IGNORE INTO extract_pending "
            "(drawer_uid, enqueued_at, prompt_template, prompt_version) "
            "VALUES (?, ?, ?, ?)",
            (drawer_uid, _now(), prompt_template, prompt_version),
        )
    except sqlite3.OperationalError:
        # Pre-T1 DB without extract_pending: graceful no-op.
        return False
    return bool(cur.rowcount)


__all__ = [
    "DEFAULT_BUDGET_USD",
    "DEFAULT_MODEL",
    "DEFAULT_PROMPT_TEMPLATE",
    "DEFAULT_PROMPT_VERSION",
    "FALLBACK_PRICE_USD_PER_1M",
    "PRICE_TABLE_USD_PER_1M",
    "BYOKConfigurationError",
    "BYOKExtractionUnavailableError",
    "BudgetExhaustedError",
    "ExtractionResult",
    "ExtractionRunner",
    "ExtractionStatus",
    "enqueue_for_extraction",
    "estimate_cost_usd",
    "estimate_input_tokens",
    "parse_extraction_response",
]
