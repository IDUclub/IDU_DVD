"""Read Word paragraphs in order, materializing automatic list labels before parsing."""

from __future__ import annotations

import re
from html import escape

from docx import Document
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph


class DocxReader:
    """Counters are local to a read; never shared between documents or ingestion jobs."""

    @staticmethod
    def _value(element, path, default=None):
        child = element.find(path) if element is not None else None
        return child.get(qn("w:val"), default) if child is not None else default

    @staticmethod
    def _format(value, fmt):
        if fmt in {"decimal", "decimalZero"}:
            return str(value).zfill(2 if fmt == "decimalZero" else 1)
        if fmt in {"upperLetter", "lowerLetter", "russianUpper", "russianLower"}:
            alphabet = (
                "абвгдежзийклмнопрстуфхцчшщъыьэюя"
                if fmt.startswith("russian")
                else "abcdefghijklmnopqrstuvwxyz"
            )
            result = ""
            while value > 0:
                value, index = divmod(value - 1, len(alphabet))
                result = alphabet[index] + result
            return result.upper() if fmt in {"upperLetter", "russianUpper"} else result
        if fmt in {"upperRoman", "lowerRoman"}:
            result = ""
            for n, token in [
                (1000, "M"),
                (900, "CM"),
                (500, "D"),
                (400, "CD"),
                (100, "C"),
                (90, "XC"),
                (50, "L"),
                (40, "XL"),
                (10, "X"),
                (9, "IX"),
                (5, "V"),
                (4, "IV"),
                (1, "I"),
            ]:
                count, value = divmod(value, n)
                result += token * count
            return result.lower() if fmt == "lowerRoman" else result
        raise ValueError(f"Unsupported Word numbering format: {fmt}")

    def read(self, path):
        doc = Document(path)
        try:
            numbering = doc.part.part_related_by(RT.NUMBERING).element
        except KeyError:
            numbering = None
        nums = {
            n.get(qn("w:numId")): n
            for n in (numbering.findall(qn("w:num")) if numbering is not None else [])
        }
        abstracts = {
            n.get(qn("w:abstractNumId")): n
            for n in (
                numbering.findall(qn("w:abstractNum")) if numbering is not None else []
            )
        }
        counters = {}

        def level(num_id, index):
            num = nums[num_id]
            aid = self._value(num, qn("w:abstractNumId"))
            abstract = abstracts[aid]
            override = next(
                (
                    n
                    for n in num.findall(qn("w:lvlOverride"))
                    if n.get(qn("w:ilvl")) == str(index)
                ),
                None,
            )
            lvl = override.find(qn("w:lvl")) if override is not None else None
            if lvl is None:
                lvl = next(
                    (
                        n
                        for n in abstract.findall(qn("w:lvl"))
                        if n.get(qn("w:ilvl")) == str(index)
                    ),
                    None,
                )
            if lvl is None:
                raise ValueError(f"Missing Word numbering level {num_id}/{index}")
            start = self._value(
                override, qn("w:startOverride"), self._value(lvl, qn("w:start"), "1")
            )
            return lvl, int(start)

        def label(paragraph):
            props = [paragraph._p.pPr]
            style, seen = paragraph.style, set()
            while style is not None and style.style_id not in seen:
                seen.add(style.style_id)
                props.append(style.element.pPr)
                style = style.base_style
            num_id = ilvl = None
            for prop in props:
                num_pr = prop.find(qn("w:numPr")) if prop is not None else None
                if num_id is None:
                    num_id = self._value(num_pr, qn("w:numId"))
                if ilvl is None:
                    ilvl = self._value(num_pr, qn("w:ilvl"))
            if num_id in {None, "0"}:
                return ""
            if num_id not in nums:
                raise ValueError(f"Missing Word numbering definition {num_id}")
            index = int(ilvl or 0)
            lvl, start = level(num_id, index)
            counts = counters.setdefault(num_id, {})
            # Default restart is after the previous level; lvlRestart=0 means never.
            for deeper in list(counts):
                if deeper <= index:
                    continue
                child, _ = level(num_id, deeper)
                restart = int(self._value(child, qn("w:lvlRestart"), str(deeper)))
                if restart and index < restart:
                    del counts[deeper]
            counts[index] = counts.get(index, start - 1) + 1
            fmt = self._value(lvl, qn("w:numFmt"), "decimal")
            pattern = self._value(lvl, qn("w:lvlText"), "")
            if fmt == "none":
                return ""
            if fmt == "bullet":
                return pattern or "•"

            def replace(match):
                k = int(match[1]) - 1
                ancestor, initial = level(num_id, k)
                ancestor_fmt = self._value(ancestor, qn("w:numFmt"), "decimal")
                if lvl.find(qn("w:isLgl")) is not None:
                    ancestor_fmt = "decimal"
                return self._format(counts.get(k, initial), ancestor_fmt)

            return re.sub(r"%([1-9])", replace, pattern)

        def paragraph_block(paragraph):
            marker = label(
                paragraph
            )  # Empty numbered paragraphs still advance Word's counter.
            text = paragraph.text.strip()
            if not text:
                return None
            return {
                "text": f"{marker} {text}" if marker else text,
                "category": "ListItem" if marker else "NarrativeText",
                "html": None,
                "page": None,
                "bbox": None,
                "source_marker": marker,
                "source_paragraph": True,
            }

        def blocks(container, owner):
            for el in container:
                if el.tag == qn("w:p"):
                    block = paragraph_block(Paragraph(el, owner))
                    if block:
                        yield block
                elif el.tag == qn("w:tbl"):
                    table = Table(el, owner)
                    rows, texts, active = [], [], {}
                    for row in table._tbl.tr_lst:
                        cells, column = [], 0
                        for tc in row.tc_lst:
                            span = int(self._value(tc.tcPr, qn("w:gridSpan"), "1"))
                            merge = (
                                tc.tcPr.find(qn("w:vMerge"))
                                if tc.tcPr is not None
                                else None
                            )
                            if (
                                merge is not None
                                and merge.get(qn("w:val"), "continue") == "continue"
                            ):
                                cell = active.get(column)
                                if cell is not None:
                                    cell["rowspan"] += 1
                                column += span
                                continue
                            content = list(blocks(tc, table))
                            text = "\n".join(b["text"] for b in content)
                            html = "".join(
                                b["html"] or f'<p>{escape(b["text"])}</p>'
                                for b in content
                            )
                            cell = {"html": html, "colspan": span, "rowspan": 1}
                            cells.append(cell)
                            texts.append(text)
                            for col in range(column, column + span):
                                active.pop(col, None)
                            if merge is not None:
                                active[column] = cell
                            column += span
                        rows.append(cells)
                    html = (
                        "<table>"
                        + "".join(
                            "<tr>"
                            + "".join(
                                f'<td colspan="{c["colspan"]}" rowspan="{c["rowspan"]}">{c["html"]}</td>'
                                for c in row
                            )
                            + "</tr>"
                            for row in rows
                        )
                        + "</table>"
                    )
                    yield {
                        "text": "\n".join(texts),
                        "category": "Table",
                        "html": html,
                        "page": None,
                        "bbox": None,
                    }
                elif el.tag == qn("w:sdt"):
                    content = el.find(qn("w:sdtContent"))
                    if content is not None:
                        yield from blocks(content, owner)

        return list(blocks(doc.element.body, doc))
