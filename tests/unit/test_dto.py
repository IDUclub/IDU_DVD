"""Unit tests for src/dvd_service/dto — pydantic request/response/payload models.

Covers: defaults, required-field validation, and nesting. These guard the API contract.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.dvd_service.dto import (
    NodePayload,
    SearchHit,
    SearchRequest,
    SearchResponse,
    UploadResponse,
)


class TestSearchRequest:
    def test_defaults(self):
        req = SearchRequest(query="hello")
        assert req.limit == 10 and req.context_height == 0
        assert req.name is None and req.tags is None and req.document_names is None

    def test_query_is_required(self):
        with pytest.raises(ValidationError):
            SearchRequest()


class TestNodePayload:
    def test_minimal_required_fields(self):
        p = NodePayload(
            doc_id="d", name="n", version="v", content_hash="h", type="clause", text="t"
        )
        assert p.kind == "text" and p.block == "main" and p.child_ids == []

    def test_missing_required_raises(self):
        with pytest.raises(ValidationError):
            NodePayload(doc_id="d")

    def test_administrative_scope_defaults_to_untagged_and_pending(self):
        """An old point (or a minimal caller) must keep validating — every field defaulted."""
        p = NodePayload(
            doc_id="d", name="n", version="v", content_hash="h", type="clause", text="t"
        )
        assert p.document_level is None and p.territory_id is None
        assert p.territory_path == []
        assert p.level_source == "unset" and p.territory_source == "unset"
        assert p.tagging_status == "pending" and p.tagging_attempts == 0

    def test_administrative_scope_round_trips(self):
        p = NodePayload(
            doc_id="d",
            name="n",
            version="v",
            content_hash="h",
            type="clause",
            text="t",
            document_level="municipal",
            territory_id=54,
            territory_name="Выборгский муниципальный район",
            territory_type_id=2,
            territory_type_name="Муниципальное образование",
            territory_path=[12639, 1, 54],
            level_source="auto",
            territory_source="manual",
            territory_confidence=0.9,
            tagging_status="ok",
        )
        assert p.territory_path == [12639, 1, 54]
        assert p.territory_source == "manual" and p.level_source == "auto"


class TestResponses:
    def test_upload_response(self):
        assert UploadResponse(job_id="j", status="queued").status == "queued"

    def test_search_response_nests_hits(self):
        hit = SearchHit(
            id="1",
            score=0.9,
            doc_id="d",
            name="n",
            version="v",
            kind="text",
            type="clause",
            text="t",
        )
        resp = SearchResponse(count=1, hits=[hit])
        assert resp.count == 1 and resp.hits[0].score == 0.9
