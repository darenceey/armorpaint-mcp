"""Static checks on the native extension and plugin for phase 4 (T2): what can be
verified without building ArmorPaint. The ops' behaviour is covered live (test_live_state.py)."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXT = (ROOT / "patch" / "mcp_ext.c").read_text(encoding="utf-8")
PLUGIN = (ROOT / "plugin" / "armorpaint_mcp_bridge.c").read_text(encoding="utf-8")


def ext_ops():
    return set(re.findall(r'strcmp\(op, "([a-z_]+)"\) == 0', EXT))


def test_extension_version_and_new_ops():
    assert int(re.search(r"#define MCP_EXT_VERSION (\d+)", EXT).group(1)) >= 2
    assert {"mesh_op", "project_snapshot"} <= ext_ops()
    listed = re.search(r'static const char \*mcp_ops =\s*((?:\s*"[^"]*")+);', EXT).group(1)
    listed = " ".join(re.findall(r'"([^"]*)"', listed)).split()
    assert set(listed) == ext_ops()  # ext_info advertises exactly what the dispatcher handles


def test_unwrap_is_compiled_only_with_plugins():
    """plugin_uv_unwrap_button exists only in WITH_PLUGINS builds (tab_plugins.c). The
    #else branch cannot be exercised on a build that has plugins -- stated, not skipped:
    this static check is its only test."""
    body = EXT[EXT.index("static char *mcp_op_mesh_op"):]
    body = body[: body.index("\n}\n")]
    assert "#ifdef WITH_PLUGINS" in body and "plugin_uv_unwrap_button()" in body and "#else" in body


def test_reimport_bypasses_the_modal_import_box():
    """project_reimport_mesh opens the 'Import Mesh' box and waits for a click; the op must
    call what the box's button calls."""
    body = EXT[EXT.index("static char *mcp_op_mesh_op"):]
    body = body[: body.index("\n}\n")]
    assert "import_mesh_run(" in body and "project_import_mesh_box" not in body


def test_snapshot_restores_what_export_arm_rewrites():
    body = EXT[EXT.index("static char *mcp_op_project_snapshot"):]
    body = body[: body.index("\n}\n")]
    for field in ("filepath", "envmap", "assets", "font_assets", "sound_assets", "mesh_assets"):
        assert re.search(rf"->{field}\s*=\s*prev_", body), field
    assert "recent_projects" in body  # the snapshot is taken back out of Recent Projects


def test_history_steps_carry_an_identity():
    body = EXT[EXT.index("static void mcp_emit_history"):]
    body = body[: body.index("\n}\n")]
    assert 'mcp_kv_s("id"' in body
    assert "> 32" not in body  # all steps, not the last 32: a checkpoint may be older


def test_plugin_marks_the_new_ops_heavy_and_can_set_the_path():
    cost = PLUGIN[PLUGIN.index("int op_cost(char *op)"):]
    cost = cost[: cost.index("\n}\n")]
    assert '"mesh_op"' in cost and '"project_snapshot"' in cost
    assert 'string_equals(op, "project_set_path")' in PLUGIN and "project_filepath_set(" in PLUGIN
