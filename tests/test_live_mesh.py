"""Live tests for mesh inspection and UV-space strokes (plan 4.1, 4.2, 1.5).
ARMORPAINT_LIVE=1 to run."""

from __future__ import annotations

import asyncio
import json
import math
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from armorpaint_mcp import mesh_inspect, server, window_capture  # noqa: E402

pytestmark = pytest.mark.skipif(os.environ.get("ARMORPAINT_LIVE") != "1", reason="needs a running ArmorPaint (ARMORPAINT_LIVE=1)")


def call(tool: str, /, **args):
    content = asyncio.run(server.call_tool(tool, args))
    texts = [c.text for c in content if getattr(c, "type", "") == "text"]
    payload = json.loads(texts[-1]) if texts else {}
    payload["_images"] = sum(1 for c in content if getattr(c, "type", "") == "image")
    return payload


@pytest.fixture
def fresh(tmp_path, monkeypatch):
    monkeypatch.setenv("ARMORPAINT_MCP_STATE", str(tmp_path / "state"))
    server._MESH_CACHE.clear()
    assert call("ap_project_new")["ok"]


def test_inspect_the_live_default_cube(fresh):
    r = call("ap_mesh_inspect")
    assert r["ok"], r
    rep = r["report"]
    assert rep["has_uvs"] is True  # the exported OBJ carries vt
    assert rep["triangles"] == 188 and rep["uv_islands"] == 6
    assert [i for i in rep["issues"] if i["level"] != "info"] == []
    assert r["_images"] == 1


def base_color(directory):
    exp = call("ap_export_textures", directory=str(directory))
    assert exp["ok"], exp
    files = exp["result"]["files"]
    files = files.split("|") if isinstance(files, str) else files
    name = [f for f in files if "base" in f.lower() and f.endswith(".png")][0]
    return window_capture._decode_png_rgb((Path(directory) / name).read_bytes())


def island_centres(mesh):
    """Per island, the centroid of its largest UV triangle (image convention)."""
    topo = mesh_inspect._topology(mesh)
    best: dict[int, tuple[float, tuple[float, float]]] = {}
    for ti, t in enumerate(mesh.triangles):
        area = abs(mesh_inspect._uv_signed_area(mesh, t))
        u = sum(mesh.uvs[i][0] for i in t.uv) / 3
        v = 1 - sum(mesh.uvs[i][1] for i in t.uv) / 3
        isl = topo["island"][ti]
        if area > best.get(isl, (0, None))[0]:
            best[isl] = (area, (u, v))
    return [c for _, c in best.values()]


def test_uv_strokes_land_on_the_requested_texels(fresh, tmp_path):
    """1.5 calibration: OBJ axes and v flip are right when the texels at the requested UVs
    change -- and nothing changes far from them (a wrong mapping paints elsewhere)."""
    assert call("ap_node_graph_apply", fill=False, spec={
        "nodes": {"c": {"existing": "RGB", "outputs": {"Color": [0.9, 0.05, 0.05, 1]}}},
    })["ok"]
    call("ap_select_tool", tool="brush")
    call("ap_set_brush", radius=0.08, opacity=1.0, hardness=1.0)
    w, h, before = base_color(tmp_path / "before")
    mesh, _ = server._current_mesh(True)
    centres = island_centres(mesh)
    assert len(centres) == 6
    painted = []
    for u, v in centres:
        r = call("ap_paint_stroke_uv", points=[[u, v], [u + 0.002, v]])
        if r["ok"]:
            painted.append((u, v))
        else:  # a face turned away from the camera is skipped, not painted wrongly
            assert r["code"] == "all_back_facing", r
    assert len(painted) >= 2, "the default camera sees two faces of the cube"
    w2, h2, after = base_color(tmp_path / "after")
    assert (w, h) == (w2, h2)

    def changed(x, y):
        i = (y * w + x) * 3
        return any(abs(before[i + k] - after[i + k]) > 16 for k in range(3))

    hit = [(u, v) for u, v in painted if changed(min(w - 1, int(u * w)), min(h - 1, int(v * h)))]
    assert hit == painted, f"only {len(hit)} of the {len(painted)} painted centres changed"
    far = 0
    for y in range(0, h, 4):
        for x in range(0, w, 4):
            if changed(x, y):
                d = min(math.hypot(x / w - u, y / h - v) for u, v in centres)
                far += d > 0.12
    assert far == 0, f"{far} sampled texels changed away from every requested UV"


def test_uv_stroke_reports_runs_and_facing(fresh):
    mesh, _ = server._current_mesh(True)
    results = [call("ap_paint_stroke_uv", points=[[u - 0.05, v], [u + 0.05, v]], spacing=0.01)
               for u, v in island_centres(mesh)]
    ok = [r for r in results if r["ok"]]
    skipped = [r for r in results if not r["ok"]]
    assert ok and skipped
    assert {r["result"]["runs"][0]["facing"] for r in ok} <= {"+x", "-y"}  # the default camera's view
    assert all(r["code"] == "all_back_facing" for r in skipped)
