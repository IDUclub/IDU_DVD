import json

import pytest

from src.api_clients.base import LlmError
from src.dvd_service.modules.doc_parsers import DocumentParser
from src.dvd_service.modules.hierarchy import HierarchyBuilder
from src.dvd_service.modules.range_partitioning import RangePartitioner
from src.dvd_service.modules.structure import StructureTagger
from src.dvd_service.services.dvd_service import IngestionService


@pytest.fixture
def parser(settings):
    settings.logical_partition_mode = "ranges"
    settings.sent_min_len = 1
    return DocumentParser(settings)


def raw(*texts):
    return [
        {"text": text, "category": "NarrativeText", "source_paragraph": True}
        for text in texts
    ]


def units_for(texts):
    units, cursor = [], 0
    for i, text in enumerate(texts):
        units.append(
            {
                "id": i,
                "text": text,
                "category": "NarrativeText",
                "char_start": cursor,
                "char_end": cursor + len(text),
                "src_ids": [i],
                "must_start": i == 0,
            }
        )
        cursor += len(text)
    return units


@pytest.mark.parametrize(
    "rows",
    [
        [],
        [{"start_id": 1, "end_id": 2}],
        [{"start_id": 0, "end_id": 0}],
        [{"start_id": 0, "end_id": 1}, {"start_id": 1, "end_id": 2}],
        [{"start_id": 0, "end_id": 0}, {"start_id": 2, "end_id": 2}],
        [{"start_id": 0, "end_id": 3}],
        [{"start_id": -1, "end_id": 2}],
        [{"start_id": 0, "end_id": -1}],
        [{"start_id": False, "end_id": 2}],
        [{"start_id": "0", "end_id": 2}],
        [{"start_id": 0, "end_id": 2, "text": "Переписанный текст"}],
    ],
)
def test_invalid_partition_is_rejected(settings, rows):
    with pytest.raises(LlmError):
        RangePartitioner(settings).validate(
            {"fragments": rows}, units_for(["a", "b", "c"])
        )


@pytest.mark.parametrize("size", [511, 512, 513])
def test_merged_fragment_size_limit(settings, size):
    units = units_for(["я" * 250, "ю" * (size - 250)])
    partitioner = RangePartitioner(settings)
    response = {"fragments": [{"start_id": 0, "end_id": 1}]}
    if size <= 512:
        assert partitioner.validate(response, units) == [(0, 1)]
    else:
        with pytest.raises(LlmError, match="512"):
            partitioner.validate(response, units)
    # Oversized source leaves are indivisible, as in the structural grouping mode.
    assert partitioner.validate(
        {"fragments": [{"start_id": 0, "end_id": 0}]}, units_for(["я" * 600])
    ) == [(0, 0)]


def test_protected_boundary_is_not_mergeable(settings):
    units = units_for(["a", "b"])
    units[1]["must_start"] = True
    with pytest.raises(LlmError, match="структурную"):
        RangePartitioner(settings).validate(
            {"fragments": [{"start_id": 0, "end_id": 1}]}, units
        )


def test_invalid_window_is_retried_and_fails_closed(settings, monkeypatch):
    monkeypatch.setattr(
        "src.dvd_service.modules.range_partitioning.time.sleep", lambda _: None
    )
    calls = []

    class BadClient:
        def chat(self, system, user, schema):
            calls.append(user)
            return {"fragments": []}

    with pytest.raises(LlmError, match="после 3 попыток"):
        RangePartitioner(settings).partition(units_for(["a", "b"]), BadClient())
    assert len(calls) == 3


def test_retry_can_recover_from_missing_last_unit(settings, monkeypatch):
    monkeypatch.setattr(
        "src.dvd_service.modules.range_partitioning.time.sleep", lambda _: None
    )
    calls = []

    class Client:
        def chat(self, system, user, schema):
            calls.append(system)
            return {
                "fragments": [{"start_id": 0, "end_id": 0 if len(calls) == 1 else 1}]
            }

    assert RangePartitioner(settings).partition(units_for(["a", "b"]), Client()) == [
        (0, 1)
    ]
    assert "Исправь ошибку" in calls[-1]


def test_input_windows_have_no_overlap_and_preserve_global_order(settings, fake_ollama):
    settings.window_chars = 5
    settings.window_max_items = 2
    settings.overlap_blocks = 99
    units = units_for(["ab", "cd", "ef", "gh", "ij"])
    partitioner = RangePartitioner(settings)
    windows = partitioner.windows(units)
    assert [i for start, end in windows for i in range(start, end)] == list(range(5))
    assert all(end - start <= 2 for start, end in windows)
    assert partitioner.partition(units, fake_ollama) == [(i, i) for i in range(5)]


def test_source_is_exact_despite_repeats_unicode_and_whitespace(parser, fake_ollama):
    blocks = raw(
        "  Проверить объект.  Проверить объект.\nПроверить улицу ёлочную.  ",
        "Дополнить акт.",
    )
    parts = parser.to_logical_parts(blocks, fake_ollama)
    source, _ = parser.source_index(blocks)
    assert len(parts) >= 3
    assert "".join(p["source_text"] for p in parts) == source
    for part in parts:
        assert (
            part["source_text"]
            == source[part["char_start"] : part["char_end"]]
            == part["text"]
        )


def test_llm_can_group_sentences_without_returning_text(parser):
    class Client:
        def chat(self, system, user, schema):
            return {
                "fragments": [
                    {"start_id": 0, "end_id": len(json.loads(user)["units"]) - 1}
                ]
            }

    blocks = raw("Проверить объект. Сохранить акт.")
    parts = parser.to_logical_parts(blocks, Client())
    assert len(parts) == 1
    assert parts[0]["text"] == blocks[0]["text"]


def test_numbered_leaves_are_preserved_until_final_assembly(parser, fake_ollama):
    blocks = raw(
        "1. " + "я" * 512,
        "1.1. Требуется:",
        "- проверка;",
        "- расчёт.",
        "1.2. Оформление.",
    )
    parts = parser.to_logical_parts(blocks, fake_ollama)
    source, _ = parser.source_index(blocks)
    assert len(parts) == 5
    assert parts[1]["source_text"] == "1.1. Требуется:\n"
    assert "".join(p["source_text"] for p in parts) == source


def test_source_survives_markup_hierarchy_and_precise_grounding(parser, fake_ollama):
    blocks = raw("1. Проверить объект.", "Проверить улицу. Сохранить акт.")
    parts = parser.to_logical_parts(blocks, fake_ollama)
    source, spans = parser.source_index(blocks)
    structure = StructureTagger(parser.settings)
    structure.tag(parts, fake_ollama)
    assert (
        parts[0]["text"] != parts[0]["source_text"]
    )  # own number stripped only for display
    hierarchy = HierarchyBuilder()
    nodes = hierarchy.flatten(hierarchy.build(parts, structure.numbering_ranks(parts)))
    for node in nodes[1:]:
        grounding = IngestionService._grounding(node, spans, "doc")
        assert (
            node["source_text"]
            == source[grounding["char_start"] : grounding["char_end"]]
        )
    assert (
        nodes[2]["char_end"] < spans[1]["end"]
    )  # true subparagraph offset, not whole raw block


def test_materialization_refuses_changed_source_units(settings):
    units = units_for(["abc"])
    units[0]["text"] = "xyz"
    with pytest.raises(ValueError, match="изменён"):
        RangePartitioner(settings).materialize("abc", units, [(0, 0)])


def test_empty_source_needs_no_model(parser, fake_ollama):
    assert parser.to_logical_parts([], fake_ollama) == []
    assert not fake_ollama.chat_calls


def test_tables_keep_original_html_and_standalone_ranges(parser, fake_ollama):
    blocks = raw("Требуется:", "Таблица", "Следующий текст.")
    blocks[1].update(category="Table", html="<table><tr><td>Таблица</td></tr></table>")
    parts = parser.to_logical_parts(blocks, fake_ollama)
    assert len(parts) == 3
    assert parts[1]["category"] == "Table" and parts[1]["html"] == blocks[1]["html"]


def semantic_tree(parser, texts, types=None):
    """Real source extraction/tag anchoring with deterministic structural decisions."""
    parts = parser.to_logical_parts(raw(*texts), None)
    tagger = StructureTagger(parser.settings)
    in_article = False
    for i, part in enumerate(parts):
        part.update(
            raw_type=types[i] if types else "paragraph",
            numbering="",
            relation="deeper",
            block="main",
            tags=[],
        )
        anchor, in_article = tagger.apply_source_anchor(part, in_article)
        part["type"] = tagger.categorize(part["raw_type"])
        if anchor.get("type") not in {"article", "chapter", "section"}:
            part["text"] = tagger.strip_leading_numbering(
                part["text"], part["numbering"]
            )
    builder = HierarchyBuilder()
    tree = builder.build(
        builder.coalesce_title_pages(parts),
        tagger.numbering_ranks(parts),
        semantic=True,
    )
    builder.assemble_semantic(tree)
    nodes = builder.flatten(tree)
    source, _ = parser.source_index(raw(*texts))
    grounded = [n for n in nodes if n.get("source_text") is not None]
    assert "".join(n["source_text"] for n in grounded) == source
    assert all(
        n["source_text"] == source[n["char_start"] : n["char_end"]] for n in grounded
    )
    return tree, nodes


@pytest.mark.parametrize("size", [511, 512, 513])
def test_final_clause_size_includes_subpoints_and_source_separators(parser, size):
    texts = [
        "1. Требования:",
        "1.1. Проверка.",
        "1.2. " + "я" * (size - len("1. Требования:\n1.1. Проверка.\n1.2. ")),
    ]
    tree, nodes = semantic_tree(parser, texts)
    clause = tree["children"][0]
    assert clause["type"] == "clause" and clause["numbering"] == "1"
    assert clause["is_container"] == (size > 512)
    if size <= 512:
        assert not clause["children"]
        assert "1.1." in clause["text"] and "1.2." in clause["text"]
        assert len(clause["source_text"]) == size
    else:
        assert len(clause["children"]) == 1
        child = clause["children"][0]
        assert child["type"] == "subclause_group"
        assert child["source_text"] == "\n".join(texts[1:])
        assert nodes[-1]["search_text"].startswith("Требования:")


def test_large_children_descend_into_their_own_subtrees(parser):
    tree, _ = semantic_tree(
        parser,
        [
            "1. Требования:",
            "1.1. " + "а" * 280,
            "1.1.1. Проверка.",
            "1.2. " + "б" * 280,
            "1.2.1. Расчёт.",
            "2. Иной пункт.",
        ],
    )
    one, two = tree["children"]
    assert one["is_container"] and two["numbering"] == "2"
    assert [c["numbering"] for c in one["children"]] == ["1.1", "1.2"]
    assert all(not c["is_container"] and not c["children"] for c in one["children"])
    assert "1.1.1." in one["children"][0]["text"]
    assert "1.2.1." in one["children"][1]["text"]


def test_number_prefix_prevents_unrelated_parentage_and_keeps_section(parser):
    tree, _ = semantic_tree(
        parser, ["Глава 1. Требования", "1. Проверка.", "10.1. Другой пункт."]
    )
    chapter = tree["children"][0]
    assert chapter["type"] == "chapter"
    assert [c["numbering"] for c in chapter["children"]] == ["1", "10.1"]


def test_cover_is_one_typed_leaf_even_above_limit(parser):
    texts = [
        "ОРГАНИЗАЦИЯ " + "А" * 520,
        "Название документа",
        "14 сентября 2026 года",
        "1. Требования.",
    ]
    tree, _ = semantic_tree(
        parser, texts, ["title_page", "cover", "титульный лист", "paragraph"]
    )
    cover, clause = tree["children"]
    assert cover["type"] == "title_page" and not cover["is_container"]
    assert cover["source_text"] == "\n".join(texts[:3]) + "\n"
    assert clause["type"] == "clause"


def test_legal_article_inserted_parts_remain_siblings(parser):
    tree, _ = semantic_tree(
        parser,
        [
            "Статья 1. Требования",
            "3. Проверка.",
            "3.3. Новое правило.",
            "1) Условие.",
            "а) Исключение.",
        ],
    )
    article = tree["children"][0]
    assert [c["numbering"] for c in article["children"]] == ["3", "3.3"]
    assert "а)" in article["children"][1]["text"]


def test_repair_cuts_structural_and_size_boundaries_without_retry(settings):
    units = units_for(["а" * 300, "б" * 300, "в", "г"])
    units[3]["must_start"] = True

    class Client:
        calls = 0

        def chat(self, system, user, schema):
            self.calls += 1
            return {"fragments": [{"start_id": 0, "end_id": 3}]}

    client = Client()
    assert RangePartitioner(settings)._window(units, client) == [(0, 0), (1, 2), (3, 3)]
    assert client.calls == 1


def test_explicit_unnumbered_list_stays_with_introduction(parser):
    tree, _ = semantic_tree(
        parser, ["Необходимые документы:", "- паспорт;", "- заявление."]
    )
    assert len(tree["children"]) == 1
    paragraph = tree["children"][0]
    assert not paragraph["is_container"]
    assert (
        paragraph["source_text"] == "Необходимые документы:\n- паспорт;\n- заявление."
    )


def test_table_survives_final_assembly(parser):
    parts = parser.to_logical_parts(
        raw("1. Требования:", "Таблица", "1.1. Расчёт."), None
    )
    parts[0].update(type="clause", numbering="1")
    parts[1].update(type="table", category="Table", html="<table>данные</table>")
    parts[2].update(type="subclause", numbering="1.1")
    builder = HierarchyBuilder()
    tree = builder.build(parts, {"1": 1, "1.1": 2}, semantic=True)
    builder.assemble_semantic(tree)
    clause = tree["children"][0]
    assert clause["is_container"]
    table, subclause = clause["children"]
    assert table["html"] == "<table>данные</table>"
    assert subclause["numbering"] == "1.1"
