"""Live tests for checkpoints, rollback, atomic batches, automatic checkpoints, verified
stock undo and mesh ops (plan 5.2, 5.4-5.7, 4.3). ARMORPAINT_LIVE=1 to run; the ◆ tests
need a build with the native extension, the stock-only ones a build without it."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from armorpaint_mcp import desktop_input, mesh_inspect, server  # noqa: E402

pytestmark = pytest.mark.skipif(os.environ.get("ARMORPAINT_LIVE") != "1", reason="needs a running ArmorPaint (ARMORPAINT_LIVE=1)")


def call(tool: str, /, **args):
    content = asyncio.run(server.call_tool(tool, args))
    texts = [c.text for c in content if getattr(c, "type", "") == "text"]
    payload = json.loads(texts[-1]) if texts else {}
    payload["_images"] = sum(1 for c in content if getattr(c, "type", "") == "image")
    return payload


@pytest.fixture(scope="module")
def ext() -> bool:
    return call("ap_get_app_info")["result"]["ext_state"] == 1


@pytest.fixture
def fresh(tmp_path, monkeypatch):
    monkeypatch.setenv("ARMORPAINT_MCP_STATE", str(tmp_path / "state"))
    server._MESH_CACHE.clear()
    assert call("ap_project_new")["ok"]


def needs_ext(ext):
    if not ext:
        pytest.skip("needs the native extension")


def colour(rgb):
    r = call("ap_node_graph_apply", fill=True, spec={"nodes": {"c": {"existing": "RGB", "outputs": {"Color": rgb}}}})
    assert r["ok"], r


def quad_obj(path: Path, stacked: bool) -> Path:
    """Two quads side by side; with stacked=True both use the same UV square (overlap)."""
    uv2 = "0 0\nvt 1 0\nvt 1 1\nvt 0 1" if stacked else "0 0\nvt 0.5 0\nvt 0.5 1\nvt 0 1"
    uv1 = "0 0\nvt 1 0\nvt 1 1\nvt 0 1" if stacked else "0.5 0\nvt 1 0\nvt 1 1\nvt 0.5 1"
    path.write_text(
        "o Quads\n"
        "v -1 0 0\nv 0 0 0\nv 0 1 0\nv -1 1 0\nv 0.1 0 0\nv 1.1 0 0\nv 1.1 1 0\nv 0.1 1 0\n"
        f"vt {uv1}\nvt {uv2}\n"
        "vn 0 0 1\n"
        "f 1/1/1 2/2/1 3/3/1\nf 1/1/1 3/3/1 4/4/1\nf 5/5/1 6/6/1 7/7/1\nf 5/5/1 7/7/1 8/8/1\n"
    )
    return path


# ---------------------------------------------------------------------------
# 4.3 mesh ops ◆
# ---------------------------------------------------------------------------


def exported_normals():
    mesh, _ = server._current_mesh(True)
    return [n for n in mesh.normals]


def test_flip_normals_twice_is_the_identity(fresh, ext):
    needs_ext(ext)
    before = exported_normals()
    r = call("ap_mesh_op", action="flip_normals")
    assert r["ok"] and r["checkpoint"]["kinds"] == ["project"], r
    once = exported_normals()
    assert once != before
    assert call("ap_mesh_op", action="flip_normals")["ok"]
    assert exported_normals() == pytest.approx(before, abs=1e-3)


def test_uv_changing_ops_need_confirmation_live(fresh, ext):
    needs_ext(ext)
    r = call("ap_mesh_op", action="unwrap")
    assert r["ok"] is False and r["code"] == "bad_args" and "confirm_invalidates_paint" in r["error"]


def test_reimport_keeps_layers_and_unwrap_removes_overlap(fresh, ext, tmp_path):
    needs_ext(ext)
    assert call("ap_layer_new", kind="paint", name="keep_me")["ok"]
    layers = call("ap_layer_list")["result"]["count"]
    obj = quad_obj(tmp_path / "stacked.obj", stacked=True)
    r = call("ap_mesh_op", action="reimport", path=str(obj), confirm_invalidates_paint=True)
    assert r["ok"], r
    assert r["result"]["triangles"] == 4
    assert call("ap_layer_list")["result"]["count"] == layers
    overlap = call("ap_mesh_inspect", refresh=True, layout=False)["report"]["overlap_percent"]
    assert overlap > 90
    r = call("ap_mesh_op", action="unwrap", confirm_invalidates_paint=True)
    assert r["ok"], r
    after = call("ap_mesh_inspect", refresh=True, layout=False)["report"]["overlap_percent"]
    assert after < 1.0, after


# ---------------------------------------------------------------------------
# 5.2 history checkpoints ◆
# ---------------------------------------------------------------------------


def test_checkpoint_three_fills_rollback(fresh, ext):
    needs_ext(ext)
    colour([0.2, 0.6, 0.2, 1])
    shot = call("ap_capture_window", downscale=2)
    cp = call("ap_checkpoint", label="green")
    assert cp["ok"] and "history" in cp["kinds"], cp
    for rgb in ([0.9, 0.1, 0.1, 1], [0.1, 0.1, 0.9, 1], [0.9, 0.9, 0.1, 1]):
        colour(rgb)
    r = call("ap_rollback", checkpoint_id=cp["checkpoint_id"])
    assert r["ok"], r
    assert r["history"]["undone"] >= 3
    after = call("ap_capture_window", downscale=2, diff_against=shot["capture_id"])
    assert after["diff"]["no_visible_change"] is True, after["diff"]


def test_rollback_refuses_a_branched_history(fresh, ext):
    needs_ext(ext)
    colour([0.2, 0.6, 0.2, 1])
    colour([0.6, 0.2, 0.2, 1])
    cp = call("ap_checkpoint")
    assert call("ap_undo", steps=2)["ok"]
    colour([0.2, 0.2, 0.6, 1])  # a new action discards the undone branch
    r = call("ap_rollback", checkpoint_id=cp["checkpoint_id"])
    assert r["ok"] is False and r["code"] == "history_branched", r


def test_destructive_op_checkpoints_itself(fresh, ext):
    r = call("ap_fill_layer")
    assert r["ok"]
    if ext:
        assert r["checkpoint"]["kinds"] == ["history"]
        assert call("ap_rollback", checkpoint_id=r["checkpoint"]["id"])["ok"]
    else:
        assert "checkpoint" not in r


# ---------------------------------------------------------------------------
# 5.4 project snapshots ◆
# ---------------------------------------------------------------------------


def test_project_snapshot_keeps_the_path_and_rolls_back(fresh, ext, tmp_path):
    needs_ext(ext)
    arm = tmp_path / "mine.arm"
    assert call("ap_project_save_as", path=str(arm))["ok"]
    for _ in range(40):
        if arm.exists():
            break
        time.sleep(0.25)
    layers = call("ap_layer_list")["result"]["count"]
    cp = call("ap_checkpoint", kind="project")
    assert cp["ok"], cp
    entry = server._checkpoint_stores()[0].get(cp["checkpoint_id"])
    assert Path(entry["project"]["path"]).exists()
    assert call("ap_project_get_info")["result"]["filepath"] == str(arm)
    assert call("ap_layer_new", kind="paint")["ok"]
    assert call("ap_layer_list")["result"]["count"] == layers + 1
    r = call("ap_rollback", checkpoint_id=cp["checkpoint_id"])
    assert r["ok"], r
    assert call("ap_layer_list")["result"]["count"] == layers
    assert call("ap_project_get_info")["result"]["filepath"] == str(arm)


# ---------------------------------------------------------------------------
# 5.5 atomic batch
# ---------------------------------------------------------------------------


def test_atomic_batch_failing_step_three_of_five(fresh, ext):
    graph_before = call("ap_node_graph_get")["graph"]
    hist_before = call("ap_history")["result"] if ext else None
    r = call("ap_batch", atomic=True, steps=[
        {"tool": "ap_fill_layer"},
        {"tool": "ap_node_add", "args": {"type": "TEX_NOISE"}},
        {"tool": "ap_node_remove", "args": {"id": 99999}},
        {"tool": "ap_node_add", "args": {"type": "TEX_WAVE"}},
        {"tool": "ap_fill_layer"},
    ])
    assert r["ok"] is False and r["code"] == "batch_failed" and r["rolled_back"] is True, r
    after = call("ap_node_graph_get")["graph"]
    assert sorted(n["type"] for n in after["nodes"]) == sorted(n["type"] for n in graph_before["nodes"])
    if ext:
        h = call("ap_history")["result"]
        assert h["active_index"] == hist_before["active_index"]
        assert h["steps"][h["active_index"]]["id"] == hist_before["steps"][hist_before["active_index"]]["id"]


# ---------------------------------------------------------------------------
# 5.7 verified stock undo
# ---------------------------------------------------------------------------


def test_stock_undo_is_verified(fresh, ext):
    if ext:
        pytest.skip("the extension handles undo exactly on this build")
    if desktop_input.supported() is None:
        pytest.skip("no synthetic-input backend")
    tag = f"verify_{int(time.time())}"
    assert call("ap_material_create", name=tag)["ok"]
    r = call("ap_undo")
    assert r["ok"], r
    assert r["verified"]["changed"] is True, r["verified"]
    call("ap_redo")
