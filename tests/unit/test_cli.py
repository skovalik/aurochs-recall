"""CLI argparse tests — subcommand dispatch, init wizard, sources.toml round-trip."""
from __future__ import annotations

import io
import json
import re
import sys
from pathlib import Path

import pytest

from cli.main import build_parser, main
from core.sources_config import (
    SourceEntry,
    detect_candidate_sources,
    discover_config_path,
    load_sources_config,
    render_starter_toml,
)


# --------------------------------------------------------------------------
# Parser dispatch
# --------------------------------------------------------------------------


def test_parser_constructs_without_error():
    p = build_parser()
    assert p.prog == "recall"


def test_help_prints_subcommands(capsys):
    # argparse exits via SystemExit(0) on --help; that's the expected path.
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    for cmd in ("init", "index", "search", "status", "errors", "migrate", "verify",
                "types", "graph"):
        assert cmd in out


def test_version_flag(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    out = capsys.readouterr().out.strip()
    assert "recall" in out


def test_no_args_prints_help(capsys):
    code = main([])
    assert code == 0
    out = capsys.readouterr().out
    assert "recall" in out


# --------------------------------------------------------------------------
# Search subcommand
# --------------------------------------------------------------------------


def test_bare_query_dispatches_to_search(fixture_db_path, capsys, monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    code = main(["--db", str(fixture_db_path), "mehrwerk"])
    assert code == 0
    out = capsys.readouterr().out
    assert "mehrwerk" in out.lower()
    assert "bm25=" in out


def test_search_subcommand_explicit(fixture_db_path, capsys, monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    code = main(["--db", str(fixture_db_path), "search", "mehrwerk"])
    assert code == 0
    out = capsys.readouterr().out
    assert "bm25=" in out


def test_search_no_hits_returns_nonzero(fixture_db_path, capsys, monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    code = main(["--db", str(fixture_db_path), "zzzz_no_such_word"])
    assert code == 1
    out = capsys.readouterr().out
    assert "No hits" in out


def test_search_json_output(fixture_db_path, capsys, monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    code = main(["--db", str(fixture_db_path), "mehrwerk", "--json"])
    assert code == 0
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert isinstance(parsed, list)
    assert parsed
    sample = parsed[0]
    for key in ("drawer_uid", "source", "created_at", "score", "rank", "snippet", "content"):
        assert key in sample


def test_search_source_filter(fixture_db_path, capsys, monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    code = main(
        ["--db", str(fixture_db_path), "search", "lorem", "--source", "markdown",
         "--json"]
    )
    assert code == 0
    parsed = json.loads(capsys.readouterr().out)
    assert all(h["source"] == "markdown" for h in parsed)


def test_search_since_until(fixture_db_path, capsys, monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    code = main(
        ["--db", str(fixture_db_path), "search", "lorem", "--since", "2024-03-01",
         "--json"]
    )
    assert code == 0
    parsed = json.loads(capsys.readouterr().out)
    assert all(h["created_at"] >= 1709251200 for h in parsed)


def test_search_invalid_date(fixture_db_path, capsys, monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("RECALL_DEBUG", "")  # ensure last-resort guard kicks in
    code = main(["--db", str(fixture_db_path), "search", "lorem", "--since", "not-a-date"])
    assert code != 0


def test_search_raw_mode(fixture_db_path, capsys, monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    code = main(
        ["--db", str(fixture_db_path), "search", "mehrwerk OR andrew",
         "--raw", "--json"]
    )
    assert code == 0
    parsed = json.loads(capsys.readouterr().out)
    assert len(parsed) >= 2  # OR should match more than either alone


def test_search_full_flag(fixture_db_path, capsys, monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    code = main(
        ["--db", str(fixture_db_path), "search", "recall architecture", "--full"]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "Four layers" in out


def test_search_limit(fixture_db_path, capsys, monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    code = main(
        ["--db", str(fixture_db_path), "search", "lorem", "--limit", "2", "--json"]
    )
    assert code == 0
    parsed = json.loads(capsys.readouterr().out)
    assert len(parsed) <= 2


# --------------------------------------------------------------------------
# Status subcommand
# --------------------------------------------------------------------------


def test_status_on_fixture(fixture_db_path, capsys):
    code = main(["--db", str(fixture_db_path), "status"])
    assert code == 0
    out = capsys.readouterr().out
    assert "DB:" in out
    assert "Drawers:" in out
    assert "Schema:" in out


def test_status_missing_db(tmp_path, capsys):
    code = main(["--db", str(tmp_path / "nope.db"), "status"])
    assert code == 1
    out = capsys.readouterr().out
    assert "not found" in out


# --------------------------------------------------------------------------
# Errors subcommand
# --------------------------------------------------------------------------


def test_errors_empty(fixture_db_path, capsys):
    code = main(["--db", str(fixture_db_path), "errors"])
    assert code == 0
    out = capsys.readouterr().out
    assert "No ingest errors" in out


# --------------------------------------------------------------------------
# Verify subcommand
# --------------------------------------------------------------------------


def test_verify_clean_fixture(fixture_db_path, capsys):
    code = main(["--db", str(fixture_db_path), "verify"])
    assert code == 0
    out = capsys.readouterr().out
    assert "OK" in out


def test_verify_deep(fixture_db_path, capsys):
    code = main(["--db", str(fixture_db_path), "verify", "--deep"])
    assert code == 0


# --------------------------------------------------------------------------
# Types subcommand
# --------------------------------------------------------------------------


def test_types_list(fixture_db_path, capsys):
    code = main(["--db", str(fixture_db_path), "types", "list"])
    assert code == 0
    out = capsys.readouterr().out
    assert "person" in out
    assert "project" in out


# --------------------------------------------------------------------------
# Graph subcommand
# --------------------------------------------------------------------------


def test_graph_entity_unknown(fixture_db_path, capsys):
    code = main(["--db", str(fixture_db_path), "graph", "entity", "Nonexistent"])
    assert code == 1
    out = capsys.readouterr().out
    assert "No entity" in out


# --------------------------------------------------------------------------
# Init wizard
# --------------------------------------------------------------------------


def test_init_non_interactive_writes_config(tmp_path, monkeypatch, capsys):
    out_path = tmp_path / "sources.toml"
    code = main(["init", "--non-interactive", "--out", str(out_path)])
    assert code == 0
    assert out_path.exists()
    text = out_path.read_text(encoding="utf-8")
    assert "schema_version = 1" in text
    assert "[database]" in text


def test_init_force_overwrite(tmp_path, capsys):
    out_path = tmp_path / "sources.toml"
    out_path.write_text("schema_version = 1\n", encoding="utf-8")
    code = main(["init", "--non-interactive", "--out", str(out_path), "--force"])
    assert code == 0
    text = out_path.read_text(encoding="utf-8")
    assert "[database]" in text


def test_init_refuses_overwrite_without_force(tmp_path, capsys):
    out_path = tmp_path / "sources.toml"
    out_path.write_text("schema_version = 1\n", encoding="utf-8")
    code = main(["init", "--non-interactive", "--out", str(out_path)])
    assert code == 1


# --------------------------------------------------------------------------
# sources.toml round-trip
# --------------------------------------------------------------------------


def test_render_and_load_roundtrip(tmp_path):
    out = tmp_path / "sources.toml"
    candidates = [
        {
            "name": "claude_code",
            "type": "claude_code",
            "path": "~/.claude/projects/",
            "exists": True,
            "hint": "test claude code",
        },
        {
            "name": "notes",
            "type": "markdown",
            "path": "~/Documents/Notes/",
            "exists": True,
            "hint": "test markdown",
        },
    ]
    text = render_starter_toml(database_path=tmp_path / "recall.db", sources=candidates)
    out.write_text(text, encoding="utf-8")

    cfg = load_sources_config(out)
    assert cfg.schema_version == 1
    assert len(cfg.sources) == 2
    assert {s.name for s in cfg.sources} == {"claude_code", "notes"}


def test_load_sources_config_rejects_unknown_type(tmp_path):
    out = tmp_path / "sources.toml"
    out.write_text(
        'schema_version = 1\n'
        '[database]\npath = "x.db"\n\n'
        '[[sources]]\nname = "x"\ntype = "made_up"\npath = "/tmp"\n',
        encoding="utf-8",
    )
    with pytest.raises(Exception):
        load_sources_config(out)


def test_load_sources_config_rejects_duplicate_name(tmp_path):
    out = tmp_path / "sources.toml"
    out.write_text(
        'schema_version = 1\n[database]\npath = "x.db"\n\n'
        '[[sources]]\nname = "a"\ntype = "markdown"\npath = "/tmp/a"\n\n'
        '[[sources]]\nname = "a"\ntype = "markdown"\npath = "/tmp/b"\n',
        encoding="utf-8",
    )
    with pytest.raises(Exception):
        load_sources_config(out)


def test_load_sources_config_rejects_future_schema(tmp_path):
    out = tmp_path / "sources.toml"
    out.write_text(
        'schema_version = 99\n[database]\npath = "x.db"\n', encoding="utf-8"
    )
    with pytest.raises(Exception):
        load_sources_config(out)


def test_load_sources_config_missing_returns_filenotfound():
    with pytest.raises(FileNotFoundError):
        load_sources_config("/nonexistent/path/to/sources.toml")


def test_discover_config_path_env_var(tmp_path, monkeypatch):
    cfg = tmp_path / "custom.toml"
    cfg.write_text(
        'schema_version = 1\n[database]\npath = "x.db"\n', encoding="utf-8"
    )
    monkeypatch.setenv("AUROCHS_RECALL_CONFIG", str(cfg))
    monkeypatch.chdir(tmp_path)
    # Remove any cwd sources.toml
    found = discover_config_path()
    assert found == cfg


def test_detect_candidate_sources_returns_list():
    """Must not raise; may be empty depending on env."""
    cands = detect_candidate_sources()
    assert isinstance(cands, list)
    for c in cands:
        assert "name" in c
        assert "type" in c
        assert "path" in c


# --------------------------------------------------------------------------
# Open-drawer flag (resolves uid prefix)
# --------------------------------------------------------------------------


def test_open_unknown_uid_returns_error(fixture_db_path, capsys):
    code = main(
        ["--db", str(fixture_db_path), "search", "anything", "--open", "deadbeef"]
    )
    assert code == 2
    err = capsys.readouterr().err
    assert "no drawer" in err.lower()
