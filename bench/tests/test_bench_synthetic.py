"""End-to-end tests for the synthetic-dataset bench harness.

These run as part of the main test suite (``pytest tests/ bench/tests``).
The synthetic dataset is small enough that a full bench fits inside a
unit test budget — we generate it on the fly into a tmp dir, run the
harness, and assert the metrics make sense.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bench.run import (
    BenchReport,
    _load_drawers_jsonl,
    _load_queries_jsonl,
    _percentile,
    _render_markdown,
    _resolve_dataset_paths,
    _write_outputs,
    run_bench,
)
from bench.scripts.generate_synthetic import (
    DEFAULT_BASE_TIMESTAMP,
    DEFAULT_SEED,
    generate_dataset,
    write_jsonl,
)

# ============================================================================
# Tiny, deterministic dataset for the harness tests
# ============================================================================


@pytest.fixture(scope="module")
def tiny_dataset_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Generate a small synthetic dataset into a per-module tmp dir.

    20 threads / 10 queries — enough for the bench to compute meaningful
    metrics, small enough to run in well under a second.
    """
    out = tmp_path_factory.mktemp("bench_tiny")
    drawers, queries = generate_dataset(
        seed=DEFAULT_SEED,
        thread_count=20,
        query_count=10,
        base_timestamp=DEFAULT_BASE_TIMESTAMP,
    )
    write_jsonl(out / "tiny.jsonl", [d.as_jsonl_dict() for d in drawers])
    write_jsonl(out / "tiny_queries.jsonl", [q.as_jsonl_dict() for q in queries])
    return out


# ============================================================================
# generate_synthetic — determinism + invariants
# ============================================================================


def test_generator_is_deterministic() -> None:
    """Same seed in must yield same drawers + same queries."""
    drawers_a, queries_a = generate_dataset(seed=DEFAULT_SEED, thread_count=10, query_count=5)
    drawers_b, queries_b = generate_dataset(seed=DEFAULT_SEED, thread_count=10, query_count=5)
    assert [d.as_jsonl_dict() for d in drawers_a] == [d.as_jsonl_dict() for d in drawers_b]
    assert [q.as_jsonl_dict() for q in queries_a] == [q.as_jsonl_dict() for q in queries_b]


def test_generator_different_seeds_diverge() -> None:
    """Different seeds should give different output (sanity check)."""
    drawers_a, _ = generate_dataset(seed=1, thread_count=10, query_count=5)
    drawers_b, _ = generate_dataset(seed=2, thread_count=10, query_count=5)
    assert [d.as_jsonl_dict() for d in drawers_a] != [d.as_jsonl_dict() for d in drawers_b]


def test_generator_anchor_is_unique_per_thread() -> None:
    """No anchor should appear in more than one thread's first drawer."""
    _drawers, queries = generate_dataset(seed=DEFAULT_SEED, thread_count=30, query_count=10)
    # Map each query's anchor to its expected_drawer_uid; every anchor must
    # appear in exactly one thread.
    seen_anchors: set[str] = set()
    for q in queries:
        assert q.anchor not in seen_anchors, f"anchor reused: {q.anchor}"
        seen_anchors.add(q.anchor)


def test_generator_each_query_resolves_to_real_drawer() -> None:
    """Every query's expected_drawer_uid must match a drawer in the corpus."""
    from aurochs_recall.core.types import compute_content_hash, compute_drawer_uid

    drawers, queries = generate_dataset(seed=DEFAULT_SEED, thread_count=20, query_count=10)
    drawer_uid_strings: set[str] = {
        compute_drawer_uid(d.source, d.source_id, compute_content_hash(d.role, d.content))
        for d in drawers
    }

    for q in queries:
        assert q.expected_drawer_uid in drawer_uid_strings, (
            f"query {q.query!r} references missing drawer {q.expected_drawer_uid}"
        )


def test_generator_rejects_query_count_over_thread_count() -> None:
    """Query count larger than thread count is unrepresentable."""
    with pytest.raises(ValueError, match="query_count"):
        generate_dataset(thread_count=5, query_count=10)


def test_generator_rejects_zero_threads() -> None:
    with pytest.raises(ValueError, match="thread_count"):
        generate_dataset(thread_count=0, query_count=0)


def test_generator_thread_lengths_in_band() -> None:
    """All threads should have 4-8 messages per the docstring."""
    drawers, _ = generate_dataset(seed=DEFAULT_SEED, thread_count=50, query_count=10)
    counts: dict[str, int] = {}
    for d in drawers:
        counts[d.thread_id] = counts.get(d.thread_id, 0) + 1
    for tid, n in counts.items():
        assert 4 <= n <= 8, f"thread {tid} has {n} drawers (expected 4-8)"


# ============================================================================
# JSONL writer — round-trip + byte stability
# ============================================================================


def test_jsonl_writer_byte_stable(tmp_path: Path) -> None:
    """Two writes of the same records should produce identical bytes."""
    drawers, _ = generate_dataset(seed=DEFAULT_SEED, thread_count=5, query_count=2)
    records = [d.as_jsonl_dict() for d in drawers]

    a = tmp_path / "a.jsonl"
    b = tmp_path / "b.jsonl"
    write_jsonl(a, records)
    write_jsonl(b, records)
    assert a.read_bytes() == b.read_bytes()


def test_jsonl_writer_uses_lf_endings(tmp_path: Path) -> None:
    """No CRLF should leak into the JSONL output (cross-platform stability)."""
    records = [{"a": 1, "b": "hello"}]
    out = tmp_path / "lf.jsonl"
    write_jsonl(out, records)
    assert b"\r" not in out.read_bytes()


# ============================================================================
# Loader contract
# ============================================================================


def test_load_drawers_round_trip(tiny_dataset_dir: Path) -> None:
    drawers = _load_drawers_jsonl(tiny_dataset_dir / "tiny.jsonl")
    assert drawers
    # Each must have a non-empty content_hash (computed lazily by Drawer).
    assert all(d.content_hash for d in drawers)


def test_load_queries_round_trip(tiny_dataset_dir: Path) -> None:
    queries = _load_queries_jsonl(tiny_dataset_dir / "tiny_queries.jsonl")
    assert queries
    assert all(q and uid for q, uid in queries)


def test_load_drawers_rejects_bad_jsonl(tmp_path: Path) -> None:
    bad = tmp_path / "bad.jsonl"
    bad.write_text("not json at all\n", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid JSONL"):
        _load_drawers_jsonl(bad)


def test_load_drawers_rejects_missing_field(tmp_path: Path) -> None:
    bad = tmp_path / "bad.jsonl"
    bad.write_text(
        json.dumps({"source": "x"}, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="malformed"):
        _load_drawers_jsonl(bad)


# ============================================================================
# Bench harness — end to end on the bundled dataset
# ============================================================================


def test_resolve_dataset_paths_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("bench.run.DATASETS_DIR", tmp_path)
    with pytest.raises(FileNotFoundError, match="Dataset drawers file"):
        _resolve_dataset_paths("nonexistent")


def test_run_bench_against_synthetic_chat(tmp_path: Path) -> None:
    """End-to-end on the bundled dataset.

    Uses the production paths (``bench/datasets/synthetic_chat.jsonl``) and
    a tmp DB. Asserts:

    * Every required metric is populated.
    * Recall@10 == 1.0 (every query resolves to its target with k=10).
    * MRR == 1.0 (queries land at rank 1 because anchors are unique).
    * Precision@10 == 1/k by construction (one relevant drawer per query).
    * Latency p50 is reasonable (<50 ms for the small bundled corpus).
    """
    db = tmp_path / "bench.db"
    report = run_bench("synthetic_chat", db_path=db, top_k=10)

    assert report.dataset_size > 0
    assert report.query_count > 0
    assert report.top_k == 10
    assert report.model_name == "fts5_bm25"
    assert report.timestamp.endswith("Z")

    # Quality on the bundled dataset must be perfect — anchor uniqueness
    # is the whole point of the synthetic design.
    assert report.recall_at_k == 1.0, (
        f"recall@10 should be 1.0 for the synthetic dataset, got {report.recall_at_k}"
    )
    assert report.mrr == 1.0, f"MRR should be 1.0, got {report.mrr}"
    assert pytest.approx(report.precision_at_k, abs=0.01) == 1.0 / report.top_k

    # Performance sanity floors — the harness must run in well under 2 mins.
    assert report.index_throughput_drawers_per_sec > 0
    assert report.query_latency_p50_ms < 50.0
    assert report.query_latency_p95_ms < 100.0


def test_run_bench_with_smaller_topk(tmp_path: Path) -> None:
    """Top-k = 1 should still hit recall@1 = 1.0 because anchors are unique."""
    db = tmp_path / "bench.db"
    report = run_bench("synthetic_chat", db_path=db, top_k=1)
    assert report.top_k == 1
    assert report.recall_at_k == 1.0


def test_run_bench_returns_dataclass(tmp_path: Path) -> None:
    db = tmp_path / "bench.db"
    report = run_bench("synthetic_chat", db_path=db, top_k=5)
    assert isinstance(report, BenchReport)
    payload = report.to_json_dict()
    # Round-trip via JSON to guarantee everything is serializable.
    rebuilt = json.loads(json.dumps(payload, sort_keys=True))
    assert rebuilt["dataset_name"] == "synthetic_chat"


def test_run_bench_writes_output_files(tmp_path: Path) -> None:
    """``--output-format both`` should land both .json and .md files."""
    db = tmp_path / "bench.db"
    report = run_bench("synthetic_chat", db_path=db, top_k=10)
    out_dir = tmp_path / "results"
    written = _write_outputs(report, "both", out_dir)
    assert len(written) == 2
    assert any(p.suffix == ".json" for p in written)
    assert any(p.suffix == ".md" for p in written)
    for p in written:
        assert p.exists()
        assert p.stat().st_size > 0


def test_render_markdown_includes_metrics() -> None:
    """The Markdown report should mention every reported metric."""
    sample = BenchReport(
        dataset_name="x",
        dataset_size=100,
        query_count=10,
        top_k=10,
        precision_at_k=0.1,
        recall_at_k=1.0,
        mrr=1.0,
        index_throughput_drawers_per_sec=1234.5,
        query_latency_p50_ms=0.42,
        query_latency_p95_ms=1.0,
        query_latency_p99_ms=2.0,
    )
    rendered = _render_markdown(sample)
    assert "precision@10" in rendered
    assert "recall@10" in rendered
    assert "MRR" in rendered
    assert "1234.5" in rendered
    assert "0.420 ms" in rendered  # p50


# ============================================================================
# Percentile helper
# ============================================================================


def test_percentile_empty_returns_zero() -> None:
    assert _percentile([], 50.0) == 0.0


def test_percentile_single_value_returns_value() -> None:
    assert _percentile([42.0], 50.0) == 42.0


def test_percentile_p50_of_uniform_sequence() -> None:
    # 100 values, 1..100 — p50 should land near the median.
    xs = [float(i) for i in range(1, 101)]
    p50 = _percentile(xs, 50.0)
    assert 49.5 <= p50 <= 51.0


def test_percentile_p99_higher_than_p50() -> None:
    xs = [float(i) for i in range(1, 101)]
    p50 = _percentile(xs, 50.0)
    p99 = _percentile(xs, 99.0)
    assert p99 > p50
