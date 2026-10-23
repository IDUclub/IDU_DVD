"""LLM-free comparison over authorized payloads and existing fragment vectors."""

from __future__ import annotations

import re
from collections import Counter, defaultdict, deque
from difflib import SequenceMatcher

import numpy as np
from qdrant_client.models import FieldCondition, Filter, HasIdCondition, MatchValue

from src.api_clients import create_embedder
from src.common.db.qdrant_client import shared_only_condition
from src.dvd_service.dto.compare import (
    CompareChange,
    ComparedFragment,
    CompareDocument,
    CompareRequest,
    CompareResponse,
    CompareSection,
    CompareStats,
    WordChange,
)
from src.dvd_service.modules.identity import extract_version_from_name, normalize_key
from src.dvd_service.modules.reference_patterns import (
    normalize_designation,
    normalize_numbering,
)

TOP_K = 60
TEXT_THRESHOLD = 0.62
VECTOR_THRESHOLD = 0.72
# Bound fuzzy work after the full-document indexed structural/exact passes.
LEFTOVER_POOL_LIMIT = 128
TEXT_CANDIDATE_LIMIT = 16
# Measurement units that make a number a normative quantity. Any other following word
# («1 статьи», «2 Правил») is not a unit: the number stays bare. Alternatives are tried in
# order and must end at a word boundary, so «мин» is not read as «м».
_UNIT = (
    r"%|‰|°\s*[CС]|°|"
    r"(?:кв|куб)\.\s*(?:км|дм|см|мм|м)|"
    r"(?:км|дм|см|мм|м|km|cm|mm|m)(?:[²³]|\^?[23])?|га|ha|"
    r"лк|дБА|дБ|кПа|МПа|Па|кг|мкг|мг|kg|мл|л|"
    r"ч|час(?:а|ов)?|мин(?:ут[аы]?)?|сут(?:ок|ки)?|сек|мес|лет|года?|"
    r"чел(?:овека?)?\.?|мест[оа]?|этаж(?:а|ей)?|шт\.?|руб\.?|"
    r"кВт|Вт|Гц"
)
NUMBERS = re.compile(
    rf"(?<![\w.,])[+-]?\d+(?:[.,]\d+)?"
    rf"(?:\s*(?:{_UNIT})(?:\s*/\s*(?:{_UNIT}))?(?![\w²³]))?"
)
_WITH_UNIT = re.compile(rf"\d\s*(?:{_UNIT})(?![\w²³])")
_MONTHS = (
    "января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря"
)
# Identifiers, not quantities: dates, act/registration numbers, clause references and
# normative designations. They are masked before numbers are compared.
_NOT_QUANTITIES = re.compile(
    rf"\b\d{{1,2}}\.\d{{1,2}}\.\d{{2,4}}\b"
    rf"|\b\d{{1,2}}\s+(?:{_MONTHS})\s+\d{{4}}(?:\s*г\.?)?"
    r"|(?<!\w)(?:№|(?-i:N))\s*\d[\w./-]*"
    r"|\b\d+(?:\.\d+)+-\d+\b"
    r"|\b\d+(?:\.\d+){2,}\b"
    r"|(?<!\w)(?:под)?(?:пункт|стать|абзац|раздел|глав|приложени)[а-яё]*\s+\d+[\w.]*"
    r"|(?<!\w)(?:пп?|ст|гл|разд)\.\s*\d+[\w.]*",
    re.IGNORECASE,
)


def quantities(text):
    """Numbers (with their unit, when it is a measurement unit) that a change can alter."""
    return NUMBERS.findall(_NOT_QUANTITIES.sub(" ", text))


def quantity_key(value):
    """Spelling-insensitive identity of a quantity: «6 человек.» = «6 человек», «-5°С» = «-5 °C»."""
    value = re.sub(r"\s+", "", value.rstrip(".")).replace(",", ".")
    return value.replace("°С", "°C").replace("²", "2").replace("³", "3")


def changed_quantities(left, right):
    """Values present on one side only (as a multiset), each side in reading order."""
    lkeys, rkeys = Counter(map(quantity_key, left)), Counter(map(quantity_key, right))
    only_l, only_r = lkeys - rkeys, rkeys - lkeys

    def pick(values, budget):
        out = []
        for v in values:
            if budget[quantity_key(v)] > 0:
                budget[quantity_key(v)] -= 1
                out.append(v)
        return out

    return pick(left, only_l), pick(right, only_r)


def text_key(text):
    return " ".join(text.split())


def location(p):
    return (
        normalize_numbering(p.get("numbering") or ""),
        tuple(
            p.get("fragment_name_path")
            or p.get("structure_path")
            or [p.get("breadcrumb") or ""]
        ),
    )


def section(p):
    path = p.get("structure_path") or p.get("fragment_name_path") or []
    if path:
        return str(path[0])
    crumbs = re.split(r" / | > ", p.get("breadcrumb") or "")
    if len(crumbs) > 1:
        return crumbs[0]
    # A flat document (e.g. directly ingested fragments) has no structure path and the
    # breadcrumb is the fragment's own heading: group by the top level of its numbering
    # instead of making every fragment a section of its own.
    numbering = normalize_numbering(p.get("numbering") or "")
    top = numbering.split(".")[0] if "." in numbering else ""
    return f"Раздел {top}" if top else "Без раздела"


def order(p):
    return (p.get("order") or 0, str(p["id"]))


def ratio(a, b):
    return SequenceMatcher(None, text_key(a), text_key(b), autojunk=False).ratio()


def fragment_keys(p):
    """Cache all matching keys once, shared by alignment and topic expansion."""
    text = text_key(p["text"])
    numbering = normalize_numbering(p.get("numbering") or "")
    name_path = tuple(p.get("fragment_name_path") or [])
    structure_path = tuple(p.get("structure_path") or [])
    loc = (numbering, name_path or structure_path or (p.get("breadcrumb") or "",))
    return (loc, text), text, numbering, name_path, structure_path


def key_indexes(keys):
    indexes = [defaultdict(deque) for _ in range(5)]
    for i, values in enumerate(keys):
        for index, value in zip(indexes, values):
            if value:
                index[value].append(i)
    return indexes


def semantic_pairs(left, right):
    """One normalized matrix product, then mutual argmax with stable first-index ties."""
    li = [i for i, p in enumerate(left) if len(p.get("_vector") or [])]
    ri = [j for j, p in enumerate(right) if len(p.get("_vector") or [])]
    if not li or not ri:
        return []
    dimensions = {
        len(p["_vector"])
        for points in (left, right)
        for p in points
        if p.get("_vector")
    }
    if len(dimensions) != 1:
        raise ValueError("incompatible stored vector dimensions")
    a = np.asarray([left[i]["_vector"] for i in li], dtype=np.float64)
    b = np.asarray([right[j]["_vector"] for j in ri], dtype=np.float64)
    if not np.isfinite(a).all() or not np.isfinite(b).all():
        raise ValueError("stored vectors must be finite")
    an, bn = np.linalg.norm(a, axis=1), np.linalg.norm(b, axis=1)
    a = np.divide(a, an[:, None], out=np.zeros_like(a), where=an[:, None] != 0)
    b = np.divide(b, bn[:, None], out=np.zeros_like(b), where=bn[:, None] != 0)
    scores = np.clip(a @ b.T, -1.0, 1.0)
    scores[an == 0, :] = -1.0
    scores[:, bn == 0] = -1.0
    row_best, col_best = scores.argmax(axis=1), scores.argmax(axis=0)
    return [
        (li[i], ri[j], float(scores[i, j]))
        for i, j in enumerate(row_best)
        if col_best[j] == i and scores[i, j] >= VECTOR_THRESHOLD
    ]


class DocumentCompareService:
    def __init__(self, qdrant, registry):
        self.qdrant = qdrant
        self.registry = registry

    def _load(self, doc_id, version, user_id):
        # Pin authorization in the database filter, before reading any private payload.
        scopes = [shared_only_condition()]
        if user_id:
            scopes.append(
                FieldCondition(key="user_id", match=MatchValue(value=user_id))
            )
        conditions = [
            FieldCondition(key="doc_id", match=MatchValue(value=doc_id)),
            Filter(should=scopes),
        ]
        if version:
            conditions.append(
                Filter(
                    should=[
                        FieldCondition(key=k, match=MatchValue(value=version))
                        for k in ("version", "versions")
                    ]
                )
            )
        flt = Filter(must=conditions)
        points = sorted(self.qdrant.comparison_points(flt), key=order)
        if not points:
            raise KeyError("document not found")
        if not version:
            current = [p for p in points if p.get("status") != "superseded"]
            points = current or points
            versions = {p.get("version") or "" for p in points}
            if len(versions) > 1:
                raise ValueError(
                    "multiple editions found; specify left_version/right_version"
                )
        # Registry is supplementary; never use it to bypass the payload's owner filter.
        rec = self.registry.get_document(doc_id) or {}
        first = points[0]
        metadata = {**rec, **first}
        edition = (
            self.registry.edition(
                metadata.get("name") or "", version or metadata.get("version") or ""
            )
            or {}
        )
        for key in ("effective_date", "status"):
            if edition.get(key) is not None:
                metadata[key] = edition[key]
        summary = CompareDocument(
            doc_id=doc_id,
            name=metadata.get("name") or "",
            title=metadata.get("title"),
            version=version or metadata.get("version") or "",
            effective_date=metadata.get("effective_date"),
            status=metadata.get("status") or "active",
            owner="user" if first.get("user_id") else "shared",
        )
        return (
            summary,
            [
                p
                for p in points
                if not p.get("is_container") and (p.get("text") or "").strip()
            ],
            flt,
        )

    def _same_identity(self, left, right):
        if left.doc_id == right.doc_id:
            return True
        if not left.name or not right.name:
            return False
        if normalize_designation(left.name) == normalize_designation(right.name):
            return True

        def identity(doc):
            year = extract_version_from_name(doc.name)
            name = doc.name
            if year and year == doc.version:
                name = re.sub(rf"(?<!\d){year}(?!\d)", "", name)
            return normalize_key(name)

        if identity(left) == identity(right):
            return True
        # Registry records can link editions with different presentation names.
        for a, b in ((left, right), (right, left)):
            edition = self.registry.edition(a.name, b.version) or {}
            if edition.get("doc_id") == b.doc_id:
                return True
        return False

    def _topic(self, points, flt, vector):
        if not points:
            return []
        # Restrict ranking to the actual selected edition and nonblank leaf fragments.
        eligible = flt.model_copy(
            update={
                "must": [
                    *(flt.must or []),
                    HasIdCondition(has_id=[p["id"] for p in points]),
                ]
            }
        )
        hits = self.qdrant.search(vector, eligible, TOP_K)
        selected = {str(h.id) for h in hits}
        return [p for p in points if p["id"] in selected]

    @staticmethod
    def _expand_topic(full, selected, other_selected):
        """Add direct counterparts of the other side's original hits, without recursion."""
        indexes = key_indexes([fragment_keys(p) for p in full])
        keep = {p["id"] for p in selected}
        for p in other_selected:
            for index, key in zip(indexes[1:], fragment_keys(p)[1:]):
                if key:
                    keep.update(full[i]["id"] for i in index.get(key, ()))
        return [p for p in full if p["id"] in keep]

    @staticmethod
    def _align(left, right, editions):
        if not editions:
            pairs = semantic_pairs(left, right)
            matched_l, matched_r = {i for i, _, _ in pairs}, {j for _, j, _ in pairs}
            return (
                pairs,
                [i for i in range(len(left)) if i not in matched_l],
                [j for j in range(len(right)) if j not in matched_r],
            )
        pairs = []
        li, ri = set(range(len(left))), set(range(len(right)))
        left_keys, right_keys = [fragment_keys(p) for p in left], [
            fragment_keys(p) for p in right
        ]
        indexes = key_indexes(right_keys)

        def pair(i, j, score):
            pairs.append((i, j, score))
            li.remove(i)
            ri.remove(j)

        # Exact location/text, moved text, numbering, name path, structure path.
        # Every bucket is ordered; consumed entries are discarded lazily in O(n).
        for field, index in enumerate(indexes):
            for i in sorted(li):
                key = left_keys[i][field]
                if not key:
                    continue
                candidates = index.get(key)
                if candidates is None:
                    continue
                while candidates and candidates[0] not in ri:
                    candidates.popleft()
                if candidates:
                    j = candidates.popleft()
                    score = (
                        1.0
                        if field < 2
                        else SequenceMatcher(
                            None, left_keys[i][1], right_keys[j][1], autojunk=False
                        ).ratio()
                    )
                    pair(i, j, score)

        # Bounded, deterministic fuzzy fallback. Prefer shared tokens and similar lengths;
        # upper bounds reject impossible matches before the expensive ratio computation.
        best_l, best_r = {}, {}
        pool_l, pool_r = (
            sorted(li)[:LEFTOVER_POOL_LIMIT],
            sorted(ri)[:LEFTOVER_POOL_LIMIT],
        )
        right_tokens = {j: set(right_keys[j][1].casefold().split()) for j in pool_r}
        for i in pool_l:
            text = left_keys[i][1]
            tokens = set(text.casefold().split())
            candidates = sorted(
                pool_r,
                key=lambda j: (
                    -len(tokens & right_tokens[j]),
                    abs(len(text) - len(right_keys[j][1])),
                    j,
                ),
            )[:TEXT_CANDIDATE_LIMIT]
            for j in candidates:
                matcher = SequenceMatcher(None, text, right_keys[j][1], autojunk=False)
                if (
                    matcher.real_quick_ratio() < TEXT_THRESHOLD
                    or matcher.quick_ratio() < TEXT_THRESHOLD
                ):
                    continue
                score = matcher.ratio()
                if score < TEXT_THRESHOLD:
                    continue
                if i not in best_l or (score, -j) > (best_l[i][1], -best_l[i][0]):
                    best_l[i] = (j, score)
                if j not in best_r or (score, -i) > (best_r[j][1], -best_r[j][0]):
                    best_r[j] = (i, score)
        for i, (j, score) in sorted(best_l.items()):
            if best_r[j][0] == i:
                pair(i, j, score)
        return pairs, sorted(li), sorted(ri)

    @staticmethod
    def _fragment(p, max_chars):
        if p is None:
            return None
        return ComparedFragment(
            id=p["id"],
            numbering=p.get("numbering") or "",
            breadcrumb=(p.get("breadcrumb") or "")[:max_chars],
            section=section(p)[:max_chars],
            text=p["text"][:max_chars],
            text_truncated=len(p["text"]) > max_chars,
            amended_by=p.get("amended_by") or [],
        )

    def compare(
        self, request: CompareRequest, user_id: str | None = None
    ) -> CompareResponse:
        ls, left, lf = self._load(request.left_doc_id, request.left_version, user_id)
        rs, right, rf = self._load(request.right_doc_id, request.right_version, user_id)
        editions = self._same_identity(ls, rs)
        if request.query:
            with create_embedder() as embedder:
                vector = embedder.embed_query(request.query)
            selected_left, selected_right = self._topic(left, lf, vector), self._topic(
                right, rf, vector
            )
            if editions:
                selected_left, selected_right = (
                    self._expand_topic(left, selected_left, selected_right),
                    self._expand_topic(right, selected_right, selected_left),
                )
            left, right = selected_left, selected_right
        pairs, only_l, only_r = self._align(left, right, editions)
        counts, sections, changes = Counter(), defaultdict(Counter), []

        def add(kind, a, b, score=None):
            counts[kind] += 1
            # Attribute a correspondence to both sections if it moves across sections.
            for name in sorted({section(p) for p in (a, b) if p is not None}):
                sections[name][kind] += 1
            if kind == "unchanged":
                return
            entry = CompareChange(
                kind=kind,
                left=self._fragment(a, request.max_chars),
                right=self._fragment(b, request.max_chars),
                similarity=round(score, 6) if score is not None else None,
            )
            if (
                a is not None
                and b is not None
                and kind in ("modified", "different", "similar")
            ):
                words_a, words_b = a["text"].split(), b["text"].split()
                budget = request.max_chars
                for op, i, j, k, l in SequenceMatcher(
                    None, words_a, words_b, autojunk=False
                ).get_opcodes():
                    if op == "equal":
                        continue
                    x, y = " ".join(words_a[i:j]), " ".join(words_b[k:l])
                    if budget <= 0:
                        entry.diff_truncated = True
                        break
                    entry.word_diff.append(
                        WordChange(left=x[:budget], right=y[:budget])
                    )
                    entry.diff_truncated |= len(x) > budget or len(y) > budget
                    budget -= max(len(x), len(y), 1)
                removed, added = changed_quantities(
                    quantities(a["text"]), quantities(b["text"])
                )
                if removed or added:
                    x, y = "; ".join(removed), "; ".join(added)
                    entry.changed_numbers = WordChange(
                        left=x[: request.max_chars], right=y[: request.max_chars]
                    )
            # Only a changed measured value (with a unit or %) outranks ordinary edits; bare
            # numbers are usually counts of references and must not lead the list.
            measured = entry.changed_numbers and _WITH_UNIT.search(
                entry.changed_numbers.left + " " + entry.changed_numbers.right
            )
            significance = (
                0 if measured else 1 if kind in ("modified", "different") else 2
            )
            changes.append(((significance, order(a or b), order(b or a)), entry))

        for i, j, score in pairs:
            a, b = left[i], right[j]
            same = text_key(a["text"]) == text_key(b["text"])
            if same:
                kind = (
                    "renumbered"
                    if editions and location(a) != location(b)
                    else "unchanged"
                )
            elif editions:
                kind = "modified"
            else:
                kind = "similar" if ratio(a["text"], b["text"]) >= 0.85 else "different"
            add(kind, a, b, score)
        for i in only_l:
            add("removed" if editions else "only_left", left[i], None)
        for j in only_r:
            add("added" if editions else "only_right", None, right[j])
        changes.sort(key=lambda c: c[0])
        kinds = (
            ("added", "removed", "modified", "renumbered")
            if editions
            else ("only_left", "only_right", "similar", "different")
        )
        return CompareResponse(
            mode="editions" if editions else "documents",
            left=ls,
            right=rs,
            stats=CompareStats(
                counts={k: counts[k] for k in (*kinds, "unchanged")},
                left_compared=len(left),
                right_compared=len(right),
                pairs_compared=len(pairs),
                unchanged=counts["unchanged"],
                total_changes=len(changes),
            ),
            sections=[
                CompareSection(section=k, counts=dict(v))
                for k, v in sorted(sections.items())
            ],
            changes=[c for _, c in changes[: request.limit]],
            truncated=len(changes) > request.limit,
            topic_top_k=TOP_K if request.query else None,
        )
