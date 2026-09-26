"""ap_mesh_inspect, ap_mesh_uv_layout, ap_paint_stroke_uv through server.call_tool, with
the bridge mocked: export_mesh writes the real default-cube OBJ (plan 4.1, 4.2, 1.5)."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from armorpaint_mcp import mesh_inspect, server, strokes  # noqa: E402
from armorpaint_mcp.transport import BadArgs  # noqa: E402

CUBE = (Path(__file__).resolve().parent / "data" / "default_cube.obj").read_text()
TRANSFORM = {"name": "Tessellated", "loc_x": 0.0, "loc_y": 0.0, "loc_z": 0.0, "rot_x": 0.0, "rot_y": 0.0,
             "rot_z": 0.0, "rot_w": 1.0, "scale_x": 0.519615, "scale_y": 0.519615, "scale_z": 0.519615}


@pytest.fixture
def bridge(monkeypatch, tmp_path):
    log: list[tuple[str, dict]] = []

    def send(op, wire, timeout=None):
        log.append((op, dict(wire)))
        if op == "export_mesh":
            Path(wire["path"] + ".obj").write_text(CUBE)
            return {"path": wire["path"] + ".obj", "exists": True}
        if op == "get_object":
            return dict(TRANSFORM)
        if op in ("paint_stroke_world", "stroke_points"):
            return {"points": wire["points"].count(";") + 1}
        return {}

    monkeypatch.setattr(server, "send_to_armorpaint", send)
    monkeypatch.setattr(server, "read_heartbeat", lambda *a, **k: None)
    monkeypatch.setenv("ARMORPAINT_MCP_STATE", str(tmp_path / "state"))
    server._MESH_CACHE.clear()
    return log


def run(tool, /, **args):
    content = asyncio.run(server.call_tool(tool, args))
    texts = [c for c in content if c.type == "text"]
    payload = json.loads(texts[-1].text)
    payload["_images"] = sum(1 for c in content if c.type == "image")
    return payload


def test_inspect_exports_parses_and_reports(bridge):
    r = run("ap_mesh_inspect")
    assert r["ok"], r
    assert r["report"]["uv_islands"] == 6 and r["report"]["triangles"] == 188
    assert r["_images"] == 1  # the layout comes along by default
    assert [op for op, _ in bridge] == ["export_mesh"]
    assert run("ap_mesh_inspect", layout=False)["_images"] == 0
    assert [op for op, _ in bridge].count("export_mesh") == 1  # cached
    run("ap_mesh_inspect", refresh=True, layout=False)
    assert [op for op, _ in bridge].count("export_mesh") == 2


def test_uv_layout_tool(bridge):
    r = run("ap_mesh_uv_layout", size=256, heat=True)
    assert r["ok"] and r["_images"] == 1 and r["size"] == 256


def test_uv_stroke_paints_world_points_from_the_uv_path(bridge):
    m = mesh_inspect.parse_obj(CUBE)
    # the centre of the first island, in image convention
    t = m.triangles[0]
    u = sum(m.uvs[i][0] for i in t.uv) / 3
    v = 1 - sum(m.uvs[i][1] for i in t.uv) / 3
    r = run("ap_paint_stroke_uv", points=[[u, v], [u + 0.001, v]])
    assert r["ok"], r
    assert r["result"]["runs"][0]["points"] == 2 and r["result"]["missed_points"] == 0
    sent = [w for op, w in bridge if op == "paint_stroke_world"]
    assert len(sent) == 1
    first = [float(x) for x in sent[0]["points"].split(";")[0].split(",")]
    hit = mesh_inspect.uv_to_local(m, u, v)
    assert first == pytest.approx(list(mesh_inspect.to_world(TRANSFORM, hit.position)), abs=1e-5)
    assert r["result"]["runs"][0]["facing"] in ("+x", "-x", "+y", "-y", "+z", "-z")


def test_uv_stroke_across_islands_is_split_and_misses_are_reported(bridge):
    r = run("ap_paint_stroke_uv", points=[[0.02, 0.5], [0.98, 0.5]], spacing=0.01)
    assert r["ok"], r
    assert len(r["result"]["runs"]) >= 2
    assert r["result"]["missed_points"] > 0  # the gaps between islands
    assert [op for op, _ in bridge].count("paint_stroke_world") + [op for op, _ in bridge].count("stroke_begin") == len(r["result"]["runs"])


def test_uv_stroke_nothing_on_the_layout(bridge):
    r = run("ap_paint_stroke_uv", points=[[0.999, 0.999], [0.9995, 0.9995]])
    assert r["ok"] is False and r["code"] == "off_uv_layout"


def test_uv_points_must_be_in_image_convention_range():
    with pytest.raises(BadArgs):  # u up to 10 is legal: UDIM tiles 1001..1010
        server._uv_points({"points": [[-0.5, 0.5], [-0.4, 0.5]]})
    assert server._uv_points({"points": [[3.5, 0.5]]}) == [(3.5, 0.5)]
    assert server._uv_points({"points": [[0.1, 0.2, 0.5]]}) == [(0.1, 0.2, 0.5)]


def test_mesh_tools_are_not_batchable():
    for tool in ("ap_mesh_inspect", "ap_mesh_uv_layout", "ap_paint_stroke_uv"):
        with pytest.raises(BadArgs):
            server._batch_tool({"steps": [{"tool": tool}]})
    assert strokes.MAX_TOTAL_POINTS >= 1000


def test_back_facing_runs_are_skipped(bridge, monkeypatch):
    """Found live: a UV point on a face turned away from the camera is projected to the
    screen and paints whatever FRONT surface is there -- the wrong texels. So runs whose
    surface faces away from the camera are skipped (and reported)."""
    real = server.send_to_armorpaint

    def with_camera(op, wire, timeout=None):
        if op == "camera_get":
            return {"name": "Camera", "world_x": 10.0, "world_y": 0.0, "world_z": 0.0}
        return real(op, wire, timeout)

    monkeypatch.setattr(server, "send_to_armorpaint", with_camera)
    m = mesh_inspect.parse_obj(CUBE)
    painted = skipped = 0
    for u, v in _island_centres(m):
        r = run("ap_paint_stroke_uv", points=[[u, v], [u + 0.001, v]])
        if r["ok"] and r["result"]["runs"]:
            painted += 1
            assert r["result"]["runs"][0]["facing"] == "+x"
        else:
            skipped += 1
            assert r["code"] == "all_back_facing" and r["skipped_runs"][0]["facing"] != "+x"
    assert (painted, skipped) == (1, 5)


def test_back_facing_filter_can_be_turned_off(bridge, monkeypatch):
    real = server.send_to_armorpaint
    monkeypatch.setattr(server, "send_to_armorpaint", lambda op, w, t=None: (
        {"world_x": 10.0, "world_y": 0.0, "world_z": 0.0} if op == "camera_get" else real(op, w, t)))
    m = mesh_inspect.parse_obj(CUBE)
    u, v = [c for c in _island_centres(m)][0]
    r = run("ap_paint_stroke_uv", points=[[u, v], [u + 0.001, v]], backfaces="paint")
    assert r["ok"] and r["result"]["runs"]


def _island_centres(mesh):
    topo = mesh_inspect._topology(mesh)
    best = {}
    for ti, t in enumerate(mesh.triangles):
        area = abs(mesh_inspect._uv_signed_area(mesh, t))
        c = (sum(mesh.uvs[i][0] for i in t.uv) / 3, 1 - sum(mesh.uvs[i][1] for i in t.uv) / 3)
        if area > best.get(topo["island"][ti], (0, None))[0]:
            best[topo["island"][ti]] = (area, c)
    return [c for _, c in best.values()]


def test_plugin_has_a_camera_arm():
    plugin = (ROOT / "plugin" / "armorpaint_mcp_bridge.c").read_text()
    assert 'string_equals(op, "camera_get")' in plugin and 'scene_get_child("Camera")' in plugin
