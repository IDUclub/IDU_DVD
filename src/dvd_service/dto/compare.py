"""Deterministic edition changes and semantic document correspondences."""

from typing import Literal

from pydantic import BaseModel, Field


class CompareRequest(BaseModel):
    left_doc_id: str = Field(min_length=1)
    right_doc_id: str = Field(min_length=1)
    left_version: str | None = None
    right_version: str | None = None
    query: str | None = Field(None, min_length=1, max_length=4000)
    limit: int = Field(60, ge=1, le=500)
    max_chars: int = Field(2000, ge=100, le=10000)


class CompareDocument(BaseModel):
    doc_id: str
    name: str = ""
    title: str | None = None
    version: str = ""
    effective_date: str | None = None
    status: str = "active"
    owner: Literal["shared", "user"]


class ComparedFragment(BaseModel):
    id: str
    numbering: str = ""
    breadcrumb: str = ""
    section: str = ""
    text: str
    text_truncated: bool = False
    amended_by: list[str] = Field(default_factory=list)


class WordChange(BaseModel):
    left: str
    right: str


class CompareChange(BaseModel):
    kind: Literal[
        "added",
        "removed",
        "modified",
        "renumbered",
        "only_left",
        "only_right",
        "similar",
        "different",
    ]
    left: ComparedFragment | None = None
    right: ComparedFragment | None = None
    similarity: float | None = None
    word_diff: list[WordChange] = Field(default_factory=list)
    diff_truncated: bool = False
    changed_numbers: WordChange | None = None


class CompareStats(BaseModel):
    counts: dict[str, int]
    left_compared: int
    right_compared: int
    pairs_compared: int
    unchanged: int
    total_changes: int


class CompareSection(BaseModel):
    section: str
    counts: dict[str, int]


class CompareResponse(BaseModel):
    mode: Literal["editions", "documents"]
    left: CompareDocument
    right: CompareDocument
    stats: CompareStats
    sections: list[CompareSection]
    changes: list[CompareChange]
    truncated: bool
    topic_top_k: int | None = None
