"""Node-graph read, declarative apply, lint, snapshot/restore (plan 3.1, 3.2, 3.4, 5.3).

All against tests/fake_armorpaint.py, which answers in the reply shapes recorded from a
live ArmorPaint 1.0 (below) and follows upstream's node semantics.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from armorpaint_mcp import node_graph  # noqa: E402
from armorpaint_mcp.node_graph import SpecError  # noqa: E402
from fake_armorpaint import FakeArmorPaint  # noqa: E402

# Recorded from ArmorPaint 1.0 (pinned commit) right after ap_project_new.
LIVE_NODE_LIST = {
    "count": 2,
    "returned": 2,
    "nodes": "1,RGB,Color,122.000000,100.000000;0,OUTPUT_MATERIAL_PBR,Material Output,386.000000,100.000000;",
    "node_format": "id,type,name,x,y;",
    "link_count": 1,
    "links_returned": 1,
    "links": "1,0,0,0;",
    "link_format": "from_id,from_socket,to_id,to_socket;",
}


@pytest.fixture
def ap() -> FakeArmorPaint:
    return FakeArmorPaint()


def by_type(graph, node_type):
    return [n for n in graph["nodes"] if n["type"] == node_type]


# ---------------------------------------------------------------------------
# 3.1 whole-graph read
# ---------------------------------------------------------------------------


def test_parse_live_node_list():
    nodes, links, truncated = node_graph.parse_node_list(LIVE_NODE_LIST)
    assert [(n["id"], n["type"], n["name"], n["x"]) for n in nodes] == [
        (1, "RGB", "Color", 122.0),
        (0, "OUTPUT_MATERIAL_PBR", "Material Output", 386.0),
    ]
    assert links == [{"from_id": 1, "from_socket": 0, "to_id": 0, "to_socket": 0}]
    assert truncated is False


def test_parse_node_list_keeps_commas_in_names_and_flags_truncation():
    nodes, _, truncated = node_graph.parse_node_list(
        {"count": 70, "returned": 1, "nodes": "5,MATH,Add, then clamp,1.000000,2.000000;", "links": ""}
    )
    assert nodes[0]["name"] == "Add, then clamp" and nodes[0]["y"] == 2.0
    assert truncated is True


def test_read_graph_is_one_description(ap):
    g = node_graph.read_graph(ap)
    assert g["material"] == "Material 1"
    assert g["channels"]["base"] is True
    out = by_type(g, "OUTPUT_MATERIAL_PBR")[0]
    rgb = by_type(g, "RGB")[0]
    assert out["inputs"][3] == {"index": 3, "name": "Roughness", "type": "VALUE", "value": [0.3], "link": None}
    assert out["inputs"][0]["link"] == {"from_id": rgb["id"], "from_socket": 0, "from_name": "Color"}
    assert rgb["outputs"][0]["value"] == [0.8, 0.8, 0.8, 1.0]
    assert g["links"] == [
        {"from_id": rgb["id"], "from_socket": 0, "from_name": "Color",
         "to_id": out["id"], "to_socket": 0, "to_name": "Base Color"}
    ]
    # One node_list, one batch of node_get: not N round trips.
    ops = [op for op, _ in ap.log]
    assert ops.count("node_list") == 1 and ops.count("node_get") == 2


# ---------------------------------------------------------------------------
# 3.2 declarative apply
# ---------------------------------------------------------------------------

EDGE_SPEC = {
    "nodes": {
        "noise": {"type": "TEX_NOISE", "inputs": {"Scale": 4.0}},
        "mix": {"type": "MIX_RGB", "inputs": {"Color 1": [0.2, 0.1, 0.05, 1.0]}, "buttons": {"blend_type": "Multiply"}},
        "out": {"existing": "OUTPUT_MATERIAL_PBR"},
    },
    "links": ["noise.Color -> mix.Color 2", "noise.Factor -> mix.Factor", "mix.Color -> out.Base Color"],
}


def test_apply_builds_the_graph_and_applies_it(ap):
    report = node_graph.apply(ap, EDGE_SPEC)
    assert report["ok"], report
    g = node_graph.read_graph(ap)
    noise, mix = by_type(g, "TEX_NOISE")[0], by_type(g, "MIX_RGB")[0]
    assert report["created"] == {"noise": noise["id"], "mix": mix["id"]}
    assert noise["inputs"][1]["value"] == [4.0]
    assert mix["inputs"][1]["value"] == [0.2, 0.1, 0.05, 1.0]
    assert mix["buttons"][0]["value"] == [2.0]  # "Multiply" is option 2
    out = by_type(g, "OUTPUT_MATERIAL_PBR")[0]
    assert out["inputs"][0]["link"]["from_id"] == mix["id"]  # replaced the RGB link
    assert ap.updates == 1 and ap.fills == 1  # recompiled, then APPLIED (the step people miss)


def test_apply_can_skip_the_fill(ap):
    assert node_graph.apply(ap, EDGE_SPEC, fill=False)["ok"]
    assert ap.updates == 1 and ap.fills == 0


def test_apply_sends_adds_then_edits_as_batches(ap):
    node_graph.apply(ap, EDGE_SPEC)
    ops = [op for op, _ in ap.log]
    first_edit = min(ops.index("node_connect"), ops.index("node_set_value"))
    assert max(i for i, op in enumerate(ops) if op == "node_add") < first_edit
    assert ops[-2:] == ["material_update", "fill_layer"]


def test_dry_run_changes_nothing_and_returns_the_plan(ap):
    before = ap.state()
    report = node_graph.apply(ap, EDGE_SPEC, dry_run=True)
    assert report["ok"] and report["dry_run"]
    assert ap.state() == before
    assert [s["op"] for s in report["plan"]["add"]] == ["node_add", "node_add"]
    assert any("mix.Color -> out.Base Color" in s["describe"] for s in report["plan"]["edit"])


@pytest.mark.parametrize(
    "spec, fragment",
    [
        ({"nodes": {"a": {"type": "TEX_NOPE"}}}, "unknown node type"),
        ({"nodes": {"a": {"type": "TEX_NOISE", "inputs": {"Sclae": 1}}}}, "no input socket 'Sclae'"),
        ({"nodes": {"a": {"type": "TEX_NOISE"}}, "links": ["a.Color -> b.Factor"]}, "unknown node 'b'"),
        ({"nodes": {"m": {"type": "MATH", "inputs": {"Value": 1}}}}, "ambiguous"),
        ({"nodes": {"a": {"type": "TEX_NOISE", "inputs": {"Scale": [1, 2, 3]}}}}, "expects 1 number"),
        ({"nodes": {"m": {"type": "MIX_RGB", "buttons": {"blend_type": "Frobnicate"}}}}, "not an option"),
        ({"nodes": {"a": {"type": "MATH"}, "b": {"type": "MATH"}},
          "links": ["a.Value -> b.Value[0]", "b.Value -> a.Value[1]"]}, "cycle"),
        ({"nodes": {"a": {"type": "TEX_NOISE"}}, "links": ["a.Color out.Base Color"]}, "'->'"),
        ({"nodes": {"g": {"type": "GROUP_INPUT"}}}, "only inside a node group"),
    ],
)
def test_spec_validation_errors(ap, spec, fragment):
    before = ap.state()
    with pytest.raises(SpecError) as info:
        node_graph.apply(ap, spec)
    assert fragment in str(info.value)
    assert ap.state() == before  # validation happens before anything is sent


def test_socket_refs_by_occurrence_and_index(ap):
    spec = {
        "nodes": {
            "a": {"type": "VALUE"},
            "m": {"type": "MATH", "inputs": {"Value[1]": 0.25}, "buttons": {"operation": "Multiply"}},
        },
        "links": ["a.Value -> m.#0"],
    }
    assert node_graph.apply(ap, spec, fill=False)["ok"]
    m = by_type(node_graph.read_graph(ap), "MATH")[0]
    assert m["inputs"][1]["value"] == [0.25] and m["inputs"][0]["link"]["from_name"] == "Value"
    assert m["buttons"][0]["value"] == [2.0]


def test_existing_node_by_id_and_output_values(ap):
    rgb = by_type(node_graph.read_graph(ap), "RGB")[0]
    spec = {"nodes": {"c": {"id": rgb["id"], "outputs": {"Color": [1, 0, 0]}}}}
    assert node_graph.apply(ap, spec, fill=False)["ok"]
    assert by_type(node_graph.read_graph(ap), "RGB")[0]["outputs"][0]["value"] == [1.0, 0.0, 0.0, 1.0]


def test_replace_mode_clears_everything_but_the_output(ap):
    ap.call("node_add", {"type": "TEX_CHECKER", "x": 0, "y": 0})
    assert node_graph.apply(ap, EDGE_SPEC, mode="replace", fill=False)["ok"]
    types = sorted(n["type"] for n in node_graph.read_graph(ap)["nodes"])
    assert types == ["MIX_RGB", "OUTPUT_MATERIAL_PBR", "TEX_NOISE"]


def test_failure_mid_apply_rolls_back_to_the_exact_previous_graph(ap):
    before = ap.state()
    ap.fail_on.add(("node_connect", 2))
    report = node_graph.apply(ap, EDGE_SPEC)
    assert report["ok"] is False and report["rolled_back"] is True
    assert "node_connect" in report["failed_step"]["op"]
    assert ap.state() == before  # including the RGB -> Base Color link the spec replaced
    assert ap.fills == 0


def test_auto_layout_places_new_nodes_left_of_their_consumers_without_overlap(ap):
    spec = {
        "nodes": {k: {"type": "TEX_NOISE"} for k in ("n1", "n2", "n3")}
        | {"mix": {"type": "MIX_RGB"}, "out": {"existing": "OUTPUT_MATERIAL_PBR"}},
        "links": ["n1.Color -> mix.Color 1", "n2.Color -> mix.Color 2", "n3.Factor -> mix.Factor",
                  "mix.Color -> out.Base Color"],
    }
    assert node_graph.apply(ap, spec, fill=False)["ok"]
    g = node_graph.read_graph(ap)
    placed = [n for n in g["nodes"] if n["type"] in ("TEX_NOISE", "MIX_RGB")]
    boxes = [node_graph.node_box(n) for n in placed]
    for i, a in enumerate(boxes):
        for b in boxes[i + 1 :]:
            assert a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1], (a, b)
    mix = by_type(g, "MIX_RGB")[0]
    out = by_type(g, "OUTPUT_MATERIAL_PBR")[0]
    assert all(n["x"] < mix["x"] for n in by_type(g, "TEX_NOISE")) and mix["x"] < out["x"]


# ---------------------------------------------------------------------------
# 3.4 lint
# ---------------------------------------------------------------------------


def codes(issues):
    return sorted(i["code"] for i in issues)


def test_lint_clean_default_material(ap):
    assert [i for i in node_graph.lint(node_graph.read_graph(ap)) if i["level"] != "info"] == []


def test_lint_cycle(ap):
    a = ap.call("node_add", {"type": "MATH"})["id"]
    b = ap.call("node_add", {"type": "MATH"})["id"]
    ap.call("node_connect", {"from_id": a, "from_socket": 0, "to_id": b, "to_socket": 0})
    ap.call("node_connect", {"from_id": b, "from_socket": 0, "to_id": a, "to_socket": 0})
    assert "cycle" in codes(node_graph.lint(node_graph.read_graph(ap)))


def test_lint_node_that_does_not_reach_the_output(ap):
    ap.call("node_add", {"type": "TEX_NOISE"})
    issues = node_graph.lint(node_graph.read_graph(ap))
    assert "does_not_reach_output" in codes(issues)


def test_lint_link_into_disabled_channel(ap):
    n = ap.call("node_add", {"type": "TEX_NOISE"})["id"]
    ap.call("node_connect", {"from_id": n, "from_socket": 1, "to_id": 0, "to_socket": 7})  # Height
    ap.channels["height"] = False
    issues = node_graph.lint(node_graph.read_graph(ap))
    hit = [i for i in issues if i["code"] == "disabled_channel_linked"]
    assert hit and "Height" in hit[0]["message"]


def test_lint_type_conversion(ap):
    n = ap.call("node_add", {"type": "TEX_COORD"})["id"]
    ap.call("node_connect", {"from_id": n, "from_socket": 0, "to_id": 0, "to_socket": 3})  # vector -> Roughness
    assert "type_conversion" in codes(node_graph.lint(node_graph.read_graph(ap)))


# ---------------------------------------------------------------------------
# 5.3 snapshots
# ---------------------------------------------------------------------------


def test_restore_undoes_adds_removes_values_and_links(ap):
    snap = node_graph.snapshot(ap)
    before = ap.state()
    rgb = by_type(node_graph.read_graph(ap), "RGB")[0]["id"]
    ap.call("node_remove", {"id": rgb})
    ap.call("node_add", {"type": "TEX_WAVE", "x": 5, "y": 5})
    ap.call("node_set_value", {"id": 0, "kind": "float", "socket": 3, "is_input": True, "value": 0.9})
    report = node_graph.restore(ap, snap)
    assert report["ok"], report
    assert ap.state() == before
    assert list(report["id_map"]) == [rgb] and report["id_map"][rgb] != rgb


def test_restore_is_a_no_op_when_nothing_changed(ap):
    snap = node_graph.snapshot(ap)
    ap.log.clear()
    report = node_graph.restore(ap, snap)
    assert report["ok"] and report["ops"] == 0
    assert [op for op, _ in ap.log if op.startswith("node_") and op not in ("node_list", "node_get")] == []


def test_restore_refuses_a_different_material(ap):
    snap = node_graph.snapshot(ap)
    ap.material["name"] = "Other"
    report = node_graph.restore(ap, snap)
    assert report["ok"] is False and report["code"] == "material_mismatch"


def test_snapshot_store_round_trip(tmp_path, ap):
    store = node_graph.SnapshotStore(tmp_path)
    sid = store.put(node_graph.snapshot(ap), label="before grunge")
    assert store.get(sid)["label"] == "before grunge"
    assert [e["id"] for e in node_graph.SnapshotStore(tmp_path).list()] == [sid]  # persisted
