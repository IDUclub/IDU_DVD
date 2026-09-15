"""Search a real local SP55 preview alongside unrelated 3.3 and a string card."""

import argparse
import json
import os
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
from src.dvd_service.services.dvd_service import SearchService
from src.dvd_service.services.fragment_search import FragmentSearchService


def main():
    cli = argparse.ArgumentParser()
    cli.add_argument("preview", type=Path)
    args = cli.parse_args()
    nodes = json.loads(args.preview.read_text(encoding="utf8"))["fragments"]
    repo = QdrantRepository.__new__(QdrantRepository)
    repo.settings = Settings()
    repo.collection = "local-resolution-check"
    repo.client = QdrantClient(":memory:")
    try:
        repo.client.create_collection(
            repo.collection,
            vectors_config=models.VectorParams(size=2, distance=models.Distance.COSINE),
        )
        payloads = [
            dict(
                n,
                doc_id="sp55",
                name="СП 55.13330.2016 Дома жилые одноквартирные",
                version="2016",
                versions=["2016"],
            )
            for n in nodes
        ]
        clause = next(n for n in payloads if n["numbering"] == "3.3")
        for doc_id, name in [("other", "СП 309.1325800.2017"), ("duplicate", "string")]:
            payloads.append(
                dict(
                    clause,
                    id=str(uuid.uuid4()),
                    doc_id=doc_id,
                    name=name,
                    parent_id=None,
                )
            )
        repo.client.upsert(
            repo.collection,
            points=[
                models.PointStruct(id=n["id"], vector=[1.0, 0.0], payload=n)
                for n in payloads
            ],
        )
        service = FragmentSearchService(
            SearchService(repo, repo.settings, SimpleNamespace())
        )
        response = service.search(
            FragmentSearchRequest(
                pattern="3.3", document_names=["СП 55"], include_children=False
            )
        )
        assert response.match_count == 1 and not response.ambiguous
        assert len(response.hits) == 1 and response.hits[0].id == clause["id"]
        assert "блокированная застройка" in response.hits[0].text
        result = response.model_dump(mode="json")
        args.preview.with_name("resolution.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf8"
        )
        print(
            json.dumps(
                {
                    "match_count": response.match_count,
                    "ambiguous": response.ambiguous,
                    "text": response.hits[0].text,
                },
                ensure_ascii=False,
            )
        )
    finally:
        repo.client.close()


if __name__ == "__main__":
    main()
