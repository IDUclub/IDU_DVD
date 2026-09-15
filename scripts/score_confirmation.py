"""Evaluate frozen source-reviewed holdout; never alter production parser rules."""

import argparse
import bisect
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs/diagnostics/accuracy-confirmation-2026-09-15"


def score(spec, raw, nodes):
    source = "\n".join(b["text"] for b in raw)
    assert hashlib.sha256(source.encode()).hexdigest() == spec["source_sha256"]
    offsets, pos = [], 0
    for b in raw:
        offsets.append(pos)
        pos += len(b["text"]) + 1
    by_id = {n["id"]: n for n in nodes}
    by_start = {}
    for n in nodes:
        by_start.setdefault(n.get("char_start"), []).append(n)

    def path(n):
        result, seen = [], set()
        while n and n["id"] not in seen:
            seen.add(n["id"])
            if n.get("numbering"):
                result.append(n["numbering"])
            n = by_id.get(n.get("parent_id"))
        return result[::-1]

    checks = []
    for expected in spec["addresses"]:
        candidates = by_start.get(expected["start"], [])
        hit = next(
            (n for n in candidates if n.get("numbering") == expected["number"]), None
        )
        correct_path = bool(hit) and path(hit) == expected["path"]
        kind_ok = bool(hit) and (
            expected["kind"] == "provision" or hit.get("type") == expected["kind"]
        )
        checks.append(
            {
                **expected,
                "address_ok": hit is not None,
                "path_ok": correct_path,
                "kind_ok": kind_ok,
                "actual_path": path(hit) if hit else [],
                "actual_type": hit.get("type") if hit else None,
            }
        )
    selected = set(spec["selected_rows"])
    valid = {(x["start"], x["number"]) for x in spec["addresses"]}
    table_keys = {
        (start, t["number"])
        for t in spec["tables"]
        if t["number"]
        for start in (t["caption_start"], t["body_start"])
    }
    predictions, false = [], []
    for n in nodes:
        start = n.get("char_start")
        if not n.get("numbering") or start is None:
            continue
        row = bisect.bisect_right(offsets, start) - 1
        if row not in selected or (start, n["numbering"]) in table_keys:
            continue
        predictions.append(n)
        if (start, n["numbering"]) not in valid:
            false.append(
                dict(
                    row=row,
                    number=n["numbering"],
                    type=n["type"],
                    source=n.get("source_text"),
                )
            )
    tables = []
    for t in spec["tables"]:
        start = t["body_start"]
        end = start + len(raw[t["body_row"]]["text"])
        hit = next(
            (
                n
                for n in nodes
                if n.get("char_start") is not None
                and n["char_start"] <= start
                and n["char_end"] >= end
                and n.get("kind") == "table"
                and n.get("table_html")
            ),
            None,
        )
        tables.append(
            {
                **t,
                "preserved": hit is not None,
                "number_on_table": (
                    bool(hit) and hit.get("numbering") == t["number"]
                    if t["number"]
                    else None
                ),
            }
        )
    grounded = [n for n in nodes if n.get("source_text") is not None]
    return dict(
        document=spec["document"],
        expected_addresses=len(checks),
        recovered=sum(c["address_ok"] for c in checks),
        correct_paths=sum(c["path_ok"] for c in checks),
        correct_paths_and_kinds=sum(c["path_ok"] and c["kind_ok"] for c in checks),
        predicted_addresses=len(predictions),
        false_addresses=len(false),
        tables_preserved=sum(t["preserved"] for t in tables),
        tables_checked=len(tables),
        source_exact=all(
            n["source_text"] == source[n["char_start"] : n["char_end"]]
            for n in grounded
        ),
        source_complete="".join(n["source_text"] for n in grounded) == source,
        checks=checks,
        false_predictions=false,
        tables=tables,
    )


def main():
    cli = argparse.ArgumentParser()
    cli.add_argument(
        "--revision-output",
        type=Path,
        help="Evaluate changed code against unchanged gold into a NEW directory",
    )
    cli.add_argument(
        "--previews",
        type=Path,
        help="Directory with one <DOCX filename>.json preview per document",
    )
    cli.add_argument(
        "--deterministic",
        action="store_true",
        help="Diagnostic with neutral model labels, NOT a fresh LLM accuracy test",
    )
    args = cli.parse_args()
    assert bool(args.previews) != args.deterministic
    gold = json.loads((OUT / "gold.json").read_text(encoding="utf8"))
    for file, sha in gold["code_sha256"].items():
        assert args.revision_output or (
            hashlib.sha256((ROOT / file).read_bytes()).hexdigest() == sha
        ), f"Code changed since freeze: {file}"
    corpus = json.loads(
        (ROOT / "docs/diagnostics/structure-accuracy/source-corpus.json").read_text(
            encoding="utf8"
        )
    )
    results = []
    for spec in gold["documents"]:
        name = spec["document"]
        if args.deterministic:
            from scripts.evaluate_structure_corpus import build

            _, nodes = build(corpus[name])
        else:
            p = args.previews / (name + ".json")
            if not p.exists():
                print("PENDING", name)
                continue
            nodes = json.loads(p.read_text(encoding="utf8"))["fragments"]
        result = score(spec, corpus[name], nodes)
        results.append(result)
        print(
            json.dumps(
                {
                    k: v
                    for k, v in result.items()
                    if k not in {"checks", "false_predictions", "tables"}
                },
                ensure_ascii=False,
            )
        )
    target = args.revision_output or OUT
    target.mkdir(parents=True, exist_ok=True)
    if args.revision_output:
        assert target.resolve() != OUT.resolve(), "Preserve baseline results"
        (target / "code-sha256.json").write_text(
            json.dumps(
                {
                    file: hashlib.sha256((ROOT / file).read_bytes()).hexdigest()
                    for file in gold["code_sha256"]
                },
                indent=2,
            ),
            encoding="utf8",
        )
    (
        target / ("deterministic.json" if args.deterministic else "fresh.json")
    ).write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf8")


if __name__ == "__main__":
    main()
