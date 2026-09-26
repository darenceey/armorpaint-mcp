"""Checkpoints and rollback, atomic batches, automatic checkpoints, verified stock undo,
mesh ops (plan 5.2, 5.4-5.7, 4.3). Offline, against tests/fake_armorpaint.py."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from armorpaint_mcp import checkpoints, desktop_input, node_graph, server  # noqa: E402
from armorpaint_mcp.transport import BadArgs, OpFailed  # noqa: E402
from fake_armorpaint import FakeArmorPaint, FakeExtArmorPaint  # noqa: E402


@pytest.fixture
def stores(tmp_path):
    return checkpoints.CheckpointStore(tmp_path / "cp"), node_graph.SnapshotStore(tmp_path / "graphs"), tmp_path / "snaps"


def take(ap, stores, kind="auto", label=None):
    cps, graphs, snaps = stores
    return checkpoints.take(ap, kind=kind, label=label, store=cps, graph_store=graphs, snapshot_dir=snaps)


def rollback(ap, stores, entry):
    return checkpoints.rollback(ap, entry, graph_store=stores[1])


# ---------------------------------------------------------------------------
# 5.2 history checkpoints
# ---------------------------------------------------------------------------


def test_rollback_undoes_back_to_the_checkpoint(stores):
    ap = FakeExtArmorPaint()
    ap.push("Paint")
    cp = take(ap, stores)
    assert cp["kinds"] == ["history", "graph"]
    for _ in range(3):
        ap.call("fill_layer", {})
    r = rollback(ap, stores, cp)
    assert r["ok"], r
    assert r["history"]["undone"] == 3
    h = ap.call("history", {})
    assert h["steps"][h["active_index"]]["id"] == cp["history"]["active_id"]


def test_rollback_can_redo_forward(stores):
    ap = FakeExtArmorPaint()
    ap.push("A")
    ap.push("B")
    cp = take(ap, stores)
    ap.call("undo", {"steps": 1})
    r = rollback(ap, stores, cp)
    assert r["ok"] and r["history"]["redone"] == 1


def test_rollback_refuses_after_the_history_was_truncated(stores):
    ap = FakeExtArmorPaint(undo_steps=4)
    ap.push("A")
    cp = take(ap, stores)
    for _ in range(5):
        ap.push("Paint")  # A falls off the end
    r = rollback(ap, stores, cp)
    assert r["ok"] is False and r["code"] == "history_truncated"
    assert "undo_steps" in r["error"]


def test_rollback_refuses_after_a_branch(stores):
    ap = FakeExtArmorPaint()
    ap.push("A")
    ap.push("B")
    cp = take(ap, stores)
    ap.call("undo", {"steps": 2})
    ap.push("C")  # discards A and B's redo branch
    r = rollback(ap, stores, cp)
    assert r["ok"] is False and r["code"] == "history_branched"


def test_headroom_warns_before_a_checkpoint_falls_off(stores):
    ap = FakeExtArmorPaint(undo_steps=4)
    ap.push("A")
    cp = take(ap, stores)
    ap.push("B")
    ap.push("C")
    assert checkpoints.headroom(ap, cp) == 1
    ap.push("D")
    assert checkpoints.headroom(ap, cp) == 0


def test_checkpoint_before_any_history(stores):
    ap = FakeExtArmorPaint()
    cp = take(ap, stores)
    ap.push("A")
    ap.push("B")
    r = rollback(ap, stores, cp)
    assert r["ok"] and r["history"]["undone"] == 2


# ---------------------------------------------------------------------------
# graph and project kinds, stock builds
# ---------------------------------------------------------------------------


def test_stock_build_checkpoints_the_graph_only(stores):
    ap = FakeArmorPaint()
    before = ap.state()
    cp = take(ap, stores)
    assert cp["kinds"] == ["graph"]
    ap.call("node_add", {"type": "TEX_NOISE"})
    r = rollback(ap, stores, cp)
    assert r["ok"] and ap.state() == before
    with pytest.raises(OpFailed):
        take(ap, stores, kind="history")


def test_node_edits_roll_back_through_the_graph_part(stores):
    ap = FakeExtArmorPaint()
    before = ap.state()
    cp = take(ap, stores)
    ap.call("fill_layer", {})
    ap.call("node_add", {"type": "TEX_WAVE"})  # not in the history
    r = rollback(ap, stores, cp)
    assert r["ok"] and r["history"]["undone"] == 1 and ap.state() == before


def test_project_checkpoint_writes_a_snapshot_and_restores_the_path(stores):
    ap = FakeExtArmorPaint()
    ap.filepath = "/work/goblin.arm"
    cp = take(ap, stores, kind="project")
    assert cp["kinds"] == ["project"]
    snap = cp["project"]["path"]
    assert snap in ap.files and snap.endswith(".arm") and ap.filepath == "/work/goblin.arm"
    ap.filepath = "/elsewhere.arm"
    r = rollback(ap, stores, cp)
    assert r["ok"] and ap.opened == [snap] and ap.filepath == "/work/goblin.arm"


def test_project_checkpoint_needs_the_extension(stores):
    with pytest.raises(OpFailed):
        take(FakeArmorPaint(), stores, kind="project")


def test_store_lists_and_rejects_unknown_ids(stores):
    ap = FakeExtArmorPaint()
    cp = take(ap, stores, label="before rust")
    assert [e["label"] for e in stores[0].list()] == ["before rust"]
    with pytest.raises(KeyError):
        stores[0].get("nope")
    assert stores[0].get(cp["id"])["kinds"] == cp["kinds"]


# ---------------------------------------------------------------------------
# the tools: ap_checkpoint / ap_rollback, atomic batch, automatic checkpoints
# ---------------------------------------------------------------------------


@pytest.fixture
def tools(monkeypatch, tmp_path):
    def install(ap):
        monkeypatch.setattr(server, "graph_bridge", lambda: ap)
        monkeypatch.setattr(server, "send_to_armorpaint", lambda op, wire, timeout=None: ap.call(op, wire))
        monkeypatch.setattr(server, "send_batch", lambda items, timeout, stop: ap.batch(items, stop))
        monkeypatch.setattr(server, "read_heartbeat", lambda *a, **k: None)
        return ap

    monkeypatch.setenv("ARMORPAINT_MCP_STATE", str(tmp_path / "state"))
    monkeypatch.delenv("ARMORPAINT_MCP_AUTOCHECKPOINT", raising=False)
    return install


def run(tool, /, **args):
    content = asyncio.run(server.call_tool(tool, args))
    return json.loads([c for c in content if c.type == "text"][-1].text)


def test_checkpoint_and_rollback_tools(tools):
    ap = tools(FakeExtArmorPaint())
    cp = run("ap_checkpoint", label="clean")
    assert cp["ok"] and cp["kinds"] == ["history", "graph"]
    ap.call("fill_layer", {})
    listed = run("ap_checkpoint_list")
    assert listed["checkpoints"][0]["id"] == cp["checkpoint_id"]
    assert listed["checkpoints"][0]["history_headroom"] is not None
    r = run("ap_rollback", checkpoint_id=cp["checkpoint_id"])
    assert r["ok"], r
    assert run("ap_rollback", checkpoint_id="nope")["code"] == "not_found"


def test_atomic_batch_rolls_back_on_failure(tools):
    ap = tools(FakeExtArmorPaint())
    before, steps_before = ap.state(), list(ap.steps)
    r = run("ap_batch", atomic=True, steps=[
        {"tool": "ap_fill_layer"},
        {"tool": "ap_node_add", "args": {"type": "TEX_NOISE"}},
        {"tool": "ap_node_connect", "args": {"from_id": 7, "from_socket": 0, "to_id": 0, "to_socket": 60}},
        {"tool": "ap_fill_layer"},
    ])
    assert r["ok"] is False and r["code"] == "batch_failed" and r["rolled_back"] is True
    assert ap.state() == before
    h = ap.call("history", {})
    assert [s["id"] for s in h["steps"][: h["active_index"] + 1]] == [s["id"] for s in steps_before]


def test_atomic_batch_on_a_stock_build_rolls_back_the_graph(tools):
    ap = tools(FakeArmorPaint())
    before = ap.state()
    r = run("ap_batch", atomic=True, steps=[
        {"tool": "ap_node_add", "args": {"type": "TEX_NOISE"}},
        {"tool": "ap_node_remove", "args": {"id": 999}},
    ])
    assert r["rolled_back"] is True and ap.state() == before
    assert "graph" in r["rollback"]["parts"]


def test_successful_atomic_batch_keeps_its_changes(tools):
    ap = tools(FakeExtArmorPaint())
    r = run("ap_batch", atomic=True, steps=[{"tool": "ap_fill_layer"}])
    assert r["ok"] and "checkpoint_id" in r and ap.fills == 1


def test_destructive_ops_take_an_automatic_checkpoint(tools, monkeypatch):
    ap = tools(FakeExtArmorPaint())
    r = run("ap_fill_layer")
    assert r["ok"] and r["checkpoint"]["kinds"] == ["history"]
    assert run("ap_rollback", checkpoint_id=r["checkpoint"]["id"])["ok"]
    monkeypatch.setenv("ARMORPAINT_MCP_AUTOCHECKPOINT", "0")
    assert "checkpoint" not in run("ap_fill_layer")


def test_project_open_takes_a_project_checkpoint(tools, tmp_path):
    ap = tools(FakeExtArmorPaint())
    target = tmp_path / "other.arm"
    target.write_bytes(b"x")
    r = run("ap_project_open", path=str(target))
    assert r["ok"] and r["checkpoint"]["kinds"] == ["project"]


def test_stock_build_gets_no_automatic_history_checkpoint(tools):
    tools(FakeArmorPaint())
    r = run("ap_fill_layer")
    assert r["ok"] and "checkpoint" not in r


# ---------------------------------------------------------------------------
# 5.7 verified stock undo
# ---------------------------------------------------------------------------


def test_stock_undo_reports_whether_anything_changed(tools, monkeypatch):
    ap = tools(FakeArmorPaint())
    pressed = []

    def key(name, mods, title=None, fence=None):
        pressed.append((name, mods))
        ap.call("node_add", {"type": "TEX_NOISE"})  # what the keystroke did, seen through the bridge

    monkeypatch.setattr(desktop_input, "key", key)
    monkeypatch.setattr(server, "_frame_fence", lambda: None)
    monkeypatch.setattr(server, "capture_window", lambda *a, **k: (_ for _ in ()).throw(server.CaptureError("unsupported", "none")))
    r = run("ap_undo")
    assert r["ok"] and pressed == [("z", ["ctrl"])]
    assert r["verified"]["changed"] is True and "node_graph" in r["verified"]["changed_parts"]

    monkeypatch.setattr(desktop_input, "key", lambda *a, **k: None)
    r = run("ap_undo")
    assert r["verified"]["changed"] is False
    assert "nothing" in r["verified"]["note"]


# ---------------------------------------------------------------------------
# 4.3 mesh ops (server side)
# ---------------------------------------------------------------------------


def test_uv_changing_mesh_ops_need_confirmation(tools):
    tools(FakeExtArmorPaint())
    with pytest.raises(BadArgs, match="confirm_invalidates_paint"):
        server._mesh_op_args({"action": "unwrap"})
    assert server._mesh_op_args({"action": "unwrap", "confirm_invalidates_paint": True})["action"] == "unwrap"
    assert server._mesh_op_args({"action": "flip_normals"})["action"] == "flip_normals"
    with pytest.raises(BadArgs):
        server._mesh_op_args({"action": "melt"})


def test_mesh_op_takes_a_project_checkpoint_and_drops_the_mesh_cache(tools):
    ap = tools(FakeExtArmorPaint())
    ap.op_mesh_op = lambda wire: {"action": wire["action"], "vertices": 8, "triangles": 12}
    server._MESH_CACHE["entry"] = ("stale", {"at": 0})
    r = run("ap_mesh_op", action="flip_normals")
    assert r["ok"] and r["checkpoint"]["kinds"] == ["project"]
    assert "entry" not in server._MESH_CACHE
    assert r["checkpoint"]["id"]


def test_an_old_extension_without_step_ids_is_named_not_crashed_on(stores, tools):
    """Found live: an extension build from before step ids (ext_version 1) made the server
    raise KeyError('id'). It must say the extension is outdated instead."""
    ap = FakeExtArmorPaint()
    real = ap.op_history
    ap.op_history = lambda wire: {**real(wire), "steps": [{k: v for k, v in s.items() if k != "id"} for s in real(wire)["steps"]]}
    ap.push("A")
    with pytest.raises(OpFailed) as info:
        take(ap, stores, kind="history")
    assert info.value.code == "ext_outdated" and "apply_ext_patch" in info.value.message
    assert take(ap, stores)["kinds"] == ["graph"]  # auto: skips the history part
    tools(ap)
    r = run("ap_fill_layer")
    assert r["ok"] and "outdated" in r["checkpoint"]["error"]
