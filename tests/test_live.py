"""Live tests against a running ArmorPaint with the bridge plugin enabled.

Skipped unless ARMORPAINT_LIVE=1. Everything goes through server.call_tool, the same entry
point an MCP client uses. Works on a stock build (extension tools must then answer
'unsupported') and on a build carrying patch/apply_ext_patch.py (they must work).

    ARMORPAINT_LIVE=1 python -m pytest tests/test_live.py -v
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from armorpaint_mcp import desktop_input, server, transport  # noqa: E402

pytestmark = pytest.mark.skipif(os.environ.get("ARMORPAINT_LIVE") != "1", reason="needs a running ArmorPaint (ARMORPAINT_LIVE=1)")


def call(tool_name: str, /, **args):
    content = asyncio.run(server.call_tool(tool_name, args))
    texts = [c.text for c in content if getattr(c, "type", "") == "text"]
    images = [c for c in content if getattr(c, "type", "") == "image"]
    payload = json.loads(texts[-1]) if texts else {}
    payload["_images"] = len(images)
    return payload


@pytest.fixture(scope="module")
def ext() -> bool:
    info = call("ap_get_app_info")
    assert info["ok"], info
    return info["result"]["ext_state"] == 1


def test_status_and_ping():
    st = call("ap_bridge_status")
    assert st["alive"] is True, st
    assert call("ap_ping")["ok"]


def test_doze_then_wake():
    assert call("ap_bridge_set_idle", linger=0.5)["ok"]
    deadline = time.time() + 60
    while time.time() < deadline:
        hb = transport.read_heartbeat() or {}
        if hb.get("dozing"):
            t0 = hb.get("t")
            time.sleep(3)
            if (transport.read_heartbeat() or {}).get("t") == t0:
                break  # dozing AND the app has stopped running frames
        time.sleep(0.5)
    else:
        pytest.fail("the app never went to sleep")
    started = time.time()
    r = call("ap_ping")
    assert r["ok"], r
    assert time.time() - started < 5
    assert call("ap_bridge_set_idle", linger=10)["ok"]


def test_polling_does_not_leak_file_descriptors():
    """Iron's POSIX close_dir() is empty, so a bridge that lists req/ to poll leaks an fd a
    frame; under a desktop launch's 1024-fd limit ArmorPaint then hangs in gpu_present."""
    if not sys.platform.startswith("linux"):
        pytest.skip("reads /proc")
    pids = [p.name for p in Path("/proc").iterdir() if p.name.isdigit() and _comm(p) == "ArmorPaint"]
    if len(pids) != 1:
        pytest.skip(f"expected one ArmorPaint process, found {len(pids)}")
    fd_dir = Path("/proc") / pids[0] / "fd"
    before = len(list(fd_dir.iterdir()))
    for _ in range(150):
        assert call("ap_ping")["ok"]
    time.sleep(2)  # the bridge keeps polling every frame for a while after a request
    after = len(list(fd_dir.iterdir()))
    assert after - before <= 3, f"{after - before} fds leaked over 150 requests"


def _comm(proc: Path) -> str:
    try:
        return (proc / "comm").read_text().strip()
    except OSError:
        return ""


def test_disabled_bridge_can_be_reenabled_remotely():
    assert call("ap_bridge_set_enabled", enabled=False)["ok"]
    r = call("ap_get_context")
    assert r["ok"] is False and r["code"] == "bridge_disabled", r
    assert call("ap_bridge_set_enabled", enabled=True)["ok"]
    assert call("ap_get_context")["ok"]


def test_batch_runs_several_steps_per_frame():
    steps = [{"tool": "ap_get_context"}, {"tool": "ap_material_get_active"}, {"tool": "ap_node_list"}]
    steps += [{"tool": "ap_select_tool", "args": {"tool": t}} for t in ("fill", "brush", "eraser", "brush")]
    steps += [{"tool": "ap_node_get", "args": {"id": 999999}}, {"tool": "ap_project_get_info"}]
    r = call("ap_batch", steps=steps)
    assert r["ok"], r
    res = r["result"]
    assert res["count"] == len(steps) and res["executed"] == len(steps)
    assert res["errors"] == 1  # the bogus node id
    assert res["frames"] < len(steps), res["frames"]
    assert [x["tool"] for x in res["results"]][:2] == ["ap_get_context", "ap_material_get_active"]


def test_batch_stop_on_error():
    r = call("ap_batch", steps=[{"tool": "ap_node_get", "args": {"id": 999999}}, {"tool": "ap_ping"}], stop_on_error=True)
    assert r["result"]["executed"] == 1 and r["result"]["stopped_early"] is True


def test_capture_window():
    r = call("ap_capture_window", downscale=2)
    assert r["ok"], r
    assert r["_images"] == 1


def test_ui_click_and_key_do_not_break_the_bridge():
    cap = call("ap_capture_window", include_image=False)
    w, h = cap["window_size"]
    assert call("ap_ui_click", x=w // 2, y=h // 2, button="middle")["ok"]
    assert call("ap_ui_scroll", x=w // 2, y=h // 2, clicks=1)["ok"]
    assert call("ap_ui_key", key="escape")["ok"]
    assert call("ap_ping")["ok"]


def test_heavy_op_waits_for_mouse_release(tmp_path):
    """An export must not start while a mouse button is held in the app."""
    if desktop_input.supported() != "x11":
        pytest.skip("holds a button through the X11 backend")
    s = desktop_input._X11Session(None)
    try:
        s.send(desktop_input.MOTION_NOTIFY, 11, 11)
        s.send(desktop_input.MOTION_NOTIFY, 10, 10)
        s.send(desktop_input.BUTTON_PRESS, 10, 10, 0, 2)  # middle button: no tool acts on it
        time.sleep(0.5)
        result: dict = {}
        worker = threading.Thread(target=lambda: result.update(call("ap_export_textures", directory=str(tmp_path))))
        worker.start()
        time.sleep(2.5)
        hb = transport.read_heartbeat() or {}
        assert hb.get("job_held") is True, hb
        assert worker.is_alive()
        s.send(desktop_input.BUTTON_RELEASE, 10, 10, 1 << 9, 2)
        worker.join(60)
        assert result.get("ok"), result
    finally:
        s.close()


def test_extension_tools(ext, tmp_path):
    if not ext:
        r = call("ap_layer_list")
        assert r["ok"] is False and r["code"] == "unsupported" and "hint" in r
        return
    before = call("ap_layer_list")["result"]["count"]
    tag = f"mcp_test_{int(time.time())}"
    new = call("ap_layer_new", kind="paint", name=tag)
    assert new["ok"] and new["result"]["layer"]["name"] == tag
    r = call("ap_layer_set", name=tag, opacity=0.25, blending="overlay", new_name=tag + "b")
    assert r["result"]["layer"]["opacity"] == pytest.approx(0.25)
    assert r["result"]["layer"]["blending"] == "overlay" and r["result"]["layer"]["name"] == tag + "b"
    hist = call("ap_history")["result"]
    assert hist["undos_available"] >= 4
    # new layer + rename + opacity + blending = four undo steps, as in the Layers panel
    u = call("ap_undo", steps=4)
    assert u["ok"] and u["result"]["undone"] == 4
    layers = call("ap_layer_list")["result"]["layers"]
    assert not any(l["name"].startswith(tag) for l in layers)
    assert call("ap_redo", steps=1)["ok"]
    call("ap_undo", steps=1)
    assert call("ap_layer_list")["result"]["count"] == before

    ex = call("ap_export_textures", directory=str(tmp_path / "exr"), format="exr", bits=16, filename="t")
    assert ex["ok"], ex
    assert any(f.endswith(".exr") for f in ex["result"]["files"])
    call("ap_export_textures", directory=str(tmp_path / "png"), format="png", bits=8)
    assert call("ap_export_presets")["result"]["presets"]
    assert call("ap_render_settings", vignette=0.2)["result"]["vignette"] == pytest.approx(0.2)
    assert call("ap_project_lists")["result"]["live"] is True
    assert call("ap_material_list")["result"]["live"] is True
    assert call("ap_camera", view="top")["ok"]
    assert call("ap_camera", view="reset")["ok"]
    assert call("ap_console_read", max_lines=5)["ok"]
    cap = call("ap_capture_viewport", path=str(tmp_path / "vp.png"), width=256, height=256)
    assert cap["ok"] and cap["_images"] == 1


def test_keyboard_undo_matches_history(ext):
    """The stock-build undo path (a ctrl+z keystroke) really undoes, checked via the extension."""
    if not ext:
        pytest.skip("needs the extension to read the history back")
    if desktop_input.supported() != "x11":
        pytest.skip("keystroke path verified on X11")
    tag = f"kbd_undo_{int(time.time())}"
    call("ap_layer_new", kind="paint", name=tag)
    before = call("ap_history")["result"]["redos_available"]
    desktop_input.key("z", ["ctrl"], fence=server._frame_fence())
    after = call("ap_history")["result"]["redos_available"]
    assert after == before + 1
    assert not any(l["name"] == tag for l in call("ap_layer_list")["result"]["layers"])


def test_resource_search_and_metadata(tmp_path):
    r = call("ap_resource_search", query="", kinds=["export_preset"])
    assert r["ok"]
    arm = tmp_path / "meta_test.arm"
    arm.write_bytes(b"x")
    m = call("ap_project_metadata", project_path=str(arm), set={"brief": "test"})
    assert m["ok"] and m["metadata"] == {"brief": "test"}


def test_regression_sweep_of_original_tools(tmp_path, ext):
    """The pre-existing tool surface still works end to end (scratch project)."""
    ok = lambda r: r.get("ok") is True  # noqa: E731

    assert ok(call("ap_project_new"))
    arm = str(tmp_path / "sweep.arm")
    assert ok(call("ap_project_save_as", path=arm))
    for _ in range(40):
        if Path(arm).exists():
            break
        time.sleep(0.25)
    assert Path(arm).exists()
    assert ok(call("ap_project_get_info"))
    assert ok(call("ap_get_config"))
    assert ok(call("ap_set_config", camera_fov=0.7))
    assert ok(call("ap_get_main_object"))
    assert ok(call("ap_shape_list"))
    shape = call("ap_shape_add", name="sphere")
    assert ok(shape), shape
    name = shape["result"]["name"]
    assert ok(call("ap_object_set_transform", name=name, location=[1, 0, 0], rotation_euler_degrees=[0, 0, 45]))
    assert ok(call("ap_object_set_visible", name=name, visible=True))
    assert ok(call("ap_material_create", name="SweepMat"))
    assert ok(call("ap_material_select", name="SweepMat"))
    mat = call("ap_material_get_active")
    assert mat["result"]["name"] == "SweepMat"
    node = call("ap_node_add", type="TEX_NOISE", x=50, y=50)
    assert ok(node), node
    nid = node["result"]["id"]
    assert ok(call("ap_node_get", id=nid))
    graph = call("ap_node_list")["result"]
    out_id = int([n for n in graph["nodes"].split(";") if "OUTPUT_MATERIAL_PBR" in n][0].split(",")[0])
    assert ok(call("ap_node_connect", from_id=nid, from_socket=0, to_id=out_id, to_socket=0))
    assert ok(call("ap_node_set_value", id=nid, kind="float", socket=2, value=4.0))
    assert ok(call("ap_material_update"))
    assert ok(call("ap_fill_layer"))
    assert ok(call("ap_node_disconnect", to_id=out_id, to_socket=0))
    assert ok(call("ap_node_remove", id=nid))
    assert ok(call("ap_set_brush", radius=0.3, opacity=0.8))
    assert ok(call("ap_select_tool", tool="brush"))
    assert ok(call("ap_paint_stroke", points=[[0.4, 0.5], [0.5, 0.5], [0.6, 0.5]]))
    assert ok(call("ap_paint_stroke_world", points=[[0, 0, 1], [0.1, 0, 1]]))
    assert ok(call("ap_set_display_channel", mode="roughness"))
    assert ok(call("ap_set_display_channel", mode="lit"))
    assert ok(call("ap_set_envmap_params", strength=1.5))
    exp = call("ap_export_textures", directory=str(tmp_path / "tex"))
    assert ok(exp) and exp["result"]["files_added"] > 0, exp
    png = [f for f in exp["result"]["files"].split("|") if f.endswith(".png")][0]
    assert call("ap_read_image_file", path=str(tmp_path / "tex" / png))["_images"] == 1
    assert ok(call("ap_export_mesh", path=str(tmp_path / "mesh")))
    assert ok(call("ap_export_material", path=str(tmp_path / "mat.arm")))
    assert ok(call("ap_fs_mkdir", path=str(tmp_path / "made")))
    assert call("ap_fs_stat", path=str(tmp_path / "made"))["result"]["is_directory"] is True
    assert ok(call("ap_fs_list", path=str(tmp_path)))
    assert ok(call("ap_capture_to_project", width=64, height=64))
    vp = call("ap_capture_viewport", path=str(tmp_path / "vp.png"), width=128, height=128)
    assert ok(vp) or vp.get("code") == "unsupported"
    assert ok(call("ap_console_write", text="armorpaint-mcp live sweep", level="info"))
    assert ok(call("ap_show_message", text="sweep done", seconds=1))
    assert ok(call("ap_material_assign", object=name, material="SweepMat"))
    assert ok(call("ap_material_delete", name="SweepMat"))
    assert ok(call("ap_project_list_texture_assets"))
    assert ok(call("ap_project_list_scripts"))
    assert ok(call("ap_material_list"))
    assert ok(call("ap_import_asset", path=str(tmp_path / "tex" / png)))
    assert ok(call("ap_project_open", path=arm))
    assert ok(call("ap_project_save"))


def test_stock_undo_redo_fall_back_to_shortcuts(ext):
    """Without the extension, ap_undo/ap_redo press ArmorPaint's own shortcuts."""
    if ext:
        pytest.skip("the extension handles undo exactly on this build")
    if desktop_input.supported() is None:
        pytest.skip("no synthetic-input backend")
    tag = f"undo_probe_{int(time.time())}"
    assert call("ap_material_create", name=tag)["ok"]  # pushes a 'New Material' step
    u = call("ap_undo")
    assert u["ok"] and "shortcut" in u["method"], u
    gone = call("ap_material_select", name=tag)
    assert gone["ok"] is False and gone["code"] == "not_found", gone
    r = call("ap_redo")
    assert r["ok"], r
    assert call("ap_material_select", name=tag)["ok"]
