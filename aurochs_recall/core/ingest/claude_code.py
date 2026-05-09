"""Claude Code session ingestor.

Handles two on-disk layouts:

**Flat** (older):
    ~/.claude/projects/<project>/<session-uuid>.jsonl

**Nested** (newer, with subagents):
    ~/.claude/projects/<project>/<session-uuid>/main.jsonl
    ~/.claude/projects/<project>/<session-uuid>/subagents/agent-<id>.jsonl

Each line is a JSON object with at least ``role`` and ``content`` (the
content can be a string or a list-of-blocks; we coerce to string).

Per plan v5:
* ``thread_id`` = session UUID derived from filename
* ``parent_uid`` = ``drawer_uid`` of previous accepted message
* ``position_in_thread`` = sequential index in the jsonl file
* ``role`` = the message's ``role`` field (``human`` or ``assistant``)
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Iterator
from pathlib import Path

from ..types import Drawer
from ..validation import compute_content_hash
from ._base import IngestError, read_text_with_fallback, should_skip_content, strip_bom

logger = logging.getLogger(__name__)

# Session UUID pattern: 8-4-4-4-12 hex (case-insensitive). Used to extract
# the session id from filenames AND parent dir names for nested layout.
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

# Allowed role values per Claude Code jsonl schema. The current schema
# wraps each event in ``{type: 'user'|'assistant'|'tool_use'|..., message:
# {role, content, ...}}``; ``user`` is the human and ``assistant`` is the
# model. Older / hand-rolled jsonl emit ``{role, content}`` at the top
# level. We accept both.
_ROLE_MAP = {
    "human": "human",
    "user": "human",
    "assistant": "assistant",
    "model": "assistant",
}

# Top-level ``type`` values that wrap an actual message (vs. a tool call,
# system message, summary, etc. — those get dropped at extraction time).
_MESSAGE_TYPES = frozenset({"user", "assistant", "human"})


class ClaudeCodeIngestor:
    """Parses Claude Code session jsonl files into Drawers."""

    name: str = "claude_code"

    # ----- Ingestor protocol ----------------------------------------------

    def can_handle(self, path: Path) -> bool:
        """Accept any ``.jsonl`` file whose name (without suffix) is a UUID,
        OR which lives in a directory whose name is a UUID (nested layout).
        """
        if path.suffix.lower() != ".jsonl":
            return False
        # Flat: <uuid>.jsonl
        if _UUID_RE.match(path.stem):
            return True
        # Nested: parent dir is a UUID
        if _UUID_RE.match(path.parent.name):
            return True
        # Nested-subagent: grandparent dir is a UUID and parent is "subagents"
        if (
            path.parent.name == "subagents"
            and _UUID_RE.match(path.parent.parent.name)
        ):
            return True
        return False

    def extract(
        self,
        path: Path,
        *,
        error_sink: Callable[..., None] | None = None,
    ) -> Iterator[Drawer]:
        """Yield drawers parsed from a single jsonl file.

        Per-line warnings (bad JSONL, non-object records) are reported
        via ``error_sink(file_path=..., reason=...)`` if provided. The
        sink is called once per malformed line. Without a sink, the
        ingestor still logs to ``logger.warning`` and skips the line.
        """
        session_uuid = self._extract_session_uuid(path)
        if session_uuid is None:
            raise IngestError(
                f"Cannot determine session UUID from path: {path}"
            )

        # Default mtime fallback for created_at when a record lacks one.
        mtime = int(path.stat().st_mtime)

        try:
            text = read_text_with_fallback(path, primary="utf-8", fallback="latin-1")
        except OSError as e:
            raise IngestError(f"Failed to read {path}: {e}") from e

        text = strip_bom(text)

        prev_drawer_uid: str | None = None
        position = 0

        for lineno, raw_line in enumerate(text.splitlines(), start=1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError as e:
                # Per plan: log + skip, don't kill the run.
                logger.warning(
                    "claude_code: bad jsonl at %s:%d: %s",
                    path,
                    lineno,
                    e,
                )
                if error_sink is not None:
                    error_sink(
                        file_path=path,
                        reason=f"bad jsonl at line {lineno}: {e}",
                    )
                continue

            if not isinstance(rec, dict):
                logger.warning(
                    "claude_code: non-object jsonl at %s:%d", path, lineno
                )
                if error_sink is not None:
                    error_sink(
                        file_path=path,
                        reason=f"non-object jsonl at line {lineno}",
                    )
                continue

            drawer = self._record_to_drawer(
                rec=rec,
                path=path,
                session_uuid=session_uuid,
                position=position,
                parent_uid=prev_drawer_uid,
                fallback_ts=mtime,
            )
            if drawer is None:
                continue

            prev_drawer_uid = drawer.drawer_uid
            position += 1
            yield drawer

    # ----- Helpers --------------------------------------------------------

    @staticmethod
    def _extract_session_uuid(path: Path) -> str | None:
        """Pull the session UUID from the filename or parent directory."""
        if _UUID_RE.match(path.stem):
            return path.stem.lower()
        if _UUID_RE.match(path.parent.name):
            return path.parent.name.lower()
        if (
            path.parent.name == "subagents"
            and _UUID_RE.match(path.parent.parent.name)
        ):
            return path.parent.parent.name.lower()
        return None

    @staticmethod
    def _coerce_content(raw: object) -> str:
        """Reduce the ``content`` field to a single string.

        Claude Code records sometimes have ``content`` as a list of blocks
        like ``[{"type": "text", "text": "..."}, {"type": "tool_use", ...}]``.
        We concatenate the ``text`` blocks; tool-use / image / etc. blocks
        are dropped (they get reconstructed elsewhere from access logs).

        Strings pass through verbatim.
        """
        if isinstance(raw, str):
            return raw
        if isinstance(raw, list):
            parts: list[str] = []
            for block in raw:
                if isinstance(block, dict):
                    if block.get("type") == "text" and isinstance(
                        block.get("text"), str
                    ):
                        parts.append(block["text"])
                elif isinstance(block, str):
                    parts.append(block)
            return "\n".join(parts)
        if raw is None:
            return ""
        # Anything else — coerce to str so we don't drop data silently
        return str(raw)

    @staticmethod
    def _coerce_timestamp(rec: dict, fallback_ts: int) -> int:
        """Pull a created-at epoch from the record, or fall back to mtime."""
        for key in ("timestamp", "created_at", "ts"):
            v = rec.get(key)
            if isinstance(v, (int, float)):
                # Heuristic: ms vs s. Anything past year-3000-in-seconds
                # is almost certainly milliseconds.
                if v > 32503680000:
                    return int(v / 1000)
                return int(v)
            if isinstance(v, str):
                # ISO 8601 string — try parsing via fromisoformat
                try:
                    from datetime import datetime

                    iso = v.replace("Z", "+00:00")
                    return int(datetime.fromisoformat(iso).timestamp())
                except (ValueError, AttributeError):
                    continue
        return fallback_ts

    def _record_to_drawer(
        self,
        rec: dict,
        path: Path,
        session_uuid: str,
        position: int,
        parent_uid: str | None,
        fallback_ts: int,
    ) -> Drawer | None:
        """Convert a single jsonl record to a Drawer, or None if filtered.

        Two on-disk shapes are accepted:

        Modern (current Claude Code, 2024+):
            ``{type: 'user'|'assistant', message: {role, content}, ...}``

        Legacy / hand-rolled:
            ``{role, content, ...}``
        """
        # The modern schema wraps everything in a top-level ``type`` field
        # ('user', 'assistant', 'tool_use', 'summary', etc.) and a ``message``
        # subobject. Filter on type FIRST so we drop tool-use / summary
        # records before trying to extract content.
        top_type = rec.get("type")
        if isinstance(top_type, str) and top_type not in _MESSAGE_TYPES:
            # Modern schema with non-message type — drop it.
            if "message" in rec:
                return None

        msg_obj = rec.get("message") if isinstance(rec.get("message"), dict) else None

        # Resolve role: prefer the inner message.role, fall back to the
        # outer ``role`` / ``type`` / ``sender`` fields for legacy files.
        raw_role: str | None = None
        if msg_obj is not None:
            v = msg_obj.get("role")
            if isinstance(v, str):
                raw_role = v
        if raw_role is None:
            for k in ("role", "type", "sender"):
                v = rec.get(k)
                if isinstance(v, str):
                    raw_role = v
                    break
        if raw_role is None:
            return None
        role = _ROLE_MAP.get(raw_role.lower())
        if role is None:
            return None

        # Resolve content: prefer inner message.content, fall back to outer.
        if msg_obj is not None and "content" in msg_obj:
            content = self._coerce_content(msg_obj["content"])
        else:
            content = self._coerce_content(rec.get("content"))
        if should_skip_content(content):
            return None

        created_at = self._coerce_timestamp(rec, fallback_ts)
        content_hash = compute_content_hash(role, content)

        # Capture schema-version + any record metadata we don't
        # promote to columns. Useful for migration debugging.
        meta: dict = {}
        if "uuid" in rec:
            meta["record_uuid"] = rec["uuid"]
        # Modern schema: model lives on message.model
        if msg_obj is not None and "model" in msg_obj:
            meta["model"] = msg_obj["model"]
        elif "model" in rec:
            meta["model"] = rec["model"]
        if "schema_version" in rec:
            meta["schema_version"] = rec["schema_version"]
        if "version" in rec:
            meta["claude_code_version"] = rec["version"]
        if "agentId" in rec:
            meta["agent_id"] = rec["agentId"]
        if "isSidechain" in rec:
            meta["is_sidechain"] = rec["isSidechain"]
        # Subagent layout: tag the agent id from the filename.
        if path.parent.name == "subagents":
            meta["subagent_file"] = path.stem

        source_id = f"{session_uuid}:{position}"
        return Drawer(
            source=self.name,
            source_id=source_id,
            source_path=str(path.resolve()),
            role=role,
            content=content,
            created_at=created_at,
            content_hash=content_hash,
            thread_id=session_uuid,
            parent_uid=parent_uid,
            position_in_thread=position,
            metadata=meta,
        )
