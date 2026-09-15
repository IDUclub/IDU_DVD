"""Check final answers and literal retrieval constraints from saved live traces."""

import argparse
import json
import re
from pathlib import Path

EXPECTED = {
    "sp55_short": ("3.3", "СП 55"),
    "sp55_full": ("3.3", "СП 55.13330.2016"),
    "point_then_section": ("раздел 3 / 3.3", "СП 55"),
    "section_then_point": ("раздел 3 / 3.3", "СП 55"),
    "nested_children": ("10.6", "СП 55"),
    "wrong_document": ("3.3", "СП 550"),
    "missing_clause": ("999.9", "СП 55"),
    "repeated": ("3.3", "СП 999"),
    "repeated_explicit_path": ("раздел II / 3.3", "СП 999"),
    "constitution_article": ("статья 19", "Конституция Российской Федерации"),
    "constitution_part": ("статья 19 / 1", "Конституция Российской Федерации"),
    "constitution_part_reordered": (
        "статья 19 / 1",
        "Конституция Российской Федерации",
    ),
}


def main():
    cli = argparse.ArgumentParser()
    cli.add_argument("directory", type=Path)
    args = cli.parse_args()
    checks = []
    for case in [*EXPECTED, "choice_followup"]:
        path = args.directory / (case + ".json")
        if not path.exists():
            checks.append(dict(case=case, passed=False, defects=["pending"]))
            continue
        data = json.loads(path.read_text(encoding="utf8"))
        defects = []
        answer = data["answer"]
        explanation = answer.split("Полная цитата:", 1)[0]
        if "[N]" in explanation:
            defects.append("unresolved source label")
        if re.search(
            r"\bтекст\b[^!?\n]{0,100}(?:не привед[её]н|отсутствует)", explanation, re.I
        ):
            defects.append("explanation contradicts quotation")
        if "Не удалось подтвердить объяснение" in explanation:
            defects.append("requested explanation unavailable")
        calls = data["calls"]
        if data["error"]:
            defects.append(data["error"])
        if not calls or any(c["mode"] != "structure" for c in calls):
            defects.append("structural search required")
        if case in EXPECTED:
            pattern, document = EXPECTED[case]
            for call in calls:
                req = call["request"]
                if req.get("pattern") != pattern or req.get("document_names") != [
                    document
                ]:
                    defects.append("explicit constraints changed")
        if case in {"wrong_document", "missing_clause"}:
            if "совпадений не найдено" not in answer:
                defects.append("expected scoped not-found")
        elif case == "repeated":
            if (
                "Уточните документ" not in answer
                or len([l for l in answer.splitlines() if l.startswith("- ")]) != 2
            ):
                defects.append("expected exactly two genuine choices")
        else:
            if "Полная цитата:" not in answer:
                defects.append("missing full quotation")
            elif calls:
                if answer.count("Полная цитата:") != 1:
                    defects.append("duplicate quotation")
                quote = answer.split("Полная цитата:", 1)[1]
                # Current cases fit one page; the pagination unit test covers
                # multi-page retrieval and a condition arriving on its last page.
                response = calls[-1]["response"]
                if response.get("complete") is not True:
                    defects.append("incomplete source")
                for hit in response.get("hits", []):
                    body = (
                        hit.get("table_html")
                        or hit.get("source_text")
                        or hit.get("text")
                        or ""
                    ).strip()
                    rendered = "\n".join("> " + line for line in body.split("\n"))
                    if rendered not in quote:
                        defects.append(
                            "source omitted or rewritten: " + hit.get("id", "?")
                        )
                ordered = sorted(
                    response.get("hits", []),
                    key=lambda h: (
                        h.get("char_start")
                        if h.get("char_start") is not None
                        else h.get("order", 0)
                    ),
                )
                positions = []
                for hit in ordered:
                    body = (
                        hit.get("table_html")
                        or hit.get("source_text")
                        or hit.get("text")
                        or ""
                    ).strip()
                    rendered = "\n".join("> " + line for line in body.split("\n"))
                    positions.append(quote.find(rendered))
                if positions != sorted(positions):
                    defects.append("source reading order changed")
            if case == "choice_followup" and calls:
                if "II" not in calls[-1]["request"].get("pattern", ""):
                    defects.append("selected section lost")
            if case.startswith("sp55_") or case in {
                "point_then_section",
                "section_then_point",
            }:
                if any(
                    s in answer.casefold().replace("ё", "е")
                    for s in ("зеленый дом", "зеленого дома", "энергоэффективност")
                ):
                    defects.append("neighbour 3.2 contamination")
            if "Уточните документ" in answer:
                defects.append("unnecessary clarification")
        checks.append(
            dict(
                case=case,
                passed=not defects,
                retrieval_quote_passed=all(
                    d == "requested explanation unavailable" for d in defects
                ),
                defects=defects,
                search_calls=len(calls),
                explanation_available="Не удалось подтвердить объяснение" not in answer,
            )
        )
    (args.directory / "acceptance.json").write_text(
        json.dumps(checks, ensure_ascii=False, indent=2), encoding="utf8"
    )
    for row in checks:
        print(row["case"], "PASS" if row["passed"] else "FAIL", row["defects"])
    if not all(row["passed"] for row in checks):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
