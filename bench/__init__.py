"""Reproducible benchmark harness for aurochs-recall.

The bench is published with full methodology so users can run it on
their own machines. See ``bench/README.md`` for the rationale and
``python -m bench.run --help`` for invocation.
"""

from __future__ import annotations

__all__ = ["BenchReport", "run_bench"]


def __getattr__(name: str) -> object:
    # Lazy import: ``import bench`` should be cheap; pulling in run.py
    # eagerly would force the whole retriever stack at import time.
    if name in ("BenchReport", "run_bench"):
        from bench.run import BenchReport, run_bench

        return {"BenchReport": BenchReport, "run_bench": run_bench}[name]
    raise AttributeError(f"module 'bench' has no attribute {name!r}")
