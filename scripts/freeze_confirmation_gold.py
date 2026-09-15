"""Freeze a source-reviewed holdout, without importing the parser under test.

Row selections were reviewed from DOCX text before looking at parser outputs.
This is a purposive structural coverage sample, not a random population sample.
"""

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs/diagnostics/accuracy-confirmation-2026-09-15"
CORPUS = ROOT / "docs/diagnostics/structure-accuracy/source-corpus.json"

SPECS = {
    "СП_311.1325800.2017_с_И1.docx": {
        "windows": [[32, 268]],
        "sections": [32, 38, 50, 54, 58, 65, 166, 202, 222, 245, 260],
        "addresses": [
            52,
            53,
            55,
            57,
            59,
            60,
            62,
            64,
            66,
            67,
            71,
            78,
            91,
            93,
            94,
            102,
            120,
            124,
            125,
            133,
            141,
            144,
            145,
            146,
            159,
            160,
            163,
            165,
            167,
            168,
            169,
            172,
            182,
            183,
            184,
            185,
            186,
            188,
            189,
            190,
            195,
            204,
            210,
            211,
            213,
            216,
            217,
            219,
            220,
            223,
            224,
            225,
            246,
            248,
            251,
            253,
            255,
            257,
            258,
            261,
            263,
            264,
            266,
            267,
        ],
        "appendices": [],
        "tables": [
            [79, 80, "6.1"],
            [81, 82, "6.2"],
            [83, 84, "6.3"],
            [86, 87, "6.4"],
            [88, 89, "6.5"],
            [98, 99, "6.6"],
            [111, 112, "6.7"],
            [115, 116, "6.8"],
            [117, 118, "6.8а"],
            [121, 122, "6.9"],
            [138, 139, "6.10"],
            [142, 143, "6.11"],
        ],
    },
    "СП_383.1325800.2018_с_И1_И2.docx": {
        "windows": [[90, 149], [300, 376]],
        "sections": [110, 131, 147, 300, 310, 318, 338],
        "addresses": [
            111,
            113,
            114,
            115,
            116,
            117,
            118,
            119,
            120,
            121,
            122,
            132,
            138,
            139,
            140,
            142,
            144,
            145,
            148,
            149,
            301,
            303,
            304,
            305,
            306,
            307,
            309,
            311,
            313,
            314,
            315,
            316,
            319,
            320,
            321,
            322,
            323,
            324,
            325,
            327,
            328,
            330,
            332,
            333,
            335,
            336,
            339,
            340,
            343,
            344,
            345,
            349,
            350,
            351,
            353,
            355,
            357,
        ],
        "appendices": [358, 361],
        "tables": [[None, 360, None], [None, 363, None]],
    },
    "СП_462.1325800.2019_с_И1.docx": {
        "windows": [[203, 310]],
        "sections": [203, 218, 231],
        "addresses": [
            204,
            205,
            206,
            207,
            208,
            209,
            210,
            211,
            212,
            214,
            215,
            216,
            217,
            219,
            220,
            222,
            223,
            224,
            225,
            226,
            227,
            228,
            229,
            230,
            232,
            233,
            234,
            235,
            236,
            237,
            238,
            239,
            240,
            241,
            242,
            243,
            244,
            245,
            246,
            247,
            248,
            249,
            251,
            252,
            253,
            254,
            255,
            260,
            261,
            262,
            263,
            264,
            265,
        ],
        "appendices": [266, 270, 274, 289, 293, 299, 303, 307],
        "tables": [
            [268, 269, "А.1"],
            [272, 273, "Б.1"],
            [287, 288, "В.1"],
            [291, 292, "Г.1"],
            [295, 296, "Д.1"],
            [297, 298, "Д.2"],
            [301, 302, "Е.1"],
            [305, 306, "Ж.1"],
            [309, 310, "И.1"],
        ],
    },
}


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    target = OUT / "gold.json"
    if target.exists():
        raise SystemExit(
            "Reference already frozen; do not overwrite it after evaluation"
        )
    corpus = json.loads(CORPUS.read_text(encoding="utf8"))
    documents = []
    for name, spec in SPECS.items():
        raw = corpus[name]
        offsets, position = [], 0
        for block in raw:
            offsets.append(position)
            position += len(block["text"]) + 1
        expected = []
        for kind, rows in [
            ("section", spec["sections"]),
            ("provision", spec["addresses"]),
            ("appendix", spec["appendices"]),
        ]:
            for row in rows:
                text = raw[row]["text"]
                number = text.split()[1] if kind == "appendix" else text.split()[0]
                assert number[0].isdigit() or kind == "appendix", (row, text)
                components = number.split(".")
                path = [".".join(components[:i]) for i in range(1, len(components) + 1)]
                expected.append(
                    dict(
                        row=row,
                        start=offsets[row],
                        number=number,
                        kind=kind,
                        path=path,
                        source=text,
                    )
                )
        selected = sorted({i for lo, hi in spec["windows"] for i in range(lo, hi + 1)})
        assert {x["row"] for x in expected}.issubset(selected)
        tables = [
            dict(
                caption_row=a,
                body_row=b,
                number=n,
                body_start=offsets[b],
                caption_start=offsets[a] if a is not None else None,
            )
            for a, b, n in spec["tables"]
        ]
        assert all(raw[t["body_row"]]["category"] == "Table" for t in tables)
        documents.append(
            dict(
                document=name,
                source_sha256=hashlib.sha256(
                    "\n".join(b["text"] for b in raw).encode()
                ).hexdigest(),
                windows=spec["windows"],
                selected_rows=selected,
                addresses=sorted(expected, key=lambda x: x["row"]),
                tables=tables,
            )
        )
    code = {
        str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in [
            ROOT / "src/dvd_service/modules/source_structure.py",
            ROOT / "src/dvd_service/modules/structure.py",
            ROOT / "src/dvd_service/modules/hierarchy.py",
            ROOT / "src/dvd_service/modules/doc_parsers.py",
            ROOT / "src/dvd_service/modules/docx_reader.py",
        ]
    }
    reference = dict(
        method="Source-reviewed row selections, frozen before holdout parser outputs",
        code_sha256=code,
        documents=documents,
    )
    target.write_text(
        json.dumps(reference, ensure_ascii=False, indent=2), encoding="utf8"
    )
    print(
        json.dumps(
            {
                "source_rows": sum(len(d["selected_rows"]) for d in documents),
                "addresses": sum(len(d["addresses"]) for d in documents),
                "tables": sum(len(d["tables"]) for d in documents),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
