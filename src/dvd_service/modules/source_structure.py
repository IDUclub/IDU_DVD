"""Deterministic source anchors; references and editorial notes are not own numbers."""

from __future__ import annotations

import re


class SourceStructure:
    HEADING = re.compile(
        r"^\s*(глава|раздел|статья|chapter|section|article)\s+([\d]+(?:\.[\d]+)*|[IVXLCDM]+)[.)]?\s+(.+)",
        re.I,
    )
    NOTE = re.compile(
        r"^\s*(?:(?:раздел|глава|статья|пункт)\s+\d{1,3}(?:\.\d{1,3})*[.)]?\s+)?"
        r"\((?:в\s+ред\.|част[ьи]|пункт|п\.|абзац|введ[её]н|утратил|измен[её]нная\s+редакция)",
        re.I,
    )
    WRITTEN_DATE = re.compile(
        r"^\s*\d{1,2}\s+(?:января|февраля|марта|апреля|мая|июня|июля|августа|"
        r"сентября|октября|ноября|декабря)\s+\d{4}\b",
        re.I,
    )
    NUMBER = re.compile(r"^\s*(\d{1,3}(?:\.\d{1,3})*)([.)]?)(?=\s)\s+\S")
    LETTER = re.compile(r"^\s*([а-яёa-z])[)]\s+\S", re.I)

    @classmethod
    def anchor(cls, text):
        if cls.NOTE.match(text):
            return {"type": "note", "numbering": "", "block": "amendment"}
        if cls.WRITTEN_DATE.match(text):
            return {"type": "paragraph", "numbering": ""}
        heading = cls.HEADING.match(text)
        if heading:
            typ = {"глава": "chapter", "раздел": "section", "статья": "article"}.get(
                heading[1].lower(), heading[1].lower()
            )
            return {
                "type": typ,
                "numbering": heading[2],
                "fragment_name": heading[3].strip(),
                "source_heading_level": {
                    "раздел": 1,
                    "глава": 2,
                    "статья": 3,
                    "chapter": 1,
                    "section": 2,
                    "article": 3,
                }[heading[1].lower()],
            }
        number = cls.NUMBER.match(text)
        if number:
            # Dates, document identifiers and references are not source list labels.
            return {"numbering": number[1], "source_delimiter": number[2]}
        letter = cls.LETTER.match(text)
        return {"numbering": letter[1] + ")", "source_delimiter": ")"} if letter else {}

    @classmethod
    def starts_part(cls, text):
        return bool(cls.anchor(text))
