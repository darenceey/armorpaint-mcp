"""Stroke shaping and streaming.

An agent describes a stroke with a handful of points; this module turns that into what
ArmorPaint's ``script_paint`` needs to look hand-made: a smooth curve through the points
(Catmull-Rom), evenly spaced dabs, a pressure-like taper, a little seeded wobble, and a few
generators (scratch, zigzag, spiral, scattered dabs).

It also plans the requests. The bridge paints a request's points inside one frame, under
a budget of script calls per frame, so a long stroke is split: ``stroke_begin``, then
``stroke_points`` chunks (each its own frame), then ``stroke_end``. ArmorPaint keeps the
stroke open between them -- upstream's ``script_paint_begin_stroke`` runs once per stroke
and ``script_paint_end`` is what closes it -- so the chunks join into one stroke.

Points carry optional pressure: ``x, y[, radius_mult[, opacity_mult]]`` in screen space,
``x, y, z[, radius_mult[, opacity_mult]]`` in world space. The bridge multiplies the
brush radius/opacity by them per dab and restores both when the stroke ends.
"""

from __future__ import annotations

import math
import random
from typing import Any, Sequence

from .transport import BadArgs

# MUST equal MAX_STROKE_POINTS / MAX_STROKE_VALUES in plugin/armorpaint_mcp_bridge.c.
# Every number in a request's point list costs the plugin one to_float script call, and a
# frame has room for roughly 280 before minic's 8 MB arena overflows
# (docs/MINIC_DIALECT_AND_API.md 1.11a); 150 values leaves the rest of the frame headroom.
MAX_POINTS = 48
MAX_VALUES = 150
MAX_TOTAL_POINTS = 4096
MAX_DABS = 256

Point = tuple[float, ...]


def _lerp(a: Sequence[float], b: Sequence[float], t: float) -> Point:
    return tuple(x + (y - x) * t for x, y in zip(a, b))


def catmull_rom(points: Sequence[Sequence[float]], samples: int = 8) -> list[Point]:
    """A uniform Catmull-Rom curve through every point (any dimension): ``samples`` points
    per segment, ending exactly on the last control point."""
    pts = [tuple(float(v) for v in p) for p in points]
    if len(pts) < 3 or samples < 2:
        return pts
    ext = [_lerp(pts[0], pts[1], -1.0)] + pts + [_lerp(pts[-1], pts[-2], -1.0)]
    out: list[Point] = []
    for i in range(1, len(ext) - 2):
        p0, p1, p2, p3 = ext[i - 1], ext[i], ext[i + 1], ext[i + 2]
        for k in range(samples):
            t = k / samples
            t2, t3 = t * t, t * t * t
            out.append(tuple(
                0.5 * (2 * b + (-a + c) * t + (2 * a - 5 * b + 4 * c - d) * t2 + (-a + 3 * b - 3 * c + d) * t3)
                for a, b, c, d in zip(p0, p1, p2, p3)
            ))
    out.append(pts[-1])
    return out


def resample(points: Sequence[Sequence[float]], spacing: float) -> list[Point]:
    """Points every ``spacing`` along the polyline (by arc length), plus the last point."""
    pts = [tuple(float(v) for v in p) for p in points]
    if len(pts) < 2 or spacing <= 0:
        return pts
    out = [pts[0]]
    carry = 0.0  # distance already travelled since the last emitted point
    for a, b in zip(pts, pts[1:]):
        seg = math.dist(a, b)
        if seg == 0:
            continue
        d = spacing - carry
        while d <= seg + 1e-12:
            out.append(_lerp(a, b, d / seg))
            d += spacing
        carry = seg - (d - spacing)
    if math.dist(out[-1], pts[-1]) > 1e-9:
        out.append(pts[-1])
    return out


def taper(n: int, kind: str = "none", minimum: float = 0.15) -> list[float]:
    """Radius multipliers along a stroke: 'none', 'in' (thin start), 'out' (thin end),
    'both'. Smoothstep ramps, never below ``minimum`` so every dab still paints."""
    if kind not in ("none", "in", "out", "both"):
        raise BadArgs("'taper' must be none, in, out or both.", arg="taper")
    if n <= 1 or kind == "none":
        return [1.0] * n

    def ramp(t: float) -> float:
        t = max(0.0, min(1.0, t))
        return minimum + (1.0 - minimum) * t * t * (3 - 2 * t)

    out = []
    for i in range(n):
        u = i / (n - 1)
        if kind == "in":
            out.append(ramp(u))
        elif kind == "out":
            out.append(ramp(1 - u))
        else:
            out.append(ramp(min(u, 1 - u) * 2))
    return out


def jitter(points: Sequence[Sequence[float]], amount: float, seed: int = 0) -> list[Point]:
    """Seeded wobble. In 2D the offset is perpendicular to the stroke, so it wiggles
    without bunching dabs up; in 3D it is a small random offset."""
    pts = [tuple(float(v) for v in p) for p in points]
    if amount <= 0 or len(pts) < 2:
        return pts
    rng = random.Random(seed)
    out = []
    for i, p in enumerate(pts):
        if len(p) == 2:
            a, b = pts[max(0, i - 1)], pts[min(len(pts) - 1, i + 1)]
            dx, dy = b[0] - a[0], b[1] - a[1]
            norm = math.hypot(dx, dy) or 1.0
            off = rng.uniform(-amount, amount)
            out.append((p[0] - dy / norm * off, p[1] + dx / norm * off))
        else:
            out.append(tuple(v + rng.uniform(-amount, amount) for v in p))
    return out


_taper_profile = taper
_jitter_points = jitter


def shape(
    points: Sequence[Sequence[float]], *, dims: int, smooth: bool = False, spacing: float | None = None,
    taper: str = "none", taper_min: float = 0.15, jitter: float = 0.0, seed: int = 0,
) -> list[Point]:
    """Apply the shaping options. Points that already carry pressure values keep them
    when no geometry option is given; smoothing/spacing/jitter act on positions only."""
    pts = [tuple(float(v) for v in p) for p in points]
    geometry = smooth or spacing or jitter
    if not geometry and taper == "none":
        return pts
    base = [p[:dims] for p in pts]
    if smooth:
        base = catmull_rom(base)
    if spacing:
        base = resample(base, spacing)
    if jitter:
        base = _jitter_points(base, jitter, seed)
    radii = _taper_profile(len(base), taper, taper_min)
    if taper == "none":
        return base
    return [p + (r,) for p, r in zip(base, radii)]


def _pair(g: dict[str, Any], key: str) -> tuple[float, float]:
    v = g.get(key)
    if not (isinstance(v, (list, tuple)) and len(v) == 2 and all(isinstance(x, (int, float)) for x in v)):
        raise BadArgs(f"generate.{key} must be [x, y].", arg="generate")
    return float(v[0]), float(v[1])


def _num(g: dict[str, Any], key: str, default: float | None = None) -> float:
    v = g.get(key, default)
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        raise BadArgs(f"generate.{key} must be a number.", arg="generate")
    return float(v)


def generate(g: dict[str, Any]) -> list[list[Point]]:
    """Screen-space path generators. Returns a list of strokes (dabs: one per dab)."""
    kind = g.get("kind")
    seed = int(g.get("seed", 0))
    if kind == "scratch":
        a, b = _pair(g, "start"), _pair(g, "end")
        segments = int(_num(g, "segments", 12))
        wobble = _num(g, "wobble", 0.004)
        line = [_lerp(a, b, i / segments) for i in range(segments + 1)]
        inner = jitter(line, wobble, seed)
        path = [line[0]] + inner[1:-1] + [line[-1]]
        return [catmull_rom(path, 4)]
    if kind == "zigzag":
        a, b = _pair(g, "start"), _pair(g, "end")
        teeth = int(_num(g, "teeth", 6))
        amp = _num(g, "amplitude", 0.02)
        dx, dy = b[0] - a[0], b[1] - a[1]
        norm = math.hypot(dx, dy) or 1.0
        nx, ny = -dy / norm, dx / norm
        n = teeth * 2
        pts = []
        for i in range(n + 1):
            p = _lerp(a, b, i / n)
            side = 0.0 if i in (0, n) else (amp if i % 2 else -amp)
            pts.append((p[0] + nx * side, p[1] + ny * side))
        return [pts]
    if kind == "spiral":
        c = _pair(g, "center")
        r0, r1 = _num(g, "radius_start", 0.0), _num(g, "radius_end", 0.1)
        turns = _num(g, "turns", 3)
        samples = int(_num(g, "samples", max(16, int(32 * turns))))
        pts = []
        for i in range(samples + 1):
            t = i / samples
            ang = t * turns * 2 * math.pi
            r = r0 + (r1 - r0) * t
            pts.append((c[0] + r * math.cos(ang), c[1] + r * math.sin(ang)))
        return [pts]
    if kind == "dabs":
        c = _pair(g, "center")
        radius = _num(g, "radius", 0.05)
        count = int(_num(g, "count", 20))
        if not 1 <= count <= MAX_DABS:
            raise BadArgs(f"generate.count must be 1..{MAX_DABS}.", arg="generate")
        rng = random.Random(seed)
        out = []
        for _ in range(count):
            r = radius * math.sqrt(rng.random())
            ang = rng.uniform(0, 2 * math.pi)
            out.append([(c[0] + r * math.cos(ang), c[1] + r * math.sin(ang))])
        return out
    raise BadArgs("generate.kind must be scratch, zigzag, spiral or dabs.", arg="generate")


def chunk_size(values_per_point: int) -> int:
    return max(1, min(MAX_POINTS, MAX_VALUES // values_per_point))


def _fmt(v: float) -> str:
    text = repr(float(v))
    return text[:-2] if text.endswith(".0") else text


def encode(points: Sequence[Sequence[float]]) -> str:
    return ";".join(",".join(_fmt(v) for v in p) for p in points)


def plan(points: Sequence[Sequence[float]], *, world: bool) -> list[tuple[str, dict[str, Any]]]:
    """The bridge requests for one stroke: a single paint request when it fits one frame,
    else stroke_begin, stroke_points chunks and stroke_end."""
    widest = max(len(p) for p in points)
    size = chunk_size(widest)
    if len(points) <= size:
        return [("paint_stroke_world" if world else "paint_stroke", {"points": encode(points)})]
    ops: list[tuple[str, dict[str, Any]]] = [("stroke_begin", {"world": world})]
    for i in range(0, len(points), size):
        ops.append(("stroke_points", {"points": encode(points[i : i + size])}))
    ops.append(("stroke_end", {}))
    return ops
