"""Claude.ai web export ingestor.

Parses the ``conversations.json`` file emitted by the official Claude.ai
"Export data" flow. The export is a single JSON array of conversation
objects; each conversation has a ``chat_messages`` (or ``messages``) array
of message objects.

Per plan v5:
* ``thread_id`` = conversation UUID
* ``parent_uid`` = ``drawer_uid`` of the previous accepted message
  in ``chat_messages[]`` (per-conversation, NOT cross-conversation)
* ``position_in_thread`` = array index
* ``role`` = the message's ``sender`` field (``human`` / ``assistant``)

Schema-handling notes:
* Some exports contain BOTH ``chat_messages`` and ``messages`` (different
  vintages). We prefer ``chat_messages`` and fall back to ``messages``.
* Message ``content`` may be a string or a list of content blocks. We
  reduce blocks to concatenated text per block-type ``text``.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path

from ..types import Drawer
from ..validation import compute_content_hash
from ._base import IngestError, read_text_with_fallback, should_skip_content, strip_bom

logger = logging.getLogger(__name__)

# Mapping from raw ``sender`` values to canonical roles.
_ROLE_MAP = {
    "human": "human",
    "user": "human",
    "assistant": "assistant",
}


class ClaudeAiIngestor:
    """Parses Claude.ai web export files into Drawers."""

    name: str = "claude_ai"

    # ----- Ingestor protocol ----------------------------------------------

    def can_handle(self, path: Path) -> bool:
        """Accept any file named ``conversations.json``.

        We don't accept arbitrary ``.json`` because the Claude.ai export
        is the only top-level-array shape we know how to parse without
        guessing.
        """
        return path.name.lower() == "conversations.json"

    def extract(
        self,
        path: Path,
        *,
        error_sink: Callable[..., None] | None = None,
    ) -> Iterator[Drawer]:
        """Yield drawers parsed from the export file.

        Per-conversation / per-message warnings (non-object records,
        missing uuid, missing message array) are reported via
        ``error_sink(file_path=..., reason=...)`` if provided.
        """
        try:
            text = read_text_with_fallback(path, primary="utf-8", fallback="latin-1")
        except OSError as e:
            raise IngestError(f"Failed to read {path}: {e}") from e

        text = strip_bom(text)

        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise IngestError(f"Invalid JSON in {path}: {e}") from e

        if not isinstance(data, list):
            raise IngestError(
                f"Expected top-level JSON array in {path}, got {type(data).__name__}"
            )

        mtime = int(path.stat().st_mtime)

        for conv in data:
            if not isinstance(conv, dict):
                logger.warning(
                    "claude_ai: skipping non-object conversation in %s", path
                )
                if error_sink is not None:
                    error_sink(
                        file_path=path,
                        reason="claude_ai: non-object conversation",
                    )
                continue
            yield from self._extract_conversation(conv, path, mtime, error_sink)

    # ----- Per-conversation -----------------------------------------------

    def _extract_conversation(
        self,
        conv: dict,
        path: Path,
        fallback_ts: int,
        error_sink: Callable[..., None] | None = None,
    ) -> Iterable[Drawer]:
        """Yield drawers from a single conversation object."""
        conv_uuid = conv.get("uuid") or conv.get("id")
        if not isinstance(conv_uuid, str) or not conv_uuid:
            logger.warning(
                "claude_ai: skipping conversation without uuid in %s", path
            )
            if error_sink is not None:
                error_sink(
                    file_path=path,
                    reason="claude_ai: conversation without uuid",
                )
            return

        # Some exports use chat_messages, some use messages, some have both.
        # Prefer chat_messages because that's what the current Claude.ai
        # schema emits; messages only appears as a legacy fallback.
        msgs = conv.get("chat_messages")
        if not isinstance(msgs, list):
            msgs = conv.get("messages")
        if not isinstance(msgs, list):
            logger.warning(
                "claude_ai: conversation %s has no message array", conv_uuid
            )
            if error_sink is not None:
                error_sink(
                    file_path=path,
                    reason=(
                        f"claude_ai: conversation {conv_uuid} has "
                        "no message array"
                    ),
                )
            return

        prev_drawer_uid: str | None = None
        position_out = 0  # only advances on accepted messages

        for array_idx, msg in enumerate(msgs):
            if not isinstance(msg, dict):
                logger.warning(
                    "claude_ai: skipping non-object message in %s[%d]",
                    conv_uuid,
                    array_idx,
                )
                if error_sink is not None:
                    error_sink(
                        file_path=path,
                        reason=(
                            f"claude_ai: non-object message in "
                            f"conversation {conv_uuid} index {array_idx}"
                        ),
                    )
                continue
            drawer = self._message_to_drawer(
                msg=msg,
                path=path,
                conv_uuid=conv_uuid,
                array_idx=array_idx,
                position=position_out,
                parent_uid=prev_drawer_uid,
                fallback_ts=fallback_ts,
            )
            if drawer is None:
                continue
            prev_drawer_uid = drawer.drawer_uid
            position_out += 1
            yield drawer

    @staticmethod
    def _coerce_content(raw: object) -> str:
        """Reduce a message ``content``/``text`` to a string.

        Claude.ai messages can carry ``text`` (string) directly OR a
        ``content`` array of blocks like
        ``[{"type":"text","text":"..."}]``. Mirror the Claude Code
        ingestor's handling so behavior is uniform.
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
                    # Some exports nest the actual text under "content"
                    elif isinstance(block.get("content"), str):
                        parts.append(block["content"])
                elif isinstance(block, str):
                    parts.append(block)
            return "\n".join(parts)
        if raw is None:
            return ""
        return str(raw)

    @staticmethod
    def _coerce_timestamp(msg: dict, fallback_ts: int) -> int:
        """Resolve a timestamp from common message fields, fall back to mtime."""
        for key in ("created_at", "timestamp", "updated_at"):
            v = msg.get(key)
            if isinstance(v, (int, float)):
                if v > 32503680000:
                    return int(v / 1000)
                return int(v)
            if isinstance(v, str):
                try:
                    from datetime import datetime

                    iso = v.replace("Z", "+00:00")
                    return int(datetime.fromisoformat(iso).timestamp())
                except (ValueError, AttributeError):
                    continue
        return fallback_ts

    def _message_to_drawer(
        self,
        msg: dict,
        path: Path,
        conv_uuid: str,
        array_idx: int,
        position: int,
        parent_uid: str | None,
        fallback_ts: int,
    ) -> Drawer | None:
        """Build a Drawer from a Claude.ai message object."""
        raw_sender = msg.get("sender") or msg.get("role")
        if not isinstance(raw_sender, str):
            return None
        role = _ROLE_MAP.get(raw_sender.lower())
        if role is None:
            return None

        # ``text`` is the common shape on Claude.ai exports; ``content``
        # is a fallback (used by older exports / API-shape messages).
        content = self._coerce_content(msg.get("text"))
        if not content.strip():
            content = self._coerce_content(msg.get("content"))
        if should_skip_content(content):
            return None

        created_at = self._coerce_timestamp(msg, fallback_ts)
        content_hash = compute_content_hash(role, content)

        meta: dict = {"array_index": array_idx}
        for k in ("uuid", "model", "stop_reason"):
            if k in msg:
                meta[k] = msg[k]

        # source_id ties together the conversation and the position so
        # that re-ingest of the same export produces stable drawer_uids.
        source_id = f"{conv_uuid}:{array_idx}"
        return Drawer(
            source=self.name,
            source_id=source_id,
            source_path=str(path.resolve()),
            role=role,
            content=content,
            created_at=created_at,
            content_hash=content_hash,
            thread_id=conv_uuid,
            parent_uid=parent_uid,
            position_in_thread=position,
            metadata=meta,
        )
