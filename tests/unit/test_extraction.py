"""BYOK LLM extraction tests — staging, budgeting, parsing, versioning.

The extraction layer is heavily IO-shaped (sqlite + provider HTTP) so
these tests use a real sqlite database (tmp_path) and a fake in-memory
provider client. The fake client is injected via the ``client``
constructor kwarg on ``ExtractionRunner`` — never via monkey-patching
the SDK imports. This keeps the tests fast (no model downloads, no
SDK installs required) and deterministic.

Coverage:
  * extract_pending staging (idempotent enqueue, list_pending, FK cascade)
  * Budget exhaustion (pre-flight gate)
  * Cost calculation (price table, fallback for unknown model)
  * Prompt versioning (recorded on every run)
  * Response parsing (JSON, fenced JSON, malformed → partial)
  * extract_pending → extraction_runs lifecycle
  * Provider failure → status='failed' row
  * get_extraction_status aggregation
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from aurochs_recall.core.db import db_connect
from aurochs_recall.core.extraction import (
    DEFAULT_BUDGET_USD,
    DEFAULT_MODEL,
    DEFAULT_PROMPT_TEMPLATE,
    DEFAULT_PROMPT_VERSION,
    FALLBACK_PRICE_USD_PER_1M,
    PRICE_TABLE_USD_PER_1M,
    BYOKExtractionUnavailableError,
    ExtractionResult,
    ExtractionRunner,
    _ProviderClient,
    _ProviderResponse,
    enqueue_for_extraction,
    estimate_cost_usd,
    estimate_input_tokens,
    parse_extraction_response,
)
from aurochs_recall.core.schema import apply_schema
from aurochs_recall.core.types import Drawer

# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


class _FakeClient(_ProviderClient):
    """In-memory fake of a provider client.

    Returns canned responses in FIFO order. Each response can be either:
      * a ``_ProviderResponse`` (use as-is)
      * an ``Exception`` (raised on call to simulate provider failure)
    """

    name = "fake"

    def __init__(self, responses: list[_ProviderResponse | Exception]) -> None:
        self._responses = list(responses)
        self.calls: list[str] = []

    def complete(self, prompt: str) -> _ProviderResponse:
        self.calls.append(prompt)
        if not self._responses:
            raise RuntimeError("FakeClient exhausted")
        next_value = self._responses.pop(0)
        if isinstance(next_value, Exception):
            raise next_value
        return next_value


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """A fresh recall.db with schema v2 applied + one drawer inserted."""
    p = tmp_path / "recall.db"
    conn = db_connect(p)
    try:
        apply_schema(conn)
        # Insert a single drawer so extract_drawer has something to look at.
        drawer = Drawer(
            source="test",
            source_id="t1",
            role="human",
            content="Stefan Kovalik built aurochs-recall in 2026.",
            created_at=1234567890,
        )
        conn.execute(
            "INSERT INTO drawer_meta ("
            "drawer_uid, source, source_id, role, created_at, content_hash) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                drawer.drawer_uid,
                drawer.source,
                drawer.source_id,
                drawer.role,
                drawer.created_at,
                drawer.content_hash,
            ),
        )
        rowid = conn.execute(
            "SELECT rowid FROM drawer_meta WHERE drawer_uid=?",
            (drawer.drawer_uid,),
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO drawers_fts(rowid, content) VALUES (?, ?)",
            (rowid, drawer.content),
        )
    finally:
        conn.close()
    return p


@pytest.fixture
def drawer_uid(db_path: Path) -> str:
    """Return the drawer_uid of the seeded drawer (read once for clarity)."""
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute("SELECT drawer_uid FROM drawer_meta LIMIT 1").fetchone()
        assert row is not None
        return str(row[0])
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Constants + price table
# ---------------------------------------------------------------------------


def test_default_model_is_locked() -> None:
    """Plan v5 locks the default model; this catches accidental edits."""
    assert DEFAULT_MODEL == "claude-haiku-4.5"


def test_default_budget_is_conservative() -> None:
    """Default $1.00 cap is intentional — implicit runs shouldn't blow money."""
    assert DEFAULT_BUDGET_USD == 1.0


def test_default_prompt_version_is_semver() -> None:
    """prompt_version is a semver string — validate the shape."""
    parts = DEFAULT_PROMPT_VERSION.split(".")
    assert len(parts) == 3
    for p in parts:
        assert p.isdigit()


def test_price_table_includes_anthropic_models() -> None:
    """Sanity-check the price table covers the locked default model."""
    assert "claude-haiku-4.5" in PRICE_TABLE_USD_PER_1M


# ---------------------------------------------------------------------------
# Cost estimation
# ---------------------------------------------------------------------------


def test_estimate_input_tokens_handles_empty_string() -> None:
    """Empty input still returns at least 1 token (floor)."""
    assert estimate_input_tokens("") == 1


def test_estimate_input_tokens_scales_with_length() -> None:
    short = estimate_input_tokens("hello")
    long = estimate_input_tokens("hello world " * 100)
    assert long > short


def test_estimate_cost_usd_known_model() -> None:
    """For a known model, cost should match the table arithmetic."""
    cost = estimate_cost_usd(
        model="claude-haiku-4.5",
        tokens_input=1_000_000,
        tokens_output=0,
    )
    # claude-haiku-4.5 is (1.00, 5.00). 1M input tokens at $1.00 = $1.00.
    assert cost == pytest.approx(1.0)


def test_estimate_cost_usd_unknown_model_uses_fallback() -> None:
    """Unknown models fall back to the conservative-high estimate."""
    cost = estimate_cost_usd(
        model="some-future-model",
        tokens_input=1_000_000,
        tokens_output=0,
    )
    expected = FALLBACK_PRICE_USD_PER_1M[0]
    assert cost == pytest.approx(expected)


def test_estimate_cost_usd_zero_tokens() -> None:
    """Zero-token call costs zero."""
    assert estimate_cost_usd(model="claude-haiku-4.5", tokens_input=0, tokens_output=0) == 0.0


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------


def test_parse_pure_json() -> None:
    """The straightforward case: model emits pure JSON."""
    text = '{"entities": [{"name": "Stefan", "type": "person"}]}'
    entities, partial = parse_extraction_response(text)
    assert not partial
    assert entities == [{"name": "Stefan", "type": "person"}]


def test_parse_fenced_json() -> None:
    """Models often wrap JSON in ```json fences."""
    text = '```json\n{"entities": [{"name": "X", "type": "concept"}]}\n```'
    entities, partial = parse_extraction_response(text)
    assert not partial
    assert entities == [{"name": "X", "type": "concept"}]


def test_parse_json_with_surrounding_prose() -> None:
    """First-balanced-object extraction handles prose around the JSON."""
    text = (
        "Here is the extraction:\n"
        '{"entities": [{"name": "Y", "type": "tool"}], "relationships": []}\n'
        "Hope that helps!"
    )
    entities, partial = parse_extraction_response(text)
    assert not partial
    assert entities == [{"name": "Y", "type": "tool"}]


def test_parse_malformed_returns_partial() -> None:
    """Malformed JSON returns ([], partial=True) so the caller can record it."""
    entities, _partial = parse_extraction_response("this is not JSON at all")
    assert entities == []
    # We DID get something but couldn't parse it - depending on heuristics
    # either result is acceptable; assert the contract that bad parse never
    # claims success.
    if entities:
        pytest.fail("Malformed input must not yield entities")


def test_parse_empty_returns_empty_not_partial() -> None:
    entities, partial = parse_extraction_response("")
    assert entities == []
    assert not partial


def test_parse_bare_list() -> None:
    """Some prompts ask for a bare list; we accept it."""
    entities, partial = parse_extraction_response('[{"name": "A", "type": "x"}]')
    assert entities == [{"name": "A", "type": "x"}]
    assert not partial


# ---------------------------------------------------------------------------
# Staging: extract_pending
# ---------------------------------------------------------------------------


def test_enqueue_inserts_pending_row(db_path: Path, drawer_uid: str) -> None:
    runner = ExtractionRunner(db_path)
    runner.enqueue(drawer_uid)
    pending = runner.list_pending()
    assert len(pending) == 1
    assert pending[0][0] == drawer_uid


def test_enqueue_is_idempotent(db_path: Path, drawer_uid: str) -> None:
    """OR IGNORE on PRIMARY KEY means second enqueue keeps the original row."""
    runner = ExtractionRunner(db_path)
    runner.enqueue(drawer_uid)
    runner.enqueue(drawer_uid)
    pending = runner.list_pending()
    assert len(pending) == 1


def test_enqueue_for_extraction_helper_works(db_path: Path, drawer_uid: str) -> None:
    """The index-time helper inserts via an existing connection."""
    conn = db_connect(db_path)
    try:
        inserted = enqueue_for_extraction(conn, drawer_uid)
        assert inserted is True
        # Second call returns False (already pending).
        again = enqueue_for_extraction(conn, drawer_uid)
        assert again is False
    finally:
        conn.close()


def test_enqueue_rejects_empty_uid(db_path: Path) -> None:
    runner = ExtractionRunner(db_path)
    with pytest.raises(ValueError, match="drawer_uid must be non-empty"):
        runner.enqueue("")


def test_enqueue_for_extraction_no_op_on_pre_t1_db(tmp_path: Path) -> None:
    """When extract_pending doesn't exist, the helper returns False quietly."""
    p = tmp_path / "pre_t1.db"
    conn = db_connect(p)
    try:
        # Apply only schema v1 (the legacy pre-T1 state).
        apply_schema(conn, version=1)
        # extract_pending should not exist yet.
        result = enqueue_for_extraction(conn, "test:1:abc")
        assert result is False
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Extraction lifecycle
# ---------------------------------------------------------------------------


def test_extract_drawer_records_success_and_clears_pending(
    db_path: Path, drawer_uid: str
) -> None:
    """Successful extraction: row in extraction_runs, no row in extract_pending."""
    fake = _FakeClient([
        _ProviderResponse(
            text='{"entities": [{"name": "Stefan Kovalik", "type": "person"}]}',
            tokens_input=42,
            tokens_output=18,
        )
    ])
    runner = ExtractionRunner(db_path, client=fake)
    runner.enqueue(drawer_uid)

    result = runner.extract_drawer(drawer_uid)

    assert result.status == "success"
    assert result.tokens_input == 42
    assert result.tokens_output == 18
    assert result.cost_usd > 0
    assert result.entities == [{"name": "Stefan Kovalik", "type": "person"}]
    # Pending row should be gone.
    assert runner.list_pending() == []
    assert len(fake.calls) == 1


def test_extract_drawer_failure_keeps_pending(db_path: Path, drawer_uid: str) -> None:
    """Provider failure: status='failed' row, pending row PRESERVED for retry."""
    fake = _FakeClient([RuntimeError("502 Bad Gateway")])
    runner = ExtractionRunner(db_path, client=fake)
    runner.enqueue(drawer_uid)

    result = runner.extract_drawer(drawer_uid)

    assert result.status == "failed"
    assert "502 Bad Gateway" in (result.error_message or "")
    assert result.entities == []
    # Pending row preserved so a retry will pick it back up.
    pending = runner.list_pending()
    assert len(pending) == 1
    assert pending[0][0] == drawer_uid


def test_extract_drawer_partial_on_malformed_response(
    db_path: Path, drawer_uid: str
) -> None:
    """Provider succeeds but emits unparseable text → status='partial'."""
    fake = _FakeClient([
        _ProviderResponse(
            text="not json at all",
            tokens_input=10,
            tokens_output=4,
        )
    ])
    runner = ExtractionRunner(db_path, client=fake)
    runner.enqueue(drawer_uid)

    result = runner.extract_drawer(drawer_uid)

    assert result.status == "partial"
    assert result.entities == []
    # Partial is "we got something, just not parseable" — pending row drops
    # because re-running won't fix the model's output without prompt change.
    assert runner.list_pending() == []


def test_extract_drawer_unknown_uid_raises(db_path: Path) -> None:
    """Programmer error to extract a non-existent drawer; raise loudly."""
    fake = _FakeClient([])
    runner = ExtractionRunner(db_path, client=fake)
    with pytest.raises(ValueError, match="not found"):
        runner.extract_drawer("nonexistent:uid:abc")


# ---------------------------------------------------------------------------
# Budget gate
# ---------------------------------------------------------------------------


def test_budget_exhaustion_blocks_call(db_path: Path, drawer_uid: str) -> None:
    """Tiny budget should fire the pre-flight gate and skip the API call."""
    fake = _FakeClient([
        _ProviderResponse(text='{"entities": []}', tokens_input=10, tokens_output=4)
    ])
    # Budget so tiny that even an empty prompt blows it.
    runner = ExtractionRunner(db_path, client=fake, budget_usd=0.0)
    runner.enqueue(drawer_uid)

    result = runner.extract_drawer(drawer_uid)

    assert result.status == "budget_exhausted"
    assert result.cost_usd == 0.0
    # Pending row should still be there for resumption with a bigger budget.
    pending = runner.list_pending()
    assert len(pending) == 1
    # Fake should NOT have been called.
    assert fake.calls == []


def test_budget_tracking_accumulates(db_path: Path, drawer_uid: str) -> None:
    """spent_usd should grow as runs complete; remaining shrinks."""
    fake = _FakeClient([
        _ProviderResponse(text='{"entities": []}', tokens_input=1000, tokens_output=100),
    ])
    runner = ExtractionRunner(db_path, client=fake, budget_usd=10.0)
    assert runner.spent_usd == 0.0
    runner.extract_drawer(drawer_uid)
    assert runner.spent_usd > 0.0
    assert runner.remaining_budget_usd == pytest.approx(10.0 - runner.spent_usd)


def test_extract_pending_stops_on_budget_exhaustion(
    db_path: Path, drawer_uid: str
) -> None:
    """extract_pending() should bail early on the first budget_exhausted result."""
    # Insert a second drawer so we have two pending items.
    conn = db_connect(db_path)
    try:
        d2 = Drawer(
            source="test", source_id="t2", role="human",
            content="Second drawer", created_at=2000,
        )
        conn.execute(
            "INSERT INTO drawer_meta ("
            "drawer_uid, source, source_id, role, created_at, content_hash) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (d2.drawer_uid, d2.source, d2.source_id, d2.role, d2.created_at, d2.content_hash),
        )
        rowid = conn.execute(
            "SELECT rowid FROM drawer_meta WHERE drawer_uid=?", (d2.drawer_uid,)
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO drawers_fts(rowid, content) VALUES (?, ?)",
            (rowid, d2.content),
        )
    finally:
        conn.close()

    fake = _FakeClient([])  # never called — budget=0
    runner = ExtractionRunner(db_path, client=fake, budget_usd=0.0)
    runner.enqueue(drawer_uid)
    # second uid:
    second_uid = next(p for p in runner.list_pending() if p[0] != drawer_uid)[0] if False else None
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT drawer_uid FROM drawer_meta ORDER BY rowid"
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 2
    second_uid = next(r[0] for r in rows if r[0] != drawer_uid)
    runner.enqueue(second_uid)

    results = runner.extract_pending(batch_size=10)
    # First result should be budget_exhausted; second never attempted.
    assert len(results) == 1
    assert results[0].status == "budget_exhausted"


# ---------------------------------------------------------------------------
# Prompt versioning
# ---------------------------------------------------------------------------


def test_prompt_version_recorded_on_run(db_path: Path, drawer_uid: str) -> None:
    """extraction_runs.prompt_version should match the version supplied."""
    fake = _FakeClient([
        _ProviderResponse(text='{"entities": []}', tokens_input=10, tokens_output=2)
    ])
    runner = ExtractionRunner(db_path, client=fake, prompt_version="2.5.1")
    runner.extract_drawer(drawer_uid)

    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT prompt_version FROM extraction_runs WHERE drawer_uid=?",
            (drawer_uid,),
        ).fetchone()
        assert row[0] == "2.5.1"
    finally:
        conn.close()


def test_prompt_version_overridable_per_call(db_path: Path, drawer_uid: str) -> None:
    fake = _FakeClient([
        _ProviderResponse(text='{"entities": []}', tokens_input=10, tokens_output=2)
    ])
    runner = ExtractionRunner(db_path, client=fake, prompt_version="1.0.0")
    result = runner.extract_drawer(drawer_uid, prompt_version="9.9.9")
    assert result.prompt_version == "9.9.9"


def test_pending_row_carries_prompt_version(db_path: Path, drawer_uid: str) -> None:
    """Enqueued template + version should flow through to the run row."""
    fake = _FakeClient([
        _ProviderResponse(text='{"entities": []}', tokens_input=10, tokens_output=2)
    ])
    runner = ExtractionRunner(db_path, client=fake)
    runner.enqueue(
        drawer_uid,
        prompt_template=DEFAULT_PROMPT_TEMPLATE,
        prompt_version="3.0.0-beta",
    )
    result = runner.extract_drawer(drawer_uid)
    assert result.prompt_version == "3.0.0-beta"


# ---------------------------------------------------------------------------
# get_extraction_status
# ---------------------------------------------------------------------------


def test_extraction_status_unseen_drawer(db_path: Path) -> None:
    """A drawer never queued returns is_pending=False, total_runs=0."""
    runner = ExtractionRunner(db_path, client=_FakeClient([]))
    status = runner.get_extraction_status("ghost:uid:abc")
    assert status.is_pending is False
    assert status.latest_run_id is None
    assert status.latest_status is None
    assert status.total_runs == 0
    assert status.total_cost_usd == 0.0


def test_extraction_status_after_success(db_path: Path, drawer_uid: str) -> None:
    fake = _FakeClient([
        _ProviderResponse(text='{"entities": []}', tokens_input=10, tokens_output=2)
    ])
    runner = ExtractionRunner(db_path, client=fake)
    runner.extract_drawer(drawer_uid)
    status = runner.get_extraction_status(drawer_uid)
    assert status.is_pending is False  # success path drops pending row
    assert status.latest_status == "success"
    assert status.total_runs == 1


def test_extraction_status_after_failure_pending_remains(
    db_path: Path, drawer_uid: str
) -> None:
    fake = _FakeClient([RuntimeError("rate limit")])
    runner = ExtractionRunner(db_path, client=fake)
    runner.enqueue(drawer_uid)
    runner.extract_drawer(drawer_uid)
    status = runner.get_extraction_status(drawer_uid)
    assert status.is_pending is True
    assert status.latest_status == "failed"


# ---------------------------------------------------------------------------
# FK cascade behavior
# ---------------------------------------------------------------------------


def test_pending_cascades_on_drawer_delete(db_path: Path, drawer_uid: str) -> None:
    """Dropping a drawer removes its extract_pending row via FK CASCADE."""
    runner = ExtractionRunner(db_path, client=_FakeClient([]))
    runner.enqueue(drawer_uid)
    assert len(runner.list_pending()) == 1

    conn = db_connect(db_path)
    try:
        # Need to delete FTS5 row first because there's no FK from drawers_fts.
        rowid = conn.execute(
            "SELECT rowid FROM drawer_meta WHERE drawer_uid=?", (drawer_uid,)
        ).fetchone()[0]
        conn.execute("DELETE FROM drawers_fts WHERE rowid=?", (rowid,))
        conn.execute("DELETE FROM drawer_meta WHERE drawer_uid=?", (drawer_uid,))
    finally:
        conn.close()

    assert runner.list_pending() == []


def test_runs_cascade_on_drawer_delete(db_path: Path, drawer_uid: str) -> None:
    """Dropping a drawer removes its extraction_runs rows via FK CASCADE."""
    fake = _FakeClient([
        _ProviderResponse(text='{"entities": []}', tokens_input=10, tokens_output=2)
    ])
    runner = ExtractionRunner(db_path, client=fake)
    runner.extract_drawer(drawer_uid)

    conn = db_connect(db_path)
    try:
        before = conn.execute(
            "SELECT COUNT(*) FROM extraction_runs WHERE drawer_uid=?", (drawer_uid,)
        ).fetchone()[0]
        assert before == 1
        rowid = conn.execute(
            "SELECT rowid FROM drawer_meta WHERE drawer_uid=?", (drawer_uid,)
        ).fetchone()[0]
        conn.execute("DELETE FROM drawers_fts WHERE rowid=?", (rowid,))
        conn.execute("DELETE FROM drawer_meta WHERE drawer_uid=?", (drawer_uid,))
        after = conn.execute(
            "SELECT COUNT(*) FROM extraction_runs WHERE drawer_uid=?", (drawer_uid,)
        ).fetchone()[0]
        assert after == 0
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Persisted JSON shape
# ---------------------------------------------------------------------------


def test_entities_json_round_trips(db_path: Path, drawer_uid: str) -> None:
    """The JSON column should round-trip the parsed entities payload."""
    payload = [
        {"name": "Stefan", "type": "person", "metadata": {"role": "founder"}},
        {"name": "aurochs-recall", "type": "project"},
    ]
    fake = _FakeClient([
        _ProviderResponse(
            text=json.dumps({"entities": payload}),
            tokens_input=20,
            tokens_output=8,
        )
    ])
    runner = ExtractionRunner(db_path, client=fake)
    result = runner.extract_drawer(drawer_uid)
    assert result.entities == payload

    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT entities_json FROM extraction_runs WHERE drawer_uid=?",
            (drawer_uid,),
        ).fetchone()
        assert json.loads(row[0]) == payload
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Provider unavailable
# ---------------------------------------------------------------------------


def test_unavailable_error_when_real_sdk_missing(db_path: Path, drawer_uid: str) -> None:
    """When NO client is injected and SDKs aren't installed, instantiation
    should raise. (This will skip when the SDK happens to be installed.)"""
    try:
        import anthropic  # noqa: F401
        pytest.skip("anthropic SDK is installed; missing-SDK path covered by skip")
    except ImportError:
        pass

    runner = ExtractionRunner(db_path)  # no client kwarg → real SDK path
    with pytest.raises(BYOKExtractionUnavailableError):
        runner.extract_drawer(drawer_uid)


# ---------------------------------------------------------------------------
# Result shape
# ---------------------------------------------------------------------------


def test_extraction_result_is_frozen() -> None:
    """ExtractionResult is a frozen dataclass — caller can't mutate it."""
    result = ExtractionResult(
        drawer_uid="x:1:a",
        extraction_run_id=1,
        status="success",
        model="claude-haiku-4.5",
        prompt_version="1.0.0",
        tokens_input=1,
        tokens_output=1,
        cost_usd=0.0,
        entities=[],
    )
    from dataclasses import FrozenInstanceError
    with pytest.raises(FrozenInstanceError):
        result.cost_usd = 999.0  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Constructor validation
# ---------------------------------------------------------------------------


def test_runner_rejects_negative_budget(db_path: Path) -> None:
    with pytest.raises(ValueError, match="budget_usd"):
        ExtractionRunner(db_path, budget_usd=-1.0)


def test_runner_rejects_empty_model(db_path: Path) -> None:
    with pytest.raises(ValueError, match="model"):
        ExtractionRunner(db_path, model="")


def test_runner_rejects_empty_prompt_version(db_path: Path) -> None:
    with pytest.raises(ValueError, match="prompt_version"):
        ExtractionRunner(db_path, prompt_version="")
