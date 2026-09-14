"""Source-grounded fragment names and document-independent structural selectors."""

from __future__ import annotations

import fnmatch
import re
import unicodedata
from difflib import SequenceMatcher

NAME_FIELDS = (
    "fragment_name",
    "fragment_name_key",
    "fragment_name_path",
    "structure_path",
    "ancestor_ids",
    "fragment_name_source",
    "fragment_name_schema",
)
NAME_SCHEMA = 1


def normalize(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "").casefold().replace("ё", "е")
    return " ".join(value.split())


def document_key(value: str) -> str:
    """Ignore presentation separators, retaining the document's actual identifier.

    A repeated trailing edition year is presentation noise, not a second identifier.
    This is independent of prefixes such as СП, ГОСТ, ISO or a document's type.
    """
    value = normalize(value)
    value = re.sub(r"(?<!\d)(\d{4})\s+\1$", r"\1", value)
    return "".join(c for c in value if c.isalnum())


def extract_name(node: dict) -> tuple[str | None, str]:
    text = (node.get("text") or "").strip()
    explicit = (node.get("fragment_name") or "").strip()
    # Caller/LLM names must be literal spans, never generated descriptions.
    if explicit and normalize(explicit) in normalize(text):
        return explicit, "source_span"
    first = text.splitlines()[0].strip() if text else ""
    numbering = str(node.get("numbering") or "")
    if numbering:
        first = re.sub(r"^" + re.escape(numbering) + r"(?:[.)]?\s+)", "", first)
    candidate = None
    if first.startswith("#"):
        candidate = first.lstrip("# ").strip()
    elif ":" in first:
        prefix = first.split(":", 1)[0].strip()
        if len(prefix) <= 180 and not re.search(r"[.!?;]", prefix):
            candidate = prefix
    elif node.get("type") in {
        "heading",
        "header",
        "title",
        "chapter",
        "section",
        "appendix",
    }:
        if len(first) <= 180 and not re.search(r"[.!?;]", first):
            candidate = first
    elif re.match(r"(?i)^(?:таблица|table|рисунок|figure)\s+\S+\s*[—–:-]\s*\S", first):
        candidate = first
    if candidate:
        numbering = str(node.get("numbering") or "")
        if numbering:
            candidate = re.sub(
                r"^" + re.escape(numbering) + r"(?:[.)]?\s+)", "", candidate
            )
        candidate = candidate.strip(" :—–")
    return (candidate, "source_span") if candidate else (None, "none")


def annotate_fragments(nodes: list[dict]) -> list[dict]:
    """Derive names/paths without following references outside the selected document scope."""
    out = [{**n} for n in nodes]
    by_id = {str(n["id"]): n for n in out}
    for n in out:
        name, source = extract_name(n)
        n.update(
            fragment_name=name,
            fragment_name_key=normalize(name or ""),
            fragment_name_source=source,
            fragment_name_schema=NAME_SCHEMA,
        )
    for n in out:
        ancestors = []
        seen = {str(n["id"])}
        parent_id = n.get("parent_id")
        while parent_id and str(parent_id) not in seen:
            seen.add(str(parent_id))
            parent = by_id.get(str(parent_id))
            if parent is None or any(
                parent.get(k) != n.get(k) for k in ("doc_id", "user_id", "project_id")
            ):
                break
            ancestors.append(parent)
            parent_id = parent.get("parent_id")
        ancestors.reverse()
        path = ancestors + [n]
        n["ancestor_ids"] = [str(a["id"]) for a in ancestors]
        n["fragment_name_path"] = [
            a["fragment_name"] for a in path if a.get("fragment_name")
        ]
        n["structure_path"] = [
            " ".join(filter(None, [a.get("numbering"), a.get("fragment_name")]))
            for a in path
            if a.get("numbering") or a.get("fragment_name")
        ]
    return out


def _number(value: str) -> tuple[str, ...]:
    return tuple(normalize(value).strip(" .)").split("."))


class StructurePattern:
    """Exact labels, glob masks and sibling ranges; '/' constrains ancestor paths.

    '*' crosses numbering segments: 3.* matches 3.1 and 3.1.2, not 30.1.
    3.3 is exact, not a prefix. 3.3–3.5 selects sibling roots inclusively.
    Ranges must have identical parents and numeric final components.
    """

    def __init__(self, pattern: str):
        self.parts = [normalize(p).strip() for p in pattern.split("/")]
        if not all(self.parts) or len(pattern) > 256:
            raise ValueError(
                "structural pattern must contain non-empty path components (max 256 chars)"
            )
        for part in self.parts:
            self._range(part)  # validate ranges before scanning any data

    @staticmethod
    def _range(part: str):
        match = re.fullmatch(r"([\w.]+)\s*[–—-]\s*([\w.]+)", part)
        if not match or not any(c.isdigit() for c in part):
            return None
        lo, hi = _number(match[1]), _number(match[2])
        if lo[:-1] != hi[:-1] or not lo[-1].isdigit() or not hi[-1].isdigit():
            raise ValueError("range endpoints must be numeric siblings, e.g. 3.3–3.5")
        if int(lo[-1]) > int(hi[-1]):
            raise ValueError("range start must not exceed range end")
        return lo, hi

    def _matches_part(self, part: str, node: dict) -> bool:
        numbering = normalize(str(node.get("numbering") or "")).strip(" .)")
        interval = self._range(part)
        if interval:
            lo, hi = interval
            num = _number(numbering)
            return (
                num[:-1] == lo[:-1]
                and num[-1].isdigit()
                and int(lo[-1]) <= int(num[-1]) <= int(hi[-1])
            )
        name = normalize(node.get("fragment_name") or "")
        labels = [numbering, name, " ".join(filter(None, [numbering, name]))]
        labeled = re.fullmatch(r".+\s+([a-zа-я]|\d+(?:\.\d+)*)", part)
        if labeled and numbering == labeled[1]:
            return True
        # Human labels ("пункт 3.3", "appendix A") are also accepted when the
        # source's own heading carries them. No document-type taxonomy is required.
        return any(label and fnmatch.fnmatchcase(label, part) for label in labels)

    def matches(self, node: dict, by_id: dict[str, dict]) -> bool:
        if not self._matches_part(self.parts[-1], node):
            return False
        ancestors = [by_id[i] for i in node.get("ancestor_ids", []) if i in by_id]
        index = len(ancestors) - 1
        for part in reversed(self.parts[:-1]):
            while index >= 0 and not self._matches_part(part, ancestors[index]):
                index -= 1
            if index < 0:
                return False
            index -= 1
        return True


def name_score(query: str, names: list[str], expanded: bool) -> tuple[float, str]:
    query = normalize(query)
    best = (0.0, "none")
    for value in names:
        name = normalize(value)
        if not name:
            continue
        if name == query:
            score = (1.0, "exact")
        elif any(c in query for c in "*?[") and fnmatch.fnmatchcase(name, query):
            score = (0.98, "mask")
        elif query in name:
            score = (0.95, "contains")
        elif expanded:
            words, terms = re.findall(r"\w+", name), re.findall(r"\w+", query)
            similarity = min(
                (
                    max((SequenceMatcher(None, t, w).ratio() for w in words), default=0)
                    for t in terms
                ),
                default=0,
            )
            score = (0.8 * similarity, "fuzzy") if similarity >= 0.75 else (0.0, "none")
        else:
            score = (0.0, "none")
        if score[0] > best[0]:
            best = score
    return best
