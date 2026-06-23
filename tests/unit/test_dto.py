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
        assert req.name is None and req.tags is None

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
