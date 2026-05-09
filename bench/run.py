"""Main benchmark harness.

Loads a JSONL drawer dataset, indexes it into a fresh sqlite database,
issues each query against the FTS5 retriever, and computes retrieval
quality + latency metrics. Output goes to ``bench/results/<timestamp>.json``
and ``bench/results/<timestamp>.md``.

CLI:

    python -m bench.run --dataset synthetic_chat \
        --output-format both \
        --top-k 10

Datasets are discovered by name from ``bench/datasets/``. The name
``synthetic_chat`` resolves to ``synthetic_chat.jsonl`` (drawers) and
``synthetic_chat_queries.jsonl`` (queries) — both must exist.

The harness must run in well under 2 minutes on commodity hardware.
On the bundled 600-drawer / 50-query dataset it completes in ~1 second
on a 2024 laptop SSD; the >2-minute ceiling exists for future,
larger bundled corpora.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import tempfile
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

# Allow `python -m bench.run` from repo root.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from aurochs_recall.core.db import connect  # noqa: E402
from aurochs_recall.core.migrations.runner import run_migrations  # noqa: E402
from aurochs_recall.core.retriever.fts5 import FTS5Retriever  # noqa: E402
from aurochs_recall.core.types import Drawer  # noqa: E402

DATASETS_DIR = Path(__file__).resolve().parent / "datasets"
RESULTS_DIR = Path(__file__).resolve().parent / "results"

DEFAULT_TOP_K: int = 10
# The retriever name pinned in the report. Change if/when hybrid /
# cross-encoder retrievers are wired into the bench.
RETRIEVER_NAME: str = "fts5_bm25"


# ---------------------------------------------------------------------------
# Report dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BenchReport:
    """Final result of a bench run.

    All fields are JSON-serialisable. ``timestamp`` is an ISO-8601 UTC
    string (ends in ``Z``) so the filename and the field agree.

    Fields
    ------
    dataset_name:
        Logical dataset identifier (matches ``--dataset``).
    dataset_size:
        Number of drawers loaded from the dataset.
    query_count:
        Number of queries evaluated.
    top_k:
        K used for precision@k / recall@k.
    precision_at_k:
        Mean per-query precision@k. Defined as |relevant ∩ retrieved@k| / k.
        For our synthetic dataset every query has exactly one relevant
        drawer, so this is ``1/k`` if the answer is in the top-k window
        and ``0`` if not. Averaged across queries.
    recall_at_k:
        Mean per-query recall@k. With a single relevant drawer this is
        ``1.0`` if the answer is in top-k and ``0.0`` otherwise — i.e.
        success rate.
    mrr:
        Mean Reciprocal Rank. ``1/rank`` if the expected drawer is found
        within the result list, ``0`` if not. Averaged across queries.
    index_throughput_drawers_per_sec:
        Drawers indexed per second (wall-clock, including sqlite fsync).
    query_latency_p50_ms / p95_ms / p99_ms:
        End-to-end retrieval latency percentiles in milliseconds.
    model_name:
        The retriever in use. Always ``fts5_bm25`` in T0.
    timestamp:
        ISO-8601 UTC marker for the run.
    """

    dataset_name: str
    dataset_size: int
    query_count: int
    top_k: int
    precision_at_k: float
    recall_at_k: float
    mrr: float
    index_throughput_drawers_per_sec: float
    query_latency_p50_ms: float
    query_latency_p95_ms: float
    query_latency_p99_ms: float
    model_name: str = RETRIEVER_NAME
    timestamp: str = field(
        default_factory=lambda: datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    )

    def to_json_dict(self) -> dict[str, object]:
        """Render the report as an ordered dict for JSON / Markdown output.

        Uses ``dataclasses.asdict`` for the round-trip.
        """
        return asdict(self)


# ---------------------------------------------------------------------------
# Dataset loader
# ---------------------------------------------------------------------------


def _resolve_dataset_paths(dataset_name: str) -> tuple[Path, Path]:
    """Resolve a dataset name to its drawers + queries file pair.

    Raises ``FileNotFoundError`` with a clear hint if either is missing.
    """
    drawers_path = DATASETS_DIR / f"{dataset_name}.jsonl"
    queries_path = DATASETS_DIR / f"{dataset_name}_queries.jsonl"
    if not drawers_path.exists():
        raise FileNotFoundError(
            f"Dataset drawers file not found: {drawers_path}. "
            f"Run `python -m bench.scripts.generate_synthetic` to create it."
        )
    if not queries_path.exists():
        raise FileNotFoundError(
            f"Dataset queries file not found: {queries_path}. "
            f"Run `python -m bench.scripts.generate_synthetic` to create it."
        )
    return drawers_path, queries_path


def _load_drawers_jsonl(path: Path) -> list[Drawer]:
    """Read a JSONL drawer file into ``Drawer`` instances.

    Each line is one record. We compute ``content_hash`` here (not in the
    file) so the dataset stays small and forward-compatible if the hash
    algorithm ever changes — the test harness is the source of truth, the
    dataset is the input.
    """
    drawers: list[Drawer] = []
    with path.open("r", encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, start=1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                rec = json.loads(raw)
            except json.JSONDecodeError as e:
                raise ValueError(
                    f"{path}:{lineno}: invalid JSONL line: {e}"
                ) from e
            try:
                drawers.append(
                    Drawer(
                        source=rec["source"],
                        source_id=rec["source_id"],
                        role=rec["role"],
                        content=rec["content"],
                        created_at=int(rec["created_at"]),
                        thread_id=rec.get("thread_id"),
                        position_in_thread=rec.get("position_in_thread"),
                    )
                )
            except (KeyError, TypeError, ValueError) as e:
                raise ValueError(
                    f"{path}:{lineno}: malformed drawer record: {e}"
                ) from e
    return drawers


def _load_queries_jsonl(path: Path) -> list[tuple[str, str]]:
    """Read a query JSONL into ``(query, expected_drawer_uid)`` tuples."""
    queries: list[tuple[str, str]] = []
    with path.open("r", encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, start=1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                rec = json.loads(raw)
            except json.JSONDecodeError as e:
                raise ValueError(
                    f"{path}:{lineno}: invalid JSONL line: {e}"
                ) from e
            try:
                queries.append((rec["query"], rec["expected_drawer_uid"]))
            except KeyError as e:
                raise ValueError(
                    f"{path}:{lineno}: missing required field: {e}"
                ) from e
    return queries


# ---------------------------------------------------------------------------
# Bench execution
# ---------------------------------------------------------------------------


def _index_drawers(drawers: Sequence[Drawer], db_path: Path) -> float:
    """Index drawers into a fresh DB and return drawers/sec throughput.

    We bypass the full ``run_index`` orchestrator because the bench
    drawers are pre-built ``Drawer`` instances, not files for an
    ingestor to walk. Direct INSERTs against ``drawer_meta`` + the
    FTS5 mirror match the indexer's contract closely enough for the
    bench number to be meaningful.
    """
    run_migrations(db_path)
    conn = connect(db_path)
    try:
        start = time.perf_counter()
        conn.execute("BEGIN")
        try:
            for drawer in drawers:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO drawer_meta ("
                    "  drawer_uid, source, source_id, source_path, role, "
                    "  register, thread_id, parent_uid, position_in_thread, "
                    "  branch_count, created_at, content_hash, risk_score, "
                    "  risk_score_version, hash_input_version) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        drawer.drawer_uid,
                        drawer.source,
                        drawer.source_id,
                        drawer.source_path,
                        drawer.role,
                        drawer.register,
                        drawer.thread_id,
                        drawer.parent_uid,
                        drawer.position_in_thread,
                        drawer.branch_count,
                        drawer.created_at,
                        drawer.content_hash,
                        drawer.risk_score,
                        drawer.risk_score_version,
                        drawer.hash_input_version,
                    ),
                )
                if cur.rowcount > 0:
                    rowid_row = conn.execute(
                        "SELECT rowid FROM drawer_meta WHERE drawer_uid = ?",
                        (drawer.drawer_uid,),
                    ).fetchone()
                    if rowid_row is not None:
                        conn.execute(
                            "INSERT OR IGNORE INTO drawers_fts(rowid, content) "
                            "VALUES (?, ?)",
                            (rowid_row[0], drawer.content),
                        )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        elapsed = time.perf_counter() - start
    finally:
        conn.close()

    if elapsed <= 0:
        # Floor to a small positive value so we never divide by zero on
        # tiny corpora that index faster than our timer resolution.
        elapsed = 1e-6
    return len(drawers) / elapsed


def _evaluate_queries(
    db_path: Path,
    queries: Sequence[tuple[str, str]],
    top_k: int,
) -> dict[str, float]:
    """Run each query, collect per-query metrics, return aggregates.

    Returns a dict with ``precision_at_k``, ``recall_at_k``, ``mrr``,
    plus the three latency percentile keys.
    """
    if top_k <= 0:
        raise ValueError("top_k must be positive")

    conn = connect(db_path)
    try:
        retriever = FTS5Retriever(conn=conn)

        precisions: list[float] = []
        recalls: list[float] = []
        reciprocal_ranks: list[float] = []
        latencies_ms: list[float] = []

        for query, expected_uid in queries:
            t0 = time.perf_counter()
            hits = retriever.search(query, limit=top_k)
            t1 = time.perf_counter()
            latencies_ms.append((t1 - t0) * 1000.0)

            uids = [h.drawer_uid for h in hits]
            relevant_in_topk = 1 if expected_uid in uids else 0

            # Single-relevant-doc shortcut (matches our synthetic ground
            # truth). For multi-relevant datasets, swap this for set ops.
            precisions.append(relevant_in_topk / top_k)
            recalls.append(float(relevant_in_topk))

            if relevant_in_topk:
                rank = uids.index(expected_uid) + 1
                reciprocal_ranks.append(1.0 / rank)
            else:
                reciprocal_ranks.append(0.0)
    finally:
        conn.close()

    return {
        "precision_at_k": _mean(precisions),
        "recall_at_k": _mean(recalls),
        "mrr": _mean(reciprocal_ranks),
        "query_latency_p50_ms": _percentile(latencies_ms, 50.0),
        "query_latency_p95_ms": _percentile(latencies_ms, 95.0),
        "query_latency_p99_ms": _percentile(latencies_ms, 99.0),
    }


def _mean(xs: Sequence[float]) -> float:
    return statistics.fmean(xs) if xs else 0.0


def _percentile(xs: Sequence[float], p: float) -> float:
    """Compute the p-th percentile of ``xs`` (linear interpolation).

    Uses ``statistics.quantiles`` for population-quantile estimation.
    Handles short sequences (<2 items) by returning the single value or 0.
    """
    if not xs:
        return 0.0
    if len(xs) == 1:
        return xs[0]
    sorted_xs = sorted(xs)
    # statistics.quantiles with n=100 gives 99 cut points, indexed 0..98
    # corresponding to 1%..99% percentiles. For p=50 we want index 49.
    cut_points = statistics.quantiles(sorted_xs, n=100, method="inclusive")
    idx = max(0, min(98, round(p) - 1))
    return cut_points[idx]


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------


def run_bench(
    dataset_name: str,
    *,
    db_path: Path | None = None,
    top_k: int = DEFAULT_TOP_K,
) -> BenchReport:
    """Run the bench against ``dataset_name`` and return a populated report.

    Parameters
    ----------
    dataset_name:
        Logical dataset name (matches the file stem in ``bench/datasets/``).
    db_path:
        Optional path for the bench database. ``None`` (default) creates
        a temporary database that is deleted after the run.
    top_k:
        K for precision@k / recall@k. Default 10.
    """
    drawers_path, queries_path = _resolve_dataset_paths(dataset_name)
    drawers = _load_drawers_jsonl(drawers_path)
    queries = _load_queries_jsonl(queries_path)

    # Use a per-run temp DB unless the caller asked for a specific location.
    # We use ``mkstemp`` (not ``NamedTemporaryFile``) because sqlite needs
    # to open and own the path; a context-managed temp file would close
    # itself before sqlite gets a chance, defeating the lifecycle.
    cleanup_temp = False
    if db_path is None:
        fd, tmp_name = tempfile.mkstemp(prefix="recall-bench-", suffix=".db")
        # We don't write through the fd; sqlite opens the path itself.
        import os as _os

        _os.close(fd)
        db_path = Path(tmp_name)
        cleanup_temp = True

    try:
        throughput = _index_drawers(drawers, db_path)
        metrics = _evaluate_queries(db_path, queries, top_k=top_k)
    finally:
        if cleanup_temp:
            db_path.unlink(missing_ok=True)
            # Clean up the WAL/SHM siblings sqlite leaves behind.
            wal = db_path.with_suffix(db_path.suffix + "-wal")
            shm = db_path.with_suffix(db_path.suffix + "-shm")
            wal.unlink(missing_ok=True)
            shm.unlink(missing_ok=True)

    return BenchReport(
        dataset_name=dataset_name,
        dataset_size=len(drawers),
        query_count=len(queries),
        top_k=top_k,
        precision_at_k=metrics["precision_at_k"],
        recall_at_k=metrics["recall_at_k"],
        mrr=metrics["mrr"],
        index_throughput_drawers_per_sec=throughput,
        query_latency_p50_ms=metrics["query_latency_p50_ms"],
        query_latency_p95_ms=metrics["query_latency_p95_ms"],
        query_latency_p99_ms=metrics["query_latency_p99_ms"],
    )


# ---------------------------------------------------------------------------
# Output formatters
# ---------------------------------------------------------------------------


def _render_markdown(report: BenchReport) -> str:
    """Render a human-readable Markdown report."""
    return (
        f"# bench results — {report.dataset_name}\n"
        f"\n"
        f"- **Timestamp:** {report.timestamp}\n"
        f"- **Model:** `{report.model_name}`\n"
        f"- **Dataset size:** {report.dataset_size} drawers\n"
        f"- **Queries evaluated:** {report.query_count}\n"
        f"- **k:** {report.top_k}\n"
        f"\n"
        f"## Retrieval quality\n"
        f"\n"
        f"| Metric          | Value     |\n"
        f"| --------------- | --------- |\n"
        f"| precision@{report.top_k:<3} | {report.precision_at_k:.4f} |\n"
        f"| recall@{report.top_k:<6} | {report.recall_at_k:.4f} |\n"
        f"| MRR             | {report.mrr:.4f} |\n"
        f"\n"
        f"## Performance\n"
        f"\n"
        f"| Metric                            | Value             |\n"
        f"| --------------------------------- | ----------------- |\n"
        f"| Index throughput (drawers / sec)  | {report.index_throughput_drawers_per_sec:.1f} |\n"
        f"| Query latency p50                 | {report.query_latency_p50_ms:.3f} ms |\n"
        f"| Query latency p95                 | {report.query_latency_p95_ms:.3f} ms |\n"
        f"| Query latency p99                 | {report.query_latency_p99_ms:.3f} ms |\n"
    )


def _write_outputs(
    report: BenchReport, fmt: str, results_dir: Path
) -> list[Path]:
    """Write the report to disk in the requested format(s).

    ``fmt`` is one of ``json``, ``markdown``, ``both``. Returns the list
    of written paths so the caller can echo them.
    """
    results_dir.mkdir(parents=True, exist_ok=True)
    # ``timestamp`` already has colons, which Windows refuses inside
    # filenames — strip them for filesystem safety.
    safe_ts = report.timestamp.replace(":", "")
    written: list[Path] = []

    if fmt in ("json", "both"):
        path = results_dir / f"{safe_ts}-{report.dataset_name}.json"
        path.write_text(
            json.dumps(report.to_json_dict(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        written.append(path)

    if fmt in ("markdown", "both"):
        path = results_dir / f"{safe_ts}-{report.dataset_name}.md"
        path.write_text(_render_markdown(report), encoding="utf-8")
        written.append(path)

    return written


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the aurochs-recall retrieval benchmark."
    )
    parser.add_argument(
        "--dataset",
        type=str,
        default="synthetic_chat",
        help="Dataset name (default: %(default)s)",
    )
    parser.add_argument(
        "--top-k",
        type=int,
        default=DEFAULT_TOP_K,
        help="K for precision@k / recall@k (default: %(default)s)",
    )
    parser.add_argument(
        "--output-format",
        type=str,
        choices=("json", "markdown", "both"),
        default="both",
        help="Result file format (default: %(default)s)",
    )
    parser.add_argument(
        "--results-dir",
        type=Path,
        default=RESULTS_DIR,
        help="Where to write result files (default: %(default)s)",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress the human-readable summary on stdout.",
    )
    args = parser.parse_args(argv)

    report = run_bench(args.dataset, top_k=args.top_k)
    written = _write_outputs(report, args.output_format, args.results_dir)

    # Always print the headline numbers — quietable for tests, but a
    # standalone bench invocation usually wants the summary inline.
    if not args.quiet:
        sys.stdout.write(_render_markdown(report))
        sys.stdout.write("\n")
        for path in written:
            sys.stdout.write(f"  -> {path}\n")
    return 0


__all__ = ["BenchReport", "main", "run_bench"]


if __name__ == "__main__":
    sys.exit(main())
