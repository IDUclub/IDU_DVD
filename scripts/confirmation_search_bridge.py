"""One-shot structured DVD search against a local memory-only verification corpus."""

import json
import os
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

for key, value in {
    "DVD_LLM_BASE_URL": "http://localhost:9999/v1",
    "DVD_SERVICE_AUTH_SERVER_URL": "http://localhost:9999",
    "DVD_SERVICE_AUTH_REALM": "test",
    "DVD_SERVICE_AUTH_CLIENT_ID": "test",
    "DVD_SERVICE_AUTH_CLIENT_SECRET": "test",
}.items():
    os.environ.setdefault(key, value)

from qdrant_client import QdrantClient, models

from src.common.config import Settings
from src.common.db.qdrant_client import QdrantRepository
from src.dvd_service.dto.fragment_search import FragmentSearchRequest
from src.dvd_service.modules.fragment_structure import annotate_fragments
from src.dvd_service.services.dvd_service import SearchService
from src.dvd_service.services.fragment_search import FragmentSearchService

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs/diagnostics/accuracy-confirmation-2026-09-15"


def points():
    previews = [
        (
            ROOT
            / "docs/diagnostics/structure-accuracy/fresh-sp55-final/fragments.json",
            "sp55",
            "СП 55.13330.2016 Дома жилые одноквартирные",
            "2016",
        )
    ]
    for path in sorted((OUT / "previews").glob("*.json")):
        code = path.name.split("_")[1]
        previews.append((path, code, "СП " + code, code.split(".")[-1]))
    result = []
    for path, doc_id, name, version in previews:
        data = json.loads(path.read_text(encoding="utf8"))
        for n in data["fragments"]:
            result.append(
                dict(n, doc_id=doc_id, name=name, version=version, versions=[version])
            )
    if extra := os.getenv("DVD_VERIFY_EXTRA_PREVIEW"):
        data = json.loads(Path(extra).read_text(encoding="utf8"))
        for n in data["fragments"]:
            result.append(
                dict(
                    n,
                    doc_id="constitution-local",
                    name="Конституция Российской Федерации",
                    version="local-source",
                    versions=["local-source"],
                )
            )
    original = next(
        n for n in result if n["doc_id"] == "sp55" and n["numbering"] == "3.3"
    )
    result.append(
        dict(
            original,
            id=str(uuid.uuid5(uuid.NAMESPACE_URL, "string-fixture")),
            doc_id="string",
            name="string",
            parent_id=None,
            child_ids=[],
        )
    )
    corpus = json.loads(
        (ROOT / "docs/diagnostics/structure-accuracy/source-corpus.json").read_text(
            encoding="utf8"
        )
    )
    text = next(
        b["text"]
        for b in corpus["СП_309.1325800.2017_с_И1.docx"]
        if b["text"].startswith("3.3 ") and "аппаратная" in b["text"]
    )
    result.extend(
        annotate_fragments(
            [
                dict(
                    id=str(uuid.uuid5(uuid.NAMESPACE_URL, "sp309-source-fixture")),
                    doc_id="sp309-source-fixture",
                    name="СП 309.1325800.2017",
                    version="2017",
                    versions=["2017"],
                    text=text,
                    source_text=text,
                    numbering="3.3",
                    type="subclause",
                    kind="text",
                    block="main",
                    order=0,
                    parent_id=None,
                    child_ids=[],
                )
            ]
        )
    )
    # Explicitly synthetic fixture: identical addresses under two real distinct paths.
    synthetic = []
    for index, section in enumerate(["I", "II"]):
        sid = str(uuid.uuid5(uuid.NAMESPACE_URL, "fixture-section-" + section))
        cid = str(uuid.uuid5(uuid.NAMESPACE_URL, "fixture-clause-" + section))
        common = dict(
            doc_id="ambiguity-fixture",
            name="СП 999.00000.2026 Контроль неоднозначности",
            version="2026",
            versions=["2026"],
            kind="text",
            block="main",
            tags=[],
        )
        synthetic.extend(
            [
                dict(
                    common,
                    id=sid,
                    parent_id=None,
                    numbering=section,
                    type="section",
                    text="Раздел " + section,
                    order=2 * index,
                    child_ids=[cid],
                ),
                dict(
                    common,
                    id=cid,
                    parent_id=sid,
                    numbering="3.3",
                    type="clause",
                    text="Контрольный пункт раздела " + section + ".",
                    order=2 * index + 1,
                    child_ids=[],
                ),
            ]
        )
    return result + annotate_fragments(synthetic)


def main():
    call = json.load(sys.stdin)
    repo = QdrantRepository.__new__(QdrantRepository)
    repo.settings = Settings()
    repo.collection = "confirmation"
    repo.client = QdrantClient(":memory:")
    try:
        repo.client.create_collection(
            repo.collection,
            vectors_config=models.VectorParams(size=2, distance=models.Distance.COSINE),
        )
        payloads = points()
        repo.client.upsert(
            repo.collection,
            points=[
                models.PointStruct(id=n["id"], vector=[1.0, 0.0], payload=n)
                for n in payloads
            ],
        )
        svc = FragmentSearchService(
            SearchService(repo, repo.settings, SimpleNamespace())
        )
        response = svc.search(FragmentSearchRequest.model_validate(call["request"]))
        print(response.model_dump_json())
    finally:
        repo.client.close()


if __name__ == "__main__":
    main()
