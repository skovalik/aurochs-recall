"""Unit tests for the markdown ingestor."""

from __future__ import annotations

from pathlib import Path

from core.ingest.markdown import MarkdownIngestor

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "ingest"
CORPUS_ROOT = FIXTURES / "markdown_corpus"
WIKI_FILE = CORPUS_ROOT / "wiki" / "concept-a.md"
MEMORY_FILE = CORPUS_ROOT / "memory" / "session-b.md"
LONG_FILE = CORPUS_ROOT / "wiki" / "long-file.md"
BOM_FILE = CORPUS_ROOT / "wiki" / "bom-test.md"


# ----- can_handle ---------------------------------------------------------


def test_can_handle_md():
    ing = MarkdownIngestor()
    assert ing.can_handle(WIKI_FILE) is True


def test_can_handle_markdown_extension():
    ing = MarkdownIngestor()
    assert ing.can_handle(Path("foo.markdown")) is True


def test_can_handle_rejects_other():
    ing = MarkdownIngestor()
    assert ing.can_handle(Path("foo.txt")) is False
    assert ing.can_handle(Path("foo.json")) is False


# ----- role inference ----------------------------------------------------


def test_wiki_path_infers_wiki_role():
    ing = MarkdownIngestor(source_root=CORPUS_ROOT)
    drawers = list(ing.extract(WIKI_FILE))
    assert len(drawers) == 1
    assert drawers[0].role == "wiki"


def test_memory_path_infers_memory_role():
    ing = MarkdownIngestor(source_root=CORPUS_ROOT)
    drawers = list(ing.extract(MEMORY_FILE))
    assert len(drawers) == 1
    assert drawers[0].role == "memory"


def test_unclassified_path_defaults_to_wiki(tmp_path: Path):
    # File outside wiki/ or memory/ — default behavior is `wiki`.
    p = tmp_path / "loose.md"
    p.write_text(
        "Lorem ipsum dolor sit amet, consectetur adipiscing elit, here is content for the loose file.",
        encoding="utf-8",
    )
    ing = MarkdownIngestor(source_root=tmp_path)
    drawers = list(ing.extract(p))
    assert len(drawers) == 1
    assert drawers[0].role == "wiki"


# ----- thread_id / source_id / position ---------------------------------


def test_thread_id_is_relative_path():
    ing = MarkdownIngestor(source_root=CORPUS_ROOT)
    drawers = list(ing.extract(WIKI_FILE))
    # thread_id uses forward-slashes regardless of platform
    assert drawers[0].thread_id == "wiki/concept-a.md"


def test_position_in_thread_is_first_lineno():
    ing = MarkdownIngestor(source_root=CORPUS_ROOT)
    drawers = list(ing.extract(WIKI_FILE))
    # Short file becomes a single drawer starting at line 1
    assert drawers[0].position_in_thread == 1


def test_source_id_combines_path_and_lineno():
    ing = MarkdownIngestor(source_root=CORPUS_ROOT)
    drawers = list(ing.extract(WIKI_FILE))
    assert drawers[0].source_id == "wiki/concept-a.md:1"


def test_parent_uid_always_none():
    ing = MarkdownIngestor(source_root=CORPUS_ROOT)
    drawers = list(ing.extract(WIKI_FILE))
    for d in drawers:
        assert d.parent_uid is None


# ----- chunking ----------------------------------------------------------


def test_short_file_yields_single_drawer():
    ing = MarkdownIngestor(source_root=CORPUS_ROOT)
    drawers = list(ing.extract(WIKI_FILE))
    assert len(drawers) == 1


def test_long_file_chunks_with_overlap():
    # Long-file fixture is 122 lines. With chunk_size=50 / overlap=5, we
    # step 45 lines at a time. Step 0..49, 45..94, 90..121 → 3 chunks.
    # We override the long-file threshold to force chunking.
    ing = MarkdownIngestor(
        source_root=CORPUS_ROOT,
        chunk_size=50,
        chunk_overlap=5,
        long_threshold=10,
    )
    drawers = list(ing.extract(LONG_FILE))
    assert len(drawers) >= 2
    # Each chunk's first_lineno is recorded in metadata
    starts = [d.metadata["first_lineno"] for d in drawers]
    # First chunk starts at line 1
    assert starts[0] == 1
    # Subsequent chunks step forward by (chunk_size - overlap) = 45
    if len(starts) > 1:
        assert starts[1] == 1 + 45
    # Source IDs are unique per chunk
    ids = [d.source_id for d in drawers]
    assert len(set(ids)) == len(ids)


def test_chunking_invariants_validate():
    import pytest

    with pytest.raises(ValueError):
        MarkdownIngestor(chunk_size=10, chunk_overlap=10)
    with pytest.raises(ValueError):
        MarkdownIngestor(chunk_size=10, chunk_overlap=15)
    with pytest.raises(ValueError):
        MarkdownIngestor(chunk_size=0)


# ----- BOM handling ------------------------------------------------------


def test_bom_file_strips_bom():
    # The fixture starts with a UTF-16 LE BOM.
    # The ingestor should read it and strip the leading ﻿ codepoint
    # so content begins cleanly with '# UTF-16 BOM Test File'.
    ing = MarkdownIngestor(source_root=CORPUS_ROOT)
    drawers = list(ing.extract(BOM_FILE))
    assert len(drawers) == 1
    assert not drawers[0].content.startswith("﻿")


# ----- source_path is absolute ------------------------------------------


def test_source_path_is_absolute():
    ing = MarkdownIngestor(source_root=CORPUS_ROOT)
    drawers = list(ing.extract(WIKI_FILE))
    assert Path(drawers[0].source_path).is_absolute()
