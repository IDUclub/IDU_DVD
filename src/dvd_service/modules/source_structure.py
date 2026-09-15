"""Deterministic source anchors; references and editorial notes are not own numbers."""

from __future__ import annotations

import re


class SourceStructure:
    HEADING = re.compile(
        r"^\s*(глава|раздел|статья|chapter|section|article)[ \t]+"
        r"(\d+(?:[._]\d+)*|[IVXLCDM]+|первый|второй|третий)[.)]?"
        r"(?=\s|$)(?:[ \t]+([^\r\n]*))?",
        re.I,
    )
    APPENDIX = re.compile(
        r"^\s*(?:приложение|appendix)[ \t]+([А-ЯЁA-Z]|\d+)(?=\s|$)(?:[ \t]+([^\r\n]*))?",
        re.I,
    )
    COMMENT = re.compile(
        r"^\s*(?:комментарий\s+к\s+(?:статье|главе)|\[(?:ГОСТ|СП|СНиП)\b)", re.I
    )
    NOTE = re.compile(
        r"^\s*(?:(?:(?:раздел|глава|статья|пункт)\s+)?\d{1,3}(?:\.\d{1,3})*[.)]?\s+)?"
        r"\((?:в\s+ред\.|част[ьи]|пункт|п\.|абзац|введ[её]н|утратил|измен[её]нная\s+редакция)",
        re.I,
    )
    WRITTEN_DATE = re.compile(
        r"^\s*\d{1,2}\s+(?:января|февраля|марта|апреля|мая|июня|июля|августа|"
        r"сентября|октября|ноября|декабря)\s+\d{4}\b",
        re.I,
    )
    EDITORIAL = re.compile(r"^\s*введ[её]н[аоы]?\s+Федеральным\s+законом\s+от\b", re.I)
    REFERENCE = re.compile(
        r"^\s*(?:(?:статья|глава|раздел)\s+)?\d+(?:\.\d+)*\s+"
        r"(?:настоящей\s+(?:статьи|главы)|настоящего\s+(?:Кодекса|раздела|пункта))\b",
        re.I,
    )
    NUMBER = re.compile(
        r"^\s*((?:[А-ЯЁA-Z]\.)?\d{1,3}(?:\.\d{1,3})*)([.)]?)(?=\s)\s+\S", re.I
    )
    LETTER = re.compile(r"^\s*([а-яёa-z])[)]\s+\S", re.I)

    @classmethod
    def anchor(cls, text):
        if cls.REFERENCE.match(text):
            return {"type": "paragraph", "numbering": ""}
        if cls.COMMENT.match(text):
            return {"type": "note", "numbering": ""}
        if cls.NOTE.match(text) or cls.EDITORIAL.match(text):
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
                "fragment_name": (heading[3] or "").strip(),
                "source_heading_level": {
                    "раздел": 1,
                    "глава": 2,
                    "статья": 3,
                    "chapter": 1,
                    "section": 2,
                    "article": 3,
                }[heading[1].lower()],
            }
        appendix = cls.APPENDIX.match(text)
        if appendix:
            return {
                "type": "appendix",
                "numbering": appendix[1],
                "fragment_name": (appendix[2] or "").strip(),
                "source_heading_level": 1,
            }
        number = cls.NUMBER.match(text)
        if number:
            # Dates, document identifiers and references are not source list labels.
            return {"numbering": number[1], "source_delimiter": number[2]}
        letter = cls.LETTER.match(text)
        return {"numbering": letter[1] + ")", "source_delimiter": ")"} if letter else {}

    @classmethod
    def annotate(cls, parts):
        """Resolve bare decimal section headings using following source addresses.

        A short `3 Terms` before `3.1 ...` is a section; a preface entry, TOC
        leader, date or legal part is not. LLM labels do not participate.
        """
        texts = [p.get("source_text") or p["text"] for p in parts]
        anchors = [
            cls.anchor(t) if p.get("category") != "Table" else {}
            for p, t in zip(parts, texts)
        ]
        # TOC entries are navigational text, even when they contain a perfectly
        # valid section/article/appendix number. Keep their source, not an address.
        in_toc = False
        toc_titles = set()
        for i, text in enumerate(texts):
            flat = " ".join(text.split())
            title = re.sub(r"[.·…]{3,}.*$", "", flat).strip().casefold()
            if flat.casefold() in {"содержание", "оглавление", "contents"}:
                in_toc = True
                toc_titles.clear()
            elif in_toc and (
                flat.casefold() in {"введение", "предисловие", "introduction"}
                or title in toc_titles
                or (not anchors[i].get("numbering") and len(flat) > 220)
            ):
                in_toc = False
            leader = bool(re.search(r"[.·…]{3,}\s*\d*\s*$", flat))
            if in_toc or (leader and anchors[i].get("numbering")):
                toc_titles.add(title)
                anchors[i] = {"type": "toc", "numbering": ""}
        in_article = False
        for i, (part, text, anchor) in enumerate(zip(parts, texts, anchors)):
            if anchor.get("source_heading_level"):
                in_article = anchor.get("type") == "article"
            num = anchor.get("numbering", "")
            if (
                not in_article
                and num.isdigit()
                and not anchor.get("type")
                and not anchor.get("source_delimiter")
                and len(text.strip()) < 220
                and not re.search(r"(?:[.·…]{3,}|[.!?;:]\s*$)", text)
            ):
                title = re.sub(r"^\s*" + re.escape(num) + r"\s+", "", text).strip()
                if re.fullmatch(
                    r"Область применения|Нормативные ссылки|Термины и определения|"
                    r"Термины, определения и сокращения",
                    " ".join(title.split()),
                    re.I,
                ):
                    anchor.update(
                        type="section", source_heading_level=1, fragment_name=title
                    )
                for later in anchors[i + 1 : i + 41]:
                    other = later.get("numbering", "")
                    if other.startswith(num + "."):
                        anchor.update(
                            type="section",
                            source_heading_level=1,
                            fragment_name=re.sub(
                                r"^\s*" + re.escape(num) + r"\s+", "", text
                            ).strip(),
                        )
                        break
                    if later.get("source_heading_level") or (
                        other.isdigit() and other != num
                    ):
                        break
            part["_source_anchor"] = anchor
        return parts

    @classmethod
    def starts_part(cls, text):
        return bool(cls.anchor(text))
