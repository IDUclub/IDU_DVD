"""Extract local reference DOCX paragraphs for independent structure annotation."""

import json
from pathlib import Path

from src.dvd_service.modules.docx_reader import DocxReader

root = Path(__file__).resolve().parents[1]
out = root / "docs/diagnostics/structure-accuracy"
out.mkdir(parents=True, exist_ok=True)
paths = sorted((root / "docs_data").glob("*.docx"))
paths.append(
    root
    / "docs/diagnostics/dev-parsing-ranges-2026-09-15/4b8213aa-a125-43bf-b2e4-d951d0dfaa4f.docx"
)
corpus = {}
for path in paths:
    try:
        rows = DocxReader().read(str(path))
    except ValueError as exc:
        print("FAILED", path.name, str(exc), flush=True)
        continue
    corpus[path.name] = rows
    print(path.name, len(rows), flush=True)
(out / "source-corpus.json").write_text(
    json.dumps(corpus, ensure_ascii=False, indent=2), encoding="utf8"
)
