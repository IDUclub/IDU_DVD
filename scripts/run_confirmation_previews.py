"""Run the frozen holdout through the approved LLM, saving local previews only."""

import argparse
import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

for key, value in {
    "DVD_SERVICE_AUTH_SERVER_URL": "http://localhost:9999",
    "DVD_SERVICE_AUTH_REALM": "test",
    "DVD_SERVICE_AUTH_CLIENT_ID": "test",
    "DVD_SERVICE_AUTH_CLIENT_SECRET": "test",
}.items():
    os.environ.setdefault(key, value)

from scripts.preview_fragments import FragmentPreview
from src.api_clients import create_llm
from src.common.config import settings

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs/diagnostics/accuracy-confirmation-2026-09-15"


def main():
    cli = argparse.ArgumentParser()
    cli.add_argument("--revision-output", type=Path)
    args = cli.parse_args()
    reference = json.loads((OUT / "gold.json").read_text(encoding="utf8"))
    for file, sha in reference["code_sha256"].items():
        assert (
            args.revision_output
            or hashlib.sha256((ROOT / file).read_bytes()).hexdigest() == sha
        ), file
    target = (args.revision_output or OUT) / "previews"
    if args.revision_output:
        assert args.revision_output.resolve() != OUT.resolve()
    target.mkdir(parents=True, exist_ok=True)

    def run(spec):
        name = spec["document"]
        destination = target / (name + ".json")
        if destination.exists():
            print("EXISTS", name, flush=True)
            return
        print("START", name, flush=True)
        client = create_llm()
        try:
            report = FragmentPreview(settings).build(ROOT / "docs_data" / name, client)
            destination.write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf8"
            )
            print("COMPLETE", name, report["fragment_count"], flush=True)
        finally:
            client.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(run, reference["documents"]))


if __name__ == "__main__":
    main()
