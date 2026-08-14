"""Pydantic DTOs package: Qdrant point payload + API request/response models.

The models live in dedicated modules by concern (``node_payload``, ``upload``, ``search``,
``document``, ``reference``); this file only marks the package and re-exports them.
"""

from src.dvd_service.dto.direct import (  # noqa: F401
    DirectDocumentIn,
    DirectFragmentIn,
    DirectJobResult,
)
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
    NodeDetail,
)
from src.dvd_service.dto.node_payload import NodePayload  # noqa: F401
from src.dvd_service.dto.reference import DocumentRef  # noqa: F401
from src.dvd_service.dto.scope import AdministrativeScope  # noqa: F401
from src.dvd_service.dto.search import (  # noqa: F401
    ScopesResponse,
    SearchHit,
    SearchRequest,
    SearchResponse,
    TagsResponse,
    TerritoryScope,
)
from src.dvd_service.dto.upload import (  # noqa: F401
    ActiveJobsResponse,
    DeleteResponse,
    JobStatusDTO,
    UploadResponse,
)
from src.dvd_service.dto.user_index import (  # noqa: F401
    UserIndexCreateRequest,
    UserIndexDeleteResponse,
    UserIndexInfo,
    UserIndexListResponse,
)

__all__ = [
    "NodePayload",
    "AdministrativeScope",
    "DirectDocumentIn",
    "DirectFragmentIn",
    "DirectJobResult",
    "DocumentRef",
    "DocumentInfo",
    "DocumentListResponse",
    "DocumentSummary",
    "DocumentFragment",
    "DocumentDetail",
    "NodeDetail",
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
    "ScopesResponse",
    "TerritoryScope",
    "UserIndexCreateRequest",
    "UserIndexInfo",
    "UserIndexListResponse",
    "UserIndexDeleteResponse",
]
