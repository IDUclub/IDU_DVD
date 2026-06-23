"""Unit tests for src/dvd_service/modules/hierarchy — Stage 4 HierarchyBuilder.

Covers: tree building from ranks/relations, flattening into reading-order nodes with
parent/child/prev/next links, amendment grouping, unnumbered-nesting cap, and __repr__.
"""

from __future__ import annotations

from src.dvd_service.modules.hierarchy import HierarchyBuilder


def _count_nodes(node) -> int:
    return 1 + sum(_count_nodes(c) for c in node.get("children", []))


class TestBuildAndFlatten:
    def test_build_returns_document_root(self, sample_parts, sample_rank_map):
        tree = HierarchyBuilder().build(sample_parts, sample_rank_map, title="doc")
        assert tree["type"] == "document"
        assert tree["children"], "root must have children"

    def test_flatten_reading_order_and_links(self, sample_parts, sample_rank_map):
        hb = HierarchyBuilder()
        nodes = hb.flatten(hb.build(sample_parts, sample_rank_map, title="doc"))
        assert len(nodes) == len(sample_parts) + 1  # +1 for the document root

        # exactly one root (no parent), prev/next form a single reading-order chain
        roots = [n for n in nodes if n["parent_id"] is None]
        assert len(roots) == 1
        assert nodes[0]["prev_id"] is None
        assert nodes[-1]["next_id"] is None
        for i in range(1, len(nodes)):
            assert nodes[i]["prev_id"] == nodes[i - 1]["id"]

        # every non-root id is referenced exactly once as a child
        child_ids = [cid for n in nodes for cid in n["child_ids"]]
        assert sorted(child_ids) == sorted(
            n["id"] for n in nodes if n["parent_id"] is not None
        )

    def test_flatten_marks_kind_text_for_non_tables(
        self, sample_parts, sample_rank_map
    ):
        hb = HierarchyBuilder()
        nodes = hb.flatten(hb.build(sample_parts, sample_rank_map, title="doc"))
        assert all(n["kind"] == "text" and n["table_html"] is None for n in nodes)


class TestGroupAmendment:
    def test_consecutive_amendment_blocks_are_grouped(self):
        def block(text):
            return {
                "type": "p",
                "text": text,
                "numbering": "",
                "is_table": False,
                "html": None,
                "_rank": None,
                "_block": "amendment",
                "children": [],
            }

        tree = {"type": "document", "text": "d", "children": [block("a1"), block("a2")]}
        out = HierarchyBuilder().group_amendment(tree)
        assert len(out["children"]) == 1
        assert out["children"][0]["type"] == "amendment"
        assert len(out["children"][0]["children"]) == 2

    def test_single_amendment_block_is_not_wrapped(self):
        blk = {
            "type": "p",
            "text": "a1",
            "numbering": "",
            "is_table": False,
            "html": None,
            "_rank": None,
            "_block": "amendment",
            "children": [],
        }
        tree = {"type": "document", "text": "d", "children": [blk]}
        out = HierarchyBuilder().group_amendment(tree)
        assert out["children"][0]["type"] == "p"  # left as-is


class TestCapUnnumberedNesting:
    def test_deep_unnumbered_chain_is_flattened_without_losing_nodes(self):
        def u(children):
            return {"type": "p", "text": "u", "_rank": None, "children": children}

        tree = u([u([u([u([])])])])  # 4 nested unnumbered levels
        total_before = _count_nodes(tree)
        out = HierarchyBuilder().cap_unnumbered_nesting(tree, max_u=1)
        assert _count_nodes(out) == total_before  # nodes re-parented, never dropped


class TestRepr:
    def test_repr(self):
        assert repr(HierarchyBuilder()) == "HierarchyBuilder()"
