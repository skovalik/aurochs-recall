"""sources.toml loader and discovery — first-run config for the indexer.

Discovery order:
  1. Explicit path (constructor arg)
  2. ./sources.toml in CWD
  3. $AUROCHS_RECALL_CONFIG (env var pointing at a file)
  4. platformdirs.user_config_dir("aurochs-recall") / "sources.toml"
     (Linux: ~/.config/aurochs-recall, macOS: ~/Library/Application Support/...,
      Windows: %LOCALAPPDATA%\\aurochs-recall\\aurochs-recall)

Schema (v1):

    schema_version = 1

    [database]
    path = "~/.aurochs-recall/recall.db"

    [[sources]]
    name = "claude_code"
    type = "claude_code"
    path = "~/.claude/projects/"
    enabled = true

    [[sources]]
    name = "claude_ai_export"
    type = "claude_ai"
    path = "~/Downloads/data-2026-01-27/conversations.json"
    enabled = true

    [[sources]]
    name = "notes"
    type = "markdown"
    path = "~/Documents/Notes/"
    enabled = true
    include = ["**/*.md"]
    exclude = ["**/.obsidian/**"]
"""
from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

try:
    import platformdirs
except ImportError:  # pragma: no cover — pinned in pyproject.toml by Agent 4
    platformdirs = None  # type: ignore[assignment]


SUPPORTED_SOURCE_TYPES = {"claude_code", "claude_ai", "chatgpt", "markdown", "capture"}


@dataclass(frozen=True, slots=True)
class SourceEntry:
    """One entry in `sources.toml`'s [[sources]] array."""

    name: str
    type: str
    path: str
    enabled: bool = True
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    options: dict = field(default_factory=dict)

    @property
    def expanded_path(self) -> Path:
        return Path(os.path.expanduser(self.path)).resolve()


@dataclass(frozen=True, slots=True)
class SourcesConfig:
    """Parsed sources.toml. `path` is the file we loaded from (None if synthesized)."""

    schema_version: int
    database_path: Path
    sources: tuple[SourceEntry, ...]
    path: Path | None = None

    @property
    def enabled_sources(self) -> tuple[SourceEntry, ...]:
        return tuple(s for s in self.sources if s.enabled)


# ----------------------------------------------------------------------
# Discovery
# ----------------------------------------------------------------------


def user_config_dir() -> Path:
    """Cross-platform config dir for aurochs-recall.

    Uses platformdirs. On Windows this resolves under %LOCALAPPDATA%,
    double-nested as aurochs-recall\\aurochs-recall, NOT %APPDATA%/Roaming.
    Falls back to an XDG-ish default only if platformdirs is absent (dev);
    that fallback uses %APPDATA% on Windows and so differs from the real path.
    """
    if platformdirs is not None:
        return Path(platformdirs.user_config_dir("aurochs-recall"))
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "aurochs-recall"
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "aurochs-recall"


def default_config_path() -> Path:
    return user_config_dir() / "sources.toml"


def default_database_path() -> Path:
    """Where the recall.db lives by default. Override via [database] path."""
    if platformdirs is not None:
        return Path(platformdirs.user_data_dir("aurochs-recall")) / "recall.db"
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "aurochs-recall" / "recall.db"
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "aurochs-recall" / "recall.db"


def discover_config_path(explicit: Path | str | None = None) -> Path | None:
    """Walk the discovery order and return the first existing path, or None."""
    if explicit is not None:
        p = Path(explicit).expanduser()
        return p if p.exists() else None

    cwd_local = Path.cwd() / "sources.toml"
    if cwd_local.exists():
        return cwd_local

    env_path = os.environ.get("AUROCHS_RECALL_CONFIG")
    if env_path:
        p = Path(env_path).expanduser()
        if p.exists():
            return p

    user_path = default_config_path()
    if user_path.exists():
        return user_path

    return None


# ----------------------------------------------------------------------
# Load + parse
# ----------------------------------------------------------------------


class SourcesConfigError(ValueError):
    """Raised when sources.toml is structurally invalid."""


def load_sources_config(explicit: Path | str | None = None) -> SourcesConfig:
    """Load and validate sources.toml from the discovered location.

    Raises FileNotFoundError if no config exists anywhere; the CLI catches
    this and tells the user to run `recall init`.
    """
    path = discover_config_path(explicit)
    if path is None:
        raise FileNotFoundError(
            "No sources.toml found. Run `recall init` to create one."
        )
    return _parse_sources_config(path)


def _parse_sources_config(path: Path) -> SourcesConfig:
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise SourcesConfigError(f"sources.toml at {path} is not valid TOML: {e}") from e

    schema_version = int(data.get("schema_version", 1))
    if schema_version > 1:
        raise SourcesConfigError(
            f"sources.toml at {path} declares schema_version={schema_version}, "
            f"but this aurochs-recall only understands version 1."
        )

    db_block = data.get("database", {}) or {}
    db_raw = db_block.get("path")
    if db_raw:
        db_path = Path(os.path.expanduser(str(db_raw)))
    else:
        db_path = default_database_path()

    sources_raw = data.get("sources", []) or []
    if not isinstance(sources_raw, list):
        raise SourcesConfigError(
            f"sources.toml at {path}: [[sources]] must be an array of tables."
        )

    parsed: list[SourceEntry] = []
    seen_names: set[str] = set()
    for i, entry in enumerate(sources_raw):
        if not isinstance(entry, dict):
            raise SourcesConfigError(
                f"sources.toml: [[sources]] entry {i} must be a table."
            )
        name = entry.get("name")
        type_ = entry.get("type")
        path_ = entry.get("path")
        if not isinstance(name, str) or not name:
            raise SourcesConfigError(
                f"sources.toml: [[sources]] entry {i} missing required 'name'."
            )
        if name in seen_names:
            raise SourcesConfigError(
                f"sources.toml: duplicate source name {name!r}."
            )
        seen_names.add(name)
        if not isinstance(type_, str) or type_ not in SUPPORTED_SOURCE_TYPES:
            raise SourcesConfigError(
                f"sources.toml: source {name!r} has unsupported type {type_!r}. "
                f"Supported: {sorted(SUPPORTED_SOURCE_TYPES)}"
            )
        if not isinstance(path_, str) or not path_:
            raise SourcesConfigError(
                f"sources.toml: source {name!r} missing required 'path'."
            )
        include = tuple(str(x) for x in (entry.get("include") or ()))
        exclude = tuple(str(x) for x in (entry.get("exclude") or ()))
        options = {
            k: v
            for k, v in entry.items()
            if k not in {"name", "type", "path", "enabled", "include", "exclude"}
        }
        parsed.append(
            SourceEntry(
                name=name,
                type=type_,
                path=path_,
                enabled=bool(entry.get("enabled", True)),
                include=include,
                exclude=exclude,
                options=options,
            )
        )

    return SourcesConfig(
        schema_version=schema_version,
        database_path=db_path,
        sources=tuple(parsed),
        path=path,
    )


# ----------------------------------------------------------------------
# Discovery wizard helpers (used by `recall init`)
# ----------------------------------------------------------------------


def detect_candidate_sources() -> list[dict]:
    """Look for likely source paths on disk. Returns dicts (not SourceEntry)
    so the wizard can present them with hints / let the user toggle them.

    Each candidate dict: {name, type, path, exists, hint}.
    """
    home = Path.home()
    candidates: list[dict] = []

    # Claude Code projects directory.
    cc_dir = home / ".claude" / "projects"
    if cc_dir.exists() and cc_dir.is_dir():
        candidates.append(
            {
                "name": "claude_code",
                "type": "claude_code",
                "path": str(cc_dir),
                "exists": True,
                "hint": "Claude Code session jsonl files",
            }
        )

    # Common notes / docs roots.
    for sub in ("Documents/Notes", "Notes", "Documents", "Obsidian"):
        p = home / sub
        if p.exists() and p.is_dir():
            candidates.append(
                {
                    "name": _slug(sub),
                    "type": "markdown",
                    "path": str(p),
                    "exists": True,
                    "hint": f"Markdown files under {sub}",
                }
            )

    # CLAUDE.md auto-discovery — show but ask before adding.
    seen_claude_md: set[Path] = set()
    for root in (home / ".claude", Path.cwd()):
        if not root.exists():
            continue
        for hit in _shallow_glob(root, "CLAUDE.md", max_depth=4):
            if hit not in seen_claude_md:
                seen_claude_md.add(hit)
                candidates.append(
                    {
                        "name": f"claude_md_{_slug(str(hit.parent.name))}",
                        "type": "markdown",
                        "path": str(hit),
                        "exists": True,
                        "hint": f"CLAUDE.md at {hit}",
                    }
                )

    return candidates


def _slug(s: str) -> str:
    out = []
    for c in s:
        if c.isalnum():
            out.append(c.lower())
        elif c in "-_":
            out.append(c)
        else:
            out.append("_")
    return "".join(out).strip("_") or "source"


def _shallow_glob(root: Path, name: str, *, max_depth: int = 4) -> list[Path]:
    """Bounded-depth glob to avoid scanning huge trees."""
    results: list[Path] = []

    def walk(p: Path, depth: int) -> None:
        if depth > max_depth or not p.is_dir():
            return
        try:
            for child in p.iterdir():
                try:
                    if child.is_file() and child.name == name:
                        results.append(child)
                    elif child.is_dir() and not child.name.startswith("."):
                        walk(child, depth + 1)
                except OSError:
                    continue
        except (PermissionError, OSError):
            return

    walk(root, 0)
    return results


def render_starter_toml(
    *,
    database_path: Path,
    sources: list[dict],
) -> str:
    """Produce a commented starter sources.toml from selected candidates."""
    lines: list[str] = [
        "# aurochs-recall — sources configuration (schema v1)",
        "# This file tells `recall index` where to find your conversations,",
        "# notes, and other text to index. Edit freely.",
        "",
        "schema_version = 1",
        "",
        "[database]",
        f'path = "{_toml_escape(str(database_path))}"',
        "",
    ]
    if not sources:
        lines.append("# No sources detected. Add entries below by hand.")
        lines.append("# Example:")
        lines.append("# [[sources]]")
        lines.append('# name = "notes"')
        lines.append('# type = "markdown"')
        lines.append('# path = "~/Documents/Notes/"')
        lines.append("# enabled = true")
        lines.append("")
    for src in sources:
        if src.get("hint"):
            lines.append(f"# {src['hint']}")
        lines.append("[[sources]]")
        lines.append(f'name = "{_toml_escape(src["name"])}"')
        lines.append(f'type = "{_toml_escape(src["type"])}"')
        lines.append(f'path = "{_toml_escape(src["path"])}"')
        lines.append(f"enabled = {str(src.get('enabled', True)).lower()}")
        lines.append("")
    return "\n".join(lines)


def _toml_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')
