"""Directed semantic dependency between two fragments of one document.

``source`` depends on ``target`` with ``weight`` (0..1): to understand or apply ``source``
correctly one has to read ``target``. Structure (parent/child, reading order) says where a
fragment sits; a relation says which other fragments its meaning actually needs, including
siblings, tables and clauses far away in the text.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

RelationKind = Literal[
    "completes",  # target continues/finishes source: rest of its sentence, items of its list
    "condition",  # target states when/where source applies
    "exception",  # target states when source does not apply or is relaxed
    "refines",  # target gives the values, measures, details source relies on
    "table_ref",  # source points at table target (or target is the table of source)
    "definition",  # target defines a term source uses
    "same_topic",  # thematically close only
]

RELATION_KINDS: tuple[str, ...] = RelationKind.__args__  # type: ignore[attr-defined]


class FragmentRelation(BaseModel):
    source_id: str
    target_id: str
    doc_id: str
    weight: float = Field(ge=0.0, le=1.0)
    kind: str = "same_topic"
    confidence: float | None = None  # scorer's own certainty, when it reports one
    method: str = ""  # scorer that produced the weight: llm | cross_encoder | heuristic
    candidate_sources: list[str] = Field(
        default_factory=list
    )  # why the pair was scored: parent_child | sibling | grandparent | ref_* | knn


class RelatedRef(BaseModel):
    """An outgoing relation of a search hit: the fragment it depends on and how strongly."""

    id: str
    weight: float
    kind: str


class DocumentRelations(BaseModel):
    doc_id: str
    count: int
    relations: list[FragmentRelation]
