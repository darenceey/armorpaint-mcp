"""Mesh and UV diagnostics, UV layout image, UV -> world mapping (plan 4.1, 4.2, 1.5).

Synthetic OBJ fixtures for each defect, plus tests/data/default_cube.obj: ArmorPaint 1.0's
own export of its default (bevelled) cube.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from armorpaint_mcp import mesh_inspect, window_capture  # noqa: E402

CUBE = (Path(__file__).resolve().parent / "data" / "default_cube.obj").read_text()


def quad(x0, x1, u0, u1, v0=0.0, v1=1.0, z=0.0, base=0, tbase=0, name=None):
    """A unit-ish quad in the OBJ XY plane at depth z, two triangles, its own vt."""
    lines = [f"o {name}"] if name else []
    lines += [f"v {x0} 0 {z}", f"v {x1} 0 {z}", f"v {x1} 1 {z}", f"v {x0} 1 {z}"]
    lines += [f"vt {u0} {v0}", f"vt {u1} {v0}", f"vt {u1} {v1}", f"vt {u0} {v1}"]
    b, t = base, tbase
    lines += [f"f {b+1}/{t+1} {b+2}/{t+2} {b+3}/{t+3}", f"f {b+1}/{t+1} {b+3}/{t+3} {b+4}/{t+4}"]
    return "\n".join(lines) + "\n"


PLANE = quad(0, 1, 0, 1, name="Plane")


def report(text, **kw):
    return mesh_inspect.inspect(mesh_inspect.parse_obj(text), **kw)


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------


def test_parse_triangulates_quads_and_resolves_negative_indices():
    text = "o Q\nv 0 0 0\nv 1 0 0\nv 1 1 0\nv 0 1 0\nvt 0 0\nvt 1 0\nvt 1 1\nvt 0 1\nf -4/-4 -3/-3 -2/-2 -1/-1\n"
    m = mesh_inspect.parse_obj(text)
    assert len(m.triangles) == 2 and m.objects == ["Q"]
    assert m.triangles[1].pos == (0, 2, 3) and m.triangles[1].uv == (0, 2, 3)


def test_parse_the_real_default_cube():
    m = mesh_inspect.parse_obj(CUBE)
    assert m.objects == ["Tessellated"]
    assert len(m.positions) == 96 and len(m.uvs) == 144 and len(m.triangles) == 188


# ---------------------------------------------------------------------------
# 4.1 diagnostics, one fixture per defect
# ---------------------------------------------------------------------------


def test_clean_plane():
    r = report(PLANE)
    assert r["triangles"] == 2 and r["has_uvs"] and r["uv_islands"] == 1
    assert r["overlap_percent"] == 0 and r["flipped_uv_triangles"] == 0 and r["zero_area_uv_triangles"] == 0
    assert r["udim_tiles"] == [1001]
    assert r["uv_bounds"] == pytest.approx([0, 0, 1, 1])
    assert r["texel_density"]["cv"] == pytest.approx(0, abs=1e-9)
    assert r["issues"] == []


def test_overlapping_uvs():
    text = quad(0, 1, 0, 1) + quad(2, 3, 0, 1, base=4, tbase=4)  # two quads, same UV square
    r = report(text)
    assert r["overlap_percent"] > 95
    assert r["uv_islands"] == 2
    assert any(i["code"] == "overlapping_uvs" for i in r["issues"])


def test_flipped_uv_islands_are_the_minority_orientation():
    """'Flipped' is relative to the rest of the mesh: ArmorPaint's own default cube is
    mirrored by the OBJ convention on every triangle (measured: all 188 counter-clockwise
    faces have negative v-up UV area), and that is consistent, not a defect. The defect is
    an island mirrored against the others."""
    text = quad(0, 1, 0, 0.3) + quad(2, 3, 0.35, 0.65, base=4, tbase=4) + quad(4, 5, 1.0, 0.7, base=8, tbase=8)
    r = report(text)
    assert r["flipped_uv_triangles"] == 2 and r["uv_orientation"] == "standard"
    assert any(i["code"] == "flipped_uvs" for i in r["issues"])


def test_a_wholly_mirrored_mesh_is_reported_as_info():
    r = report(quad(0, 1, 1, 0))
    assert r["flipped_uv_triangles"] == 0 and r["uv_orientation"] == "mirrored"
    assert [i["level"] for i in r["issues"] if i["code"] == "mirrored_uvs"] == ["info"]


def test_zero_area_uv_triangle():
    text = "v 0 0 0\nv 1 0 0\nv 0 1 0\nvt 0.5 0.5\nvt 0.5 0.5\nvt 0.5 0.5\nf 1/1 2/2 3/3\n"
    r = report(text)
    assert r["zero_area_uv_triangles"] == 1
    assert any(i["code"] == "degenerate_uvs" for i in r["issues"])


def test_udim_tiles():
    text = quad(0, 1, 0, 1) + quad(2, 3, 1.1, 1.9, 0.1, 0.9, base=4, tbase=4)
    r = report(text)
    assert r["udim_tiles"] == [1001, 1002]
    assert any(i["code"] == "outside_0_1" for i in r["issues"])


def test_non_manifold_and_boundary_edges():
    text = "\n".join([
        "v 0 0 0", "v 1 0 0", "v 0 1 0", "v 0 -1 0", "v 0 0 1",
        "vt 0 0", "vt 1 0", "vt 0 1",
        "f 1/1 2/2 3/3", "f 1/1 2/2 4/3", "f 1/1 2/2 5/3",  # edge 1-2 shared by three faces
    ]) + "\n"
    r = report(text)
    assert r["non_manifold_edges"] == 1
    assert r["boundary_edges"] == 6


def test_texel_density_outlier_island():
    text = quad(0, 1, 0, 0.25, 0, 0.25) + quad(2, 3, 0.5, 1.0, 0.5, 1.0, base=4, tbase=4)
    r = report(text)
    d = r["texel_density"]
    assert d["max_over_min"] == pytest.approx(2.0, rel=1e-6)
    assert d["cv"] > 0.2
    assert any(i["code"] == "uneven_texel_density" for i in r["issues"])


def test_missing_uvs():
    r = report("v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n")
    assert r["has_uvs"] is False
    assert [i["code"] for i in r["issues"]] == ["no_uvs"]


def test_the_default_cube_is_clean_and_has_seams():
    r = report(CUBE)
    assert r["overlap_percent"] < 0.5
    assert r["flipped_uv_triangles"] == 0 and r["uv_orientation"] == "mirrored"
    assert r["uv_islands"] >= 6
    assert r["seam_edges"] > 0
    assert r["non_manifold_edges"] == 0 and r["boundary_edges"] == 0  # a closed solid
    assert [i for i in r["issues"] if i["level"] != "info"] == []


# ---------------------------------------------------------------------------
# 4.2 UV layout image
# ---------------------------------------------------------------------------


def px(rgb, w, x, y):
    i = (y * w + x) * 3
    return tuple(rgb[i : i + 3])


def test_layout_image_size_fill_and_overlap_colour():
    png = mesh_inspect.uv_layout_png(mesh_inspect.parse_obj(PLANE), size=64)
    w, h, rgb = window_capture._decode_png_rgb(png)
    assert (w, h) == (64, 64)
    inside = px(rgb, w, 32, 32)
    assert inside != mesh_inspect.LAYOUT_BACKGROUND
    stacked = quad(0, 1, 0, 0.5, 0, 0.5) + quad(2, 3, 0, 0.5, 0, 0.5, base=4, tbase=4)
    png = mesh_inspect.uv_layout_png(mesh_inspect.parse_obj(stacked), size=64)
    w, h, rgb = window_capture._decode_png_rgb(png)
    r, g, b = px(rgb, w, 16, 48)  # u 0.25, v 0.25 (image rows go down: v=0 at the bottom row)
    assert r > 180 and g < 100 and b < 100  # overlap is red
    assert px(rgb, w, 48, 16) == mesh_inspect.LAYOUT_BACKGROUND  # outside every island


def test_layout_heat_map_marks_density():
    text = quad(0, 1, 0, 0.25, 0, 0.25) + quad(2, 3, 0.5, 1.0, 0.5, 1.0, base=4, tbase=4)
    png = mesh_inspect.uv_layout_png(mesh_inspect.parse_obj(text), size=64, heat=True)
    w, _, rgb = window_capture._decode_png_rgb(png)
    low, high = px(rgb, w, 8, 56), px(rgb, w, 48, 16)
    assert low != high


# ---------------------------------------------------------------------------
# 1.5 UV -> world
# ---------------------------------------------------------------------------


def test_uv_to_local_on_a_plane_converts_obj_axes_to_armorpaint():
    """OBJ is Y-up; ArmorPaint's mesh data is Z-up: the exporter writes (x, z, -y).
    UVs here are image convention (v down), the exporter writes 1 - v."""
    m = mesh_inspect.parse_obj(PLANE)
    hit = mesh_inspect.uv_to_local(m, 0.25, 0.25)
    assert hit is not None
    # image (0.25, 0.25) = OBJ uv (0.25, 0.75) = OBJ position (0.25, 0.75, 0) = AP (0.25, 0, 0.75)
    assert hit.position == pytest.approx((0.25, 0.0, 0.75))
    assert hit.normal == pytest.approx((0.0, -1.0, 0.0))  # OBJ +z face normal -> AP -y
    assert hit.object == "Plane" and hit.island == 0
    assert mesh_inspect.uv_to_local(m, 1.5, 0.5) is None


def test_object_transform_to_world():
    t = {"loc_x": 1.0, "loc_y": 2.0, "loc_z": 3.0, "rot_x": 0.0, "rot_y": 0.0, "rot_z": 0.7071068,
         "rot_w": 0.7071068, "scale_x": 2.0, "scale_y": 2.0, "scale_z": 2.0}
    # scale 2, then 90 degrees about z: (1, 0, 0) -> (0, 2, 0), then + loc
    assert mesh_inspect.to_world(t, (1.0, 0.0, 0.0)) == pytest.approx((1.0, 4.0, 3.0), abs=1e-6)


def test_uv_path_splits_at_island_changes():
    """Two islands side by side in UV space: a straight UV line crossing from one to the
    other must become two strokes, not a line cut through the model."""
    text = quad(0, 1, 0, 0.5, 0, 1) + quad(5, 6, 0.5, 1.0, 0, 1, base=4, tbase=4)
    m = mesh_inspect.parse_obj(text)
    runs, missed = mesh_inspect.uv_path_to_runs(m, [(0.1 + 0.08 * i, 0.5) for i in range(11)])
    assert len(runs) == 2 and missed == []
    assert all(len(r["points"]) >= 2 for r in runs)
    assert runs[0]["island"] != runs[1]["island"]


def test_uv_path_reports_points_off_the_layout():
    m = mesh_inspect.parse_obj(quad(0, 1, 0, 0.5))
    runs, missed = mesh_inspect.uv_path_to_runs(m, [(0.1, 0.5), (0.2, 0.5), (0.9, 0.5)])
    assert missed == [2] and len(runs) == 1
