"""Reproduce loss of a retrieved clause in gMART's actual context formatter.

Uses synthetic fragments, no services or credentials. Exit 1 means the formatter
discarded the target text; exit 0 means every case preserved it. The sibling gMART
checkout is read only. This probe does not establish what happened in production.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--context-builder",
        type=Path,
        default=(
            Path(__file__).resolve().parents[2]
            / "gMART/src/agents/services/dvd/dvd_context.py"
        ),
        help="Path to the actual gMART dvd_context.py module",
    )
    args = parser.parse_args()
    path = args.context_builder.resolve()
    spec = importlib.util.spec_from_file_location("dvd_context_diagnostic", path)
    if spec is None or spec.loader is None:
        parser.error(f"Cannot load context builder: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    builder = module.DvdContextBuilder()

    # A marker, not the actual wording of the regulation.
    target = "3.3 SYNTHETIC_TARGET_CLAUSE_TEXT"
    base = {
        "name": "СП 2.13130.2020",
        "version": "2020",
        "numbering": "3.3",
        "text": target,
    }
    contexts = {
        "without_neighbours": None,
        "short_previous_fragment": "Previous fragment. " + target,
        "long_previous_fragment": "P" * 1501 + " " + target,
        "long_following_fragment": target + " " + "N" * 1501,
    }
    failed = []
    for name, context in contexts.items():
        hit = {**base, "context": context}
        rendered = builder.build_context([hit])
        preserved = target in rendered
        print(
            json.dumps(
                {
                    "case": name,
                    "target_in_search_hit": target in hit["text"],
                    "target_in_llm_context": preserved,
                }
            )
        )
        if not preserved:
            failed.append(name)
    if failed:
        print("FAIL: retrieved target lost in " + ", ".join(failed))
        return 1
    print("PASS: every case preserves the retrieved target")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
