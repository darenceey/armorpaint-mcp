"""Live tests for visual feedback (plan 2.1, 2.2, 2.4). ARMORPAINT_LIVE=1 to run."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from armorpaint_mcp import server  # noqa: E402

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
    assert call("ap_project_new")["ok"]


def red_material():
    r = call("ap_node_graph_apply", fill=False, spec={
        "nodes": {"c": {"existing": "RGB", "outputs": {"Color": [0.9, 0.05, 0.05, 1]}}},
    })
    assert r["ok"], r


def test_fill_with_capture_sees_the_change(fresh):
    red_material()
    r = call("ap_fill_layer", capture={"downscale": 2})
    assert r["ok"], r
    d = r["capture"]["diff"]
    assert d["no_visible_change"] is False and d["changed_fraction"] > 0.01, d
    assert r["_images"] == 2


def test_invisible_stroke_is_flagged_as_no_visible_change(fresh):
    call("ap_fill_layer")
    call("ap_select_tool", tool="brush")
    call("ap_set_brush", radius=0.5, opacity=0.0)
    try:
        r = call("ap_paint_stroke", points=[[0.45, 0.5], [0.55, 0.5]], capture={"downscale": 2})
        assert r["ok"], r
        assert r["capture"]["diff"]["no_visible_change"] is True, r["capture"]["diff"]
        assert "no-op" in r["capture"]["warning"]
    finally:
        call("ap_set_brush", opacity=1.0)


def test_capture_diff_against_last(fresh):
    a = call("ap_capture_window", downscale=2)
    red_material()
    call("ap_fill_layer")
    b = call("ap_capture_window", downscale=2, diff_against="last")
    assert b["diff"]["against"] == a["capture_id"]
    assert b["diff"]["bbox"] is not None and b["_images"] == 2


def test_timing_is_reported():
    r = call("ap_ping")
    t = r["timing"]
    assert isinstance(t["handler_ms"], (int, float)) and t["round_trip_ms"] >= t["handler_ms"]
