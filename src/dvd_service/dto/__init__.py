"""Pydantic DTOs package: Qdrant point payload + API request/response models.

The models live in dedicated modules by concern (``node_payload``, ``upload``, ``search``,
``document``, ``reference``); this file only marks the package and re-exports them.
"""

from src.dvd_service.dto.document import (  # noqa: F401
    DocumentDetail,
    DocumentFragment,
    DocumentInfo,
    DocumentList,
    DocumentListResponse,
    DocumentSummary,
    DocumentUpdateRequest,
    DocumentUpdateResponse,
    FragmentUpdateRequest,
)
from src.dvd_service.dto.node_payload import NodePayload  # noqa: F401
from src.dvd_service.dto.reference import DocumentRef  # noqa: F401
from src.dvd_service.dto.search import (  # noqa: F401
    SearchHit,
    SearchRequest,
    SearchResponse,
    TagsResponse,
)
from src.dvd_service.dto.upload import (  # noqa: F401
    ActiveJobsResponse,
    DeleteResponse,
    JobStatusDTO,
    UploadResponse,
)

__all__ = [
    "NodePayload",
    "DocumentRef",
    "DocumentInfo",
    "DocumentListResponse",
    "DocumentSummary",
    "DocumentFragment",
    "DocumentDetail",
    "DocumentList",
    "DocumentUpdateRequest",
    "DocumentUpdateResponse",
    "FragmentUpdateRequest",
    "UploadResponse",
    "ActiveJobsResponse",
    "JobStatusDTO",
    "DeleteResponse",
    "SearchRequest",
    "SearchHit",
    "SearchResponse",
    "TagsResponse",
]
