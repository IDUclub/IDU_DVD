"""Amendments: operations read from an act are applied to the amended edition's raw blocks."""

from html import escape

from src.dvd_service.modules.amendments import (
    Table,
    apply_operations,
    extract_operations,
    quoted_blocks,
)


def _p(text: str) -> dict:
    return {"text": text, "category": "NarrativeText", "html": None, "page": 1}


def _table(rows: list[list[str]]) -> dict:
    html = (
        "<table>"
        + "".join(
            "<tr>"
            + "".join(
                (
                    f'<td colspan="3"><p>{escape(c)}</p></td>'
                    if len(r) == 1
                    else f"<td><p>{escape(c)}</p></td>"
                )
                for c in r
            )
            + "</tr>"
            for r in rows
        )
        + "</table>"
    )
    return {
        "text": "\n".join(c for r in rows for c in r),
        "category": "Table",
        "html": html,
        "page": 1,
    }


USES = [
    ["Наименование вида разрешенного использования", "Описание", "Код"],
    ["ОСНОВНЫЕ ВИДЫ РАЗРЕШЕННОГО ИСПОЛЬЗОВАНИЯ"],
    ["Малоэтажная жилая застройка", "Размещение домов", "2.1.1"],
    ["УСЛОВНО РАЗРЕШЕННЫЕ ВИДЫ ИСПОЛЬЗОВАНИЯ"],
    ["Спорт", "Размещение спортивных объектов", "5.1"],
    ["ВСПОМОГАТЕЛЬНЫЕ ВИДЫ РАЗРЕШЕННОГО ИСПОЛЬЗОВАНИЯ"],
    ["Отдых", "Обустройство мест отдыха", "5.0"],
]
LIMITS = [
    ["1.", "Минимальный отступ", "м", "3"],
    ["7.", "Максимальная высота", "м", "15"],
    ["9.", "Максимальный процент застройки", "%", "40"],
]

BASE = [
    _p("Статья 17.1. Жилые зоны 28"),  # 0  table of contents
    _p("Статья 19. Охрана объектов наследия 40"),  # 1
    _p("Статья 17.1. ЖИЛЫЕ ЗОНЫ"),  # 2
    _p("Ж-2 ЗОНА МАЛОЭТАЖНОЙ ЗАСТРОЙКИ"),  # 3
    _table(USES),  # 4
    _p("Ж-2.15 ЗОНА МАЛОЭТАЖНОЙ ЗАСТРОЙКИ ЗРЗ 1"),  # 5
    _table(USES),  # 6
    _p("Предельные размеры земельных участков, предельные параметры"),  # 7
    _table(LIMITS),  # 8
    _p("Статья 17.3. ОБЩЕСТВЕННО-ДЕЛОВЫЕ ЗОНЫ"),  # 9
    _p("О-8.15 ЗОНА СМЕШАННОЙ ЗАСТРОЙКИ"),  # 10
    _table(USES),  # 11
    _p("Статья 19. ОХРАНА ОБЪЕКТОВ НАСЛЕДИЯ"),  # 12
    _p("1. Общие требования"),  # 13
    _p("1. Действие регламентов не распространяется на памятники."),  # 14
    _p("2. Владение участком осуществляется с соблюдением условий."),  # 15
    _p("3. Земляные работы ведутся после исследований."),  # 16
    _p("4. Использование зон охраны ведется по проектам."),  # 17
    _p("2. Перечень зон охраны"),  # 18
    _p("ОЗ 1 — единая охранная зона"),  # 19
    _p("3. Ограничения по экологическим условиям"),  # 20
    _p("1. Общий режим в границах охранных зон."),  # 21
    _p("2. Общий режим в границах зон регулирования."),  # 22
]

ACT = [
    _p("ПРИКАЗ от 20 ноября 2023 года № 170"),  # 0
    _p("1.1. Условно разрешенные виды зоны Ж-2.15 дополнить строкой:"),  # 1
    _p("«"),  # 2
    _table([["Размещение гаражей", "Гаражи для собственных нужд", "2.7.2 <*>"]]),  # 3
    _p("<*> — применяется к участкам под существующими гаражами"),  # 4
    _p("»."),  # 5
    _p("1.2. Строки 7, 9 второго столбца дополнить словами «кроме кода 2.7.2»;"),  # 6
    _p("1.3. Дополнить строками 7.1, 10 следующего содержания:"),  # 7
    _p("«"),  # 8
    _table(
        [
            ["7.1.", "Этажность для кода 2.7.2", "этаж", "2"],
            ["10.", "Минимальные отступы", "м", "1"],
        ]
    ),  # 9
    _p("»."),  # 10
    _p("2. Часть 1 статьи 19 дополнить пунктом 5:"),  # 11
    _p("«5. Режим зоны «Столбы верстовые» устанавливается проектом.»;"),  # 12
    _p(
        "3. В пункте 2 части 1 слова «с соблюдением условий» заменить словами «по закону»;"
    ),
    _p("4. Пункт 3 части 1 признать утратившим силу."),  # 14
]


def op(**fields) -> dict:
    return {
        "item": "1",
        "item_block": 1,
        "scope": [],
        "target": "text",
        "table": "",
        "section": "",
        "numbering": "",
        "rows": [],
        "column": 0,
        "action": "insert",
        "find": "",
        "text": "",
        "content_blocks": [],
        **fields,
    }


def _rows(block: dict) -> list[list[str]]:
    table = Table.parse(block["html"])
    return [[Table.cell_text(c) for c in r] for r in table.rows]


def _apply(*ops):
    return apply_operations(BASE, ACT, list(ops), "Приказ № 170")


def test_rows_go_to_the_named_section_of_the_right_zone():
    result = _apply(
        op(
            scope=["Статья 17.1", "Ж-2.15"],
            target="rows",
            table="виды разрешенного использования",
            section="условно разрешенные виды использования",
            content_blocks=[3],
        )
    )
    assert [r.status for r in result.results] == ["applied"]
    rows = _rows(result.blocks[6])
    # Not "основные", although it shares most words with the requested section.
    assert rows[5] == ["Размещение гаражей", "Гаражи для собственных нужд", "2.7.2 <*>"]
    assert rows[6] == ["ВСПОМОГАТЕЛЬНЫЕ ВИДЫ РАЗРЕШЕННОГО ИСПОЛЬЗОВАНИЯ"]
    # The footnote inside the same «…» passage follows the table.
    assert result.blocks[7]["text"].startswith("<*>")
    assert result.blocks[7]["amended_by"] == ["Приказ № 170"]
    # Ж-2 is a different zone: its table is untouched.
    assert result.blocks[4] == BASE[4]
    assert "amended_by" not in result.blocks[4]


def test_numbered_rows_are_placed_by_number_and_cells_get_words():
    result = _apply(
        op(
            scope=["Статья 17.1", "Ж-2.15"],
            target="rows",
            table="предельные размеры земельных участков",
            rows=["7", "9"],
            column=2,
            action="append_words",
            text="кроме кода 2.7.2",
        ),
        op(
            scope=["Статья 17.1", "Ж-2.15"],
            target="rows",
            table="предельные параметры",
            content_blocks=[9],
        ),
    )
    assert [r.status for r in result.results] == ["applied", "applied"]
    rows = _rows(result.blocks[8])
    assert [r[0] for r in rows] == ["1.", "7.", "7.1.", "9.", "10."]
    assert rows[1][1] == "Максимальная высота кроме кода 2.7.2"
    assert rows[3][1] == "Максимальный процент застройки кроме кода 2.7.2"
    assert "кроме кода 2.7.2" in result.blocks[8]["text"]


def test_parts_and_items_follow_nested_numbering():
    result = _apply(
        op(
            item="2",
            item_block=11,
            scope=["Статья 19", "часть 1"],
            target="item",
            numbering="5",
            content_blocks=[12],
        ),
        op(
            item="3",
            item_block=13,
            scope=["Статья 19", "часть 1"],
            target="item",
            numbering="2",
            action="replace_words",
            find="с соблюдением условий",
            text="по закону",
        ),
        op(
            item="4",
            item_block=14,
            scope=["Статья 19", "часть 1"],
            target="item",
            numbering="3",
            action="repeal",
        ),
    )
    assert [r.status for r in result.results] == ["applied"] * 3
    texts = [b["text"] for b in result.blocks[13:21]]
    assert texts == [
        "1. Общие требования",
        "1. Действие регламентов не распространяется на памятники.",
        "2. Владение участком осуществляется по закону.",
        "3. Утратил силу.",
        "4. Использование зон охраны ведется по проектам.",
        # Item 5 closes part 1, before part 2 — not after the article's last "4.".
        "5. Режим зоны «Столбы верстовые» устанавливается проектом.",
        "2. Перечень зон охраны",
        "ОЗ 1 — единая охранная зона",
    ]


def test_a_wrong_article_is_forgiven_only_for_a_zone_code():
    result = _apply(
        op(
            scope=["Статья 17.1", "О-8.15"],  # the zone is in article 17.3
            target="rows",
            table="виды разрешенного использования",
            section="условно разрешенные виды использования",
            content_blocks=[3],
        ),
        op(scope=["Статья 17.2", "Жилые зоны"], content_blocks=[3]),
    )
    first, second = result.results
    assert first.status == "applied"
    assert "без «Статья 17.1»" in first.reason
    assert _rows(result.blocks[11])[5][0] == "Размещение гаражей"
    assert second.status == "failed"
    assert "не найдено место" in second.reason


def test_a_heading_title_given_as_its_own_element_is_the_same_heading():
    result = _apply(
        op(scope=["Статья 17.3", "ОБЩЕСТВЕННО-ДЕЛОВЫЕ ЗОНЫ"], content_blocks=[12])
    )
    assert result.results[0].status == "applied"
    # Inserted at the end of article 17.3, i.e. before article 19.
    assert result.blocks[12]["text"].startswith("5. Режим зоны")
    assert result.blocks[13]["text"] == "Статья 19. ОХРАНА ОБЪЕКТОВ НАСЛЕДИЯ"


def test_bad_operations_fail_and_the_rest_still_apply():
    result = _apply(
        op(scope=[], content_blocks=[12]),  # nowhere
        op(scope=["Статья 19"], content_blocks=[1]),  # not quoted new text
        op(
            scope=["Статья 19"],
            action="replace_words",
            find="слов нет в акте",
            text="x",
        ),
        op(
            scope=["Статья 17.1", "Ж-2.15"],
            target="rows",
            table="виды разрешенного использования",
            content_blocks=[3],
        ),  # sectioned table, no section
        op(
            scope=["Статья 19", "часть 1"],
            target="item",
            numbering="4",
            content_blocks=[12],
        ),  # item 4 exists already
        op(
            scope=["Статья 19", "часть 1"],
            target="item",
            numbering="2",
            action="replace_words",
            find="с соблюдением условий",
            text="по закону",
        ),
    )
    reasons = [r.reason for r in result.results]
    assert [r.status for r in result.results] == ["failed"] * 5 + ["applied"]
    assert "не указано место" in reasons[0]
    assert "не в кавычках" in reasons[1]
    assert "текста нет в правке" in reasons[2]
    assert "не указан раздел" in reasons[3]
    assert "уже есть" in reasons[4]
    assert result.failed == result.results[:5]


def test_quoted_blocks_balance_nested_quotes():
    assert quoted_blocks(ACT) == {3, 4, 9}
    assert quoted_blocks([_p("«начало «имя»"), _p("середина"), _p("конец».")]) == {1}


class FakeClient:
    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []

    def chat(self, system, user, schema, model=None):
        self.calls.append(user)
        return self.answers.pop(0)


def test_extraction_drops_operations_read_off_quoted_text_or_appendices():
    good = op(item="1.1", item_block=1, scope=["Ж-2.15"], content_blocks=[3, 99])
    inside = op(
        item="1", item_block=4, scope=["Статья 1"]
    )  # a numbered line of new text
    appendix = op(item="", item_block=14, scope=["Раздел 2"])
    client = FakeClient([{"operations": [good, inside, appendix, good]}])
    ops = extract_operations(ACT, client)
    assert len(ops) == 1
    assert ops[0]["item"] == "1.1"
    assert ops[0]["content_blocks"] == [3]  # ids outside the window are dropped
    assert "[3] <table>" in client.calls[0]
