"""Amendments: read the changes an amending act makes and apply them to the amended document.

An amendment ("О внесении изменений в …") does not restate the document, it lists operations on
it: "в статье 17.1 … в таблице видов разрешенного использования условно разрешенные виды
дополнить строкой …", "пункт 4 изложить в следующей редакции …", "слова «А» заменить словами
«Б»". The new edition is the base text with those operations applied.

The work is split so that the language model only reads and never writes the law:

* ``extract_operations`` — the LLM turns the amendment into a closed list of operations: where
  (a path of headings, a table, a numbered item, table rows), what (replace / append words,
  insert, replace, delete, repeal) and with what. New content is never retyped: the model points
  at the amendment's own blocks (``content_blocks``), and the words it quotes are checked
  against the amendment text.
* ``apply_operations`` — deterministic: it resolves each address in the base edition's raw
  blocks, applies the change and reports what happened. An operation whose address or quoted
  words are not found is *not* guessed at: it is reported as failed, the edition is marked for
  review, and every other operation still applies.

The output is a list of raw blocks in the parser's own format, so the new edition goes through
the ordinary delta update: blocks the amendment did not touch keep their hashes.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from html import escape, unescape

import structlog

from src.api_clients import ChatClient
from src.dvd_service.modules.pdf_reader import table_text
from src.dvd_service.modules.windowing import make_windows

log = structlog.get_logger(__name__)

#: Bumped whenever extraction changes meaning, so cached operations are re-extracted.
EXTRACTOR_VERSION = 2

ACTIONS = ("replace_words", "append_words", "insert", "replace", "delete", "repeal")
TARGETS = ("text", "item", "table", "rows")

OPS_SCHEMA = {
    "type": "object",
    "properties": {
        "operations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "item": {"type": "string"},
                    "item_block": {"type": "integer"},
                    "scope": {"type": "array", "items": {"type": "string"}},
                    "target": {"type": "string", "enum": list(TARGETS)},
                    "table": {"type": "string"},
                    "section": {"type": "string"},
                    "numbering": {"type": "string"},
                    "rows": {"type": "array", "items": {"type": "string"}},
                    "column": {"type": "integer"},
                    "action": {"type": "string", "enum": list(ACTIONS)},
                    "find": {"type": "string"},
                    "text": {"type": "string"},
                    "content_blocks": {"type": "array", "items": {"type": "integer"}},
                },
                "required": [
                    "item",
                    "item_block",
                    "scope",
                    "target",
                    "table",
                    "section",
                    "numbering",
                    "rows",
                    "column",
                    "action",
                    "find",
                    "text",
                    "content_blocks",
                ],
            },
        }
    },
    "required": ["operations"],
}

OPS_SYSTEM = """Ты разбираешь акт о внесении изменений в нормативный документ (правила, положение, \
регламент) и переводишь каждое изменение в операцию над текстом изменяемого документа.
Дан фрагмент акта: блоки с номерами [id]. Таблицы даны в HTML. Верни ВСЕ изменения из фрагмента, \
по одной операции на каждое конкретное действие. Преамбулу, подписи, сведения о публикации и \
вступлении в силу пропускай. Если изменение касается только карт, схем, графических материалов \
или описания границ без изменения текста, операций не возвращай. Приложения к акту (карты, \
описание местоположения границ, координаты характерных точек) — не изменения текста: по ним \
операций не возвращай, даже если акт велит дополнить ими документ.

Поля операции:
item — номер пункта самого акта, задающего изменение ("1.1.2.1"), или "".
item_block — id блока акта, в котором записан этот пункт (его текст «… дополнить …»). \
Нумерованные пункты внутри нового содержания (между « и ») — это текст документа, а не \
пункты акта: из них операций не делай.
scope — путь к месту в ИЗМЕНЯЕМОМ документе от общего к частному: начала заголовков дословно, \
как они пишутся в документе ("Статья 17.1", "Ж-2.15", "Статья 19"). Код зоны пиши отдельным \
элементом ("ОИ-1.15"), без её названия. Не включай в scope таблицу, пункт или часть статьи.
target — что меняется: "text" — текст в пределах scope; "item" — нумерованный пункт/подпункт/\
часть (номер в numbering); "table" — таблица целиком; "rows" — строки таблицы.
table — слова из названия или шапки таблицы, по которым её отличить от других таблиц в scope \
("виды разрешенного использования", "предельные размеры земельных участков"); иначе "".
section — для таблицы с разделами: название раздела строк ("основные виды разрешенного \
использования", "условно разрешенные виды использования", "вспомогательные виды \
разрешенного использования"). При добавлении строк в такую таблицу section обязателен: \
возьми его из слов акта («основные виды … дополнить строкой» → "основные виды \
разрешенного использования"). Иначе "".
numbering — номер пункта/части в изменяемом документе ("4", "2.3", "а)"); для части статьи \
пиши номер части ("1"); иначе "".
rows — номера строк таблицы, которых касается изменение (["7", "9"]); для вставки новых строк — \
пусто.
column — номер столбца (с 1), если меняются слова в ячейках; иначе 0.
action:
  replace_words — «слова "А" заменить словами "Б"»: find = А, text = Б (дословно);
  append_words — «дополнить словами "Б"»: text = Б; если сказано «после слов "А"», то find = А;
  insert — «дополнить пунктом/строкой/абзацем/текстом следующего содержания»: content_blocks;
  replace — «изложить в следующей редакции»: content_blocks;
  delete — «исключить»; для слов — find = исключаемые слова;
  repeal — «признать утратившим силу».
find, text — дословно из акта, без кавычек-ёлочек; иначе "".
content_blocks — id блоков акта с новым содержанием (то, что стоит между « и »): текст, \
таблицы, примечания. Не перепечатывай содержание — только id. Иначе [].
Пример: «1.1. В таблице видов разрешенного использования зоны Ж-2.15 статьи 17.1 условно \
разрешенные виды использования дополнить строкой следующего содержания: «[таблица в блоке 7]»» \
→ {"item":"1.1","scope":["Статья 17.1","Ж-2.15"],"target":"rows","table":"виды разрешенного \
использования","section":"условно разрешенные виды использования","numbering":"","rows":[],\
"column":0,"action":"insert","find":"","text":"","content_blocks":[7]}."""


# --- normalization and addressing ---------------------------------------------------------

_QUOTES = str.maketrans({"«": '"', "»": '"', "“": '"', "”": '"', "„": '"', "ё": "е"})
_DASHES = re.compile(r"[‐-―−]")
_SPACE = re.compile(r"\s+")


def norm(text: str) -> str:
    """Comparison form: case, quotes, dashes, ё and whitespace do not matter."""
    text = _DASHES.sub("-", (text or "").casefold().translate(_QUOTES))
    return _SPACE.sub(" ", text).strip()


def _starts_with(text: str, prefix: str) -> bool:
    """``text`` begins with ``prefix`` as a whole designation: "Ж-2" is not "Ж-2.15"."""
    if not prefix or not text.startswith(prefix):
        return False
    rest = text[len(prefix) :]
    return not (rest[:1].isalnum() and prefix[-1:].isalnum() or re.match(r"\.\d", rest))


def _shape(anchor: str) -> re.Pattern:
    """What a sibling heading of ``anchor`` looks like: "статья 17.1" → "статья <n>"."""
    m = re.match(r"([^\d]*?)(\d+(?:\.\d+)*)", anchor)
    if not m:
        return re.compile(re.escape(anchor))
    prefix = m.group(1)
    if re.fullmatch(r"[^\W\d_]{1,4}-", prefix):  # a zone code: Ж-, ОИ-, П-
        prefix_re = r"[^\W\d_]{1,4}-"
    else:
        prefix_re = re.escape(prefix)
    return re.compile(prefix_re + r"\d+(?:\.\d+)*(?![\d])")


_NUMBERED = re.compile(r"^(\(?[0-9]+(?:\.[0-9]+)*[.)]?|[а-яa-z]\))(?=\s|$)")


def _numbering_of(text: str) -> str | None:
    m = _NUMBERED.match(text)
    return m.group(1) if m else None


def _num_key(numbering: str) -> tuple:
    return tuple(int(p) for p in re.findall(r"\d+", numbering))


_PART = re.compile(r"част[ьи] [ivxlc]+\.?")
_CODE = re.compile(r"[^\W\d_]{1,4}-\d+(?:\.\d+)*")
_STRUCTURAL = re.compile(
    r"^(?:часть|части|пункт|пункта|подпункт|подпункта|п\.|пп\.)\s*(\S+?)[.)]?$"
)


def _same_number(found: str, wanted: str) -> bool:
    return found.strip("().").casefold() == wanted.strip("().").casefold()


def _stems(text: str) -> set[str]:
    return {w[:5] for w in re.findall(r"[^\W\d_]{4,}", norm(text))}


# --- tables ---------------------------------------------------------------------------------

_ROW = re.compile(r"<tr\b[^>]*>(.*?)</tr>", re.S | re.I)
_CELL = re.compile(r"(<t[dh]\b[^>]*>)(.*?)(</t[dh]>)", re.S | re.I)
_TAG = re.compile(r"<[^>]+>")


@dataclass
class Table:
    """A table as rows of ``[open tag, inner html, close tag]`` cells — enough to edit cells."""

    rows: list[list[list[str]]]

    @classmethod
    def parse(cls, html: str) -> "Table":
        return cls(
            [[list(c) for c in _CELL.findall(row)] for row in _ROW.findall(html or "")]
        )

    def html(self) -> str:
        return (
            "<table>"
            + "".join(
                "<tr>" + "".join("".join(cell) for cell in row) + "</tr>"
                for row in self.rows
            )
            + "</table>"
        )

    @staticmethod
    def cell_text(cell: list[str]) -> str:
        return " ".join(unescape(_TAG.sub(" ", cell[1])).split())

    def row_number(self, row: list[list[str]]) -> str | None:
        """The row's own number when its first cell is one ("7.", "9.1")."""
        if not row:
            return None
        first = self.cell_text(row[0])
        return first.rstrip(".") if re.fullmatch(r"\d+(?:\.\d+)*\.?", first) else None

    def row_key(self, row: list[list[str]]) -> tuple[str, tuple] | None:
        """How a row is ordered: by its number ("7.", "9.1") or code ("Ж-3.15.2")."""
        first = self.cell_text(row[0]) if row else ""
        if re.fullmatch(r"\d+(?:\.\d+)*\.?", first):
            return "", _num_key(first)
        m = re.fullmatch(r"([^\W\d_]{1,4})[-.]?(\d+(?:\.\d+)*)", first)
        return (m.group(1).casefold(), _num_key(m.group(2))) if m else None

    def row_text(self, row: list[list[str]]) -> str:
        return " ".join(t for c in row if (t := self.cell_text(c)))

    def is_section(self, row: list[list[str]]) -> bool:
        """A section header row: one filled cell ("ОСНОВНЫЕ ВИДЫ …", "ЖИЛЫЕ ЗОНЫ")."""
        filled = [c for c in row if self.cell_text(c)]
        return len(filled) == 1 and len(self.rows) > 1 and max(map(len, self.rows)) > 1


def _table_block(block: dict, table: Table) -> dict:
    html = table.html()
    return {**block, "html": html, "text": table_text(html)}


# --- operations -----------------------------------------------------------------------------


@dataclass
class OpResult:
    op: dict
    status: str = "failed"  # applied | failed
    reason: str = ""  # why it failed, or how the address was found when not as written

    def as_dict(self) -> dict:
        return {
            "item": self.op.get("item", ""),
            "action": self.op.get("action"),
            "target": self.op.get("target"),
            "scope": self.op.get("scope", []),
            "status": self.status,
            "reason": self.reason,
        }


class OperationError(ValueError):
    """An operation's address or quoted words were not found: reported, never guessed."""


class Applier:
    """Applies operations to a copy of the base edition's raw blocks, one at a time."""

    def __init__(self, base: list[dict], amendment: list[dict], source: str) -> None:
        self.blocks = [dict(b) for b in base]
        self.amendment = amendment
        self.source = source  # recorded on every block the amendment touched
        self.amendment_text = norm("\n".join(b["text"] for b in amendment))
        self.passages = passages(amendment)

    # -- addressing --

    def _region(self, scope: list[str]) -> tuple[int, int]:
        """``[start, end)`` of the deepest scope element; the best-resolving candidate wins.

        When the whole path does not resolve, outer elements are dropped one by one: an act
        that names the wrong article for a zone code still points at the one zone with that
        code. Such a fallback is reported in the operation's note.
        """
        path = [
            # "части II" (genitive) names the heading "ЧАСТЬ II".
            re.sub(r"^части\b", "часть", norm(s)) if _PART.fullmatch(norm(s)) else s
            for s in scope
            if s.strip()
        ]
        # "часть II" of the rules is their outermost division, wherever the model put it.
        path.sort(key=lambda s: not _PART.fullmatch(norm(s)))
        if not path:
            raise OperationError("не указано место изменения")
        for drop in range(len(path)):
            if drop and not _CODE.fullmatch(norm(path[-1])):
                # Only a code names one place on its own; "жилые зоны" may be anywhere.
                break
            regions = self._resolve(path[drop:], 0, len(self.blocks))
            if regions:
                if drop:
                    self.note = "место найдено без «%s»" % " / ".join(path[:drop])
                # A table of contents also starts with the headings; the real section is the
                # largest.
                return max(regions, key=lambda r: r[1] - r[0])
        raise OperationError(f"не найдено место: {' / '.join(scope)}")

    def _resolve(
        self, path: list[str], start: int, end: int, head: bool = False
    ) -> list[tuple[int, int]]:
        """Regions matching ``path`` inside ``[start, end)``; ``head``: ``start`` is a heading."""
        if not path:
            return [(start, end)]
        found = []
        anchor = norm(path[0])
        if head and self._head_names(start, anchor):
            # "Статья 17.1", "ЖИЛЫЕ ЗОНЫ": a heading's title given as its own element.
            found += self._resolve(path[1:], start, end, True)
        numbered = _STRUCTURAL.match(anchor)
        if numbered:
            for i in self._numbered(start + 1, end, numbered.group(1)):
                found += self._resolve(path[1:], i, self._item_end(i, end), True)
            if found:
                return found
        shape = _shape(anchor)
        for i in range(start, end):
            if not _starts_with(norm(self.blocks[i]["text"]), anchor):
                continue
            stop = next(
                (
                    j
                    for j in range(i + 1, end)
                    if self.blocks[j].get("category") != "Table"
                    and shape.match(norm(self.blocks[j]["text"]))
                    and not _starts_with(norm(self.blocks[j]["text"]), anchor)
                ),
                end,
            )
            found += self._resolve(path[1:], i, stop, True)
        return found

    def _head_names(self, head: int, anchor: str) -> bool:
        """The scope heading at ``head`` itself carries ``anchor`` after its designation."""
        text = norm(self.blocks[head]["text"])
        return len(anchor) > 3 and anchor in text[1:] and not _starts_with(text, anchor)

    def _numbered(self, start: int, end: int, numbering: str) -> list[int]:
        return [
            i
            for i in range(start, end)
            if self.blocks[i].get("category") != "Table"
            and (n := _numbering_of(self.blocks[i]["text"]))
            and _same_number(n, numbering)
        ]

    def _item_end(self, head: int, end: int) -> int:
        """Where the numbered item at ``head`` ends: at its next sibling.

        Dotted numbers say their depth ("2.3" ends at the next number of depth ≤ 2). Plain
        numbers do not: part 1 of an article may hold items 1-4 before part 2 begins, so
        sequences are tracked — a number continues the innermost open sequence that expects
        it, "1" opens a nested one, and only a number continuing the item's own sequence
        ends it.
        """
        own = _numbering_of(self.blocks[head]["text"]) or ""
        key = _num_key(own)
        for j in range(head + 1, end):
            n = _numbering_of(self.blocks[j]["text"])
            if not n or self.blocks[j].get("category") == "Table":
                continue
            if not key:  # a letter item: the next letter item or any numbered one
                return j
            if len(key) > 1 and 0 < len(_num_key(n)) <= len(key):
                return j
        if len(key) != 1:
            return end
        levels = [key[0]]
        for j in range(head + 1, end):
            n = _numbering_of(self.blocks[j]["text"])
            if not n or self.blocks[j].get("category") == "Table":
                continue
            value = _num_key(n)
            if len(value) != 1:
                continue
            for depth in range(len(levels) - 1, -1, -1):
                if value[0] == levels[depth] + 1:
                    if depth == 0:
                        return j
                    levels[depth] = value[0]
                    del levels[depth + 1 :]
                    break
            else:
                if value[0] == 1:
                    levels.append(1)
        return end

    def _item(self, region: tuple[int, int], numbering: str) -> tuple[int, int]:
        """``[start, end)`` of a numbered item inside ``region`` (the outermost one wins)."""
        start, end = region
        heads = self._numbered(start + 1 if start else 0, end, numbering)
        if not heads:
            raise OperationError(f"пункт {numbering} не найден")
        spans = [(i, self._item_end(i, end)) for i in heads]
        return max(spans, key=lambda r: r[1] - r[0])

    def _table(self, region: tuple[int, int], hint: str) -> int:
        tables = [
            i for i in range(*region) if self.blocks[i].get("category") == "Table"
        ]
        if not tables:
            raise OperationError("в указанном месте нет таблиц")
        if not hint.strip():
            if len(tables) == 1:
                return tables[0]
            raise OperationError("в указанном месте несколько таблиц, не указано какая")
        wanted = _stems(hint)

        def score(i: int) -> float:
            around = " ".join(
                [self.blocks[i]["text"][:400]]
                + [
                    self.blocks[j]["text"]
                    for j in range(max(region[0], i - 2), i)
                    if self.blocks[j].get("category") != "Table"
                ]
            )
            return len(wanted & _stems(around)) / max(1, len(wanted))

        best = max(tables, key=score)
        if score(best) < 0.6:
            raise OperationError(f"таблица не найдена: {hint}")
        return best

    # -- content --

    def _check_quoted(self, text: str) -> None:
        if text and norm(text) not in self.amendment_text:
            raise OperationError(f"текста нет в правке: «{text[:80]}»")

    def _passage(self, i: int) -> tuple[int, int] | None:
        """``[opening, closing]`` of the «…» passage block ``i`` belongs to, if any."""
        return next(((a, b) for a, b in self.passages if a <= i <= b), None)

    def _content(self, ids: list[int]) -> list[dict]:
        """The amendment's blocks with the new content, without the quote marks around it.

        New content stands between « and » and may span many blocks (a table and its
        footnote, text running over several pages); the model tends to name only some of
        them, or the quote-mark block next to them, so each id is widened to its passage.
        """
        picked: dict[int, str] = {}
        for i in ids:
            if not 0 <= i < len(self.amendment):
                raise OperationError(f"нет блока правки {i}")
            passage = self._passage(i)
            if passage is None:
                # An act quotes the text it adds; unquoted blocks are its own appendices
                # (maps, boundary descriptions), not text of the amended document.
                raise OperationError(f"новое содержание не в кавычках (блок {i})")
            opening, closing = passage
            for j in range(opening, closing + 1):
                edge = ("open" if j == opening else "") + (
                    "close" if j == closing else ""
                )
                picked[j] = edge
        out = []
        for i in sorted(picked):
            block = dict(self.amendment[i])
            block.pop("bbox", None)
            block["page"] = None
            if block.get("category") != "Table":
                text = block["text"].strip()
                if "open" in picked[i]:
                    text = re.sub(r"^«\s*", "", text)
                if "close" in picked[i]:
                    text = re.sub(r"\s*»\s*[.;,]?$", "", text)
                if not text:
                    continue
                block["text"] = text
            out.append(block)
        if not out:
            raise OperationError("нет нового содержания")
        return out

    def _mark(self, block: dict) -> dict:
        sources = list(block.get("amended_by") or [])
        if self.source not in sources:
            sources.append(self.source)
        return {**block, "amended_by": sources}

    # -- the operations --

    def apply(self, op: dict) -> OpResult:
        result = OpResult(op)
        before = [b["text"] for b in self.blocks]
        self.note = ""
        try:
            self._apply(op)
        except OperationError as exc:
            result.reason = str(exc)
            return result
        result.status, result.reason = "applied", self.note
        if [b["text"] for b in self.blocks] == before:
            result.status, result.reason = "failed", "текст не изменился"
        return result

    def _apply(self, op: dict) -> None:
        action, target = op.get("action"), op.get("target")
        if action not in ACTIONS or target not in TARGETS:
            raise OperationError(f"неизвестная операция: {action}/{target}")
        self._check_quoted(op.get("text", ""))
        region = self._region(op.get("scope") or [])
        if target in ("table", "rows"):
            self._apply_table(op, region)
        elif target == "item" and op.get("numbering") and action == "insert":
            self._apply_text(
                op, (region[0], self._insert_point(region, op["numbering"]))
            )
        elif target == "item" and op.get("numbering"):
            self._apply_text(op, self._item(region, op["numbering"]))
        else:
            self._apply_text(op, region)

    def _insert_point(self, region: tuple[int, int], numbering: str) -> int:
        """Where a new item goes: after its predecessor ("4" for "5"), else at the end."""
        start, end = region
        if self._numbered(start + 1 if start else 0, end, numbering):
            raise OperationError(f"пункт {numbering} уже есть")
        key = _num_key(numbering)
        if key and key[-1] > 1:
            previous = ".".join(map(str, key[:-1] + (key[-1] - 1,)))
        elif not key and len(numbering.strip("()")) == 1:
            previous = chr(ord(numbering.strip("()")) - 1) + ")"
        else:
            return end
        try:
            return self._item(region, previous)[1]
        except OperationError:
            return end

    def _apply_text(self, op: dict, span: tuple[int, int]) -> None:
        action = op["action"]
        start, end = span
        if action in ("replace_words", "delete") and op.get("find"):
            self._replace_words(range(start, end), op["find"], op.get("text", ""))
        elif action == "append_words":
            self._append_words(range(start, end), op)
        elif action == "insert":
            content = [self._mark(b) for b in self._content(op["content_blocks"])]
            self.blocks[end:end] = content
        elif action == "replace":
            content = [self._mark(b) for b in self._content(op["content_blocks"])]
            if op.get("target") == "text":
                raise OperationError(
                    "не указано, что именно излагается в новой редакции"
                )
            self.blocks[start:end] = content
        elif action == "delete":
            if op.get("target") != "item":
                raise OperationError("не указано, что исключается")
            del self.blocks[start:end]
        elif action == "repeal":
            if op.get("target") != "item":
                raise OperationError("не указано, что утрачивает силу")
            number = _numbering_of(self.blocks[start]["text"]) or op["numbering"]
            self.blocks[start:end] = [
                self._mark(
                    {
                        **self.blocks[start],
                        "text": f"{number} Утратил силу.",
                        "category": "NarrativeText",
                        "html": None,
                    }
                )
            ]

    def _replace_words(self, indices, find: str, new: str) -> None:
        self._check_quoted(find)
        pattern = re.compile(
            r"\s+".join(re.escape(w) for w in find.split()).replace("ё", "[её]"),
            re.I,
        )
        hits = 0
        for i in indices:
            block = self.blocks[i]
            text, n = pattern.subn(lambda _: new, block["text"])
            if not n:
                continue
            hits += n
            if block.get("category") == "Table" and block.get("html"):
                table = Table.parse(block["html"])
                for row in table.rows:
                    for cell in row:
                        cell[1] = pattern.sub(
                            lambda _: escape(new, quote=False), cell[1]
                        )
                self.blocks[i] = self._mark(_table_block(block, table))
            else:
                self.blocks[i] = self._mark({**block, "text": _SPACE.sub(" ", text)})
        if not hits:
            raise OperationError(f"слова не найдены: «{find[:80]}»")

    def _append_words(self, indices, op: dict) -> None:
        words, find = op.get("text", ""), op.get("find", "")
        if not words:
            raise OperationError("нет добавляемых слов")
        if find:
            self._replace_words(indices, find, f"{find} {words}")
            return
        last = next(
            (i for i in reversed(list(indices)) if self.blocks[i]["text"].strip()), None
        )
        if last is None:
            raise OperationError("некуда добавить слова")
        text = self.blocks[last]["text"].rstrip()
        end = text[-1:] if text[-1:] in ".;:," else ""
        text = (text[: -len(end)] if end else text).rstrip()
        self.blocks[last] = self._mark(
            {**self.blocks[last], "text": f"{text} {words}{end}"}
        )

    def _apply_table(self, op: dict, region: tuple[int, int]) -> None:
        index = self._table(region, op.get("table", ""))
        block = self.blocks[index]
        table = Table.parse(block.get("html") or "")
        if not table.rows:
            raise OperationError("таблица без HTML-разметки")
        action = op["action"]
        rows = [r for r in op.get("rows") or [] if r.strip()]
        if op["target"] == "table":
            if action == "replace":
                content = self._content(op["content_blocks"])
                self.blocks[index : index + 1] = [self._mark(b) for b in content]
                return
            if action in ("replace_words", "delete") and op.get("find"):
                self._replace_words([index], op["find"], op.get("text", ""))
                return
            if action == "append_words":
                self._append_words([index], op)
                return
            if action in ("delete", "repeal"):
                del self.blocks[index]
                return
            if action != "insert":
                raise OperationError(f"{action} для таблицы не поддерживается")
        if action == "insert":
            self._insert_rows(index, table, op)
            return
        positions = self._row_positions(table, rows)
        if action in ("replace_words", "append_words", "delete"):
            self._edit_cells(index, table, positions, op)
        elif action == "replace":
            new_rows = self._content_rows(op)
            if len(new_rows) != len(positions):
                raise OperationError("число новых строк не совпадает с заменяемыми")
            for pos, row in zip(positions, new_rows):
                table.rows[pos] = row
            self.blocks[index] = self._mark(_table_block(block, table))
        elif action in ("delete", "repeal"):
            for pos in sorted(positions, reverse=True):
                del table.rows[pos]
            self.blocks[index] = self._mark(_table_block(block, table))

    def _row_positions(self, table: Table, rows: list[str]) -> list[int]:
        if not rows:
            raise OperationError("не указаны строки таблицы")
        positions = []
        for wanted in rows:
            pos = next(
                (
                    k
                    for k, row in enumerate(table.rows)
                    if (n := table.row_number(row)) and _same_number(n, wanted)
                ),
                None,
            )
            if pos is None:
                raise OperationError(f"строка {wanted} не найдена")
            positions.append(pos)
        return positions

    def _edit_cells(self, index: int, table: Table, positions, op: dict) -> None:
        column = op.get("column") or 0
        find, words = op.get("find", ""), op.get("text", "")
        if find:
            self._check_quoted(find)
        pattern = (
            re.compile(r"\s+".join(re.escape(w) for w in find.split()), re.I)
            if find
            else None
        )
        changed = 0
        for pos in positions:
            cells = table.rows[pos]
            targets = [cells[column - 1]] if 0 < column <= len(cells) else cells
            if column and not 0 < column <= len(cells):
                raise OperationError(f"в строке нет столбца {column}")
            for cell in targets:
                inner = cell[1]
                if op["action"] == "append_words" and not find:
                    cut = inner.rfind("</p>")
                    cut = cut if cut >= 0 else len(inner)
                    cell[1] = (
                        inner[:cut].rstrip()
                        + " "
                        + escape(words, quote=False)
                        + inner[cut:]
                    )
                    changed += 1
                elif pattern is not None:
                    replacement = (
                        f"{find} {words}"
                        if op["action"] == "append_words"
                        else ("" if op["action"] == "delete" else words)
                    )
                    cell[1], n = pattern.subn(
                        lambda _: escape(replacement, quote=False), inner
                    )
                    changed += n
        if not changed:
            raise OperationError("изменяемые слова в ячейках не найдены")
        self.blocks[index] = self._mark(_table_block(self.blocks[index], table))

    def _content_rows(self, op: dict) -> list[list[list[str]]]:
        rows = []
        for block in self._content(op["content_blocks"]):
            if block.get("category") == "Table" and block.get("html"):
                rows += Table.parse(block["html"]).rows
        if not rows:
            raise OperationError("в новом содержании нет строк таблицы")
        return rows

    def _insert_rows(self, index: int, table: Table, op: dict) -> None:
        content = self._content(op["content_blocks"])
        new_rows = self._content_rows(op)
        width = max(map(len, table.rows))
        for row in new_rows:
            if len(row) != width and not table.is_section(row):
                raise OperationError(
                    f"в новой строке {len(row)} ячеек, а в таблице {width}"
                )
        section = op.get("section", "")
        for row in new_rows:
            table.rows.insert(self._row_slot(table, row, section), row)
        notes = [self._mark(b) for b in content if b.get("category") != "Table"]
        self.blocks[index : index + 1] = [
            self._mark(_table_block(self.blocks[index], table)),
            *notes,
        ]

    def _row_slot(self, table: Table, row, section: str) -> int:
        key = table.row_key(row)
        if key:
            # A numbered or coded row follows the last one ordered before it: row 7.1 after
            # row 7, zone Ж-3.15.2 after Ж-3.15 — inside the right section by construction.
            after = [
                k
                for k, r in enumerate(table.rows)
                if (other := table.row_key(r))
                and other[0] == key[0]
                and other[1] <= key[1]
            ]
            if after:
                return after[-1] + 1
        if section.strip():
            wanted = _stems(section)
            scored = [
                (len(wanted & _stems(table.row_text(r))) / max(1, len(wanted)), -k)
                for k, r in enumerate(table.rows)
                if table.is_section(r)
            ]
            # "основные виды разрешенного использования" shares most words with "условно
            # разрешенные виды использования": the best match wins, not the first good one.
            best = max(scored, default=(0.0, 0))
            if best[0] < 0.6:
                raise OperationError(f"раздел таблицы не найден: {section}")
            heads = [-best[1]]
            end = next(
                (
                    k
                    for k in range(heads[0] + 1, len(table.rows))
                    if table.is_section(table.rows[k])
                ),
                len(table.rows),
            )
            return end
        if any(table.is_section(r) for r in table.rows):
            raise OperationError("не указан раздел таблицы для новой строки")
        return len(table.rows)


@dataclass
class Consolidation:
    """The new edition's raw blocks plus a report on every operation."""

    blocks: list[dict]
    results: list[OpResult]

    @property
    def failed(self) -> list[OpResult]:
        return [r for r in self.results if r.status != "applied"]


def apply_operations(
    base: list[dict], amendment: list[dict], operations: list[dict], source: str
) -> Consolidation:
    """Apply ``operations`` read from ``amendment`` to ``base``; never raises on a bad op."""
    applier = Applier(base, amendment, source)
    results = [applier.apply(op) for op in operations]
    for r in results:
        if r.status != "applied":
            log.warning(
                "amendment_op_failed",
                source=source,
                item=r.op.get("item"),
                reason=r.reason,
            )
    return Consolidation(applier.blocks, results)


# --- extraction -----------------------------------------------------------------------------


def _render(blocks: list[dict], start: int, end: int) -> str:
    lines = []
    for i in range(start, end):
        b = blocks[i]
        body = (
            b.get("html")
            if b.get("category") == "Table" and b.get("html")
            else b["text"]
        )
        lines.append(f"[{i}] {body}")
    return "\n".join(lines)


def passages(blocks: list[dict]) -> list[tuple[int, int]]:
    """``[opening, closing]`` block ranges of the act's top-level «…» passages.

    Quotes are balanced, not just looked up: new text often quotes names itself
    («… «Столбы верстовые», XVIII в.»), and a passage may run over many pages.
    """
    found, depth, opened = [], 0, 0
    for i, b in enumerate(blocks):
        for ch in b["text"]:
            if ch == "«":
                if depth == 0:
                    opened = i
                depth += 1
            elif ch == "»" and depth > 0:
                depth -= 1
                if depth == 0:
                    found.append((opened, i))
    return found


def quoted_blocks(blocks: list[dict]) -> set[int]:
    """Blocks inside «…» passages, the opening and closing ones excluded."""
    return {i for a, b in passages(blocks) for i in range(a + 1, b)}


def _clean(op: dict, start: int, end: int) -> dict:
    op = copy.deepcopy(op)
    op["content_blocks"] = sorted(
        {i for i in op.get("content_blocks") or [] if start <= i < end}
    )
    op["scope"] = [s.strip() for s in op.get("scope") or [] if s.strip()]
    op["rows"] = [r.strip().rstrip(".") for r in op.get("rows") or [] if r.strip()]
    op["item"] = (op.get("item") or "").strip()
    if op.get("action") == "append_words" and not op.get("text"):
        # "дополнить текстом следующего содержания: «…»" read as adding words.
        if op["content_blocks"]:
            op["action"] = "insert"
    return op


def extract_operations(
    amendment: list[dict], client: ChatClient, *, max_chars: int = 12000
) -> list[dict]:
    """The operations an amendment makes, in the order it lists them.

    Long acts are read in windows; an operation the overlap repeats is kept once.
    """
    operations: list[dict] = []
    seen: set[str] = set()
    for start, end in make_windows(amendment, max_chars=max_chars, overlap=2):
        answer = client.chat(OPS_SYSTEM, _render(amendment, start, end), OPS_SCHEMA)
        for op in answer.get("operations") or []:
            op = _clean(op, start, end)
            key = repr(sorted(op.items()))
            if key not in seen:
                seen.add(key)
                operations.append(op)
    # Every change of a numbered act sits in one of its items; an operation outside them was
    # read off an appendix (coordinates, boundary descriptions), and one "found" inside quoted
    # new text (a new zone has items of its own) is part of that text.
    quoted = quoted_blocks(amendment)
    operations = [op for op in operations if op["item_block"] not in quoted]
    if any(op["item"] for op in operations):
        operations = [op for op in operations if op["item"]]
    # An act without numbered items still quotes the text it adds.
    operations = [
        op
        for op in operations
        if op["item"]
        or not op["content_blocks"]
        or any(i in quoted for i in op["content_blocks"])
    ]
    operations = _by_wording(operations, amendment)
    log.info("amendment_ops_extracted", operations=len(operations))
    return operations


# The act's own words for what an item does; they win over the model's reading of them.
_ADDS = re.compile(r"\bдополн", re.I)
_RESTATES = re.compile(r"\bизлож\w*\b.{0,40}\bредакци", re.I)
# Appendices that are not text: boundary descriptions with coordinates, the zoning map.
_NOT_TEXT = re.compile(
    r"описани\w*\s+местоположени\w*\s+границ|координат|"
    r"карт\w*\s+градостроительного\s+зонирования",
    re.I,
)
_QUOTED = re.compile(r"«[^«»]*»")


def _unquoted(text: str) -> str:
    """An item's own words: the names and new text it quotes taken out (nested ones too)."""
    while True:
        stripped = _QUOTED.sub(" ", text)
        if stripped == text:
            return text
        text = stripped


def _by_wording(operations: list[dict], amendment: list[dict]) -> list[dict]:
    """Correct the operations by what each item says it does.

    The model reads the same act differently from run to run: «часть 2 дополнить текстом
    следующего содержания: «…»» came back as a ``replace`` with no content, and an item
    adding boundary descriptions to the appendix «Сведения о границах территориальных
    зон» as a text change. An item that adds (and does not restate) is an ``insert``; one
    about boundary descriptions, coordinates or the zoning map changes no text; new
    content missing from an ``insert``/``replace`` is the first passage quoted after the
    item, before the next one.
    """
    found = passages(amendment)
    heads = sorted(
        {op["item_block"] for op in operations if op.get("item_block") is not None}
    )
    kept = []
    for op in operations:
        head = op.get("item_block")
        if head is None or not 0 <= head < len(amendment):
            kept.append(op)
            continue
        words = _unquoted(amendment[head]["text"])
        if _NOT_TEXT.search(words):
            log.info("amendment_op_not_text", item=op.get("item"), block=head)
            continue
        if (
            op.get("action") == "replace"
            and _ADDS.search(words)
            and not _RESTATES.search(words)
        ):
            op["action"] = "insert"
        if op.get("action") in ("insert", "replace") and not op.get("content_blocks"):
            later = [h for h in heads if h > head]
            limit = later[0] if later else len(amendment)
            quoted = [a for a, _ in found if head <= a < limit]
            if quoted:
                op["content_blocks"] = [quoted[0]]
        kept.append(op)
    return kept
