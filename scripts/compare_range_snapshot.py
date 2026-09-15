"""Audit downloaded range-mode fragments against their original source; no network/LLM."""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import re
from pathlib import Path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("snapshot", type=Path)
    ap.add_argument("doc_id")
    args = ap.parse_args()
    from src.dvd_service.modules.docx_reader import DocxReader

    directory = args.snapshot
    detail = json.loads(
        (directory / (args.doc_id + ".json")).read_text(encoding="utf8")
    )
    raw = DocxReader().read(str(directory / (args.doc_id + ".docx")))
    source = "\n".join(b["text"] for b in raw)
    source_hash = hashlib.sha256(source.encode()).hexdigest()
    hash_matches = source_hash == detail["content_hash"]
    if not hash_matches:
        print(
            "WARNING: source hash differs from catalog; assess exact source slices before concluding source identity"
        )
    fs = detail["fragments"]
    byid = {f["id"]: f for f in fs}
    coverage = bytearray(len(source))
    spans = []
    errors = []
    norm = lambda s: " ".join(s.split())
    for f in fs:
        a, b = f.get("char_start"), f.get("char_end")
        if a is None or b is None:
            continue
        if not 0 <= a <= b <= len(source):
            errors.append({"id": f["id"], "issue": "invalid_offsets"})
            continue
        coverage[a:b] = b"\x01" * (b - a)
        original = source[a:b]
        display = norm(f["text"])
        stripped = (
            re.sub(r"^" + re.escape(f["numbering"]) + r"[.)]?\s*", "", norm(original))
            if f["numbering"]
            else norm(original)
        )
        spans.append(
            {
                "id": f["id"],
                "order": f["order"],
                "numbering": f["numbering"],
                "source_exact": f.get("source_text") == original,
                "display_exact": display in [norm(original), stripped],
                "length": b - a,
                "source": original,
                "text": f["text"],
            }
        )
    numbered = []
    pos = 0
    for block in raw:
        t = block["text"]
        m = re.match(r"^(\d+\.\d+(?:\.\d+)*)\s+", t)
        if m:
            num = m[1]
            hits = [f for f in fs if f["numbering"] == num]
            covering = [
                f
                for f in fs
                if f.get("char_start") is not None
                and f["char_start"] < pos + len(t)
                and f["char_end"] > pos
            ]
            source_covering = sorted(covering, key=lambda f: f["char_start"])
            numbered.append(
                {
                    "number": num,
                    "direct_orders": [f["order"] for f in hits],
                    "covering_orders": [f["order"] for f in covering],
                    "text_present": norm(t)
                    in norm(
                        "".join(f.get("source_text") or "" for f in source_covering)
                    ),
                    "parents": [
                        {
                            "order": f["order"],
                            "parent_number": byid.get(f["parent_id"], {}).get(
                                "numbering"
                            ),
                            "parent_type": byid.get(f["parent_id"], {}).get("type"),
                            "expected_parent": num.rsplit(".", 1)[0],
                        }
                        for f in hits
                    ],
                }
            )
        pos += len(t) + 1
    for f in fs:
        if f.get("parent_id") and f["parent_id"] not in byid:
            errors.append({"id": f["id"], "issue": "missing_parent"})
        for cid in f.get("child_ids", []):
            if cid not in byid or byid[cid].get("parent_id") != f["id"]:
                errors.append(
                    {"id": f["id"], "issue": "child_parent_mismatch", "child": cid}
                )
    numbers = {x["number"] for x in numbered}
    numbered_nodes = [f for f in fs if f["numbering"] in numbers]
    good_parents = [
        f
        for f in numbered_nodes
        if byid.get(f["parent_id"], {}).get("numbering")
        == f["numbering"].rsplit(".", 1)[0]
    ]
    missing = sorted(numbers - {f["numbering"] for f in fs})
    summary = {
        "name": detail["name"],
        "uploaded_at": detail["uploaded_at"],
        "fragments": len(fs),
        "source_hash": source_hash,
        "source_chars": len(source),
        "uncovered_nonspace": sum(
            not c.isspace() and not coverage[i] for i, c in enumerate(source)
        ),
        "grounded": len(spans),
        "source_exact": sum(x["source_exact"] for x in spans),
        "display_exact": sum(x["display_exact"] for x in spans),
        "search_text_present": sum(bool(f.get("search_text")) for f in fs),
        "containers": sum(f.get("is_container", False) for f in fs),
        "unique_source_numbers": len(numbers),
        "direct_unique_numbers": len(numbers) - len(missing),
        "missing_numbers": missing,
        "numbered_nodes": len(numbered_nodes),
        "correct_number_parent": len(good_parents),
        "source_blocks": len(numbered),
        "source_blocks_preserved": sum(x["text_present"] for x in numbered),
        "integrity_errors": len(errors),
        "types": dict(collections.Counter(f["type"] for f in fs)),
    }
    report = {
        "summary": summary,
        "spans": spans,
        "numbered": numbered,
        "integrity_errors": errors,
    }
    summary["catalog_hash_matches"] = hash_matches
    summary["catalog_content_hash"] = detail["content_hash"]
    (directory / (args.doc_id + "-comparison.json")).write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
