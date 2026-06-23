"""Stage 4: the HierarchyBuilder class — document tree and flattening into flat nodes.

Nodes receive prev_id/next_id (reading order, for context), kind (text/table), and table_html.
"""

from __future__ import annotations

import uuid

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

    def build(self, parts, rank_map, title="document"):
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
                    "rank": rank_map.get(num) if num else None,
                    "relation": p.get("relation", "deeper"),
                    "block": p.get("block", "main"),
                    "is_table": p.get("category") == "Table",
                    "html": p.get("html"),
                    "parent": None,
                }
            )
        stack = [nodes[0]]
        for n in nodes[1:]:
            top = stack[-1]
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

        def nest(node_id):
            node = next(n for n in nodes if n["_id"] == node_id)
            out = {
                "type": node["type"],
                "text": node["text"],
                "numbering": node["numbering"],
                "is_table": node["is_table"],
                "html": node["html"],
                "_rank": node["rank"],
                "_block": node["block"],
            }
            kids = [nest(c["_id"]) for c in children.get(node_id, [])]
            if kids:
                out["children"] = kids
            return out

        return nest(0)

    def cap_unnumbered_nesting(self, tree, max_u=1):
        def collect_flat(c):
            out = [c]
            for ch in list(c.get("children", [])):
                out.extend(collect_flat(ch))
            c["children"] = []
            return out

        def walk(node, u):
            survivors, moved = [], []
            for c in list(node.get("children", [])):
                if c.get("_rank") is not None:
                    walk(c, 0)
                    survivors.append(c)
                elif u < max_u:
                    walk(c, u + 1)
                    survivors.append(c)
                else:
                    moved.extend(collect_flat(c))
            node["children"] = survivors + moved

        walk(tree, 0)
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

    def flatten(self, tree) -> list[dict]:
        """Flatten into a flat list (reading order) with parent/child/prev/next/kind/html."""
        nodes: list[dict] = []

        def walk(node, parent_id, parent_text, depth, path):
            nid = str(uuid.uuid4())
            rec = {
                "id": nid,
                "text": node.get("text", ""),
                "type": node.get("type", ""),
                "kind": "table" if node.get("is_table") else "text",
                "table_html": node.get("html") if node.get("is_table") else None,
                "numbering": node.get("numbering", "") or "",
                "block": node.get("_block", "main"),
                "depth": depth,
                "parent_id": parent_id,
                "parent_text": parent_text,
                "breadcrumb": " / ".join(path),
                "child_ids": [],
                "prev_id": None,
                "next_id": None,
            }
            nodes.append(rec)
            label = (node.get("numbering", "") + " " + node.get("text", "")).strip()[
                :60
            ]
            for ch in node.get("children", []):
                cid = walk(
                    ch, nid, node.get("text", "")[:300], depth + 1, path + [label]
                )
                rec["child_ids"].append(cid)
            return nid

        walk(tree, None, None, 0, [])

        # prev/next in reading order (DFS preorder = document order)
        for i, n in enumerate(nodes):
            n["prev_id"] = nodes[i - 1]["id"] if i > 0 else None
            n["next_id"] = nodes[i + 1]["id"] if i + 1 < len(nodes) else None
        return nodes
