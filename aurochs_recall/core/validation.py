"""Input validation gate.

Single module routing all user input through validators per plan v5.
Every ingestor and CLI surface should call into here rather than rolling
its own checks. Keeps the rules in one place and one place only.

``normalize_whitespace`` and ``compute_content_hash`` live on
:mod:`core.types` (the spine owns them so the dataclass and the
validators can't drift apart). They're re-exported here so callers that
import from ``core.validation`` find them in the obvious place.

Risk-score scanner
------------------
The multi-pass risk-score scanner runs three independent passes over a
candidate drawer body and sums their evidence into a single score:

* **Pass 1 — raw bytes:** bidi overrides (U+202A-202E, U+2066-2069) and
  zero-width characters (U+200B-200D, U+FEFF) plus ASCII control chars.
  These are the unicode-spoofing attacks that survive only when we
  inspect the literal codepoints; downstream passes run on a stripped
  copy and would miss them.
* **Pass 2 — unicode-stripped:** classic prompt-injection / jailbreak
  templates ("ignore previous instructions", DAN, "you are now",
  role-play resets, etc.). Patterns are case-insensitive and run AFTER
  the bidi/ZW stripping so an attacker can't hide a template behind a
  zero-width separator.
* **Pass 3 — entity-confusable:** mixed-script confusables — Cyrillic
  letters such as U+0430, U+043E, U+0435 (which render visually like
  Latin a/o/e) inside otherwise-Latin words, etc. These show up in
  homograph attacks against entity names; flagged separately so the
  entity linker can choose to canonicalize or reject.

Each pass returns a list of evidence strings; ``compute_risk_score``
sums per-pass scores into ``RiskScore.total_score`` (capped at 100 for
the ``drawer_meta.risk_score`` storage column). Callers map the total
to a band: BLOCKER (>=10), MAJOR (5-9), MINOR (1-4), CLEAN (0).

The version constant ``RISK_SCORE_VERSION`` (in :mod:`core.types`) is
bumped whenever the scoring model changes; bumping it requires a
re-scan of stored drawers because old scores are no longer comparable.
"""

from __future__ import annotations

import re
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

# Re-export from the spine so there's exactly one definition each.
from .types import RISK_SCORE_VERSION, compute_content_hash, normalize_whitespace

# ----- Public API ---------------------------------------------------------


class InvalidInput(ValueError):
    """Raised when validation rejects input. Caller should turn this into
    a user-facing error (CLI exit 2, MCP error response, etc.) rather
    than a stack trace."""


__all__ = [
    "RISK_SCORE_VERSION",
    "InvalidInput",
    "RiskBand",
    "RiskScore",
    "classify_risk_band",
    "compute_content_hash",
    "compute_risk_score",
    "normalize_whitespace",
    "validate_entity_name",
    "validate_file_path",
    "validate_predicate_name",
    "validate_query_string",
]

# Sentinel strings that show up in user data but mean "no value." We refuse
# to store these as entity names because they collide with NULL semantics
# in human-written queries.
_NAME_SENTINELS = frozenset({"null", "none", "undefined"})

_PREDICATE_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")

# Windows reserved names (case-insensitive, applies to stem only).
_WIN_RESERVED_FIXED = frozenset({"CON", "NUL", "AUX", "PRN"})
_WIN_RESERVED_NUMBERED = re.compile(r"^(COM|LPT)[1-9]$")


def validate_entity_name(name: str) -> str:
    """Normalize and validate a knowledge-graph entity name.

    Rejects empty / whitespace-only and the sentinel strings ``null``,
    ``none``, ``undefined`` (case-insensitive). Returns NFC-normalized
    text so equivalent unicode forms collapse to one canonical entry.
    """
    if not isinstance(name, str):
        raise InvalidInput(f"Entity name must be str, got {type(name).__name__}")
    stripped = name.strip()
    if not stripped:
        raise InvalidInput("Entity name cannot be empty")
    if stripped.lower() in _NAME_SENTINELS:
        raise InvalidInput(f"Entity name cannot be sentinel: {stripped!r}")
    return unicodedata.normalize("NFC", stripped)


def validate_query_string(
    query: str,
    mode: Literal["literal", "fts5_raw"] = "literal",
) -> str:
    """Prepare a query for FTS5 MATCH.

    ``literal`` (default): the entire query is wrapped in quotes and any
    embedded quotes doubled. This makes FTS5 treat the input as a phrase
    rather than parsing it as MATCH syntax. Safe for arbitrary user text
    including parens, ``OR``, ``NEAR``, etc.

    ``fts5_raw``: pass-through. Caller has explicitly opted in to MATCH
    syntax via ``--raw`` and accepts the responsibility of producing
    valid FTS5.
    """
    if not isinstance(query, str):
        raise InvalidInput(f"Query must be str, got {type(query).__name__}")
    if mode == "literal":
        return '"' + query.replace('"', '""') + '"'
    if mode == "fts5_raw":
        return query
    raise InvalidInput(f"Unknown query mode: {mode!r}")


def validate_file_path(path: Path | str) -> Path:
    """Reject paths with null bytes or Windows reserved component names.

    Always returns a ``Path``, but does NOT resolve / canonicalize — the
    caller decides whether to ``resolve()`` based on whether they need
    the path to actually exist.
    """
    if isinstance(path, str):
        path = Path(path)
    s = str(path)
    if "\x00" in s:
        raise InvalidInput("Path contains null byte")
    if sys.platform == "win32":
        for component in path.parts:
            stem = component.split(".")[0].upper()
            if not stem:
                continue
            if stem in _WIN_RESERVED_FIXED or _WIN_RESERVED_NUMBERED.match(stem):
                raise InvalidInput(
                    f"Path contains Windows reserved name: {component!r}"
                )
    return path


def validate_predicate_name(pred: str) -> str:
    """Validate a knowledge-graph predicate name.

    Convention: uppercase snake (``WORKS_FOR``, ``MENTIONED_BY``, etc.).
    Enforced by regex so the taxonomy stays consistent and predicates
    can be substring-searched without ambiguity.
    """
    if not isinstance(pred, str):
        raise InvalidInput(f"Predicate must be str, got {type(pred).__name__}")
    if not _PREDICATE_RE.match(pred):
        raise InvalidInput(
            f"Predicate must match /^[A-Z][A-Z0-9_]*$/: {pred!r}"
        )
    return pred


# ============================================================================
# Risk-score scanner (multi-pass)
# ============================================================================
#
# The scanner is intentionally regex/codepoint-based rather than ML-driven:
# it runs at ingest time against every drawer, must be deterministic, and
# must be fast enough that the indexer doesn't slow down. The trade-off is
# that we miss novel attack patterns and over-fire on benign-but-adversarial
# adjacent text — that's why ``bench/safety/`` measures both axes.

# ---- Pass 1: bidi + zero-width + control codepoints ----------------------

# Bidi overrides — U+202A (LRE), U+202B (RLE), U+202C (PDF), U+202D (LRO),
# U+202E (RLO) and the bidi isolates U+2066 (LRI), U+2067 (RLI), U+2068
# (FSI), U+2069 (PDI). All are legal Unicode but used in attacks to make
# rendered text disagree with codepoint order.
_BIDI_CODEPOINTS: frozenset[str] = frozenset(
    chr(c) for c in (0x202A, 0x202B, 0x202C, 0x202D, 0x202E, 0x2066, 0x2067, 0x2068, 0x2069)
)

# Zero-width characters — U+200B (ZWSP), U+200C (ZWNJ), U+200D (ZWJ),
# U+FEFF (BOM/ZWNBSP). Used to split keywords past naive matchers.
_ZERO_WIDTH_CODEPOINTS: frozenset[str] = frozenset(
    chr(c) for c in (0x200B, 0x200C, 0x200D, 0xFEFF)
)

# ASCII control characters that have no business inside a drawer body.
# Excludes \t (0x09), \n (0x0A), \r (0x0D) — those are legitimate.
_CONTROL_CODEPOINTS: frozenset[str] = frozenset(
    chr(c) for c in range(0x00, 0x20) if c not in (0x09, 0x0A, 0x0D)
) | {chr(0x7F)}

# Per-finding scores. Tuned so a single bidi override clears the BLOCKER
# threshold (>=10) on its own, while a single jailbreak template alone
# also blocks. Multiple zero-width chars accumulate to MAJOR.
_SCORE_BIDI = 12          # one bidi override → BLOCKER
_SCORE_ZERO_WIDTH = 4     # >=3 ZW chars → MAJOR; >=3 different ones → BLOCKER
_SCORE_CONTROL = 3        # control chars usually mean a corrupt or hostile paste
_SCORE_JAILBREAK = 12     # any classic-template hit → BLOCKER on its own
_SCORE_CONFUSABLE = 5     # mixed-script word → MAJOR

# Score bands. Aligned with the launch targets in bench/safety/README.md.
_BAND_BLOCKER_THRESHOLD = 10
_BAND_MAJOR_THRESHOLD = 5
_BAND_MINOR_THRESHOLD = 1

# Cap the integer total at 100 because the storage column is BETWEEN 0 AND 100.
_RISK_SCORE_MAX = 100


# ---- Pass 2: jailbreak template patterns ---------------------------------
#
# The patterns below are the cross-section of templates that show up in
# every public jailbreak corpus we surveyed (2024-2025). Patterns are
# case-insensitive; they run on the unicode-stripped form so an attacker
# can't slip them past with zero-width separators.

_JAILBREAK_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "ignore_prior_instructions",
        re.compile(
            r"\b(?:ignore|disregard|forget)\b[^.]{0,40}\b"
            r"(?:prior|previous|earlier|above|all)\b[^.]{0,30}\b"
            r"(?:instructions?|prompts?|rules?|guidelines?|directions?)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "dan_template",
        # DAN must be UPPERCASE (the jailbreak always is); otherwise it
        # collides with the common name "Dan" / variable name "dan".
        # "do anything now" / "developer mode" are case-insensitive
        # via the inline (?i:) flag group.
        re.compile(
            r"\bDAN\b|(?i:\b(?:do anything now|developer mode)\b)",
        ),
    ),
    (
        "you_are_now",
        re.compile(
            r"\byou\s+(?:are|will\s+be)\s+now\b[^.]{0,50}\b"
            r"(?:unrestricted|jailbroken|no\s+longer|free\s+from|unfiltered)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "system_prompt_leak",
        re.compile(
            r"\b(?:reveal|print|show|output|repeat|leak)\b[^.]{0,30}\b"
            r"(?:your|the)\s+(?:system\s+prompt|initial\s+instructions|"
            r"hidden\s+instructions|original\s+prompt)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "role_play_reset",
        re.compile(
            r"\b(?:pretend|act\s+as\s+if|role[\s-]*play\s+as)\b[^.]{0,40}\b"
            r"(?:no\s+(?:rules|restrictions|filters|safety)|"
            r"unfiltered|unrestricted|jailbroken)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "anthropic_override",
        re.compile(
            r"\b(?:anthropic|openai|the\s+(?:developers?|creators?))\b[^.]{0,40}\b"
            r"(?:authorized|approved|granted|told\s+(?:you|me)|asked\s+(?:you|me))\b"
            r"[^.]{0,40}\b(?:override|bypass|ignore|disable)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "policy_off",
        re.compile(
            r"\b(?:turn\s+off|disable|deactivate|switch\s+off)\b[^.]{0,30}\b"
            r"(?:safety|content\s+(?:policy|policies)|moderation|filters?|"
            r"guardrails?|restrictions?)\b",
            re.IGNORECASE,
        ),
    ),
)


# ---- Pass 3: confusable detection ---------------------------------------
#
# Heuristic: a word that mixes Latin and Cyrillic letters within a single
# token is almost always a homograph attack. We tokenize on whitespace,
# check each token's letters against the two scripts. Punctuation and
# numbers don't count as either.

_LATIN_RE = re.compile(r"[A-Za-z]")
_CYRILLIC_RE = re.compile(r"[Ѐ-ӿ]")
_GREEK_RE = re.compile(r"[Ͱ-Ͽ]")
_TOKEN_RE = re.compile(r"\S+")


# ---- Output dataclasses --------------------------------------------------


RiskBand = Literal["BLOCKER", "MAJOR", "MINOR", "CLEAN"]


@dataclass(frozen=True, slots=True)
class RiskScore:
    """Aggregate output of the multi-pass risk_score scanner.

    Attributes
    ----------
    total_score:
        Sum of per-pass scores, capped at 100. Stored verbatim into
        ``drawer_meta.risk_score`` and used by ``classify_risk_band``.
    bidi_findings:
        One string per bidi override codepoint detected. Format
        ``"U+XXXX at byte N"`` for traceability.
    zero_width_findings:
        One string per zero-width codepoint detected.
    control_findings:
        One string per ASCII control character (excluding \\t, \\n, \\r).
    jailbreak_findings:
        Pattern-name + matched substring for each jailbreak template hit.
    confusable_findings:
        ``"<token>"`` for each mixed-script token detected.
    version:
        ``RISK_SCORE_VERSION`` at compute time. Stored alongside the
        score so a later version bump triggers a rescan.
    """

    total_score: int
    bidi_findings: list[str] = field(default_factory=list)
    zero_width_findings: list[str] = field(default_factory=list)
    control_findings: list[str] = field(default_factory=list)
    jailbreak_findings: list[str] = field(default_factory=list)
    confusable_findings: list[str] = field(default_factory=list)
    version: int = RISK_SCORE_VERSION


def classify_risk_band(score: int) -> RiskBand:
    """Map an integer ``risk_score`` to its band label.

    Bands are inclusive lower bounds: BLOCKER >= 10, MAJOR 5-9, MINOR 1-4,
    CLEAN == 0. The scanner uses this; the indexer stores both the
    integer and (implicitly) the band via the version column.
    """
    if score >= _BAND_BLOCKER_THRESHOLD:
        return "BLOCKER"
    if score >= _BAND_MAJOR_THRESHOLD:
        return "MAJOR"
    if score >= _BAND_MINOR_THRESHOLD:
        return "MINOR"
    return "CLEAN"


def compute_risk_score(text: str) -> RiskScore:
    """Multi-pass risk scanner.

    Pass 1 (raw bytes): bidi overrides (U+202A-202E, U+2066-2069),
    zero-width chars (U+200B-200D, U+FEFF), ASCII control chars.
    Pass 2 (unicode-stripped): jailbreak template patterns
    (DAN, "ignore prior instructions", role-play resets, etc.).
    Pass 3 (entity-confusable): mixed-script confusables (Cyrillic
    letters embedded in Latin words, etc.).

    Returns a :class:`RiskScore` with the summed total plus per-pass
    evidence lists. Total score is capped at 100 to fit the
    ``drawer_meta.risk_score`` column.

    Empty / non-string input maps to a CLEAN score with empty evidence
    rather than raising — the scanner is meant to be called on every
    drawer body at ingest, including stripped-down or empty bodies.
    """
    if not isinstance(text, str) or not text:
        return RiskScore(total_score=0)

    bidi_findings: list[str] = []
    zero_width_findings: list[str] = []
    control_findings: list[str] = []

    # Pass 1 — walk codepoints once. Build the stripped-form copy in the
    # same loop so pass 2/3 don't need to re-scan.
    stripped_chars: list[str] = []
    for idx, ch in enumerate(text):
        if ch in _BIDI_CODEPOINTS:
            bidi_findings.append(f"U+{ord(ch):04X} at offset {idx}")
            # Drop the codepoint from the stripped form.
            continue
        if ch in _ZERO_WIDTH_CODEPOINTS:
            zero_width_findings.append(f"U+{ord(ch):04X} at offset {idx}")
            continue
        if ch in _CONTROL_CODEPOINTS:
            control_findings.append(f"U+{ord(ch):04X} at offset {idx}")
            continue
        stripped_chars.append(ch)

    stripped_text = "".join(stripped_chars)

    # Pass 2 — jailbreak templates over the stripped form.
    jailbreak_findings: list[str] = []
    for pattern_name, pattern in _JAILBREAK_PATTERNS:
        for match in pattern.finditer(stripped_text):
            # Cap excerpt length so we never blow up the audit log.
            excerpt = match.group(0).strip()
            if len(excerpt) > 80:
                excerpt = excerpt[:77] + "..."
            jailbreak_findings.append(f"{pattern_name}: {excerpt!r}")

    # Pass 3 — per-token script-mix detection over the stripped form.
    confusable_findings: list[str] = []
    seen_tokens: set[str] = set()
    for match in _TOKEN_RE.finditer(stripped_text):
        token = match.group(0)
        if token in seen_tokens:
            continue
        scripts = 0
        if _LATIN_RE.search(token):
            scripts += 1
        if _CYRILLIC_RE.search(token):
            scripts += 1
        if _GREEK_RE.search(token):
            scripts += 1
        if scripts >= 2:
            seen_tokens.add(token)
            confusable_findings.append(token)

    # Score aggregation.
    raw_total = (
        len(bidi_findings) * _SCORE_BIDI
        + len(zero_width_findings) * _SCORE_ZERO_WIDTH
        + len(control_findings) * _SCORE_CONTROL
        + len(jailbreak_findings) * _SCORE_JAILBREAK
        + len(confusable_findings) * _SCORE_CONFUSABLE
    )
    total_score = min(raw_total, _RISK_SCORE_MAX)

    return RiskScore(
        total_score=total_score,
        bidi_findings=bidi_findings,
        zero_width_findings=zero_width_findings,
        control_findings=control_findings,
        jailbreak_findings=jailbreak_findings,
        confusable_findings=confusable_findings,
        version=RISK_SCORE_VERSION,
    )
