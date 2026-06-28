"""Seed regex patterns + designation normalization for the reference-extraction stage.

The committed seed lives here so that a full Redis/Qdrant wipe still leaves the extractor able to
recognise standard Russian regulatory designations. Learned patterns are stored durably in Qdrant
(see ``QdrantRepository`` pattern-collection methods) and extend this baseline at runtime — that is
the self-improving part of the stage.

``normalize_designation`` is the matching key: the same normalization is applied to extracted
references and to the registered document names, so "сп  42.13330.2016" and "СП 42.13330.2016"
resolve to the same document.
"""

from __future__ import annotations

import re

# Designation prefixes of Russian regulatory documents (longest first so "ГОСТ Р" wins over "ГОСТ").
DESIGNATION_PREFIXES: tuple[str, ...] = (
    "ГОСТ Р",
    "ТР ТС",
    "ТР ЕАЭС",
    "СНиП",
    "СанПиН",
    "ГОСТ",
    "СП",
    "СН",
    "РД",
    "ВСН",
    "ТСН",
    "НПБ",
    "ППБ",
    "ПУЭ",
    "ОДМ",
    "МДС",
    "СТО",
    "ФЗ",
)

_PREFIX_ALT = "|".join(p.replace(" ", r"\s+") for p in DESIGNATION_PREFIXES)

# A document designation, e.g. "СП 42.13330.2016", "ГОСТ 12.1.004-91", "ГОСТ Р 21.1101-2013".
# The code part is digits with dots/dashes; an optional year tail.
DESIGNATION_RE = re.compile(
    rf"(?P<name>(?:{_PREFIX_ALT})\s*[\d][\d.\-]*(?:[-\s]\d{{4}})?)",
    re.IGNORECASE,
)

# A clause reference that may trail a designation: "п. 7.5", "пункт 4.2.1", "раздел 6".
CLAUSE_RE = re.compile(
    r"(?:п\.?|пункт[а-я]*|подпункт[а-я]*|раздел[а-я]*|разд\.?|гл\.?|глав[а-я]*)\s*"
    r"(?P<num>\d+(?:\.\d+)*)",
    re.IGNORECASE,
)

# Seed patterns shipped with the code. Each is a named, documented regex with a single capture
# group "name" (and optionally "num"). They double as the baseline fast-path and as the template
# the learning step generalizes from.
SEED_PATTERNS: tuple[dict[str, str], ...] = (
    {
        "name": "ru_designation",
        "regex": DESIGNATION_RE.pattern,
        "description": "Russian regulatory document designation (ГОСТ/СП/СНиП/СанПиН/ФЗ/ТР ТС...).",
        "source": "seed",
    },
)


def normalize_designation(name: str) -> str:
    """Canonical matching key for a document designation.

    Upper-cases, collapses inner whitespace, and trims trailing punctuation so designations that
    differ only by spacing/case map to the same key.
    """
    s = re.sub(r"\s+", " ", (name or "").strip()).upper()
    return s.strip(" .,;:")


def normalize_numbering(num: str) -> str:
    """Canonical clause number: drop a leading "п./пункт", keep the dotted digits ("7.5")."""
    s = (num or "").strip()
    m = re.search(r"\d+(?:\.\d+)*", s)
    return m.group(0) if m else s.strip(" .,;:)")
