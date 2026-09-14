"""Stage 4: the HierarchyBuilder class — document tree and flattening into flat nodes.

Nodes receive prev_id/next_id (reading order, for context), kind (text/table), and table_html.
"""

from __future__ import annotations

import uuid
from copy import deepcopy

import structlog

log = structlog.get_logger(__name__)


class HierarchyBuilder:
    def __repr__(self) -> str:
        return f"{type(self).__name__}()"

    @staticmethod
    def _depth_from_relation(top_depth: int, rel: str) -> int:
        if rel == "top":
            return 1
        if rel == "deeper":
            return top_depth + 1
        if rel == "shallower":
            return max(1, top_depth - 1)
        return top_depth

    @staticmethod
    def _heading_parent(stack, node, root):
        levels = {"section": 1, "chapter": 2, "article": 3}
        level = node.get("source_heading_level") or levels[node["type"]]
        parents = [
            ancestor
            for ancestor in stack
            if ancestor["type"] in levels
            # An inferred section in a preface or wrapped title must not own
            # explicit source chapters/articles.
            and (
                not node.get("source_heading_level")
                or ancestor.get("source_heading_level")
            )
            and (ancestor.get("source_heading_level") or levels[ancestor["type"]])
            < level
        ]
        return parents[-1] if parents else root

    def build(self, parts, rank_map, title="document", *, semantic=False):
        nodes = [
            {
                "_id": 0,
                "depth": 0,
                "type": "document",
                "text": title,
                "numbering": "",
                "rank": None,
                "relation": "top",
                "block": "main",
                "is_table": False,
                "html": None,
                "src_ids": [],
                "tags": [],
                "parent": None,
            }
        ]
        for p in parts:
            num = p.get("numbering", "") or ""
            nodes.append(
                {
                    "_id": p["id"] + 1,
                    "depth": None,
                    "type": p.get("type", "paragraph"),
                    "text": p["text"],
                    "numbering": num,
                    "fragment_name": p.get("fragment_name"),
                    "rank": rank_map.get(num) if num else None,
                    "relation": p.get("relation", "deeper"),
                    "source_delimiter": p.get("source_delimiter", ""),
                    "source_heading_level": p.get("source_heading_level"),
                    "block": p.get("block", "main"),
                    "is_table": p.get("category") == "Table",
                    "html": p.get("html"),
                    "src_ids": p.get("src_ids", []),
                    "source_text": p.get("source_text"),
                    "char_start": p.get("char_start"),
                    "char_end": p.get("char_end"),
                    "tags": p.get("tags", []),
                    "parent": None,
                }
            )
        stack = [nodes[0]]
        source_headings = []
        node_by_id = {n["_id"]: n for n in nodes}
        for n in nodes[1:]:
            top = stack[-1]
            article = next((a for a in reversed(stack) if a["type"] == "article"), None)
            if n.get("source_heading_level"):
                # Source headings have their own stack: an inferred preface or
                # wrapped title may reset the LLM stack, but cannot end a chapter.
                level = n["source_heading_level"]
                source_headings = [
                    a for a in source_headings if a["source_heading_level"] < level
                ]
                parent = self._heading_parent(source_headings, n, nodes[0])
                source_headings.append(n)
                stack = [parent]
                while stack[-1]["parent"] is not None:
                    stack.append(node_by_id[stack[-1]["parent"]])
                stack.reverse()
            elif n["type"] == "article":
                parent = self._heading_parent(stack, n, nodes[0])
                stack = stack[: stack.index(parent) + 1]
            elif article is not None and not (
                semantic
                and n["type"]
                in {
                    "title_page",
                    "toc",
                    "preface",
                    "introduction",
                    "appendix",
                    "bibliography",
                }
            ):
                if n["numbering"]:
                    # Inserted legal parts (3.1, 3.3) are siblings of part 3.
                    # Parenthesized list items remain below the current part.
                    is_item = n["source_delimiter"] == ")" or n["numbering"].endswith(
                        ")"
                    )
                    parent = article
                    if is_item:
                        numeric = n["numbering"].rstrip(").").replace(".", "").isdigit()
                        candidates = [
                            a
                            for a in stack[stack.index(article) + 1 :]
                            if a["numbering"]
                            and (
                                a.get("source_delimiter") != ")"
                                if numeric
                                else a["numbering"]
                                .rstrip(").")
                                .replace(".", "")
                                .isdigit()
                            )
                        ]
                        if candidates:
                            parent = candidates[-1]
                    stack = stack[: stack.index(parent) + 1]
                else:
                    # Notes and continuations belong to the preceding provision,
                    # never to a chain of unrelated unnumbered paragraphs.
                    parent = next(
                        (a for a in reversed(stack) if a["numbering"]), article
                    )
                    stack = stack[: stack.index(parent) + 1]
            elif semantic:
                heading = source_headings[-1] if source_headings else nodes[0]
                if n["type"] in {
                    "title_page",
                    "toc",
                    "preface",
                    "introduction",
                    "appendix",
                    "bibliography",
                }:
                    parent = nodes[0]
                    source_headings = []
                elif n["numbering"]:
                    number = n["numbering"].rstrip(".)")
                    item = n["source_delimiter"] == ")" or n["numbering"].endswith(")")
                    numeric = number.replace(".", "").isdigit()
                    candidates = [
                        a
                        for a in stack
                        if a["numbering"]
                        and a["type"] in {"clause", "subclause", "list_item"}
                        and (
                            (
                                not item
                                and a.get("source_delimiter") != ")"
                                and number.startswith(a["numbering"].rstrip(".") + ".")
                            )
                            or (item and numeric and a.get("source_delimiter") != ")")
                            or (
                                item
                                and not numeric
                                and a["numbering"]
                                .rstrip(".)")
                                .replace(".", "")
                                .isdigit()
                            )
                        )
                    ]
                    parent = candidates[-1] if candidates else heading
                elif n["type"] in {"paragraph", "note", "list_item", "table"}:
                    parent = next(
                        (
                            a
                            for a in reversed(stack)
                            if (
                                a["type"] in {"clause", "subclause", "list_item"}
                                and a["numbering"]
                            )
                            or (
                                n["type"] == "list_item"
                                and a["text"].rstrip().endswith(":")
                            )
                        ),
                        heading,
                    )
                else:
                    parent = heading
                stack = [parent]
                while stack[-1]["parent"] is not None:
                    stack.append(node_by_id[stack[-1]["parent"]])
                stack.reverse()
            else:
                d = (
                    max(1, n["rank"])
                    if n["rank"] is not None
                    else max(1, self._depth_from_relation(top["depth"], n["relation"]))
                )
                while len(stack) > 1 and stack[-1]["depth"] >= d:
                    stack.pop()
                parent = stack[-1]
            n["parent"] = parent["_id"]
            n["depth"] = parent["depth"] + 1
            stack.append(n)

        children = {}
        for n in nodes:
            children.setdefault(n["parent"], []).append(n)
        node_by_id = {n["_id"]: n for n in nodes}

        # Iterative post-order build (not recursive `nest`): a document whose
        # structure-tagging pass marks most fragments "deeper" than the previous
        # one degenerates into a near-linear chain as deep as the node count,
        # which blows Python's recursion limit if built via plain recursion.
        built: dict[int, dict] = {}
        stack: list[tuple[int, bool]] = [(0, False)]
        while stack:
            node_id, expanded = stack.pop()
            if not expanded:
                stack.append((node_id, True))
                for c in children.get(node_id, []):
                    stack.append((c["_id"], False))
                continue
            node = node_by_id[node_id]
            out = {
                "type": node["type"],
                "text": node["text"],
                "numbering": node["numbering"],
                "fragment_name": node.get("fragment_name"),
                "is_table": node["is_table"],
                "html": node["html"],
                "_rank": node["rank"],
                "_block": node["block"],
                "_src_ids": node.get("src_ids", []),
                "source_text": node.get("source_text"),
                "char_start": node.get("char_start"),
                "char_end": node.get("char_end"),
                "_tags": node.get("tags", []),
            }
            kids = [built.pop(c["_id"]) for c in children.get(node_id, [])]
            if kids:
                out["children"] = kids
            built[node_id] = out

        return built[0]

    def cap_unnumbered_nesting(self, tree, max_u=1):
        def collect_flat(c):
            out = []
            stack = [c]
            while stack:
                node = stack.pop()
                node_children = list(node.get("children", []))
                node["children"] = []
                out.append(node)
                stack.extend(reversed(node_children))
            return out

        stack = [(tree, 0)]
        while stack:
            node, u = stack.pop()
            survivors, moved = [], []
            for c in list(node.get("children", [])):
                if c.get("_rank") is not None:
                    survivors.append(c)
                    stack.append((c, 0))
                elif u < max_u:
                    survivors.append(c)
                    stack.append((c, u + 1))
                else:
                    moved.extend(collect_flat(c))
            node["children"] = survivors + moved

        return tree

    def group_amendment(self, tree):
        top = tree.get("children", [])
        new_top, i = [], 0
        while i < len(top):
            if top[i].get("_block") == "amendment":
                j = i
                while j < len(top) and top[j].get("_block") == "amendment":
                    j += 1
                run = top[i:j]
                if len(run) >= 2:
                    new_top.append(
                        {
                            "type": "amendment",
                            "text": "Изменения к документу",
                            "numbering": "",
                            "is_table": False,
                            "html": None,
                            "_rank": None,
                            "_block": "amendment",
                            "_src_ids": [],
                            "_tags": [],
                            "children": run,
                        }
                    )
                else:
                    new_top.extend(run)
                i = j
            else:
                new_top.append(top[i])
                i += 1
        tree["children"] = new_top
        return tree

    @staticmethod
    def _source_run(nodes):
        """Return an exact contiguous source span, or None for unsafe joins."""
        if not nodes or any(
            n.get("is_table") or n.get("category") == "Table" for n in nodes
        ):
            return None
        if any(
            n.get("source_text") is None
            or type(n.get("char_start")) is not int
            or type(n.get("char_end")) is not int
            for n in nodes
        ):
            return None
        if any(a["char_end"] != b["char_start"] for a, b in zip(nodes, nodes[1:])):
            return None
        source = "".join(n["source_text"] for n in nodes)
        if len(source) != nodes[-1]["char_end"] - nodes[0]["char_start"]:
            return None
        return source

    def coalesce_title_pages(self, parts):
        """Collect adjacent cover lines, including across LLM input windows."""
        result = []
        for part in deepcopy(parts):
            if result and part.get("type") == result[-1].get("type") == "title_page":
                previous = result[-1]
                source = self._source_run([previous, part])
                if source is not None and previous.get("block") == part.get("block"):
                    previous.update(
                        text=source, source_text=source, char_end=part["char_end"]
                    )
                    previous["src_ids"] = sorted(
                        set(previous["src_ids"] + part["src_ids"])
                    )
                    previous["tags"] = sorted(
                        set(previous.get("tags", []) + part.get("tags", []))
                    )
                    continue
            if part.get("type") == "title_page":
                part.update(relation="top", numbering="")
            part["id"] = len(result)
            result.append(part)
        return result

    @staticmethod
    def _preorder(tree):
        result, stack = [], [tree]
        while stack:
            node = stack.pop()
            result.append(node)
            stack.extend(reversed(node.get("children", [])))
        return result

    @staticmethod
    def _combine(target, members, source):
        target.update(
            source_text=source,
            char_start=members[0]["char_start"],
            char_end=members[-1]["char_end"],
            _src_ids=sorted({i for n in members for i in n.get("_src_ids", [])}),
            _tags=sorted({t for n in members for t in n.get("_tags", [])}),
            children=[],
            is_container=False,
        )

    def assemble_semantic(self, tree, max_chars=512):
        """Choose the largest fitting provision from the top down, after typing."""
        provisions = {"clause", "subclause", "list_item"}
        joinable = provisions | {"paragraph", "note", "definition"}
        stack = [tree]
        while stack:
            node = stack.pop()
            children = node.get("children", [])
            node["is_container"] = False
            introduced_list = (
                node["type"] == "paragraph"
                and node["text"].rstrip().endswith(":")
                and any(c["type"] == "list_item" for c in children)
            )
            if children and (node["type"] in provisions or introduced_list):
                members = self._preorder(node)
                same_block = len({n.get("_block", "main") for n in members}) == 1
                allowed = same_block and all(n["type"] in joinable for n in members)
                source = self._source_run(members) if allowed else None
                if source is not None and len(source) <= max_chars:
                    # Preserve the parent's display text and every child's source numbering.
                    text = node["text"] + "".join(n["source_text"] for n in members[1:])
                    self._combine(node, members, source)
                    node["text"] = text
                    continue
                node["is_container"] = True
                descendants = members[1:]
                source = self._source_run(descendants) if allowed else None
                if (
                    source is not None
                    and len(source) <= max_chars
                    and len(children) > 1
                ):
                    group = {
                        "type": "subclause_group",
                        "text": source,
                        "numbering": "",
                        "is_table": False,
                        "html": None,
                        "_rank": None,
                        "_block": node.get("_block", "main"),
                    }
                    self._combine(group, descendants, source)
                    node["children"] = [group]
                    continue
            elif children:
                node["is_container"] = True
            stack.extend(reversed(node.get("children", [])))
        return tree

    def flatten(self, tree) -> list[dict]:
        """Flatten into a flat list (reading order) with parent/child/prev/next/kind/html."""
        nodes: list[dict] = []

        # Iterative pre-order (not recursive `walk`): see the note in `build` —
        # a degenerate deep tree would otherwise blow the recursion limit here too.
        stack: list[tuple[dict, dict | None, str | None, int, list[str]]] = [
            (tree, None, None, 0, [])
        ]
        while stack:
            node, parent_rec, parent_text, depth, path = stack.pop()
            nid = str(uuid.uuid4())
            rec = {
                "id": nid,
                "text": node.get("text", ""),
                "type": node.get("type", ""),
                "kind": "table" if node.get("is_table") else "text",
                "table_html": node.get("html") if node.get("is_table") else None,
                "numbering": node.get("numbering", "") or "",
                "fragment_name": node.get("fragment_name"),
                "block": node.get("_block", "main"),
                "depth": depth,
                "src_ids": node.get("_src_ids", []),
                "source_text": node.get("source_text"),
                "is_container": node.get("is_container", False),
                "char_start": node.get("char_start"),
                "char_end": node.get("char_end"),
                "tags": node.get("_tags", []),
                "parent_id": parent_rec["id"] if parent_rec else None,
                "parent_text": parent_text,
                "breadcrumb": " / ".join(path),
                "child_ids": [],
                "prev_id": None,
                "next_id": None,
            }
            if "is_container" in node:
                # Full parent context is separate from the bounded, source-grounded text.
                context = (
                    parent_rec.get("search_text", "")
                    if parent_rec
                    and parent_rec.get("is_container")
                    and parent_rec["type"] != "document"
                    else ""
                )
                rec["search_text"] = (
                    (context.rstrip() + "\n" + rec["text"]).strip()
                    if context
                    else rec["text"]
                )
            nodes.append(rec)
            if parent_rec is not None:
                parent_rec["child_ids"].append(nid)
            label = (node.get("numbering", "") + " " + node.get("text", "")).strip()[
                :60
            ]
            for ch in reversed(node.get("children", [])):
                stack.append(
                    (ch, rec, node.get("text", "")[:300], depth + 1, path + [label])
                )

        # prev/next in reading order (DFS preorder = document order)
        for i, n in enumerate(nodes):
            n["prev_id"] = nodes[i - 1]["id"] if i > 0 else None
            n["next_id"] = nodes[i + 1]["id"] if i + 1 < len(nodes) else None
        return nodes
