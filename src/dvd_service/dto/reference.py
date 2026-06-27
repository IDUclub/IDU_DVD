"""Outgoing document reference — a link from one fragment to another document/clause.

A reference is kept in two complementary forms, as requested by the domain:
  * human-readable — ``raw`` (verbatim, as written in the document) plus ``target_name`` /
    ``target_numbering`` (the designation and clause it points at);
  * machine — ``target_node_id`` (the Qdrant point id of the exact clause) and ``target_doc_id``,
    which uniquely identify the referenced part in the store.

When the target document is not loaded yet, only the human-readable form is filled and
``resolved`` is ``False``; the link is later completed by the back-fill step once that document
is ingested (see ``DocumentRegistry`` pending references).
"""

from __future__ import annotations

from pydantic import BaseModel


class DocumentRef(BaseModel):
    raw: str  # the reference verbatim, as written in the source fragment
    target_name: str = ""  # normalized designation of the referenced document
    target_numbering: str = ""  # clause/subclause inside the target ("" = whole document)
    scope: str = "external"  # external | internal (a reference within the same document)

    # Resolution against the store (filled when the target is present):
    target_doc_id: str | None = None
    target_version: str | None = None
    target_node_id: str | None = None  # Qdrant point id of the exact clause (if pinpointed)
    resolved: bool = False  # whether the target document was found in the store
