"""Live tests for whole-graph tools, recipes and the catalogue (plan 3.1-3.5, 5.1, 5.3).

Skipped unless ARMORPAINT_LIVE=1, like tests/test_live.py.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from armorpaint_mcp import node_catalogue, node_graph, recipes, server  # noqa: E402
from armorpaint_mcp.transport import OpFailed  # noqa: E402

pytestmark = pytest.mark.skipif(os.environ.get("ARMORPAINT_LIVE") != "1", reason="needs a running ArmorPaint (ARMORPAINT_LIVE=1)")


def call(tool: str, /, **args):
    content = asyncio.run(server.call_tool(tool, args))
    texts = [c.text for c in content if getattr(c, "type", "") == "text"]
    return json.loads(texts[-1]) if texts else {}


@pytest.fixture(scope="module")
def ext() -> bool:
    return call("ap_get_app_info")["result"]["ext_state"] == 1


@pytest.fixture
def fresh(tmp_path, monkeypatch):
    monkeypatch.setenv("ARMORPAINT_MCP_STATE", str(tmp_path / "state"))
    assert call("ap_project_new")["ok"]


def graph():
    r = call("ap_node_graph_get")
    assert r["ok"], r
    return r["graph"]


def test_graph_read_matches_node_list(fresh):
    g = graph()
    nodes, links, _ = node_graph.parse_node_list(call("ap_node_list")["result"])
    assert [n["id"] for n in g["nodes"]] == [n["id"] for n in nodes]
    assert [(l["from_id"], l["to_id"]) for l in g["links"]] == [(l["from_id"], l["to_id"]) for l in links]
    out = [n for n in g["nodes"] if n["type"] == "OUTPUT_MATERIAL_PBR"][0]
    assert out["inputs"][0]["name"] == "Base Color" and out["inputs"][0]["link"] is not None


def test_catalogue_matches_every_live_node_type(fresh):
    """3.3: node_add on this build reports the sockets the catalogue predicts."""
    cat = node_catalogue.load()
    experimental = False  # config.experimental is not readable through the bridge
    drift = []
    for t in sorted(cat):
        if t == "OUTPUT_MATERIAL_PBR":
            continue
        availability = cat[t].get("availability")
        if availability is not None and t not in server.NODE_TYPES:
            # Pin the recorded fact: ArmorPaint itself cannot create it here (asked
            # directly, past the server's own validation).
            with pytest.raises(OpFailed):
                server.send_to_armorpaint("node_add", {"type": t, "x": 0, "y": 0}, 30)
            continue
        if availability == "experimental_only" and not experimental:
            continue
        r = call("ap_node_add", type=t)
        if not r["ok"]:
            drift.append(f"{t}: node_add failed: {r.get('error')}")
            continue
        live = {k: [(s["name"], s["type"]) for s in node_catalogue.parse_socket_table(r["result"][k])]
                for k in ("input_sockets", "output_sockets")}
        want = {"input_sockets": [(s["name"], s["type"]) for s in cat[t]["inputs"]],
                "output_sockets": [(s["name"], s["type"]) for s in cat[t]["outputs"]]}
        if live != want:
            drift.append(f"{t}: live {live} != catalogue {want}")
        call("ap_node_remove", id=r["result"]["id"])
    assert drift == []


def test_apply_recipe_then_read_back(fresh):
    r = call("ap_node_recipe", name="edge_wear_grunge", apply=True, params={"grunge_scale": 11})
    assert r["ok"], r
    g = graph()
    by_id = {n["id"]: n for n in g["nodes"]}
    created = r["created"]
    grunge = by_id[created["grunge"]]
    assert grunge["type"] == "TEX_VORONOI" and grunge["inputs"][1]["value"] == pytest.approx([11.0])
    wear = by_id[created["wear"]]
    assert wear["buttons"][0]["value"] == [2.0]  # Multiply
    out = [n for n in g["nodes"] if n["type"] == "OUTPUT_MATERIAL_PBR"][0]
    assert out["inputs"][0]["link"]["from_id"] == created["mix"]
    assert out["inputs"][3]["link"]["from_id"] == created["rough"]
    assert out["inputs"][4]["link"]["from_id"] == created["metal"]
    assert [i for i in call("ap_node_graph_lint")["issues"] if i["level"] != "info"] == []


@pytest.mark.parametrize("name", sorted(r["name"] for r in recipes.list_recipes()))
def test_each_recipe_applies(fresh, name):
    r = call("ap_node_recipe", name=name, apply=True)
    assert r["ok"], r


def test_failed_apply_leaves_the_graph_as_it_was(fresh, monkeypatch):
    before = graph()
    real = server.graph_bridge

    class Sabotage:
        """Corrupt the 2nd node_connect so the real bridge rejects it (bad_args)."""

        def __init__(self):
            self.inner, self.connects = real(), 0

        def call(self, op, wire):
            return self.inner.call(op, wire)

        def batch(self, items, stop_on_error=False):
            out = []
            for op, wire in items:
                if op == "node_connect":
                    self.connects += 1
                    if self.connects == 2:
                        wire = {**wire, "to_socket": 60}
                out.append((op, wire))
            return self.inner.batch(out, stop_on_error)

    monkeypatch.setattr(server, "graph_bridge", Sabotage)
    r = call("ap_node_recipe", name="stone", apply=True, mode="merge")
    assert r["ok"] is False and r["rolled_back"] is True, r
    monkeypatch.setattr(server, "graph_bridge", real)
    after = graph()
    strip = lambda g: sorted((n["type"], n["x"], n["y"], json.dumps([[s["value"] for s in n[k]] for k in ("inputs", "outputs", "buttons")])) for n in g["nodes"])  # noqa: E731
    assert strip(after) == strip(before)
    assert [(l["from_name"], l["to_name"]) for l in after["links"]] == [(l["from_name"], l["to_name"]) for l in before["links"]]


def test_snapshot_restore_round_trip(fresh):
    before = graph()
    snap = call("ap_node_graph_snapshot", label="t")
    assert call("ap_node_recipe", name="painted_wood", apply=True, fill=False)["ok"]
    rgb = [n for n in before["nodes"] if n["type"] == "RGB"][0]
    r = call("ap_node_graph_restore", snapshot_id=snap["snapshot_id"])
    assert r["ok"], r
    after = graph()
    assert sorted((n["type"], n["x"], n["y"]) for n in after["nodes"]) == sorted((n["type"], n["x"], n["y"]) for n in before["nodes"])
    new_rgb = [n for n in after["nodes"] if n["type"] == "RGB"][0]
    assert new_rgb["outputs"][0]["value"] == pytest.approx(rgb["outputs"][0]["value"])
    assert r["id_map"].get(str(rgb["id"])) == new_rgb["id"] or r["id_map"].get(rgb["id"]) == new_rgb["id"]


def test_node_edits_are_not_in_armorpaints_history(fresh, ext):
    """5.1 pins the documented fact: a node edit pushes no undo step."""
    if not ext:
        pytest.skip("reading the history needs the native extension")
    steps = call("ap_history")["result"]["steps"]
    n = call("ap_node_add", type="TEX_NOISE")["result"]["id"]
    call("ap_node_set_value", id=n, kind="float", socket=1, value=3.0)
    assert call("ap_history")["result"]["steps"] == steps
