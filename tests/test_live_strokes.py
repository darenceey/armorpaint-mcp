"""Live tests for stroke kinematics and the filmstrip (plan 1.1-1.4, 2.3).
ARMORPAINT_LIVE=1 to run."""

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

from armorpaint_mcp import desktop_input, image_diff, server, transport  # noqa: E402

pytestmark = pytest.mark.skipif(os.environ.get("ARMORPAINT_LIVE") != "1", reason="needs a running ArmorPaint (ARMORPAINT_LIVE=1)")


def call(tool: str, /, **args):
    content = asyncio.run(server.call_tool(tool, args))
    texts = [c.text for c in content if getattr(c, "type", "") == "text"]
    payload = json.loads(texts[-1]) if texts else {}
    payload["_images"] = sum(1 for c in content if getattr(c, "type", "") == "image")
    return payload


@pytest.fixture
def red_brush(tmp_path, monkeypatch):
    """A fresh project whose material is red, with the brush selected and nothing painted
    yet: anything the brush paints shows up red on the grey default cube."""
    monkeypatch.setenv("ARMORPAINT_MCP_STATE", str(tmp_path / "state"))
    assert call("ap_project_new")["ok"]
    r = call("ap_node_graph_apply", fill=False, spec={
        "nodes": {"c": {"existing": "RGB", "outputs": {"Color": [0.9, 0.05, 0.05, 1]}}},
    })
    assert r["ok"], r
    assert call("ap_select_tool", tool="brush")["ok"]
    assert call("ap_set_brush", radius=0.2, opacity=1.0, hardness=0.9)["ok"]
    yield
    call("ap_stroke_end")
    call("ap_set_brush", radius=0.5, opacity=1.0)


def hb():
    return transport.read_heartbeat() or {}


def diff_of(r):
    return r["capture"]["diff"]


def column_thickness(before, after, x0, x1):
    """Changed pixels per column, averaged over columns x0..x1 (after-capture pixels)."""
    w, h = after.width, after.height
    a, b = before.pixels(), after.pixels()
    total = 0
    for x in range(x0, x1):
        for y in range(h):
            i = (y * w + x) * 3
            if abs(a[i] - b[i]) > 8 or abs(a[i + 1] - b[i + 1]) > 8 or abs(a[i + 2] - b[i + 2]) > 8:
                total += 1
    return total / max(1, x1 - x0)


def test_200_point_stroke_streams_and_paints_its_whole_length(red_brush):
    pts = [[0.35 + 0.3 * i / 199, 0.5] for i in range(200)]
    r = call("ap_paint_stroke", points=pts, capture={"downscale": 2})
    assert r["ok"], r
    assert r["result"]["streamed"] is True and r["result"]["requests"] == 7 and r["result"]["points"] == 200
    d = diff_of(r)
    assert d["no_visible_change"] is False, d
    # 30% of the viewport's width: the viewport is most of a 1720 px window, 860 px at downscale 2
    assert d["bbox"][2] > 150, d


def test_world_stroke_streams(red_brush):
    pts = [[-0.8 + 1.6 * i / 99, -0.8 + 1.6 * i / 99, 0.0] for i in range(100)]
    r = call("ap_paint_stroke_world", points=pts, capture={"downscale": 2})
    assert r["ok"], r
    assert r["result"]["streamed"] is True
    assert diff_of(r)["no_visible_change"] is False, diff_of(r)


def test_an_idle_stroke_is_closed_by_the_bridge(red_brush):
    assert call("ap_stroke_begin")["ok"]
    assert call("ap_stroke_points", points=[[0.45, 0.5], [0.5, 0.5]])["ok"]
    assert hb().get("stroke_open") is True
    time.sleep(6.5)  # STROKE_IDLE_S is 5
    assert hb().get("stroke_open") is False
    r = call("ap_stroke_points", points=[[0.5, 0.5], [0.55, 0.5]])
    assert r["ok"] is False and r["code"] == "no_stroke", r


def test_another_op_closes_an_open_stroke_but_a_capture_does_not(red_brush):
    assert call("ap_stroke_begin")["ok"]
    assert call("ap_stroke_points", points=[[0.40, 0.45], [0.45, 0.45]])["ok"]
    assert call("ap_capture_window", downscale=4)["ok"]  # settles with pings: stays open
    assert call("ap_stroke_points", points=[[0.45, 0.45], [0.5, 0.45]])["ok"]
    assert call("ap_select_tool", tool="brush")["ok"]  # any other op: closes it
    assert hb().get("stroke_open") is False
    assert call("ap_stroke_points", points=[[0.5, 0.45]])["code"] == "no_stroke"
    assert call("ap_stroke_end")["result"]["was_open"] is False


def test_manual_stroke_with_a_look_in_between(red_brush):
    assert call("ap_stroke_begin")["ok"]
    assert call("ap_stroke_points", points=[[0.40 + 0.005 * i, 0.55] for i in range(20)])["ok"]
    mid = call("ap_capture_window", downscale=2)
    assert mid["ok"]
    assert call("ap_stroke_points", points=[[0.50 + 0.005 * i, 0.55] for i in range(20)])["ok"]
    end = call("ap_stroke_end", capture={"downscale": 2, "diff": False})
    assert end["ok"] and end["result"]["was_open"] is True and end["result"]["total"] == 40
    after = call("ap_capture_window", downscale=2, diff_against=mid["capture_id"])
    assert after["diff"]["no_visible_change"] is False  # the second half landed after the look


def test_taper_makes_the_start_thicker_than_the_end(red_brush):
    r = call("ap_paint_stroke", points=[[0.35, 0.5], [0.65, 0.5]], spacing=0.004, taper="out",
             taper_min=0.1, capture={"downscale": 1})
    assert r["ok"], r
    d = diff_of(r)
    assert d["bbox"], d
    before, after = server._CAPTURES[d["against"]], server._CAPTURES[r["capture"]["capture_id"]]
    x, _, w, _ = d["bbox"]
    start = column_thickness(before, after, x + int(w * 0.05), x + int(w * 0.2))
    end = column_thickness(before, after, x + int(w * 0.8), x + int(w * 0.95))
    assert start > 1.5 * end, (start, end)


def test_pointer_stroke_paints_where_it_was_dragged(red_brush):
    if desktop_input.supported() is None:
        pytest.skip("no synthetic-input backend")
    # step_ms=20 is faster than a lavapipe frame (~60 ms): the tool must pace itself.
    r = call("ap_paint_stroke_pointer", points=[[650, 520], [900, 520]], spacing=10, step_ms=20,
             capture={"downscale": 1, "crop": [600, 470, 340, 100]})
    assert r["ok"], r
    assert r["step_ms_used"] >= r["frame_ms"]
    d = diff_of(r)
    assert d["no_visible_change"] is False, d
    bx, by, bw, bh = d["bbox"]  # in the crop: the stroke spans x 50..300, y 50
    assert bx <= 60 and bx + bw >= 290 and by <= 50 <= by + bh, d
    # continuous: most columns along the path changed (a broken stroke left gaps)
    before, after = server._CAPTURES[d["against"]], server._CAPTURES[r["capture"]["capture_id"]]
    covered = sum(1 for x in range(60, 290) if column_thickness(before, after, x, x + 1) > 0)
    assert covered > 0.9 * 230, covered


def test_recorded_stroke_returns_a_filmstrip(red_brush):
    pts = [[0.35 + 0.3 * i / 199, 0.45] for i in range(200)]
    r = call("ap_paint_stroke", points=pts, record={"downscale": 4})
    assert r["ok"], r
    assert r["record"]["frames"] == 5 and r["record"]["distinct_frames"] > 1, r["record"]
    assert r["_images"] == 1


def test_capture_sequence(red_brush):
    r = call("ap_capture_sequence", duration_s=1, fps=4, downscale=4)
    assert r["ok"] and r["frames"] == 4 and r["_images"] == 1
