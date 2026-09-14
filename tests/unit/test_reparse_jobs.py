"""The admin job feed must retain every item in a large, long-lived reparse queue."""

from src.dvd_service.services.dvd_service import DocumentsService
from tests.unit.test_routers import client


def test_bulk_reparse_feed_survives_expired_progress_records(client):
    http, fakes = client
    fakes["documents"] = DocumentsService(fakes["qdrant"])
    for i in range(25):
        for version in ("v1", "v2"):
            key = f"doc-{i}-{version}.docx"
            fakes["document_storage"].upload(key, b"source")
            fakes["qdrant"].points[key] = (
                [0.0],
                dict(
                    name=f"Doc {i:02}",
                    version=version,
                    doc_id=key,
                    source_object_key=key,
                ),
            )
    response = http.post("/documents/reparse")
    assert response.status_code == 202
    assert response.json()["queued_documents"] == 25
    assert response.json()["queued_versions"] == 50
    entries = fakes["queue"].pending()
    running = fakes["queue"].claim()
    fakes["jobs"].store.clear()  # equivalent to progress keys expiring; work persists
    fakes["jobs"].set(
        running["job_id"],
        dict(
            job_id=running["job_id"],
            name=running["name"],
            operation="reparse",
            status="processing",
            version_total=2,
            version_index=1,
            overall_progress=50,
        ),
    )
    feed = http.get("/documents/jobs/active").json()
    assert feed["count"] == 25
    by_id = {job["job_id"]: job for job in feed["jobs"]}
    assert by_id[running["job_id"]]["overall_progress"] == 50
    for position, entry in enumerate(entries[1:], 1):
        job = by_id[entry["job_id"]]
        assert job["status"] == "queued"
        assert job["name"] == entry["name"]
        assert job["version_total"] == 2
        assert job["version_index"] == 0
        assert job["queue_position"] == position


def test_finished_inflight_job_is_not_resurrected(client):
    http, fakes = client
    fakes["queue"].enqueue(
        dict(job_id="finished", operation="reparse", name="N", editions=[{}])
    )
    fakes["queue"].claim()
    fakes["jobs"].set("finished", dict(job_id="finished", status="done"))
    assert http.get("/documents/jobs/active").json()["count"] == 0


def test_missing_inflight_progress_still_has_document_identity(client):
    http, fakes = client
    fakes["queue"].enqueue(
        dict(job_id="running", operation="reparse", name="N", editions=[{}, {}])
    )
    fakes["queue"].claim()
    [job] = http.get("/documents/jobs/active").json()["jobs"]
    assert job["status"] == "processing"
    assert job["name"] == "N"
    assert job["version_total"] == 2
