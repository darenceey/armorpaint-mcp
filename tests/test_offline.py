"""Offline tests: everything that can be checked without a running ArmorPaint."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import zlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from armorpaint_mcp import local_tools, server, transport, window_capture  # noqa: E402
from armorpaint_mcp.transport import BadArgs  # noqa: E402

PLUGIN = (ROOT / "plugin" / "armorpaint_mcp_bridge.c").read_text(encoding="utf-8")
EXT = (ROOT / "patch" / "mcp_ext.c").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# wire format
# ---------------------------------------------------------------------------


def test_request_is_flat_compact_and_prefixed():
    raw = transport.build_request("select_tool", {"tool": 3, "flag": True, "x": 0.5}, "1-2", 30)
    text = raw.decode()
    assert " " not in text
    assert "[" not in text
    body = json.loads(text)
    assert body["op"] == "select_tool" and body["a_tool"] == "3" and body["a_flag"] == "true"


def test_batch_request_flattens_items_in_order():
    raw = transport.build_batch_request(
        [("get_context", {}), ("select_tool", {"tool": 2}), ("node_add", {"type": "RGB", "x": 1.5})],
        "9-9",
        60,
        True,
    )
    body = json.loads(raw)
    assert body["op"] == "batch"
    assert body["a_count"] == "3" and body["a_stop_on_error"] == "true"
    assert body["b0_op"] == "get_context"
    assert body["b1_op"] == "select_tool" and body["b1_a_tool"] == "2"
    assert body["b2_a_type"] == "RGB" and body["b2_a_x"] == "1.5"
    assert "[" not in raw.decode()


def test_batch_request_limits():
    with pytest.raises(BadArgs):
        transport.build_batch_request([], "1-1", 10, False)
    with pytest.raises(BadArgs):
        transport.build_batch_request([("ping", {})] * 65, "1-1", 10, False)
    with pytest.raises(BadArgs):
        transport.build_batch_request([("batch", {})], "1-1", 10, False)
    big = [("console_write", {"text": "x" * 900})] * 30
    with pytest.raises(BadArgs):
        transport.build_batch_request(big, "1-1", 10, False)


def test_bridge_majors_accepted(tmp_path):
    for ver in ("1.1.0", "2.0.0"):
        transport._check_bridge_version({"bridge_version": ver}, tmp_path)
    with pytest.raises(transport.BridgeVersionMismatch):
        transport._check_bridge_version({"bridge_version": "3.0.0"}, tmp_path)


def test_dozing_detection():
    assert transport._is_dozing({"dozing": True})
    assert transport._is_dozing({"dozing": False, "enabled": False})
    assert not transport._is_dozing({"dozing": False, "enabled": True})
    assert not transport._is_dozing(None)


def test_doorbell_rung_after_request_is_on_disk(tmp_path, monkeypatch):
    """The bridge opens req/<id>.json by the id in the doorbell, never by listing req/."""
    (tmp_path / "heartbeat.json").write_text(json.dumps({"bridge_version": "2.0.0", "t": 1.0, "dozing": False}))
    res = transport.SpoolResolution(path=tmp_path, source="test", is_fallback=False, trace=[])
    monkeypatch.setattr(transport, "spool_resolution", lambda refresh=False: res)
    seen = {}

    def fake_await(spool, rid, op, timeout_s):
        seen["bell"] = (spool / transport.DOORBELL_FILE).read_text()
        seen["req"] = (spool / transport.REQ_DIR / f"{rid}.json").exists()
        return {"v": 1, "id": rid, "ok": True, "result": {"pong": True}}

    monkeypatch.setattr(transport, "_await_response", fake_await)
    assert transport.send_to_armorpaint("ping", {}, 5, follow_pending=False) == {"pong": True}
    assert seen["req"] and re.fullmatch(r"[0-9]+-[0-9]+", seen["bell"]), seen


def test_plugin_poll_does_not_list_req_once_rung():
    """Every directory listing leaks an fd on Linux/macOS (Iron's close_dir is empty)."""
    code = re.sub(r"//[^\n]*", "", PLUGIN)
    assert "iron_read_directory(" not in code
    on_update = code[code.index("void on_update("):code.index("void on_ui(")]
    assert "read_directory" not in on_update
    assert "file_here(path_bell)" in on_update


# ---------------------------------------------------------------------------
# server argument mapping
# ---------------------------------------------------------------------------


def test_tool_names_unique_and_prefixed():
    names = [t.name for t in server.TOOLS]
    assert len(names) == len(set(names))
    assert all(n.startswith("ap_") for n in names)


def test_every_bridge_tool_maps_to_a_handled_op():
    """Every op the server can send is handled by the plugin or by the native extension."""
    plugin_ops = set(re.findall(r'string_equals\(op, "([a-z_]+)"\)', PLUGIN))
    ext_ops = set(re.findall(r'strcmp\(op, "([a-z_]+)"\) == 0', EXT))
    local = server.LOCAL_TOOLS | server.GRAPH_TOOLS | {"ap_batch", "ap_project_metadata"}
    for tool in server.TOOLS:
        if tool.name in local:
            continue
        op = tool.name[3:]
        assert op in plugin_ops or op in ext_ops, f"{tool.name} -> {op} is handled nowhere"
    assert "export_textures_ex" in ext_ops
    for op in server.OP_TIMEOUTS:
        assert op in plugin_ops or op in ext_ops, op


def test_ext_tools_are_ext_ops():
    ext_ops = set(re.findall(r'strcmp\(op, "([a-z_]+)"\) == 0', EXT))
    for name in server.EXT_TOOLS:
        assert name[3:] in ext_ops, name


def test_export_textures_routes_options_to_extension():
    plain = server._build_wire_args("ap_export_textures", {"directory": "/tmp/out"})
    assert server._wire_op("ap_export_textures", plain) == "export_textures"
    ex = server._build_wire_args("ap_export_textures", {"directory": "/tmp/out", "bits": 16})
    assert ex["format"] == "exr" and server._wire_op("ap_export_textures", ex) == "export_textures_ex"
    with pytest.raises(BadArgs):
        server._build_wire_args("ap_export_textures", {"directory": "/tmp/out", "format": "png", "bits": 16})
    with pytest.raises(BadArgs):
        server._build_wire_args("ap_export_textures", {"directory": "/tmp/out", "filename": "a/b"})


def test_layer_args():
    assert server._build_wire_args("ap_layer_new", {"kind": "fill", "name": "Rust"}) == {
        "kind": "fill",
        "new_name": "Rust",
    }
    with pytest.raises(BadArgs):
        server._build_wire_args("ap_layer_new", {"kind": "bogus"})
    out = server._build_wire_args("ap_layer_set", {"index": 2, "opacity": 0.5, "blending": "multiply"})
    assert out == {"index": 2, "opacity": 0.5, "blending": "multiply"}
    with pytest.raises(BadArgs):
        server._build_wire_args("ap_layer_set", {"index": 2})  # nothing to change
    with pytest.raises(BadArgs):
        server._build_wire_args("ap_layer_set", {"index": 1, "name": "x", "visible": True})  # two targets
    with pytest.raises(BadArgs):
        server._build_wire_args("ap_layer_action", {"action": "explode"})
    assert server._build_wire_args("ap_layer_move", {"name": "A", "to_index": 0}) == {"name": "A", "to_index": 0}


def test_bake_render_camera_args():
    out = server._build_wire_args("ap_bake", {"node_id": 4, "type": "curvature", "samples": 64})
    assert out == {"node_id": 4, "type": "curvature", "samples": 64}
    with pytest.raises(BadArgs):
        server._build_wire_args("ap_bake", {"node_id": 4, "type": "ambient"})
    assert server._build_wire_args("ap_render_settings", {"gamma": 1.1, "lut_path": ""}) == {
        "gamma": 1.1,
        "lut_path": "",
    }
    with pytest.raises(BadArgs):
        server._build_wire_args("ap_render_settings", {"lut_path": "/x/y.png"})
    with pytest.raises(BadArgs):
        server._build_wire_args("ap_camera", {"view": "isometric"})
    with pytest.raises(BadArgs):
        server._build_wire_args("ap_texture_resolution", {"size": 1000})


def test_batch_tool_rejects_local_and_nested(monkeypatch):
    with pytest.raises(BadArgs):
        server._batch_tool({"steps": [{"tool": "ap_capture_window"}]})
    with pytest.raises(BadArgs):
        server._batch_tool({"steps": [{"tool": "ap_batch", "args": {"steps": []}}]})
    with pytest.raises(BadArgs):
        server._batch_tool({"steps": [{"tool": "ap_node_add", "args": {"type": "NOPE"}}]})

    sent = {}

    def fake_send_batch(items, timeout, stop):
        sent["items"], sent["stop"] = items, stop
        return {"count": len(items), "results": [{"i": i, "op": op, "ok": True, "result": {}} for i, (op, _) in enumerate(items)]}

    monkeypatch.setattr(server, "send_batch", fake_send_batch)
    out = server._batch_tool(
        {"steps": [{"tool": "select_tool", "args": {"tool": "fill"}}, {"tool": "ap_fill_layer"}], "stop_on_error": True}
    )
    assert sent["items"] == [("select_tool", {"tool": 2}), ("fill_layer", {})]
    assert sent["stop"] is True
    assert [r["tool"] for r in out["result"]["results"]] == ["ap_select_tool", "ap_fill_layer"]


# ---------------------------------------------------------------------------
# PNG encode / decode, crop
# ---------------------------------------------------------------------------


def test_png_roundtrip_and_crop():
    w, h = 5, 3
    bgrx = bytes(v for y in range(h) for x in range(w) for v in (x * 10, y * 20, 7, 0))
    scan, ow, oh = window_capture._bgrx_to_rgb_rows(bgrx, w * 4, (1, 1, 3, 2), 1)
    png = window_capture._png(ow, oh, scan)
    dw, dh, rgb = window_capture._decode_png_rgb(png)
    assert (dw, dh) == (3, 2)
    assert rgb[:3] == bytes((7, 20, 10))  # pixel (1,1): R=7 G=20 B=10
    with pytest.raises(window_capture.CaptureError):
        window_capture._crop_box((100, 100, 5, 5), 10, 10)


@pytest.mark.skipif(shutil.which("convert") is None, reason="ImageMagick not installed")
def test_png_decoder_handles_all_filters(tmp_path):
    src = tmp_path / "p.png"
    subprocess.run(["convert", "-size", "31x17", "plasma:fractal", "-depth", "8", str(src)], check=True)
    ref = subprocess.run(["convert", str(src), "-depth", "8", "rgb:-"], check=True, capture_output=True).stdout
    w, h, rgb = window_capture._decode_png_rgb(src.read_bytes())
    assert (w, h) == (31, 17) and rgb == ref


# ---------------------------------------------------------------------------
# local tools
# ---------------------------------------------------------------------------


def test_resource_search(tmp_path, monkeypatch):
    monkeypatch.setattr(local_tools, "_armorpaint_data_dirs", lambda: [])
    (tmp_path / "textures").mkdir()
    (tmp_path / "textures" / "rusty_metal_albedo.png").write_bytes(b"x")
    (tmp_path / "textures" / "wood.jpg").write_bytes(b"x")
    (tmp_path / "export_presets").mkdir()
    (tmp_path / "export_presets" / "unreal.json").write_text("{}")
    (tmp_path / "notes.json").write_text("{}")
    (tmp_path / "studio.hdr").write_bytes(b"x")
    out = local_tools.search_resources("rust", None, [str(tmp_path)])
    assert [r["name"] for r in out["results"]] == ["rusty_metal_albedo"]
    presets = local_tools.search_resources("", ["export_preset"], [str(tmp_path)])
    assert [r["name"] for r in presets["results"]] == ["unreal"]  # notes.json is not a preset
    env = local_tools.search_resources("studio", ["envmap"], [str(tmp_path)])
    assert env["results"][0]["kind"] == "envmap"
    with pytest.raises(ValueError):
        local_tools.search_resources("", ["sound"], [str(tmp_path)])


def test_project_metadata_sidecar(tmp_path):
    arm = tmp_path / "goblin.arm"
    arm.write_bytes(b"project")
    out = local_tools.project_metadata(str(arm), {"brief": "worn bronze", "texel_density": 512})
    assert out["changed"] and out["metadata"]["brief"] == "worn bronze"
    again = local_tools.project_metadata(str(arm), None, ["texel_density"])
    assert again["metadata"] == {"brief": "worn bronze"}
    assert arm.read_bytes() == b"project"
    assert json.loads((tmp_path / "goblin.arm.mcp.json").read_text())["metadata"] == {"brief": "worn bronze"}
    with pytest.raises(ValueError):
        local_tools.project_metadata("", {"a": 1})


# ---------------------------------------------------------------------------
# the plugin obeys minic's rules (docs/MINIC_DIALECT_AND_API.md Appendix B)
# ---------------------------------------------------------------------------


def _functions(src: str) -> list[str]:
    return re.findall(r"^[a-z_]+[ *]+([a-z_0-9]+)\([^)]*\) \{$", src, re.M)


def test_plugin_function_budget_and_main_last():
    fns = _functions(PLUGIN)
    assert fns[-1] == "main", "main must be the last function"
    assert len(fns) - 1 <= 32, f"{len(fns) - 1} functions: minic silently drops the 33rd"


def test_plugin_dialect_traps():
    code = re.sub(r"//[^\n]*", "", PLUGIN)
    code = re.sub(r'"(?:\\.|[^"\\])*"', '""', code)
    assert not re.search(r"^\s*#define", code, re.M)
    assert "switch" not in code
    assert "?" not in code, "no ternary operator in minic"
    assert not re.search(r"\([a-z_]+\)\s*[a-z_(]", code.replace("sizeof(", "")), "no casts"
    body_lines = code.split("\n")
    for line in body_lines:
        assert not re.search(r"\w\+\+\s*[,)]", line), f"postfix ++ in an expression: {line.strip()}"


def test_plugin_optional_bindings_only_behind_try_wrappers():
    """A missing binding must abort only a one-line wrapper (see ext_state)."""
    code = re.sub(r"//[^\n]*", "", PLUGIN)
    code = re.sub(r'"(?:\\.|[^"\\])*"', '""', code)
    starts = [(m.start(), m.group(1)) for m in re.finditer(r"^[a-z_]+[ *]+([a-z_0-9]+)\([^)]*\) \{$", code, re.M)]
    for binding, wrapper in (("mcp_ext_call(", "try_ext"), ("viewport_save_texture_to_file(", "try_save_png")):
        uses = [m.start() for m in re.finditer(re.escape(binding), code)]
        assert len(uses) == 1, binding
        enclosing = [name for pos, name in starts if pos < uses[0]][-1]
        assert enclosing == wrapper, f"{binding} is called from {enclosing}, not {wrapper}"


# ---------------------------------------------------------------------------
# the native patch applies, is idempotent, and reverts
# ---------------------------------------------------------------------------


def _fake_checkout(root: Path) -> None:
    src = root / "paint" / "sources"
    src.mkdir(parents=True)
    (src / "main.c").write_text('#include "global.h"\n\n#include "a.c"\n#include "viewport.c"\n\nint main() {}\n')
    (src / "functions.h").write_text("#pragma once\nvoid f();\n")
    (src / "minic_api_list.h").write_text('X1(project_reskin_mesh, "b(i frame)", b, i)\nX0(iron_delay_idle_sleep, "v()", v)\n')


def test_ext_patch_apply_idempotent_and_revert(tmp_path):
    _fake_checkout(tmp_path)
    script = ROOT / "patch" / "apply_ext_patch.py"
    subprocess.run([sys.executable, str(script), str(tmp_path)], check=True, capture_output=True)
    src = tmp_path / "paint" / "sources"
    main_c = (src / "main.c").read_text()
    assert main_c.index('#include "mcp_ext.c"') > main_c.index('#include "viewport.c"')
    assert "mcp_ext_call" in (src / "functions.h").read_text()
    assert "X3(mcp_ext_call" in (src / "minic_api_list.h").read_text()
    assert (src / "mcp_ext.c").read_text() == EXT
    snapshot = {p.name: p.read_text() for p in src.iterdir()}
    subprocess.run([sys.executable, str(script), str(tmp_path)], check=True, capture_output=True)
    assert {p.name: p.read_text() for p in src.iterdir()} == snapshot
    subprocess.run([sys.executable, str(script), str(tmp_path), "--revert"], check=True, capture_output=True)
    assert not (src / "mcp_ext.c").exists()
    assert "mcp_ext" not in (src / "main.c").read_text() + (src / "functions.h").read_text() + (src / "minic_api_list.h").read_text()
