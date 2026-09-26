"""Stroke kinematics: shaping, streaming, pressure, pointer strokes, filmstrip
(plan 1.1-1.4, 2.3). Offline: pure geometry, request plans, static plugin checks."""

from __future__ import annotations

import asyncio
import json
import math
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from armorpaint_mcp import desktop_input, image_diff, server, strokes, window_capture  # noqa: E402
from armorpaint_mcp.transport import BadArgs, OpFailed  # noqa: E402

PLUGIN = (ROOT / "plugin" / "armorpaint_mcp_bridge.c").read_text(encoding="utf-8")


def dist(a, b):
    return math.dist(a[:2], b[:2])


# ---------------------------------------------------------------------------
# 1.3 shaping (T1 is enough: pure geometry)
# ---------------------------------------------------------------------------


def test_catmull_rom_passes_through_the_control_points():
    ctrl = [(0.1, 0.1), (0.3, 0.4), (0.6, 0.2), (0.9, 0.5)]
    curve = strokes.catmull_rom(ctrl, samples=6)
    for p in ctrl:
        assert min(dist(p, q) for q in curve) < 1e-9
    assert curve[0] == pytest.approx(ctrl[0]) and curve[-1] == pytest.approx(ctrl[-1])
    assert len(curve) == 3 * 6 + 1


def test_catmull_rom_works_in_3d():
    ctrl = [(0, 0, 0), (1, 0, 1), (2, 1, 1)]
    curve = strokes.catmull_rom(ctrl, samples=4)
    assert all(len(p) == 3 for p in curve) and curve[-1] == pytest.approx((2, 1, 1))


def test_resample_spacing_is_even_and_keeps_the_ends():
    line = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0)]
    pts = strokes.resample(line, 0.1)
    steps = [dist(a, b) for a, b in zip(pts, pts[1:])]
    assert pts[0] == pytest.approx((0, 0)) and pts[-1] == pytest.approx((1, 1))
    assert all(s == pytest.approx(0.1, abs=1e-9) for s in steps[:-1])
    assert steps[-1] <= 0.1 + 1e-9
    assert len(pts) == 21


@pytest.mark.parametrize(
    "kind, first, last",
    [("none", 1.0, 1.0), ("in", 0.15, 1.0), ("out", 1.0, 0.15), ("both", 0.15, 0.15)],
)
def test_taper_profiles(kind, first, last):
    t = strokes.taper(11, kind, minimum=0.15)
    assert len(t) == 11 and t[0] == pytest.approx(first) and t[-1] == pytest.approx(last)
    assert max(t) == pytest.approx(1.0) and min(t) >= 0.15 - 1e-9
    if kind == "both":
        assert t[5] == pytest.approx(1.0)


def test_jitter_is_seeded_and_bounded():
    line = strokes.resample([(0.2, 0.5), (0.8, 0.5)], 0.05)
    a = strokes.jitter(line, 0.01, seed=7)
    assert a == strokes.jitter(line, 0.01, seed=7)
    assert a != strokes.jitter(line, 0.01, seed=8)
    assert all(abs(p[1] - 0.5) <= 0.01 + 1e-12 and p[0] == q[0] for p, q in zip(a, line))  # perpendicular


def test_shape_appends_pressure_only_when_tapered():
    plain = strokes.shape([(0.2, 0.5), (0.8, 0.5)], dims=2)
    assert all(len(p) == 2 for p in plain)
    tapered = strokes.shape([(0.2, 0.5), (0.8, 0.5)], dims=2, spacing=0.1, taper="out")
    assert all(len(p) == 3 for p in tapered)
    assert tapered[0][2] == pytest.approx(1.0) and tapered[-1][2] == pytest.approx(0.15)


def test_shape_keeps_explicit_pressure_values():
    pts = strokes.shape([(0.2, 0.5, 1.0, 0.5), (0.8, 0.5, 0.3, 1.0)], dims=2)
    assert pts == [(0.2, 0.5, 1.0, 0.5), (0.8, 0.5, 0.3, 1.0)]


@pytest.mark.parametrize(
    "gen, check",
    [
        ({"kind": "scratch", "start": [0.3, 0.4], "end": [0.7, 0.45], "seed": 3},
         lambda s: len(s) == 1 and s[0][0] == pytest.approx((0.3, 0.4)) and s[0][-1] == pytest.approx((0.7, 0.45))),
        ({"kind": "zigzag", "start": [0.2, 0.5], "end": [0.8, 0.5], "teeth": 5, "amplitude": 0.05},
         lambda s: len(s) == 1 and len(s[0]) == 11 and max(p[1] for p in s[0]) == pytest.approx(0.55)),
        ({"kind": "spiral", "center": [0.5, 0.5], "radius_start": 0.0, "radius_end": 0.1, "turns": 2},
         lambda s: len(s) == 1 and max(dist(p, (0.5, 0.5)) for p in s[0]) == pytest.approx(0.1, abs=1e-6)),
        ({"kind": "dabs", "center": [0.5, 0.5], "radius": 0.1, "count": 12, "seed": 1},
         lambda s: len(s) == 12 and all(len(d) == 1 and dist(d[0], (0.5, 0.5)) <= 0.1 for d in s)),
    ],
)
def test_generators(gen, check):
    assert check(strokes.generate(gen))


def test_generator_errors():
    with pytest.raises(BadArgs, match="kind"):
        strokes.generate({"kind": "flower"})
    with pytest.raises(BadArgs, match="count"):
        strokes.generate({"kind": "dabs", "center": [0.5, 0.5], "radius": 0.1, "count": 10_000})


# ---------------------------------------------------------------------------
# 1.1 / 1.2 request plans
# ---------------------------------------------------------------------------


def test_chunk_limits_follow_the_value_budget():
    assert strokes.chunk_size(2) == 48  # 96 values
    assert strokes.chunk_size(3) == 48  # 144 values
    assert strokes.chunk_size(4) == 37  # 148 values
    assert strokes.chunk_size(5) == 30  # 150 values


def test_short_stroke_is_one_request():
    plan = strokes.plan([(0.1 * i, 0.5) for i in range(10)], world=False)
    assert [op for op, _ in plan] == ["paint_stroke"]
    assert plan[0][1]["points"].count(";") == 9


def test_200_points_stream_as_begin_five_chunks_end():
    pts = [(i / 200, 0.5) for i in range(200)]
    plan = strokes.plan(pts, world=False)
    ops = [op for op, _ in plan]
    assert ops == ["stroke_begin"] + ["stroke_points"] * 5 + ["stroke_end"]
    sizes = [w["points"].count(";") + 1 for op, w in plan if op == "stroke_points"]
    assert sizes == [48, 48, 48, 48, 8]
    assert plan[0][1] == {"world": False}


def test_pressure_wire_format():
    plan = strokes.plan([(0.1, 0.2, 0.5), (0.3, 0.4, 0.25, 0.75)], world=False)
    assert plan == [("paint_stroke", {"points": "0.1,0.2,0.5;0.3,0.4,0.25,0.75"})]


@pytest.fixture
def sent(monkeypatch):
    log: list[tuple[str, dict]] = []
    fail_at: dict[str, int] = {}

    def send(op, wire, timeout=None):
        log.append((op, wire))
        if fail_at.get(op) == sum(1 for o, _ in log if o == op):
            raise OpFailed(op, "internal", "injected")
        return {"points": wire.get("points", "").count(";") + 1 if "points" in wire else 0}

    monkeypatch.setattr(server, "send_to_armorpaint", send)
    monkeypatch.setattr(server, "read_heartbeat", lambda *a, **k: None)
    return log, fail_at


def run(tool, /, **args):
    content = asyncio.run(server.call_tool(tool, args))
    texts = [c for c in content if c.type == "text"]
    return json.loads(texts[-1].text)


def test_long_stroke_tool_streams(sent):
    log, _ = sent
    r = run("ap_paint_stroke", points=[[i / 200, 0.5] for i in range(200)])
    assert r["ok"], r
    assert [op for op, _ in log] == ["stroke_begin"] + ["stroke_points"] * 5 + ["stroke_end"]
    assert r["result"]["points"] == 200 and r["result"]["requests"] == 7


def test_a_failed_chunk_still_closes_the_stroke(sent):
    log, fail_at = sent
    fail_at["stroke_points"] = 2
    r = run("ap_paint_stroke", points=[[i / 200, 0.5] for i in range(200)])
    assert r["ok"] is False
    assert [op for op, _ in log][-1] == "stroke_end"
    assert r["painted_points"] == 48


def test_taper_reaches_the_wire(sent):
    log, _ = sent
    run("ap_paint_stroke", points=[[0.2, 0.5], [0.8, 0.5]], spacing=0.1, taper="both")
    wire = log[0][1]["points"].split(";")
    assert len(wire) == 7 and all(len(p.split(",")) == 3 for p in wire)


def test_generated_dabs_are_separate_strokes(sent):
    log, _ = sent
    r = run("ap_paint_stroke", generate={"kind": "dabs", "center": [0.5, 0.5], "radius": 0.1, "count": 5, "seed": 2})
    assert r["ok"] and [op for op, _ in log] == ["paint_stroke"] * 5


def test_stroke_tools_for_manual_streaming(sent):
    log, _ = sent
    assert run("ap_stroke_begin", world=True)["ok"]
    assert run("ap_stroke_points", points=[[0, 0, 1], [0.1, 0, 1]])["ok"]
    assert run("ap_stroke_end")["ok"]
    assert [op for op, _ in log] == ["stroke_begin", "stroke_points", "stroke_end"]
    assert log[1][1]["points"] == "0,0,1;0.1,0,1"


def test_batch_step_stroke_must_fit_one_request():
    with pytest.raises(BadArgs, match="batch"):
        server._build_wire_args("ap_paint_stroke", {"points": [[i / 100, 0.5] for i in range(100)]})
    assert server._build_wire_args("ap_paint_stroke", {"points": [[0.1, 0.5], [0.2, 0.5]]}) == {"points": "0.1,0.5;0.2,0.5"}


# ---------------------------------------------------------------------------
# 1.4 pointer strokes
# ---------------------------------------------------------------------------


class RecordingSession:
    def __init__(self):
        self.events = []

    def send(self, etype, x, y, state=0, detail=0):
        self.events.append((etype, x, y, detail))

    def close(self):
        pass


def test_pointer_stroke_event_sequence(monkeypatch):
    rec = RecordingSession()
    monkeypatch.setattr(desktop_input, "_session", lambda title: rec)
    monkeypatch.setattr(desktop_input, "supported", lambda: "x11")
    monkeypatch.setattr(desktop_input.time, "sleep", lambda s: None)
    monkeypatch.setattr(server, "read_heartbeat", lambda *a, **k: {"app_title": "t"})
    monkeypatch.setattr(server, "_frame_fence", lambda: None)
    monkeypatch.setattr(server, "_frame_interval_ms", lambda pings=3: None)  # no bridge offline
    r = run("ap_paint_stroke_pointer", points=[[100, 200], [300, 200]], spacing=50, step_ms=5)
    assert r["ok"], r
    kinds = [e[0] for e in rec.events]
    press = kinds.index(desktop_input.BUTTON_PRESS)
    assert kinds[-1] == desktop_input.BUTTON_RELEASE
    moves = [e for e in rec.events[press + 1 : -1] if e[0] == desktop_input.MOTION_NOTIFY]
    assert [(e[1], e[2]) for e in moves] == [(150, 200), (200, 200), (250, 200), (300, 200)]
    assert r["dragged"] == 5


def test_pointer_stroke_rejects_normalised_looking_points():
    with pytest.raises(BadArgs, match="window pixels"):
        server._pointer_points({"points": [[0.2, 0.5], [0.8, 0.5]]})


# ---------------------------------------------------------------------------
# 2.3 filmstrip
# ---------------------------------------------------------------------------


def test_contact_sheet_layout():
    frames = [(4, 3, bytes([i * 40] * 36)) for i in range(5)]
    png = image_diff.contact_sheet(frames, columns=3, gap=2)
    w, h, rgb = window_capture._decode_png_rgb(png)
    assert (w, h) == (3 * 4 + 2 * 2, 2 * 3 + 2)
    assert rgb[0] == 0 and rgb[(0 * w + 6) * 3] == 40  # second tile starts after one gap


def test_capture_sequence_limits():
    with pytest.raises(BadArgs):
        server._sequence_args({"duration_s": 60})
    with pytest.raises(BadArgs):
        server._sequence_args({"duration_s": 2, "fps": 30})
    assert server._sequence_args({"duration_s": 2, "fps": 5}) == (2.0, 5)


# ---------------------------------------------------------------------------
# T2: the plugin side, statically
# ---------------------------------------------------------------------------


def _code():
    code = re.sub(r"//[^\n]*", "", PLUGIN)
    return re.sub(r'"(?:\\.|[^"\\])*"', '""', code)


def test_plugin_has_the_stroke_arms_and_a_three_argument_do_stroke():
    for op in ("stroke_begin", "stroke_points", "stroke_end"):
        assert f'string_equals(op, "{op}")' in PLUGIN, op
    assert re.search(r"int do_stroke\(char \*pts, int is_world, int close\)", PLUGIN)


def test_stroke_limits_match_the_server():
    assert int(re.search(r"int MAX_STROKE_POINTS\s*=\s*(\d+);", PLUGIN).group(1)) == server.MAX_STROKE_POINTS == strokes.MAX_POINTS
    assert int(re.search(r"int MAX_STROKE_VALUES\s*=\s*(\d+);", PLUGIN).group(1)) == strokes.MAX_VALUES


def test_plugin_global_budget():
    """Script globals live in minic's top-level env, capped at MINIC_MAX_VARS = 128
    (minic.c:1980 declares them there, :2092 sets var_cap); a 129th is a load error.
    (The 64 in minic.h, MINIC_MAX_GLOBALS, is the cap on HOST globals, not these.)"""
    code = _code()
    depth, globals_ = 0, 0
    for line in code.split("\n"):
        if depth == 0 and re.match(r"^[A-Za-z_][\w\s\*]*\s\**\w+\s*(=[^;]*)?;\s*$", line.strip()):
            globals_ += 1
        depth += line.count("{") - line.count("}")
    assert globals_ <= 128, globals_


def test_stroke_end_restores_the_brush():
    arm = PLUGIN[PLUGIN.index('string_equals(op, "stroke_end")') :]
    arm = arm[: arm.index("else if (string_equals(op,")]
    assert re.search(r"brush_radius\s*=\s*stroke_r0", arm) and re.search(r"brush_opacity\s*=\s*stroke_o0", arm)


def test_pointer_step_is_paced_to_the_frame_rate():
    """Measured live (lavapipe, ~16 fps): events every 20 ms broke the stroke into pieces;
    at >= one frame per event it was continuous. So the step is at least 1.25 frames."""
    assert server._pointer_step(20, 60.0) == 75
    assert server._pointer_step(100, 60.0) == 100
    assert server._pointer_step(20, None) == 20


def test_frame_interval_is_measured_with_pings(monkeypatch):
    clock = iter([0.0, 0.18])
    monkeypatch.setattr(server.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(server, "send_to_armorpaint", lambda op, wire, t=None: {})
    assert server._frame_interval_ms(3) == pytest.approx(60.0)
