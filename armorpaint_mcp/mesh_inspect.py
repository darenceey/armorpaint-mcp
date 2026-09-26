"""Mesh and UV diagnostics from an OBJ export, the UV layout as an image, and the mapping
from a UV coordinate to a point on the model.

ArmorPaint paints into textures laid out by the mesh's UVs, so UV problems -- overlaps,
flipped or collapsed triangles, wildly uneven texel density -- show up as paint landing
in two places, smeared, or blurry. A plugin cannot read the mesh arrays in bulk (each
element would cost a script call), but it can export the mesh: ``script_export_mesh``
writes an OBJ, which this module reads.

Conventions, from ArmorPaint 1.0's exporter (``paint/sources/io/export_obj.c``):

* OBJ positions are ``(x, z, -y) * scale_pos`` of ArmorPaint's Z-up mesh data, so
  ArmorPaint local = ``(obj.x, -obj.z, obj.y)``.
* OBJ texture coordinates are ``(u, 1 - v)`` of ArmorPaint's. Everything this module
  hands out uses IMAGE convention -- u to the right, v DOWN, (0, 0) the top-left corner of
  an exported texture -- which is ArmorPaint's own and matches the layout image.

Pure Python: meshes are rasterised onto a coarse grid, which is plenty for percentages.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any, Sequence

from .window_capture import _png

LAYOUT_BACKGROUND = (32, 32, 32)
LAYOUT_FILL = (70, 110, 160)
LAYOUT_EDGE = (150, 185, 225)
LAYOUT_OVERLAP = (225, 40, 40)

OVERLAP_WARN_PERCENT = 1.0
DENSITY_WARN_RATIO = 2.0
ISLAND_MIN_AREA_SHARE = 0.01  # islands below this share of the surface don't count for density
EPS = 1e-12


@dataclass
class Triangle:
    pos: tuple[int, int, int]
    uv: tuple[int, int, int] | None
    nor: tuple[int, int, int] | None
    obj: int


@dataclass
class Mesh:
    positions: list[tuple[float, float, float]] = field(default_factory=list)
    uvs: list[tuple[float, float]] = field(default_factory=list)  # OBJ convention (v up)
    normals: list[tuple[float, float, float]] = field(default_factory=list)
    objects: list[str] = field(default_factory=list)
    triangles: list[Triangle] = field(default_factory=list)
    _cache: dict[str, Any] = field(default_factory=dict, repr=False)


def parse_obj(text: str) -> Mesh:
    m = Mesh()
    current = -1

    def index(token: str, count: int) -> int | None:
        if not token:
            return None
        i = int(token)
        return i - 1 if i > 0 else count + i

    for raw in text.splitlines():
        parts = raw.split()
        if not parts:
            continue
        tag = parts[0]
        if tag == "v":
            m.positions.append((float(parts[1]), float(parts[2]), float(parts[3])))
        elif tag == "vt":
            m.uvs.append((float(parts[1]), float(parts[2])))
        elif tag == "vn":
            m.normals.append((float(parts[1]), float(parts[2]), float(parts[3])))
        elif tag == "o":
            m.objects.append(" ".join(parts[1:]) or f"object{len(m.objects)}")
            current = len(m.objects) - 1
        elif tag == "f":
            if current < 0:
                m.objects.append("object")
                current = 0
            corners = []
            for c in parts[1:]:
                bits = (c.split("/") + ["", ""])[:3]
                corners.append((index(bits[0], len(m.positions)), index(bits[1], len(m.uvs)),
                                index(bits[2], len(m.normals))))
            for k in range(1, len(corners) - 1):
                a, b, c = corners[0], corners[k], corners[k + 1]
                uv = (a[1], b[1], c[1]) if None not in (a[1], b[1], c[1]) else None
                nor = (a[2], b[2], c[2]) if None not in (a[2], b[2], c[2]) else None
                m.triangles.append(Triangle((a[0], b[0], c[0]), uv, nor, current))
    return m


# ---------------------------------------------------------------------------
# geometry helpers
# ---------------------------------------------------------------------------


def _sub(a: Sequence[float], b: Sequence[float]) -> tuple[float, ...]:
    return tuple(x - y for x, y in zip(a, b))


def _cross(a: Sequence[float], b: Sequence[float]) -> tuple[float, float, float]:
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _area3(m: Mesh, t: Triangle) -> float:
    p0, p1, p2 = (m.positions[i] for i in t.pos)
    c = _cross(_sub(p1, p0), _sub(p2, p0))
    return 0.5 * math.sqrt(c[0] ** 2 + c[1] ** 2 + c[2] ** 2)


def _uv_signed_area(m: Mesh, t: Triangle) -> float:
    (u0, v0), (u1, v1), (u2, v2) = (m.uvs[i] for i in t.uv)  # type: ignore[union-attr]
    return 0.5 * ((u1 - u0) * (v2 - v0) - (u2 - u0) * (v1 - v0))


def _canon(values: Sequence[Sequence[float]], digits: int = 6) -> list[int]:
    """Map each value to the index of its first equal (rounded) value: the fast exporter
    does not merge vertices, so connectivity must not depend on shared indices."""
    seen: dict[tuple[float, ...], int] = {}
    out = []
    for i, v in enumerate(values):
        out.append(seen.setdefault(tuple(round(x, digits) for x in v), i))
    return out


class _DSU:
    def __init__(self, n: int) -> None:
        self.p = list(range(n))

    def find(self, x: int) -> int:
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a: int, b: int) -> None:
        self.p[self.find(a)] = self.find(b)


def _topology(m: Mesh) -> dict[str, Any]:
    """Islands (triangles joined by an edge shared in BOTH 3D and UV space), seams,
    boundary and non-manifold edges."""
    if "topology" in m._cache:
        return m._cache["topology"]
    cpos = _canon(m.positions)
    cuv = _canon(m.uvs) if m.uvs else []
    dsu = _DSU(len(m.triangles))
    edge_tris: dict[tuple[int, int], list[tuple[int, tuple[int, int] | None]]] = defaultdict(list)
    for ti, t in enumerate(m.triangles):
        for k in range(3):
            a, b = cpos[t.pos[k]], cpos[t.pos[(k + 1) % 3]]
            key = (a, b) if a < b else (b, a)
            uvpair = None
            if t.uv is not None:
                ua, ub = cuv[t.uv[k]], cuv[t.uv[(k + 1) % 3]]
                uvpair = (ua, ub) if a < b else (ub, ua)
            edge_tris[key].append((ti, uvpair))
    seams = boundary = non_manifold = 0
    for users in edge_tris.values():
        if len(users) == 1:
            boundary += 1
        elif len(users) > 2:
            non_manifold += 1
        else:
            (t0, uv0), (t1, uv1) = users
            if uv0 is not None and uv0 == uv1:
                dsu.union(t0, t1)
            else:
                seams += 1
    roots: dict[int, int] = {}
    island = [roots.setdefault(dsu.find(i), len(roots)) for i in range(len(m.triangles))]
    topo = {"island": island, "islands": len(roots), "seams": seams, "boundary": boundary,
            "non_manifold": non_manifold}
    m._cache["topology"] = topo
    return topo


def _raster(m: Mesh, res: int, tris: Sequence[int] | None = None) -> Counter:
    """Coverage count per (tile_u, tile_v, x, y) cell, sampled at cell centres."""
    counts: Counter = Counter()
    for ti in tris if tris is not None else range(len(m.triangles)):
        t = m.triangles[ti]
        if t.uv is None or abs(_uv_signed_area(m, t)) < EPS:
            continue
        (u0, v0), (u1, v1), (u2, v2) = (m.uvs[i] for i in t.uv)
        tu, tv = math.floor((u0 + u1 + u2) / 3), math.floor((v0 + v1 + v2) / 3)
        pts = [((u - tu) * res, (v - tv) * res) for u, v in ((u0, v0), (u1, v1), (u2, v2))]
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        (ax, ay), (bx, by), (cx, cy) = pts
        det = (by - cy) * (ax - cx) + (cx - bx) * (ay - cy)
        for y in range(max(0, math.floor(min(ys))), min(res, math.ceil(max(ys)) + 1)):
            sy = y + 0.5 + 1.7e-7
            for x in range(max(0, math.floor(min(xs))), min(res, math.ceil(max(xs)) + 1)):
                sx = x + 0.5 + 1.1e-7
                w0 = ((by - cy) * (sx - cx) + (cx - bx) * (sy - cy)) / det
                w1 = ((cy - ay) * (sx - cx) + (ax - cx) * (sy - cy)) / det
                if w0 >= 0 and w1 >= 0 and 1 - w0 - w1 >= 0:
                    counts[(tu, tv, x, y)] += 1
    return counts


# ---------------------------------------------------------------------------
# 4.1 inspection
# ---------------------------------------------------------------------------


def inspect(m: Mesh, grid: int = 256) -> dict[str, Any]:
    topo = _topology(m)
    has_uvs = bool(m.triangles) and all(t.uv is not None for t in m.triangles)
    out: dict[str, Any] = {
        "objects": [
            {"name": name, "triangles": sum(1 for t in m.triangles if t.obj == oi)}
            for oi, name in enumerate(m.objects)
        ],
        "vertices": len(set(_canon(m.positions))),
        "triangles": len(m.triangles),
        "has_uvs": has_uvs,
        "has_normals": bool(m.triangles) and all(t.nor is not None for t in m.triangles),
        "boundary_edges": topo["boundary"],
        "non_manifold_edges": topo["non_manifold"],
    }
    issues: list[dict[str, Any]] = []
    if not has_uvs:
        issues.append({"level": "error", "code": "no_uvs",
                       "message": "the mesh has no UV coordinates (or some faces lack them), so there is "
                                  "nothing to paint into",
                       "suggestion": "unwrap it: ap_mesh_op(op='unwrap') with the native extension, or in a "
                                     "3D tool, then re-import"})
        out["issues"] = issues
        return out

    us = [u for u, _ in m.uvs]
    vs = [1 - v for _, v in m.uvs]
    out["uv_bounds"] = [min(us), min(vs), max(us), max(vs)]
    tiles = sorted({1001 + math.floor(sum(m.uvs[i][0] for i in t.uv) / 3)  # type: ignore[union-attr]
                    + 10 * math.floor(sum(m.uvs[i][1] for i in t.uv) / 3) for t in m.triangles})  # type: ignore[union-attr]
    out["udim_tiles"] = tiles
    out["uv_islands"] = topo["islands"]
    out["seam_edges"] = topo["seams"]

    # UV orientation relative to each face's 3D winding (its vertex normals say which side
    # is out): +1 is the usual OBJ convention (counter-clockwise in v-up UV space for a
    # counter-clockwise face). A mesh can be wholly mirrored -- ArmorPaint's own default
    # cube is -- which is consistent; the defect is a MINORITY that disagrees.
    zero = 0
    area3 = [_area3(m, t) for t in m.triangles]
    area_uv = []
    orient: list[int] = []
    for t, a3 in zip(m.triangles, area3):
        s = _uv_signed_area(m, t)
        area_uv.append(abs(s))
        if abs(s) < EPS:
            if a3 > EPS:
                zero += 1
            orient.append(0)
            continue
        side = 1
        if t.nor is not None:
            p0, p1, p2 = (m.positions[i] for i in t.pos)
            wn = _cross(_sub(p1, p0), _sub(p2, p0))
            vn = [sum(m.normals[i][k] for i in t.nor) for k in range(3)]
            side = 1 if sum(a * b for a, b in zip(wn, vn)) >= 0 else -1
        orient.append(side if s > 0 else -side)
    pos_n, neg_n = orient.count(1), orient.count(-1)
    majority = 1 if pos_n >= neg_n else -1
    flipped = neg_n if majority == 1 else pos_n
    out["uv_orientation"] = "standard" if majority == 1 else "mirrored"
    out["flipped_uv_triangles"] = flipped
    out["zero_area_uv_triangles"] = zero

    counts = _raster(m, grid)
    covered = len(counts)
    overlapped = sum(1 for c in counts.values() if c > 1)
    out["overlap_percent"] = round(100.0 * overlapped / covered, 3) if covered else 0.0
    out["uv_coverage_percent"] = round(100.0 * covered / (grid * grid * max(1, len(tiles))), 2)

    # Texel density: sqrt(UV area / surface area), per island (islands under 1% of the
    # surface are ignored: bevel strips and slivers skew the ratio without mattering).
    total3 = sum(area3) or 1.0
    isl_uv: dict[int, float] = defaultdict(float)
    isl_3d: dict[int, float] = defaultdict(float)
    for i, (a3, au) in enumerate(zip(area3, area_uv)):
        isl_uv[topo["island"][i]] += au
        isl_3d[topo["island"][i]] += a3
    dens = {k: math.sqrt(isl_uv[k] / isl_3d[k]) for k in isl_3d if isl_3d[k] / total3 >= ISLAND_MIN_AREA_SHARE and isl_uv[k] > 0}
    weights = {k: isl_3d[k] for k in dens}
    wsum = sum(weights.values()) or 1.0
    mean = sum(dens[k] * weights[k] for k in dens) / wsum if dens else 0.0
    var = sum(weights[k] * (dens[k] - mean) ** 2 for k in dens) / wsum if dens else 0.0
    ratio = max(dens.values()) / min(dens.values()) if dens else 1.0
    worst = sorted(dens, key=lambda k: abs(math.log(dens[k] / mean)) if mean else 0, reverse=True)[:5]
    out["texel_density"] = {
        "cv": math.sqrt(var) / mean if mean else 0.0,
        "max_over_min": ratio,
        "islands_measured": len(dens),
        "outlier_islands": [{"island": k, "relative_density": round(dens[k] / mean, 3)} for k in worst
                            if mean and abs(math.log(dens[k] / mean)) > math.log(1.4)],
    }
    m._cache["density"] = {k: (dens[k] / mean if mean else 1.0) for k in dens}

    if out["overlap_percent"] > OVERLAP_WARN_PERCENT:
        issues.append({"level": "warning", "code": "overlapping_uvs",
                       "message": f"{out['overlap_percent']:.1f}% of the used UV area is covered more than once: "
                                  f"paint on one of those spots also appears on the other(s)",
                       "suggestion": "fine if intentional (mirrored/stacked parts); otherwise unwrap before "
                                     "painting (ap_mesh_op unwrap, extension) or fix it in a 3D tool"})
    if flipped:
        issues.append({"level": "warning", "code": "flipped_uvs",
                       "message": f"{flipped} triangle(s) are mirrored in UV space against the rest of the "
                                  f"mesh: detail authored on the texture (text, a logo, a directional "
                                  f"pattern) reads backwards there",
                       "suggestion": "usually mirrored halves on purpose; if not, flip those islands in a 3D tool"})
    if majority == -1:
        issues.append({"level": "info", "code": "mirrored_uvs",
                       "message": "the whole UV layout is mirrored relative to the usual OBJ convention "
                                  "(ArmorPaint's own primitives are too). Painting on the model is "
                                  "unaffected; an image edited in another program appears mirrored",
                       "suggestion": "nothing to do unless textures are authored outside ArmorPaint"})
    if zero:
        issues.append({"level": "warning", "code": "degenerate_uvs",
                       "message": f"{zero} triangle(s) have zero UV area: nothing painted on them shows",
                       "suggestion": "re-unwrap"})
    if len(tiles) > 1 or tiles != [1001]:
        issues.append({"level": "info", "code": "outside_0_1",
                       "message": f"UVs span UDIM tiles {tiles}",
                       "suggestion": "export with layers='per_udim_tile' (extension) to get one texture per tile"})
    if ratio >= DENSITY_WARN_RATIO:
        issues.append({"level": "warning", "code": "uneven_texel_density",
                       "message": f"texel density varies {ratio:.1f}x between islands: some parts will look "
                                  f"sharp and others blurry at the same texture size",
                       "suggestion": "rescale islands in a 3D tool, or accept it for parts seen up close"})
    if topo["non_manifold"]:
        issues.append({"level": "info", "code": "non_manifold",
                       "message": f"{topo['non_manifold']} edge(s) are shared by more than two faces",
                       "suggestion": "bake maps (curvature, occlusion) can show artefacts there"})
    out["issues"] = issues
    return out


# ---------------------------------------------------------------------------
# 4.2 layout image
# ---------------------------------------------------------------------------


def _heat(rel: float) -> tuple[int, int, int]:
    """Relative density -> blue (low) .. green .. red (high), on a log scale."""
    t = max(-1.0, min(1.0, math.log2(rel) if rel > 0 else -1.0))
    if t < 0:
        return (40, int(120 + 100 * (1 + t)), int(230 - 60 * (1 + t)))
    return (int(60 + 180 * t), int(220 - 120 * t), 60)


def uv_layout_png(m: Mesh, size: int = 1024, heat: bool = False) -> bytes:
    """UDIM tile 1001 as an image, v down (as an exported texture): islands filled, edges
    drawn, overlaps red; with ``heat`` islands are coloured by relative texel density."""
    counts = _raster(m, size)
    topo = _topology(m)
    if heat and "density" not in m._cache:
        inspect(m, grid=64)
    density = m._cache.get("density", {})
    owner: dict[tuple[int, int], int] = {}
    if heat:
        for ti, t in enumerate(m.triangles):
            for key in _raster(m, size, [ti]):
                if key[0] == 0 and key[1] == 0:
                    owner[(key[2], key[3])] = topo["island"][ti]
    img = bytearray(bytes(LAYOUT_BACKGROUND) * (size * size))

    def put(x: int, y_obj: int, color: tuple[int, int, int]) -> None:
        y = size - 1 - y_obj
        if 0 <= x < size and 0 <= y < size:
            i = (y * size + x) * 3
            img[i : i + 3] = bytes(color)

    for (tu, tv, x, y), c in counts.items():
        if tu or tv:
            continue
        color = LAYOUT_OVERLAP if c > 1 else LAYOUT_FILL
        if c == 1 and heat and (x, y) in owner:
            color = _heat(density.get(owner[(x, y)], 1.0))
        put(x, y, color)
    for t in m.triangles:
        if t.uv is None:
            continue
        pts = [(m.uvs[i][0] * size, m.uvs[i][1] * size) for i in t.uv]
        for k in range(3):
            (x0, y0), (x1, y1) = pts[k], pts[(k + 1) % 3]
            steps = int(max(abs(x1 - x0), abs(y1 - y0))) + 1
            for s in range(steps + 1):
                x = int(x0 + (x1 - x0) * s / steps)
                y = int(y0 + (y1 - y0) * s / steps)
                if counts.get((0, 0, x, y), 0) > 1:
                    continue  # overlap stays red
                put(x, y, LAYOUT_EDGE)
    rows = b"".join(b"\x00" + bytes(img[y * size * 3 : (y + 1) * size * 3]) for y in range(size))
    return _png(size, size, rows)


# ---------------------------------------------------------------------------
# 1.5 UV -> model
# ---------------------------------------------------------------------------


@dataclass
class Hit:
    position: tuple[float, float, float]  # ArmorPaint local space (Z up)
    normal: tuple[float, float, float]
    object: str
    island: int
    triangle: int


def _obj_to_ap(p: Sequence[float]) -> tuple[float, float, float]:
    return (p[0], -p[2], p[1])


def _uv_index(m: Mesh, res: int = 64) -> dict[tuple[int, int, int, int], list[int]]:
    if "uv_index" in m._cache:
        return m._cache["uv_index"]
    index: dict[tuple[int, int, int, int], list[int]] = defaultdict(list)
    for ti, t in enumerate(m.triangles):
        if t.uv is None:
            continue
        (u0, v0), (u1, v1), (u2, v2) = (m.uvs[i] for i in t.uv)
        tu, tv = math.floor((u0 + u1 + u2) / 3), math.floor((v0 + v1 + v2) / 3)
        us = [(u - tu) * res for u in (u0, u1, u2)]
        vs = [(v - tv) * res for v in (v0, v1, v2)]
        for x in range(max(0, math.floor(min(us))), min(res - 1, math.floor(max(us))) + 1):
            for y in range(max(0, math.floor(min(vs))), min(res - 1, math.floor(max(vs))) + 1):
                index[(tu, tv, x, y)].append(ti)
    m._cache["uv_index"] = index
    return index


def uv_to_local(m: Mesh, u: float, v_img: float, res: int = 64) -> Hit | None:
    """The point of the model at image-convention UV (u, v down), or None if no triangle
    covers it. Position and normal are in ArmorPaint's local (Z-up) mesh space."""
    v = 1.0 - v_img
    tu, tv = math.floor(u), math.floor(v)
    cell = (tu, tv, min(res - 1, int((u - tu) * res)), min(res - 1, int((v - tv) * res)))
    topo = _topology(m)
    for ti in _uv_index(m, res).get(cell, ()):
        t = m.triangles[ti]
        (ax, ay), (bx, by), (cx, cy) = (m.uvs[i] for i in t.uv)  # type: ignore[union-attr]
        det = (by - cy) * (ax - cx) + (cx - bx) * (ay - cy)
        if abs(det) < EPS:
            continue
        w0 = ((by - cy) * (u - cx) + (cx - bx) * (v - cy)) / det
        w1 = ((cy - ay) * (u - cx) + (ax - cx) * (v - cy)) / det
        w2 = 1 - w0 - w1
        if min(w0, w1, w2) < -1e-9:
            continue
        p = [sum(w * m.positions[i][k] for w, i in zip((w0, w1, w2), t.pos)) for k in range(3)]
        if t.nor is not None:
            n = [sum(w * m.normals[i][k] for w, i in zip((w0, w1, w2), t.nor)) for k in range(3)]
        else:
            p0, p1, p2 = (m.positions[i] for i in t.pos)
            n = list(_cross(_sub(p1, p0), _sub(p2, p0)))
        length = math.sqrt(sum(c * c for c in n)) or 1.0
        return Hit(_obj_to_ap(p), _obj_to_ap([c / length for c in n]), m.objects[t.obj],
                   topo["island"][ti], ti)
    return None


def _quat_rotate(q: Sequence[float], v: Sequence[float]) -> tuple[float, float, float]:
    x, y, z, w = q
    # v' = v + 2w (q x v) + 2 q x (q x v)
    tx, ty, tz = 2 * (y * v[2] - z * v[1]), 2 * (z * v[0] - x * v[2]), 2 * (x * v[1] - y * v[0])
    return (v[0] + w * tx + (y * tz - z * ty), v[1] + w * ty + (z * tx - x * tz), v[2] + w * tz + (x * ty - y * tx))


def to_world(transform: dict[str, Any], p: Sequence[float], *, direction: bool = False) -> tuple[float, float, float]:
    """Apply an object transform as ap_get_object reports it (loc, quaternion xyzw,
    scale). ``direction`` rotates only (for normals)."""
    q = (transform.get("rot_x", 0.0), transform.get("rot_y", 0.0), transform.get("rot_z", 0.0), transform.get("rot_w", 1.0))
    if direction:
        return _quat_rotate(q, p)
    s = (transform.get("scale_x", 1.0), transform.get("scale_y", 1.0), transform.get("scale_z", 1.0))
    r = _quat_rotate(q, (p[0] * s[0], p[1] * s[1], p[2] * s[2]))
    return (r[0] + transform.get("loc_x", 0.0), r[1] + transform.get("loc_y", 0.0), r[2] + transform.get("loc_z", 0.0))


def uv_path_to_runs(
    m: Mesh, points: Sequence[Sequence[float]]
) -> tuple[list[dict[str, Any]], list[int]]:
    """Map a UV path (image convention, optional pressure after u, v) onto the model.
    Returns runs -- consecutive points on one island of one object, each to be painted as
    its own stroke, since a straight line between islands would cut across the model --
    and the indices of points no triangle covers."""
    runs: list[dict[str, Any]] = []
    missed: list[int] = []
    current: dict[str, Any] | None = None
    for i, p in enumerate(points):
        hit = uv_to_local(m, p[0], p[1])
        if hit is None:
            missed.append(i)
            current = None
            continue
        key = (hit.object, hit.island)
        if current is None or current["key"] != key:
            current = {"key": key, "object": hit.object, "island": hit.island, "points": [], "normals": [], "indices": []}
            runs.append(current)
        current["points"].append(hit.position + tuple(p[2:]))
        current["normals"].append(hit.normal)
        current["indices"].append(i)
    for r in runs:
        del r["key"]
    return runs, missed
