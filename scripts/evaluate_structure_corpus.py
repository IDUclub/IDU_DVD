"""Local structural audit with a frozen source-derived reference set and noisy LLM labels.

The generated audit is diagnostic, not an independently annotated accuracy estimate.
Pass --gold to score the separately reviewed reference rows instead.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path

for key, value in {
    "DVD_LLM_BASE_URL": "http://localhost:9999/v1",
    "DVD_SERVICE_AUTH_SERVER_URL": "http://localhost:9999",
    "DVD_SERVICE_AUTH_REALM": "test",
    "DVD_SERVICE_AUTH_CLIENT_ID": "test",
    "DVD_SERVICE_AUTH_CLIENT_SECRET": "test",
}.items():
    os.environ.setdefault(key, value)

from src.common.config import Settings
from src.dvd_service.modules.doc_parsers import DocumentParser
from src.dvd_service.modules.hierarchy import HierarchyBuilder
from src.dvd_service.modules.structure import StructureTagger

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs/diagnostics/structure-accuracy"


def build(raw, recorded=None):
    settings = Settings(logical_partition_mode="ranges")
    parser = DocumentParser(settings)
    parts = parser.to_logical_parts(raw, None)

    class Labels:
        def chat(self, system, user, schema):
            rows = []
            for m in re.finditer(
                r"^\[(\d+)\] (.*?)(?=^\[\d+\] |\Z)", user, re.M | re.S
            ):
                text = m[2].strip()
                prior = next(
                    (
                        f
                        for f in recorded or []
                        if text and text in (f.get("source_text") or "")
                    ),
                    {},
                )
                rows.append(
                    {
                        "id": int(m[1]),
                        "type": prior.get("type", "paragraph"),
                        "numbering": prior.get("numbering", ""),
                        "relation": "deeper",
                        "block": "main",
                        "tags": [],
                        "fragment_name": "",
                    }
                )
            return {"nodes": rows}

    tagger = StructureTagger(settings)
    tagger.tag(parts, Labels())
    hb = HierarchyBuilder()
    tree = hb.build(
        hb.coalesce_title_pages(parts), tagger.numbering_ranks(parts), semantic=True
    )
    # Score source structure before optional size-based grouping; then also keep final nodes.
    structural = hb.flatten(tree)
    hb.group_amendment(tree)
    hb.assemble_semantic(tree)
    return structural, hb.flatten(tree)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gold", type=Path)
    ap.add_argument("--output", type=Path)
    ap.add_argument("--names", nargs="*")
    args = ap.parse_args()
    target = args.output or OUT
    target.mkdir(parents=True, exist_ok=True)
    corpus = json.loads((OUT / "source-corpus.json").read_text(encoding="utf8"))
    gold = json.loads(args.gold.read_text(encoding="utf8")) if args.gold else []
    results = []
    for name, raw in corpus.items():
        if args.names and name not in args.names:
            continue
        recorded = None
        snapshot = {
            "СП_55.13330.2016_с_И1_И2.docx": "a7e1c951-c663-4366-aec8-6c3ebffbef29",
            "4b8213aa-a125-43bf-b2e4-d951d0dfaa4f.docx": "4b8213aa-a125-43bf-b2e4-d951d0dfaa4f",
        }.get(name)
        if snapshot:
            recorded = json.loads(
                (
                    ROOT
                    / "docs/diagnostics/dev-parsing-ranges-2026-09-15"
                    / f"{snapshot}.json"
                ).read_text(encoding="utf8")
            )["fragments"]
        nodes, final = build(raw, recorded)
        (target / (name + ".nodes.json")).write_text(
            json.dumps(
                {"structural": nodes, "final": final}, ensure_ascii=False, indent=2
            ),
            encoding="utf8",
        )
        byid = {n["id"]: n for n in nodes}
        final_byid = {n["id"]: n for n in final}
        offsets = []
        pos = 0
        for b in raw:
            offsets.append(pos)
            pos += len(b["text"]) + 1
        checks = []
        for g in [r for r in gold if r["document"] == name]:
            start = offsets[g["row"]]
            hit = next((n for n in nodes if n.get("char_start") == start), {})
            parent = byid.get(hit.get("parent_id"), {})
            address = hit.get("numbering") == g["number"]
            parent_ok = parent.get("numbering", "") == g["parent"]
            final_hit = next((n for n in final if n.get("char_start") == start), {})
            final_parent = final_byid.get(final_hit.get("parent_id"), {})
            checks.append(
                {
                    **g,
                    "address_ok": address,
                    "parent_ok": parent_ok,
                    "final_address_ok": final_hit.get("numbering") == g["number"],
                    "final_parent_ok": final_parent.get("numbering", "") == g["parent"],
                    "actual_number": hit.get("numbering"),
                    "actual_parent": parent.get("numbering"),
                    "actual_type": hit.get("type"),
                }
            )
        source = "\n".join(b["text"] for b in raw)
        grounded = [n for n in final if n.get("source_text") is not None]
        summary = {
            "document": name,
            "source_blocks": len(raw),
            "structural_nodes": len(nodes),
            "final_nodes": len(final),
            "source_exact": all(
                n["source_text"] == source[n["char_start"] : n["char_end"]]
                for n in grounded
            ),
            "source_complete_in_reading_order": "".join(
                n["source_text"] for n in grounded
            )
            == source,
            "checks": checks,
        }
        results.append(summary)
        print(
            name,
            len(nodes),
            "gold",
            len(checks),
            "fail",
            sum(not (c["address_ok"] and c["parent_ok"]) for c in checks),
            "final_fail",
            sum(not (c["final_address_ok"] and c["final_parent_ok"]) for c in checks),
            "source",
            summary["source_complete_in_reading_order"],
            flush=True,
        )
    (target / "evaluation.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf8"
    )


if __name__ == "__main__":
    main()
