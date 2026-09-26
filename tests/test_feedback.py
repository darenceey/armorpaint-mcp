"""Visual feedback: capture diff, look-in-the-same-call, timing (plan 2.1, 2.2, 2.4)."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from armorpaint_mcp import image_diff, server, window_capture  # noqa: E402
from armorpaint_mcp.window_capture import Capture  # noqa: E402


def canvas(w=40, h=30, fill=(50, 60, 70)):
    return bytearray(bytes(fill) * (w * h))


def paint(buf, w, x, y, rw, rh, color=(250, 10, 10)):
    for yy in range(y, y + rh):
        for xx in range(x, x + rw):
            i = (yy * w + xx) * 3
            buf[i : i + 3] = bytes(color)
    return buf


# ---------------------------------------------------------------------------
# 2.2 image diff
# ---------------------------------------------------------------------------


def test_identical_images_have_no_change():
    a = canvas()
    d = image_diff.diff(40, 30, bytes(a), bytes(a))
    assert d == {"changed_pixels": 0, "changed_fraction": 0.0, "bbox": None, "no_visible_change": True,
                 "noise_floor": image_diff.noise_floor(40, 30), "size": [40, 30]}


def test_changed_rectangle_bbox_and_count():
    a = canvas()
    b = paint(canvas(), 40, 10, 5, 3, 2)
    d = image_diff.diff(40, 30, bytes(a), bytes(b))
    assert d["bbox"] == [10, 5, 3, 2]
    assert d["changed_pixels"] == 6
    assert d["changed_fraction"] == pytest.approx(6 / 1200)
    assert d["no_visible_change"] is False


def test_noise_below_threshold_is_ignored():
    a = canvas()
    b = paint(canvas(), 40, 0, 0, 40, 30, color=(53, 58, 72))  # every pixel off by <= 3
    b = paint(b, 40, 39, 29, 1, 1)
    d = image_diff.diff(40, 30, bytes(a), bytes(b), threshold=8)
    assert d["changed_pixels"] == 1 and d["bbox"] == [39, 29, 1, 1]


def test_size_mismatch_is_an_error():
    with pytest.raises(ValueError, match="size"):
        image_diff.diff(40, 30, bytes(canvas()), bytes(canvas(41, 30)), w2=41, h2=30)


def test_highlight_crops_around_the_change_and_marks_it():
    a = canvas()
    b = paint(canvas(), 40, 10, 5, 3, 2)
    png = image_diff.highlight(40, 30, bytes(a), bytes(b), [10, 5, 3, 2], pad=4)
    w, h, rgb = window_capture._decode_png_rgb(png)
    assert (w, h) == (3 + 8, 2 + 8)  # padded by 4 on each side, inside the image
    # the changed pixel at (10,5) is at (4,4) of the crop and is marked (magenta tint)
    i = (4 * w + 4) * 3
    assert rgb[i] > 200 and rgb[i + 2] > 100
    # an unchanged pixel is dimmed, not marked
    assert rgb[0] < 50


def test_decoder_fast_path_for_unfiltered_rows():
    w, h = 300, 200
    rgb = bytes(range(256)) * (w * h * 3 // 256) + bytes(w * h * 3 % 256)
    rows = b"".join(b"\x00" + rgb[y * w * 3 : (y + 1) * w * 3] for y in range(h))
    assert window_capture._decode_png_rgb(window_capture._png(w, h, rows)) == (w, h, rgb)


# ---------------------------------------------------------------------------
# capture ring, diff_against, capture-in-the-same-call, timing
# ---------------------------------------------------------------------------


def fake_capture(rgb: bytes, w=40, h=30) -> Capture:
    rows = b"".join(b"\x00" + rgb[y * w * 3 : (y + 1) * w * 3] for y in range(h))
    return Capture(scan=rows, width=w, height=h, window_width=w,
                   window_height=h, window_id=1, window_title="t", method="fake", rgb=rgb)


@pytest.fixture
def frames(monkeypatch):
    """Queue of images the fake capture hands out; bridge calls recorded."""
    queue: list[bytes] = []
    sent: list[tuple[str, dict]] = []

    def cap(title=None, crop=None, downscale=1):
        return fake_capture(queue.pop(0))

    def send(op, wire, timeout=None):
        sent.append((op, wire))
        return {"elapsed_ms": 3}

    monkeypatch.setattr(server, "capture_window", cap)
    monkeypatch.setattr(server, "send_to_armorpaint", send)
    monkeypatch.setattr(server, "read_heartbeat", lambda *a, **k: {"app_title": "t"})
    # One capture per grab: the display settle has its own tests below.
    monkeypatch.setattr(server, "_settle_display", lambda grab, cap, baseline: (cap, {}))
    monkeypatch.setenv("ARMORPAINT_MCP_AUTOCHECKPOINT", "0")  # fill_layer would take one first
    server._CAPTURES.clear()
    return queue, sent


def run(tool, /, **args):
    content = asyncio.run(server.call_tool(tool, args))
    texts = [c for c in content if c.type == "text"]
    payload = json.loads(texts[-1].text)
    payload["_images"] = sum(1 for c in content if c.type == "image")
    return payload


def test_capture_ids_and_diff_against_last(frames):
    queue, _ = frames
    queue += [bytes(canvas()), bytes(paint(canvas(), 40, 1, 2, 2, 2))]
    first = run("ap_capture_window")
    assert first["capture_id"] and "diff" not in first
    second = run("ap_capture_window", diff_against="last")
    assert second["diff"]["bbox"] == [1, 2, 2, 2]
    assert second["diff"]["against"] == first["capture_id"]
    assert second["_images"] == 2  # the capture and the highlighted change


def test_diff_against_unknown_capture(frames):
    queue, _ = frames
    queue.append(bytes(canvas()))
    r = run("ap_capture_window", diff_against="c999")
    assert r["ok"] is True and r["diff"]["error"].startswith("no capture")


def test_fill_with_capture_returns_the_result_and_flags_a_no_op(frames):
    queue, sent = frames
    queue += [bytes(canvas()), bytes(canvas())]  # before == after
    r = run("ap_fill_layer", capture={})
    assert r["ok"] and r["_images"] == 1
    assert r["capture"]["diff"]["no_visible_change"] is True
    assert "warning" in r["capture"]
    assert [op for op, _ in sent if op != "ping"] == ["fill_layer"]


def test_stroke_with_capture_reports_the_change(frames):
    queue, _ = frames
    queue += [bytes(canvas()), bytes(paint(canvas(), 40, 5, 5, 4, 1))]
    r = run("ap_paint_stroke", points=[[0.1, 0.1], [0.2, 0.1]], capture={"downscale": 1})
    assert r["capture"]["diff"]["bbox"] == [5, 5, 4, 1]
    assert r["_images"] == 2


def test_capture_without_diff_takes_one_frame(frames):
    queue, _ = frames
    queue.append(bytes(canvas()))
    r = run("ap_fill_layer", capture={"diff": False})
    assert r["_images"] == 1 and "diff" not in r["capture"] and queue == []


def test_replies_carry_timing(frames):
    r = run("ap_ping")
    assert r["timing"]["handler_ms"] == 3
    assert r["timing"]["total_ms"] >= 0 and "round_trip_ms" in r["timing"]


def test_capture_argument_is_in_each_capture_tools_schema():
    schemas = {t.name: t.inputSchema for t in server.TOOLS}
    for name in server.CAPTURE_TOOLS:
        assert "capture" in schemas[name]["properties"], name
    assert "capture" not in schemas["ap_ping"]["properties"]
    assert "diff_against" in schemas["ap_capture_window"]["properties"]


def test_settle_waits_three_frames_by_default(frames):
    """Measured live: a fill's result is complete on the 3rd frame after it (440 -> 54772
    -> 55011 changed pixels after 1, 2, 3 frame-pings). One ping was not enough."""
    queue, sent = frames
    queue += [bytes(canvas()), bytes(canvas())]
    run("ap_capture_window")
    assert [op for op, _ in sent].count("ping") == 3
    sent.clear()
    run("ap_capture_window", settle_frames=1)
    assert [op for op, _ in sent].count("ping") == 1


def test_noise_floor_keeps_the_brush_cursor_from_counting_as_a_change():
    """Measured live: an opacity-0 stroke moves the brush cursor ring, 18-74 changed pixels
    in a 6x39 box. Below the floor the change is reported but flagged as no visible change."""
    a = canvas(400, 300)
    # 20 pixels of 120 000 -- the ring's live proportion (74 of 412 800, 0.018 %)
    b = paint(canvas(400, 300), 400, 100, 100, 2, 10)
    d = image_diff.diff(400, 300, bytes(a), bytes(b))
    assert d["changed_pixels"] == 20 and d["bbox"] == [100, 100, 2, 10]
    assert d["noise_floor"] == image_diff.noise_floor(400, 300)
    assert d["no_visible_change"] is True
    big = paint(canvas(400, 300), 400, 100, 100, 30, 30)
    assert image_diff.diff(400, 300, bytes(a), bytes(big))["no_visible_change"] is False


# ---------------------------------------------------------------------------
# display settle: rendered is not yet visible (real-hardware pass, KWin/XWayland)
# ---------------------------------------------------------------------------


@pytest.fixture
def fast_settle(monkeypatch):
    monkeypatch.setattr(server, "SETTLE_POLL_S", 0.0)
    monkeypatch.setattr(server, "SETTLE_QUIET_S", 0.05)
    monkeypatch.setattr(server, "SETTLE_LAG_S", 0.2)
    monkeypatch.setattr(server, "SETTLE_CHANGE_WAIT_S", 0.3)
    monkeypatch.setattr(server, "SETTLE_MAX_S", 1.0)


def timed_window(*phases):
    """A fake window whose image is phases[i][1] from phases[i][0] seconds on."""
    import time
    t0 = time.monotonic()

    def grab():
        now = time.monotonic() - t0
        img = phases[0][1]
        for at, rgb in phases:
            if now >= at:
                img = rgb
        return fake_capture(img)
    return grab


def test_settle_waits_for_an_edit_to_reach_the_window(fast_settle):
    """Measured live: an edit reached the window 100-550 ms after the bridge replied, so a
    capture taken at once showed the old frame. With a before-image the settle waits for
    the change, then for the window to stop changing."""
    old, new = bytes(canvas()), bytes(paint(canvas(), 40, 5, 5, 6, 6))
    grab = timed_window((0.0, old), (0.12, new))
    cap, info = server._settle_display(grab, grab(), fake_capture(old))
    assert cap.pixels() == new
    assert info["changed"] is True and info["stable"] is True and info["settled_ms"] >= 120


def test_settle_reports_a_no_op_after_the_change_wait(fast_settle):
    old = bytes(canvas())
    grab = timed_window((0.0, old))
    cap, info = server._settle_display(grab, grab(), fake_capture(old))
    assert info["changed"] is False and info["stable"] is True
    assert 300 <= info["settled_ms"] < 1000


def test_settle_without_a_before_image_waits_out_the_lag_after_an_edit(fast_settle):
    """Two equal captures prove nothing: two stale ones match too. Without a before-image
    the settle waits SETTLE_LAG_S after the last edit."""
    old, new = bytes(canvas()), bytes(paint(canvas(), 40, 1, 1, 8, 8))
    server.note_edit()
    grab = timed_window((0.0, old), (0.1, new))
    cap, info = server._settle_display(grab, grab(), None)
    assert cap.pixels() == new and info["stable"] is True and "changed" not in info


def test_settle_without_a_recent_edit_only_waits_for_quiet(fast_settle, monkeypatch):
    monkeypatch.setattr(server, "seconds_since_edit", lambda: None)
    grab = timed_window((0.0, bytes(canvas())))
    _, info = server._settle_display(grab, grab(), None)
    assert info["stable"] is True and info["settled_ms"] < 200


def test_settle_gives_up_on_a_window_that_keeps_changing(fast_settle, monkeypatch):
    monkeypatch.setattr(server, "seconds_since_edit", lambda: None)
    n = [0]

    def grab():
        n[0] += 1
        return fake_capture(bytes(paint(canvas(), 40, n[0] % 30, 0, 10, 10)))
    _, info = server._settle_display(grab, grab(), None)
    assert info["stable"] is False and info["settled_ms"] >= 1000


def test_capture_encodes_its_png_only_when_asked():
    rgb = bytes(paint(canvas(), 40, 3, 3, 2, 2))
    cap = fake_capture(rgb)
    cap.encoded = None
    assert cap.pixels() == rgb and cap.encoded is None
    assert window_capture._decode_png_rgb(cap.png) == (40, 30, rgb)


def test_failed_calls_are_flagged_as_mcp_errors(frames):
    """A reply that says ok: false reaches the client with isError set."""
    ok = asyncio.run(server._mcp_call_tool("ap_ping", {}))
    assert ok.isError is False
    bad = asyncio.run(server._mcp_call_tool("ap_node_set_value", {"id": 1, "kind": "color"}))
    assert bad.isError is True and json.loads(bad.content[-1].text)["code"] == "bad_args"
    assert asyncio.run(server._mcp_call_tool("ap_nope", {})).isError is True


def test_app_info_reports_the_viewport_rect_not_a_window_size(monkeypatch):
    """Read live: the bridge's window_w/h/x/y (sys_w/h/x/y) were 893x738 at 36,60 in a
    1209x831 window -- the 3D viewport's rectangle."""
    monkeypatch.setattr(server, "send_to_armorpaint", lambda op, wire, timeout=None: {
        "app_title": "t", "window_w": 893, "window_h": 738, "window_x": 36, "window_y": 60, "elapsed_ms": 1})
    r = run("ap_get_app_info")
    assert r["result"]["viewport_rect"] == [36, 60, 893, 738]
    assert not any(k.startswith("window_") for k in r["result"])
