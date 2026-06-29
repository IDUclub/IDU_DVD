"""Document identity helpers — general-purpose, domain-neutral key derivation.

These produce the cross-service identity fields stored on every node payload so consumers
(e.g. MSI-TSIM) can join, look up, and cite documents without DVD knowing the domain. The
normalization is deliberately generic (no СП/ГОСТ-specific parsing): a document's canonical
code, DOI, ISBN, URL, etc. arrive from the uploader via ``external_ids`` and are folded into
``lookup_keys`` as-is.
"""

from __future__ import annotations

import re

_SEP = re.compile(r"[\s./\\:\-—–]+", re.U)
_NON_WORD = re.compile(r"[^\w]+", re.U)
_MULTI = re.compile(r"_+")


def normalize_key(value: str) -> str:
    """Lowercase a name/code into a stable lookup slug (unicode-aware, separator-agnostic).

    ``"СП 2.13130.2020"`` -> ``"сп_2_13130_2020"``; ``"ГОСТ 12.1.004-91"`` -> ``"гост_12_1_004_91"``.
    """
    s = (value or "").strip().lower()
    s = _SEP.sub("_", s)
    s = _NON_WORD.sub("_", s)
    s = _MULTI.sub("_", s).strip("_")
    return s or "unknown"


def make_version_id(name: str, content_hash: str) -> str:
    """Stable id for a concrete revision/source file: same text -> same version_id."""
    return f"{normalize_key(name)}__sha256_{content_hash[:12]}"


def make_span_id(
    doc_id: str, char_start: int | None, char_end: int | None
) -> str | None:
    """Deterministic id of a source span, or None when the node has no source offsets."""
    if char_start is None or char_end is None:
        return None
    return f"{doc_id}:span:{char_start}:{char_end}"


def build_aliases(name: str, external_ids: dict[str, str] | None) -> list[str]:
    """Human-readable designations: the document name plus any external id values."""
    aliases = {name.strip()} if name and name.strip() else set()
    for v in (external_ids or {}).values():
        if str(v).strip():
            aliases.add(str(v).strip())
    return sorted(aliases)


def build_lookup_keys(name: str, external_ids: dict[str, str] | None) -> list[str]:
    """Exact-match keys for document resolution: normalized name + every external id form."""
    keys = {normalize_key(name)}
    for k, v in (external_ids or {}).items():
        val = str(v).strip()
        if not val:
            continue
        keys.add(val)
        keys.add(normalize_key(val))
        keys.add(f"{k}:{val}")
    return sorted(k for k in keys if k)
