"""Score a fresh read-only parser preview against the frozen source addresses."""

import argparse
import json
from pathlib import Path


def main():
    cli = argparse.ArgumentParser()
    cli.add_argument("preview", type=Path)
    cli.add_argument(
        "--corpus-dir", type=Path, default=Path("docs/diagnostics/structure-accuracy")
    )
    args = cli.parse_args()
    preview = json.loads(args.preview.read_text(encoding="utf8"))
    name = Path(preview["source"]).name
    raw = json.loads(
        (args.corpus_dir / "source-corpus.json").read_text(encoding="utf8")
    )[name]
    gold = json.loads((args.corpus_dir / "gold.json").read_text(encoding="utf8"))
    nodes = preview["fragments"]
    by_id = {n["id"]: n for n in nodes}
    offsets, position = [], 0
    for block in raw:
        offsets.append(position)
        position += len(block["text"]) + 1
    checks = []
    for expected in (g for g in gold if g["document"] == name):
        hit = next(
            (n for n in nodes if n.get("char_start") == offsets[expected["row"]]), {}
        )
        parent = by_id.get(hit.get("parent_id"), {})
        checks.append(
            {
                **expected,
                "address_ok": hit.get("numbering") == expected["number"],
                "parent_ok": parent.get("numbering", "") == expected["parent"],
                "actual_number": hit.get("numbering"),
                "actual_parent": parent.get("numbering"),
            }
        )
    source = "\n".join(b["text"] for b in raw)
    grounded = [n for n in nodes if n.get("source_text") is not None]
    report = {
        "document": name,
        "llm_model": preview["llm_model"],
        "logical_partition_mode": preview["logical_partition_mode"],
        "checked": len(checks),
        "passed": sum(c["address_ok"] and c["parent_ok"] for c in checks),
        "source_exact": all(
            n["source_text"] == source[n["char_start"] : n["char_end"]]
            for n in grounded
        ),
        "source_complete_in_reading_order": "".join(n["source_text"] for n in grounded)
        == source,
        "checks": checks,
    }
    args.preview.with_name("score.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf8"
    )
    print(
        json.dumps(
            {k: v for k, v in report.items() if k != "checks"}, ensure_ascii=False
        )
    )
    for check in checks:
        if not (check["address_ok"] and check["parent_ok"]):
            print(json.dumps(check, ensure_ascii=False))


if __name__ == "__main__":
    main()
