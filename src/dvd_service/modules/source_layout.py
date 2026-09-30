"""Page-layout artefacts of documents converted from PDF, found without an LLM.

A PDF-to-DOCX conversion breaks the source structure in two ways the rest of the parser
cannot see:

* running headers («СП 42.13330.2026 85») are glued into the text, often right in front of a
  clause number, so the clause no longer *starts* its paragraph and its address is lost;
* paragraphs follow the page layout, not the document: one Word paragraph can hold the tail of
  one clause, a section heading and the first clauses of the next section.

``SourceLayout`` finds both. Headers are reported as spans (the caller keeps them in the exact
source but out of the fragment text); clause starts inside a paragraph are accepted only when
the address is a plausible *next* address of the document, so a reference such as «согласно
6.1.11» never splits a paragraph.
"""

from __future__ import annotations

import re
from collections import Counter

# «СП 42.13330.2026 85», «ГОСТ Р 21.101-2020 12» — a designation followed by a page number.
# The number has a dot or a year (an OCR-spaced «ГОСТ 1 2 . 1 .0 0 4» is not «ГОСТ 1», page 2)
# and the page is on the same line: «Согласно СП 118.13330» over «23 Библиотека» is a table row.
HEADER = re.compile(
    r"(?<![\w.])((?:СП|ГОСТ(?:\s+Р)?|СНиП|СанПиН|СН|РД|МДС|ОДМ|ВСН)\s+"
    r"(?:\d+(?:\.\d+)+(?:-\d{2,4})?|\d+-\d{2,4}))[ \t]+(\d{1,4})(?=\s|$)"
)
# A running header repeats on most pages; five is well above any in-text citation run.
MIN_HEADER_REPEATS = 5
# The column head of a table continued on the next page follows the running header verbatim.
# Two page tops sharing 40+ characters are a repeated head, not body text.
MIN_HEAD_REPEATS = 2
REPEAT_KEY_WORDS = 4
REPEAT_MIN_CHARS = 40
REPEAT_MAX_CHARS = 1200
WORD = re.compile(r"\S+")

ADDRESS = re.compile(r"(?<![\w.,/–-])(\d{1,2}(?:\.\d{1,3}){0,3})\.?[ \t]+(?=[А-ЯЁA-Z])")
# «Т а б л и ц а 6.8», «Таблица 6.8 –», «П р и м е ч а н и я»: captions start a unit.
CAPTION = re.compile(
    r"(?<![\w])(?:Т ?а ?б ?л ?и ?ц ?а[ \t]+[А-ЯЁA-Z]?\.?\d+(?:\.\d+)*|"
    r"П ?р ?и ?м ?е ?ч ?а ?н ?и ?[ея])(?=[\s–—-])"
)
# A repeat that opens with an appendix heading, a caption or a clause address is structure.
STRUCTURAL_START = re.compile(
    rf"\s*(?:Приложение|ПРИЛОЖЕНИЕ)\s|{CAPTION.pattern}|{ADDRESS.pattern}"
)
SPACED_WORD = re.compile(r"(?<![\w])((?:[А-ЯЁа-яё] ){3,}[А-ЯЁа-яё])(?![\w])")
TERMINAL = ".;:!?)»"
# A bare section number («5 Население») is followed by its short title, then by «5.1».
SECTION_TITLE_MAX = 160
SECTION_LOOKAHEAD = 4000
MAX_STEP = 3  # tolerate up to two lost addresses between neighbours
SUBHEADING_MAX = 100
# A number right after these words is a reference, not the start of a clause.
REFERENCE_WORD = re.compile(
    r"(?:^|\s)(?:в|во|по|с|со|к|на|и|или|согласно|пункт\w*|подпункт\w*|п\.|пп\.|"
    r"раздел\w*|таблиц\w*|табл\.|см\.)$",
    re.I,
)


def unspace(text: str) -> str:
    """Join letter-spaced words of PDF headings: «Т а б л и ц а» -> «Таблица»."""
    return SPACED_WORD.sub(lambda m: m.group(1).replace(" ", ""), text)


def _key(number: str) -> tuple[int, ...]:
    return tuple(int(p) for p in number.split("."))


class SourceLayout:
    """Running headers and in-paragraph clause starts of one extracted source text."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.headers = self._headers()

    def _headers(self) -> list[tuple[int, int]]:
        found = list(HEADER.finditer(self.text))
        counts = Counter(m.group(1) for m in found)
        running = {d for d, n in counts.items() if n >= MIN_HEADER_REPEATS}
        spans = []
        for m in found:
            if m.group(1) not in running:
                continue
            spans.append((m.start(), self._skip_blanks(m.end())))
        return self._with_repeated_heads(spans)

    def _skip_blanks(self, pos: int) -> int:
        while pos < len(self.text) and self.text[pos] in " \t":
            pos += 1
        return pos

    def _with_repeated_heads(self, spans):
        """Extend headers by the column head a multi-page table repeats under them.

        «СП 42.13330.2026 251 Объекты1) Нормативная потребность1) …» — the repeat carries no
        text of its own and would glue the table head into the middle of a row.
        """
        tails = [self._words_after(b) for _, b in spans]
        groups: dict[tuple[str, ...], list[int]] = {}
        for i, words in enumerate(tails):
            key = tuple(w for w, _ in words[:REPEAT_KEY_WORDS])
            if len(key) == REPEAT_KEY_WORDS:
                groups.setdefault(key, []).append(i)
        out = list(spans)
        for members in groups.values():
            if len(members) < MIN_HEAD_REPEATS:
                continue
            # Compared by words: a repeat may wrap onto a new line on one page only.
            n = min(len(tails[i]) for i in members)
            for k in range(REPEAT_KEY_WORDS, n):
                if len({tails[i][k][0] for i in members}) > 1:
                    n = k
                    break
            first = members[0]
            head = self.text[spans[first][1] : tails[first][n - 1][1]]
            # An appendix heading repeated by the contents page is a heading, not a head.
            if len(head) < REPEAT_MIN_CHARS or STRUCTURAL_START.match(head):
                continue
            for i in members:
                out[i] = (spans[i][0], self._skip_blanks(tails[i][n - 1][1]))
        return out

    def _words_after(self, pos: int) -> list[tuple[str, int]]:
        """Words (with their end offsets) of the window after ``pos``, a cut word dropped."""
        end = min(len(self.text), pos + REPEAT_MAX_CHARS)
        words = [(m.group(), m.end()) for m in WORD.finditer(self.text, pos, end)]
        return words[:-1] if end < len(self.text) else words

    def strip_headers(self, start: int, end: int) -> str:
        """Fragment text of ``source[start:end]`` without running headers."""
        pieces, cursor = [], start
        for a, b in self.headers:
            if b <= start or a >= end:
                continue
            pieces.append(self.text[cursor : max(cursor, a)])
            cursor = min(b, end)
        pieces.append(self.text[cursor:end])
        return unspace(" ".join(p.strip() for p in pieces if p.strip()))

    def _header_end_before(self, pos: int) -> int | None:
        for a, b in self.headers:
            if b == pos or (b < pos and not self.text[b:pos].strip()):
                return b
        return None

    def _after_boundary(self, pos: int, block_starts: set[int]) -> bool:
        """The address follows a sentence end, a paragraph start or a running header.

        An unnumbered group heading between them is allowed: «… ОКС. Жилая застройка 6.4.13
        Функционально-планировочная …».
        """
        if pos in block_starts or self._header_end_before(pos) is not None:
            return True
        before = self.text[:pos].rstrip()
        if not before:
            return False
        if before[-1] in TERMINAL or before[-1] == "\n":
            return True
        tail = before[-SUBHEADING_MAX:]
        cut = max(tail.rfind(c) for c in TERMINAL + "\n")
        if cut < 0:
            return False
        heading = tail[cut + 1 :].strip()
        return bool(
            heading
            and heading[0].isupper()
            and not re.search(r"\d", heading)
            and not REFERENCE_WORD.search(heading)
        )

    @staticmethod
    def _predecessor_prefixes(key: tuple[int, ...]) -> list[tuple[int, ...]]:
        """Key prefixes of every address that ``key`` may directly follow.

        ``key`` follows T when it opens T's first child (T + (1,)), or when it is the next
        (skipping up to MAX_STEP - 1 lost numbers) at some level of T — at that level or as the
        first child there. Each rule pins a prefix of T, never T's whole tail.
        """
        prefixes = []
        if len(key) > 1 and key[-1] == 1:
            prefixes.append(("exact",) + key[:-1])
        for step in range(1, MAX_STEP + 1):
            if key[-1] - step >= 1:
                prefixes.append(key[:-1] + (key[-1] - step,))
            if len(key) > 1 and key[-1] == 1 and key[-2] - step >= 1:
                prefixes.append(key[:-2] + (key[-2] - step,))
        return prefixes

    def _eligible(self, found, block_starts: set[int]) -> list[bool]:
        eligible = []
        for i, (pos, key) in enumerate(found):
            if len(key) == 1:
                if f"{key[0]}.1" not in self.text[pos : pos + SECTION_LOOKAHEAD]:
                    eligible.append(False)  # a count or a list label, not a section
                    continue
                # «1 Область применения 1.1 …»: a heading straight before its first clause.
                if re.search(
                    rf"\s{key[0]}\.1\s",
                    self.text[pos : pos + SECTION_TITLE_MAX],
                ):
                    eligible.append(True)
                    continue
            # «6.1 Планировочная структура территории 6.1.1 …»: a heading has no final stop.
            heading_title = any(
                other == key[:-1]
                and pos - at <= SECTION_TITLE_MAX
                and not re.search(r"[.;:!?]\s", self.text[at:pos])
                for at, other in found[max(0, i - 4) : i]
            )
            eligible.append(heading_title or self._after_boundary(pos, block_starts))
        return eligible

    def clause_addresses(self, block_starts: set[int]) -> list[tuple[int, str]]:
        """The longest consistent run of clause addresses: ``[(offset, number), …]``.

        Table-of-contents entries, codes in flattened tables and references each form short
        runs of their own; the document body is the one long run of consecutive addresses.
        """
        header_cover = set()
        for a, b in self.headers:
            header_cover.update(range(a, b))
        found = [
            (m.start(), _key(m.group(1)))
            for m in ADDRESS.finditer(self.text)
            if m.start() not in header_cover
        ]
        eligible = self._eligible(found, block_starts)
        best: dict[tuple, tuple[int, int]] = {}  # prefix -> (chain length, index)
        length, back = [0] * len(found), [-1] * len(found)
        for i, (pos, key) in enumerate(found):
            if not eligible[i]:
                continue
            length[i] = 1
            for prefix in self._predecessor_prefixes(key):
                hit = best.get(prefix)
                if hit and hit[0] + 1 > length[i]:
                    length[i], back[i] = hit[0] + 1, hit[1]
            for n in range(1, len(key) + 1):
                if best.get(key[:n], (0,))[0] < length[i]:
                    best[key[:n]] = (length[i], i)
            if best.get(("exact",) + key, (0,))[0] < length[i]:
                best[("exact",) + key] = (length[i], i)
        if not any(length):
            return []
        i = max(range(len(found)), key=length.__getitem__)
        chain = []
        while i >= 0:
            chain.append(i)
            i = back[i]
        return [(found[i][0], ".".join(map(str, found[i][1]))) for i in reversed(chain)]

    @staticmethod
    def section_title(text: str, number: str) -> str:
        """Title of a section unit cut right before its first clause; "" when it has a body.

        «1 Область применения» -> «Область применения»; a unit that goes on with running
        text («6 Организация территории Организацию территории формируют …») has no
        recoverable title boundary.
        """
        rest = re.sub(rf"^\s*{re.escape(number)}\.?\s+", "", text).strip()
        if not rest or len(rest) > SECTION_TITLE_MAX or re.search(r"[.;:!?]", rest):
            return ""
        return unspace(" ".join(rest.split()))

    def caption_starts(self, block_starts: set[int]) -> list[int]:
        return [
            m.start()
            for m in CAPTION.finditer(self.text)
            if m.start() not in block_starts
            and self._after_boundary(m.start(), block_starts)
        ]
