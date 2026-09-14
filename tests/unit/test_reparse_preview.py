from src.dvd_service.services.dvd_service import DocumentsService
from tests.unit.test_routers import client


def test_preview_then_reparse_one_exact_edition(client):
    http, fakes = client
    fakes["documents"] = DocumentsService(fakes["qdrant"])
    for name, version in [("A", "v1"), ("A", "v2"), ("B", "v1")]:
        key = f"{name}-{version}.docx"
        fakes["document_storage"].upload(key, b"source")
        fakes["qdrant"].points[key] = (
            [0.0],
            dict(
                name=name,
                version=version,
                doc_id=f"{name}-{version}",
                source_object_key=key,
                content_hash=key,
            ),
        )
    params = dict(name="A", version="v2", dry_run="true")
    preview = http.post("/documents/reparse", params=params).json()
    assert preview["queued_documents"] == 0
    assert preview["planned"] == [dict(name="A", version="v2", doc_id="A-v2")]
    assert not fakes["queue"].pending()
    result = http.post(
        "/documents/reparse", params={**params, "dry_run": "false"}
    ).json()
    assert result["queued_documents"] == 1 and result["queued_versions"] == 1
    task = fakes["queue"].pending()[0]
    assert task["name"] == "A" and [e["version"] for e in task["editions"]] == ["v2"]


def test_unknown_target_never_falls_back_to_all_documents(client):
    http, fakes = client
    fakes["documents"] = DocumentsService(fakes["qdrant"])
    result = http.post("/documents/reparse", params={"name": "absent"}).json()
    assert result["queued_documents"] == 0
