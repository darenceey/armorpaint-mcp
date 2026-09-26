"""The node-graph MCP tools, end to end through server.call_tool (plan 3.x, 5.1, 5.3)."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from armorpaint_mcp import server  # noqa: E402
from armorpaint_mcp.transport import BadArgs  # noqa: E402
from fake_armorpaint import FakeArmorPaint  # noqa: E402

GRAPH_TOOLS = {
    "ap_node_graph_get", "ap_node_graph_apply", "ap_node_graph_lint",
    "ap_node_graph_snapshot", "ap_node_graph_restore", "ap_node_recipe",
}


@pytest.fixture
def ap(monkeypatch, tmp_path) -> FakeArmorPaint:
    fake = FakeArmorPaint()
    monkeypatch.setattr(server, "graph_bridge", lambda: fake)
    monkeypatch.setenv("ARMORPAINT_MCP_STATE", str(tmp_path / "state"))
    return fake


def call(tool, /, **args):
    content = asyncio.run(server.call_tool(tool, args))
    return json.loads(content[-1].text)


def test_graph_tools_are_listed_and_not_batchable():
    assert GRAPH_TOOLS <= server.TOOL_NAMES
    for tool in GRAPH_TOOLS:
        with pytest.raises(BadArgs):
            server._batch_tool({"steps": [{"tool": tool}]})


def test_get_returns_the_whole_graph(ap):
    r = call("ap_node_graph_get")
    assert r["ok"] and {n["type"] for n in r["graph"]["nodes"]} == {"RGB", "OUTPUT_MATERIAL_PBR"}


def test_apply_keeps_a_restorable_snapshot(ap):
    before = ap.state()
    r = call("ap_node_graph_apply", spec={
        "nodes": {"n": {"type": "TEX_NOISE"}, "out": {"existing": "OUTPUT_MATERIAL_PBR"}},
        "links": ["n.Color -> out.Base Color"],
    })
    assert r["ok"] and r["snapshot_id"] and "snapshot" not in r
    assert ap.state() != before
    listed = call("ap_node_graph_snapshot", list=True)
    assert [s["id"] for s in listed["snapshots"]] == [r["snapshot_id"]]
    back = call("ap_node_graph_restore", snapshot_id=r["snapshot_id"])
    assert back["ok"], back
    assert ap.state() == before


def test_bad_spec_is_reported_not_raised(ap):
    r = call("ap_node_graph_apply", spec={"nodes": {"a": {"type": "NOPE"}}})
    assert r["ok"] is False and r["code"] == "bad_spec"
    assert any("unknown node type" in p for p in r["problems"])


def test_snapshot_then_restore_by_label(ap):
    snap = call("ap_node_graph_snapshot", label="clean")
    ap.call("node_add", {"type": "TEX_WAVE"})
    assert call("ap_node_graph_restore", snapshot_id=snap["snapshot_id"])["ok"]
    assert "TEX_WAVE" not in {n["type"] for n in call("ap_node_graph_get")["graph"]["nodes"]}


def test_restore_unknown_snapshot(ap):
    r = call("ap_node_graph_restore", snapshot_id="g1")
    assert r["ok"] is False and r["code"] == "not_found"


def test_lint_tool(ap):
    ap.call("node_add", {"type": "TEX_NOISE"})
    r = call("ap_node_graph_lint")
    assert r["ok"] and any(i["code"] == "does_not_reach_output" for i in r["issues"])


def test_recipe_list_render_and_apply(ap):
    listed = call("ap_node_recipe")
    assert "edge_wear_grunge" in {x["name"] for x in listed["recipes"]}
    shown = call("ap_node_recipe", name="stone", params={"cell_scale": 9})
    assert shown["spec"]["nodes"]["cell"]["inputs"]["Scale"] == 9 and ap.updates == 0
    applied = call("ap_node_recipe", name="stone", apply=True, mode="replace")
    assert applied["ok"] and applied["snapshot_id"]
    assert "TEX_VORONOI" in {n["type"] for n in call("ap_node_graph_get")["graph"]["nodes"]}


def test_undo_tools_no_longer_claim_node_edits_are_undoable():
    """Plan 5.1: script_material_* push no history step, so ap_undo cannot take back a
    node edit. The descriptions must say so and point at graph snapshots."""
    desc = {t.name: t.description for t in server.TOOLS}
    for name in ("ap_undo", "ap_redo", "ap_history"):
        assert "node edits" not in desc[name].split("NOT")[0], name
    assert "ap_node_graph_snapshot" in desc["ap_undo"]
    assert "not" in desc["ap_undo"].lower() and "node" in desc["ap_undo"].lower()
