"""
ArmorPaint MCP Server
=====================
An MCP server that drives a running ArmorPaint 1.0 through a minic bridge plugin.

Install deps:
  pip install mcp

Run (for testing):
  python -m armorpaint_mcp.server

Configure in an MCP client:
  {
    "mcpServers": {
      "armorpaint": {
        "command": "armorpaint-mcp",
        "env": { "ARMORPAINT_SPOOL": "C:/ArmorPaint/data/mcp_spool" }
      }
    }
  }

ArmorPaint must be running with the MCP bridge plugin enabled (Plugins tab). Every tool
call is a file round-trip through the spool directory — see ``docs/PROTOCOL.md`` for the
wire contract and ``transport.py`` for how the spool path is discovered. Start with
``ap_bridge_status``: it diagnoses the connection without needing the bridge to answer.

Scope note, because it shapes the whole tool surface: ArmorPaint's stock plugin API is 529
bindings, and it has no binding at all for layer management, undo/redo, export format,
bake runs, render settings or camera views. Those tools are served by the optional native
extension (``patch/apply_ext_patch.py``: one added binding, ``mcp_ext_call``), which the
bridge detects at run time; on a stock build they answer ``unsupported`` and say so. Undo
and redo also fall back to the app's own keyboard shortcuts, sent as synthetic input, and
UI automation, window capture, resource search and project metadata are answered by this
server itself, so they work on any build.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import sys
import time
from collections import OrderedDict
from functools import partial
from pathlib import Path
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp import types

try:  # normal package import
    from . import __version__
    from .transport import (
        BadArgs,
        BridgeError,
        OpFailed,
        RequestTimeout,
        bridge_diagnostics,
        read_heartbeat,
        send_to_armorpaint,
        spool_resolution,
    )
    from .window_capture import MAX_DOWNSCALE, CaptureError, capture_window
    from . import desktop_input, image_diff, local_tools, node_catalogue, node_graph, recipes
    from .transport import send_batch
except ImportError:  # running server.py as a loose script
    __version__ = "1.1.0"
    from transport import (  # type: ignore[no-redef]
        BadArgs,
        BridgeError,
        OpFailed,
        RequestTimeout,
        bridge_diagnostics,
        read_heartbeat,
        send_to_armorpaint,
        spool_resolution,
    )
    from window_capture import (  # type: ignore[no-redef]
        MAX_DOWNSCALE,
        CaptureError,
        capture_window,
    )
    import desktop_input  # type: ignore[no-redef]
    import image_diff  # type: ignore[no-redef]
    import local_tools  # type: ignore[no-redef]
    import node_catalogue  # type: ignore[no-redef]
    import node_graph  # type: ignore[no-redef]
    import recipes  # type: ignore[no-redef]
    from transport import send_batch  # type: ignore[no-redef]

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

SERVER_NAME = "armorpaint"
TOOL_PREFIX = "ap_"

DEFAULT_TIMEOUT_S = 30.0

# Per-op deadlines. Most ops answer in a frame or two; imports, exports and saves are
# measured in seconds because the plugin runs them inline on the render thread.
OP_TIMEOUTS: dict[str, float] = {
    "ping": 10,
    "get_app_info": 10,
    "bridge_set_enabled": 10,
    "console_write": 10,
    "show_message": 15,
    "project_new": 120,
    "project_open": 300,
    "project_save": 300,
    "project_save_as": 300,
    "project_get_info": 15,
    "project_list_texture_assets": 15,
    "project_list_scripts": 15,
    "quit": 5,
    "import_asset": 300,
    "import_envmap": 300,
    "set_envmap_params": 15,
    "export_textures": 600,
    "export_material_bake": 600,
    "export_mesh": 300,
    "export_material": 120,
    "fs_list": 30,
    "fs_stat": 15,
    "fs_mkdir": 15,
    "get_context": 15,
    "get_config": 15,
    "set_config": 20,
    "get_main_object": 15,
    "get_object": 15,
    "shape_list": 15,
    "shape_add": 120,
    "object_duplicate": 120,
    "object_set_transform": 30,
    "object_set_visible": 15,
    "append_mesh": 300,
    "material_get_active": 15,
    "material_create": 60,
    "material_select": 60,
    "material_delete": 60,
    "material_assign": 60,
    "material_set_channels": 20,
    "material_list": 15,
    "material_update": 120,
    "node_list": 20,
    "node_add": 60,
    "node_remove": 60,
    "node_connect": 60,
    "node_disconnect": 60,
    "node_set_value": 60,
    "select_tool": 15,
    "set_brush": 15,
    "paint_stroke": 120,
    "paint_stroke_world": 120,
    "fill_layer": 180,
    "set_display_channel": 30,
    "capture_to_project": 180,
    "capture_viewport": 180,
    "bridge_set_idle": 10,
    # native extension (patch/apply_ext_patch.py)
    "layer_list": 15,
    "layer_select": 30,
    "layer_new": 60,
    "layer_delete": 60,
    "layer_duplicate": 60,
    "layer_set": 30,
    "layer_move": 30,
    "layer_action": 120,
    "undo": 60,
    "redo": 60,
    "history": 15,
    "export_presets": 15,
    "export_textures_ex": 900,
    "bake": 120,
    "bake_status": 15,
    "bake_settings": 15,
    "render_settings": 30,
    "texture_resolution": 300,
    "project_lists": 15,
    "camera": 15,
    "console_read": 10,
}

# Answered by the optional native extension; on a stock build the bridge says "unsupported".
EXT_TOOLS = frozenset(
    {
        "ap_layer_list", "ap_layer_select", "ap_layer_new", "ap_layer_delete",
        "ap_layer_duplicate", "ap_layer_set", "ap_layer_move", "ap_layer_action",
        "ap_history", "ap_export_presets", "ap_bake", "ap_bake_status", "ap_bake_settings",
        "ap_render_settings", "ap_texture_resolution", "ap_project_lists", "ap_camera",
        "ap_console_read",
    }
)
EXT_HINT = (
    "This needs the optional native extension: run patch/apply_ext_patch.py against an "
    "ArmorPaint source checkout and rebuild (docs/UPSTREAM_CHANGES.md). The bridge plugin "
    "detects it automatically; nothing else changes."
)

LAYER_KINDS = ("paint", "fill", "decal", "group", "black_mask", "white_mask", "fill_mask")
LAYER_ACTIONS = ("clear", "merge_down", "merge_group", "to_fill", "to_paint", "apply_mask", "invert_mask")
BLEND_MODES = (
    "mix", "darken", "multiply", "burn", "lighten", "screen", "dodge", "add", "overlay",
    "soft_light", "linear_light", "difference", "subtract", "divide", "hue", "saturation",
    "color", "value",
)
BAKE_TYPES = (
    "curvature", "normal", "normal_object", "height", "derivative", "position", "texcoord",
    "material_id", "object_id", "vertex_color", "occlusion", "lightmap", "bent_normal",
    "thickness",
)
EXPORT_LAYER_MODES = ("visible", "selected", "per_object", "per_udim_tile")
CAMERA_VIEWS = ("front", "back", "left", "right", "top", "bottom", "reset")
UI_TOOLS = frozenset({"ap_ui_click", "ap_ui_key", "ap_ui_drag", "ap_ui_scroll"})

# Tools answered entirely by this process — they work even when ArmorPaint is closed.
LOCAL_TOOLS = frozenset(
    {"ap_bridge_status", "ap_read_image_file", "ap_capture_window", "ap_resource_search"}
    | UI_TOOLS
)

# Bulk data travels by path; only these tools ever inline bytes into an MCP response.
MAX_IMAGE_BYTES = 6_000_000
MAX_CAPTURE_DIM = 4096
MIN_CAPTURE_DIM = 16

# MUST equal MAX_STROKE_POINTS in plugin/armorpaint_mcp_bridge.c. The bridge paints a
# whole stroke inside one ArmorPaint frame, and each point costs three minic script calls
# against a per-frame budget of roughly 280 before the 8 MB context arena overflows
# (docs/MINIC_DIALECT_AND_API.md 1.11a). A longer list is rejected by the plugin rather
# than truncated, so a mismatch here turns into a bad_args on every long stroke.
MAX_STROKE_POINTS = 48

# OBJ line breaks cannot cross the wire: the plugin's JSON parser does not decode escapes,
# so encode_value() refuses any control character including "\n". Lines are joined with
# this sentinel and split apart again by the plugin's append_mesh arm. Wavefront OBJ has
# no use for "|", which is why it is safe as a separator.
OBJ_LINE_SEP = "|"

IMAGE_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}

# context_select_tool(i) — TOOL_TYPE_* (minic_api.c:535).
TOOL_TYPES = {
    "brush": 0,
    "eraser": 1,
    "fill": 2,
    "decal": 3,
    "text": 4,
    "clone": 5,
    "blur": 6,
    "particle": 7,
    "colorid": 8,
    "picker": 9,
    "material": 10,
    "cursor": 11,
    "select": 12,
    "bake": 13,
}

# context_set_viewport_mode(i) — enums.h:58. viewport_mode_t is not a registered enum, so
# these are plain integers.
VIEWPORT_MODES = {
    "none": -1,
    "lit": 0,
    "base_color": 1,
    "normal_map": 2,
    "occlusion": 3,
    "roughness": 4,
    "metallic": 5,
    "opacity": 6,
    "height": 7,
    "emission": 8,
    "subsurface": 9,
    "texcoord": 10,
    "object_normal": 11,
    "material_id": 12,
    "object_id": 13,
    "mask": 14,
    "path_trace": 15,
}

# Valid script_material_create_node(type) strings: the node types in the socket catalogue
# (data/node_sockets.json, generated from nodes_material/*.c and nodes_neural/*.c) that a
# material canvas can create on this platform. The output node is pre-created with each
# material. Validated here so a typo cannot reach the binding.
NODE_TYPES = frozenset(node_catalogue.creatable(node_catalogue.load(), sys.platform))

# Built-in primitives accepted by script_shape_add (minic_impl.c:702). Reported, not
# enforced — the list is per-build and the plugin validates against script_shape_list().
KNOWN_SHAPES = (
    "cone cube cube_bevel cube_bevel_shared_uvs cube_shared_uvs cylinder empty torus "
    "plane plane_2048 sphere sphere_2048"
)

PAINT_CHANNELS = (
    "base",
    "opacity",
    "occlusion",
    "roughness",
    "metallic",
    "normal",
    "height",
    "emission",
    "subsurface",
)

CONFIG_INTS = ("window_w", "window_h", "undo_steps", "layer_res", "workspace", "workflow")
CONFIG_FLOATS = ("window_scale", "rp_supersample", "camera_fov")
CONFIG_STRINGS = ("keymap", "theme")
CONFIG_BOOLS = ("brush_live", "node_previews", "material_live")

_ABS_PATH_RE = re.compile(r"^(?:[A-Za-z]:/|//|/)")


# ---------------------------------------------------------------------------
# Argument helpers — everything is validated here, before the plugin sees it
# ---------------------------------------------------------------------------


def _norm_path(args: dict[str, Any], key: str, required: bool = True) -> str | None:
    raw = args.get(key)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        if required:
            raise BadArgs(f"'{key}' is required and must be a non-empty absolute path.", arg=key)
        return None
    if not isinstance(raw, str):
        raise BadArgs(f"'{key}' must be a string path.", arg=key)
    path = raw.strip().replace("\\", "/")
    while "//" in path[2:]:
        path = path[:2] + path[2:].replace("//", "/")
    if "$" in path or "`" in path:
        # The plugin hands some paths to a shell (mkdir/rm via system()); on Linux and
        # macOS sh expands $(...) and `...` even inside the double quotes it adds.
        raise BadArgs(
            f"'{key}' must not contain '$' or '`' (got {raw!r}): ArmorPaint passes paths "
            f"through a shell, where they would be expanded.",
            arg=key,
        )
    if not _ABS_PATH_RE.match(path):
        raise BadArgs(
            f"'{key}' must be an absolute path (got {raw!r}). A relative path resolves "
            f"against ArmorPaint's working directory or its ./data/ folder depending on the "
            f"binding, which silently reads or writes the wrong file.",
            arg=key,
        )
    return path


def _req_str(args: dict[str, Any], key: str) -> str:
    value = args.get(key)
    if not isinstance(value, str) or not value.strip():
        raise BadArgs(f"'{key}' is required and must be a non-empty string.", arg=key)
    return value.strip()


def _opt_str(args: dict[str, Any], key: str) -> str | None:
    value = args.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise BadArgs(f"'{key}' must be a string.", arg=key)
    return value


def _num(value: Any, key: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BadArgs(f"'{key}' must be a number.", arg=key)
    return float(value)


def _opt_float(
    args: dict[str, Any], key: str, lo: float | None = None, hi: float | None = None
) -> float | None:
    if args.get(key) is None:
        return None
    value = _num(args[key], key)
    if lo is not None and value < lo:
        raise BadArgs(f"'{key}' must be >= {lo} (got {value}).", arg=key)
    if hi is not None and value > hi:
        raise BadArgs(f"'{key}' must be <= {hi} (got {value}).", arg=key)
    return value


def _opt_int(
    args: dict[str, Any], key: str, lo: int | None = None, hi: int | None = None
) -> int | None:
    if args.get(key) is None:
        return None
    value = args[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BadArgs(f"'{key}' must be an integer.", arg=key)
    if isinstance(value, float) and not value.is_integer():
        raise BadArgs(f"'{key}' must be a whole number (got {value}).", arg=key)
    out = int(value)
    if lo is not None and out < lo:
        raise BadArgs(f"'{key}' must be >= {lo} (got {out}).", arg=key)
    if hi is not None and out > hi:
        raise BadArgs(f"'{key}' must be <= {hi} (got {out}).", arg=key)
    return out


def _req_int(args: dict[str, Any], key: str, lo: int | None = None, hi: int | None = None) -> int:
    if args.get(key) is None:
        raise BadArgs(f"'{key}' is required.", arg=key)
    value = _opt_int(args, key, lo, hi)
    assert value is not None
    return value


def _opt_bool(args: dict[str, Any], key: str) -> bool | None:
    value = args.get(key)
    if value is None:
        return None
    if not isinstance(value, bool):
        raise BadArgs(f"'{key}' must be a boolean.", arg=key)
    return value


def _vec(args: dict[str, Any], key: str, size: int) -> list[float] | None:
    value = args.get(key)
    if value is None:
        return None
    if not isinstance(value, (list, tuple)) or len(value) != size:
        raise BadArgs(f"'{key}' must be an array of {size} numbers.", arg=key)
    return [_num(v, key) for v in value]


def _points(args: dict[str, Any], key: str, dims: int) -> str:
    """Flatten a point list to ``x,y[,z];x,y[,z]``.

    No JSON array can cross this wire: an array anywhere in the request corrupts the
    remainder of the plugin's parse (``iron_json.c:297``).
    """
    value = args.get(key)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise BadArgs(f"'{key}' is empty.", arg=key)
        groups = [g for g in text.split(";") if g.strip()]
        for g in groups:
            parts = g.split(",")
            if len(parts) != dims:
                raise BadArgs(
                    f"'{key}': segment {g!r} has {len(parts)} components, expected {dims}.",
                    arg=key,
                )
            for p in parts:
                try:
                    float(p)
                except ValueError as exc:
                    raise BadArgs(f"'{key}': {p!r} is not a number.", arg=key) from exc
        if len(groups) > MAX_STROKE_POINTS:
            raise BadArgs(
                f"'{key}' has {len(groups)} points; the limit is {MAX_STROKE_POINTS} because "
                f"the whole stroke is applied inside a single ArmorPaint frame.",
                arg=key,
            )
        return ";".join(g.strip() for g in groups)

    if not isinstance(value, (list, tuple)) or not value:
        raise BadArgs(
            f"'{key}' is required: an array of {dims}-number points, e.g. "
            f"{'[[0.4,0.5],[0.6,0.5]]' if dims == 2 else '[[0,0,0],[1,0,0]]'}.",
            arg=key,
        )
    if len(value) > MAX_STROKE_POINTS:
        raise BadArgs(
            f"'{key}' has {len(value)} points; the limit is {MAX_STROKE_POINTS} because the "
            f"whole stroke is applied inside a single ArmorPaint frame.",
            arg=key,
        )
    out: list[str] = []
    for point in value:
        if isinstance(point, dict):
            keys = ("x", "y", "z")[:dims]
            if any(k not in point for k in keys):
                raise BadArgs(f"'{key}': each point object needs {', '.join(keys)}.", arg=key)
            comps = [_num(point[k], key) for k in keys]
        elif isinstance(point, (list, tuple)):
            if len(point) != dims:
                raise BadArgs(
                    f"'{key}': each point needs exactly {dims} numbers (got {len(point)}).",
                    arg=key,
                )
            comps = [_num(c, key) for c in point]
        else:
            raise BadArgs(f"'{key}': each point must be an array or object.", arg=key)
        out.append(",".join(f"{c:.6f}".rstrip("0").rstrip(".") or "0" for c in comps))
    return ";".join(out)


def _pick(args: dict[str, Any], key: str, table: dict[str, int], label: str) -> int:
    """Resolve an enum argument given either its name or its integer value."""
    value = args.get(key)
    if value is None:
        raise BadArgs(
            f"'{key}' is required: one of {', '.join(sorted(table))} (or the integer index).",
            arg=key,
        )
    if isinstance(value, bool):
        raise BadArgs(f"'{key}' must be a {label} name or integer.", arg=key)
    if isinstance(value, str):
        name = value.strip().lower().replace(" ", "_").replace("-", "_")
        if name not in table:
            raise BadArgs(
                f"'{key}': unknown {label} {value!r}. Valid: {', '.join(sorted(table))}.",
                arg=key,
            )
        return table[name]
    if isinstance(value, (int, float)) and float(value).is_integer():
        index = int(value)
        if index not in table.values():
            raise BadArgs(
                f"'{key}': {index} is not a valid {label} index. Valid: "
                + ", ".join(f"{v}={k}" for k, v in sorted(table.items(), key=lambda kv: kv[1])),
                arg=key,
            )
        return index
    raise BadArgs(f"'{key}' must be a {label} name or integer.", arg=key)


def _drop_none(mapping: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in mapping.items() if v is not None}


# ---------------------------------------------------------------------------
# Tool declarations
# ---------------------------------------------------------------------------


def _tool(
    name: str,
    description: str,
    properties: dict[str, Any] | None = None,
    required: list[str] | None = None,
) -> types.Tool:
    schema: dict[str, Any] = {"type": "object", "properties": properties or {}}
    if required:
        schema["required"] = required
    return types.Tool(name=name, description=description, inputSchema=schema)


def _s(description: str, **extra: Any) -> dict[str, Any]:
    return {"type": "string", "description": description, **extra}


def _n(description: str, **extra: Any) -> dict[str, Any]:
    return {"type": "number", "description": description, **extra}


def _i(description: str, **extra: Any) -> dict[str, Any]:
    return {"type": "integer", "description": description, **extra}


def _b(description: str, **extra: Any) -> dict[str, Any]:
    return {"type": "boolean", "description": description, **extra}


_PATH_NOTE = "Absolute path, forward slashes (backslashes are converted for you)."

TOOLS: list[types.Tool] = [
    # ---- bridge & session -------------------------------------------------
    _tool(
        "ap_bridge_status",
        "Diagnose the connection to ArmorPaint. Answered entirely by this server, with no "
        "round trip, so it works when everything else is failing: it reports the resolved "
        "spool directory and how it was resolved, whether the bridge heartbeat exists, "
        "whether the heartbeat's clock is advancing (the only valid liveness test), the "
        "bridge/app versions, the open project, queue depth, and a plain-language diagnosis "
        "plus next step. Call this FIRST whenever any other tool reports a transport error.",
        {
            "probe": _b(
                "Watch the heartbeat for up to ~4 s to prove the bridge is actually "
                "running rather than merely present. Set false for an instant answer.",
                default=True,
            )
        },
    ),
    _tool(
        "ap_ping",
        "Round-trip health check: sends a real request through the file mailbox and returns "
        "the bridge's app time, window title and current project path. Confirms the whole "
        "path works end to end, unlike ap_bridge_status which only reads the heartbeat.",
    ),
    _tool(
        "ap_get_app_info",
        "Application-level facts: window title, window size and position, ArmorPaint's data "
        "directory, and the project format version.",
    ),
    _tool(
        "ap_bridge_set_enabled",
        "Turn the bridge on or off. Disabled, it never holds ArmorPaint awake and refuses "
        "every op except this one and ap_ping; the server still wakes the app to deliver "
        "requests, so it can be re-enabled from here. You rarely need this: an enabled bridge "
        "already lets ArmorPaint sleep between requests (see ap_bridge_set_idle).",
        {"enabled": _b("True to poll every frame; false to let the app idle.")},
        ["enabled"],
    ),
    _tool(
        "ap_bridge_set_idle",
        "Control how long the bridge keeps ArmorPaint awake after a request. ArmorPaint only "
        "runs plugin code while awake, and awake means rendering at full frame rate; the "
        "bridge therefore holds it awake for 'linger' seconds after the last request and then "
        "lets it sleep. The next request wakes it (this server sends the window a synthetic "
        "1-pixel pointer move), which costs one frame. -1 keeps it awake permanently (the old "
        "behaviour). Defaults: 10 s on Windows/Linux, -1 on macOS where waking is untested. "
        "Persisted in the spool across restarts.",
        {"linger": _n("Seconds to stay awake after a request; -1 = never sleep.", minimum=-1, maximum=3600)},
        ["linger"],
    ),
    _tool(
        "ap_console_write",
        "Write a line to ArmorPaint's own console. ap_console_read reads the console back on "
        "a build with the native extension.",
        {
            "text": _s("Message text."),
            "level": _s("Console channel.", enum=["log", "info", "error"], default="log"),
        },
        ["text"],
    ),
    _tool(
        "ap_show_message",
        "Show a message to the user in ArmorPaint: a transient toast by default, or a modal "
        "box. Prefer the toast — a modal blocks ArmorPaint's UI thread, which also stalls "
        "the bridge until the user dismisses it.",
        {
            "text": _s("Message text."),
            "seconds": _n("Toast duration in seconds.", default=4),
            "modal": _b("Show a blocking message box instead of a toast.", default=False),
            "title": _s("Title, modal only.", default="ArmorPaint MCP"),
        },
        ["text"],
    ),
    # ---- project ----------------------------------------------------------
    _tool(
        "ap_project_new",
        "Start a new, empty project. Discards the current project WITHOUT prompting and "
        "without saving — call ap_project_save first if the work matters.",
    ),
    _tool(
        "ap_project_open",
        "Open an existing .arm project file. Discards the current project without prompting.",
        {"path": _s(f"Project .arm file. {_PATH_NOTE}")},
        ["path"],
    ),
    _tool(
        "ap_project_save",
        "Save the current project to its existing path. Fails with code 'no_project' if the "
        "project has never been saved — use ap_project_save_as to give it a path first. THE "
        "SAVE IS DEFERRED: ArmorPaint queues the write for the next frame, so a successful "
        "reply means 'queued', not 'written'. Call ap_fs_stat on the path if you need proof "
        "it landed.",
    ),
    _tool(
        "ap_project_save_as",
        "Set the project file path and save to it (project_filepath_set + project_save). Like "
        "ap_project_save, the write is DEFERRED to the next frame, so the reply confirms the "
        "path was set and the save queued — verify with ap_fs_stat.",
        {"path": _s(f"Destination .arm file. {_PATH_NOTE}")},
        ["path"],
    ),
    _tool(
        "ap_project_get_info",
        "Project-level state: file path, base path, format version, BGRA flag, environment "
        "map name/strength/angle, and camera FOV.",
    ),
    _tool(
        "ap_project_list_texture_assets",
        "List the names of texture assets imported into the project. DEGRADED: "
        "project_t.assets is a snapshot written at save/load, so it is empty before the first "
        "save and does not show textures imported since the last save (the reply says "
        "live=false).",
    ),
    _tool(
        "ap_project_list_scripts",
        "List script assets attached to the project. DEGRADED: project_t.script_datas is a "
        "snapshot written at save/load, so it is empty in a fresh project and stale after "
        "changes made this session.",
    ),
    _tool(
        "ap_quit",
        "Quit ArmorPaint. Unsaved work is LOST — save first. Requires confirm=true. The app "
        "usually exits before it can commit a reply, so a 'no reply' result here is normal "
        "and the response says whether the request was picked up.",
        {"confirm": _b("Must be true. Guards against an accidental shutdown.")},
        ["confirm"],
    ),
    # ---- import / export --------------------------------------------------
    _tool(
        "ap_import_asset",
        "Import a file into the project, dispatched by extension: an image becomes a texture "
        "asset, a mesh replaces/adds paint geometry, a .arm becomes a material.",
        {"path": _s(f"File to import. {_PATH_NOTE}")},
        ["path"],
    ),
    _tool(
        "ap_import_envmap",
        "Import an HDR image as the environment map (script_import_asset with "
        "hdr_as_envmap=1) and report the resulting envmap name.",
        {"path": _s(f"HDR/EXR image. {_PATH_NOTE}")},
        ["path"],
    ),
    _tool(
        "ap_set_envmap_params",
        "Set environment map strength and/or rotation angle (direct writes to project_t).",
        {
            "strength": _n("Envmap strength multiplier, e.g. 1.0.", minimum=0),
            "angle": _n("Envmap rotation in radians."),
        },
    ),
    _tool(
        "ap_export_textures",
        "Export the project's texture channels to real image files on disk, and return the "
        "files found in the directory afterwards; feed one to ap_read_image_file to look at "
        "it. 'directory' is a DIRECTORY, not a filename. With no other arguments this works "
        "on any build and uses ArmorPaint's current settings (8-bit PNG, 'generic' preset, "
        "base name from the last export dialog or 'untitled'). The format / bits / quality / "
        "preset / layers / filename options need the native extension (see "
        "ap_export_presets). NOTE: ArmorPaint exports at the layers' own bit depth, so asking "
        "for 16/32-bit EXR converts the project's layers to that depth first, exactly as the "
        "export dialog's Color setting does.",
        {
            "directory": _s(f"Output directory. {_PATH_NOTE}"),
            "format": _s("Image format.", enum=["png", "jpg", "exr"]),
            "bits": _i("Bit depth: 8 for png/jpg, 16 or 32 for exr.", enum=[8, 16, 32]),
            "quality": _n("JPEG quality 0..100.", minimum=0, maximum=100),
            "preset": _s("Export preset name, e.g. generic, unreal, unity (see ap_export_presets)."),
            "layers": _s("Which layers to export.", enum=list(EXPORT_LAYER_MODES)),
            "filename": _s("Base filename; channel suffixes come from the preset."),
        },
        ["directory"],
    ),
    _tool(
        "ap_export_material_bake",
        "Bake the ACTIVE MATERIAL onto a plane and export the result as images "
        "(export_texture_run with bake_material=1). For mesh map baking (normal, AO, "
        "curvature, ...) see ap_bake.",
        {"directory": _s(f"Output directory. {_PATH_NOTE}")},
        ["directory"],
    ),
    _tool(
        "ap_export_mesh",
        "Export the paint geometry as Wavefront OBJ. The binding appends '.obj' to the path "
        "you give, so pass a path WITHOUT the extension.",
        {"path": _s(f"Destination path without extension. {_PATH_NOTE}")},
        ["path"],
    ),
    _tool(
        "ap_export_material",
        "Export the active material as a reusable .arm material file.",
        {"path": _s(f"Destination .arm path. {_PATH_NOTE}")},
        ["path"],
    ),
    # ---- files ------------------------------------------------------------
    _tool(
        "ap_fs_list",
        "List a directory as seen by the ArmorPaint process (which may be a different "
        "machine/container than this server). Useful for finding what an export actually "
        "wrote.",
        {"path": _s(f"Directory to list. {_PATH_NOTE}")},
        ["path"],
    ),
    _tool(
        "ap_fs_stat",
        "Report whether a path exists, whether it is a directory, and whether ArmorPaint "
        "considers it absolute.",
        {"path": _s(f"Path to test. {_PATH_NOTE}")},
        ["path"],
    ),
    _tool(
        "ap_fs_mkdir",
        "Create a directory (as the ArmorPaint process), e.g. before an export.",
        {"path": _s(f"Directory to create. {_PATH_NOTE}")},
        ["path"],
    ),
    # ---- introspection ----------------------------------------------------
    _tool(
        "ap_get_context",
        "The workhorse read: the live painting context — active tool, brush "
        "radius/opacity/hardness/scale/angle/blending, viewport display mode, x-ray flag, "
        "whether a layer and a material are selected, and the active material's name. Note "
        "that only whether a layer is selected can be reported here; ap_layer_list describes "
        "the layers themselves (native extension).",
    ),
    _tool(
        "ap_get_config",
        "Application preferences that are readable from a plugin: window size/scale, "
        "supersampling, keymap, theme, undo steps, camera FOV, default layer resolution, "
        "live-brush/live-material/node-preview toggles, workspace and workflow, plus the "
        "recent-project and plugin lists. Tone and post-processing (SSAO, bloom, gamma, "
        "contrast, vignette, grain, LUT) are in ap_render_settings.",
    ),
    _tool(
        "ap_set_config",
        "Change application preferences. Only the listed fields are writable; anything else "
        "in ArmorPaint's preferences is not exposed to plugins. layer_res is the DEFAULT "
        "resolution index for new layers — changing it does not resize existing ones "
        "(ap_texture_resolution does).",
        {
            "window_w": _i("Window width in pixels."),
            "window_h": _i("Window height in pixels."),
            "window_scale": _n("UI scale factor, e.g. 1.0 or 1.5."),
            "rp_supersample": _n("Render supersampling factor, e.g. 1.0 or 2.0."),
            "keymap": _s("Keymap preset name."),
            "theme": _s("Theme name."),
            "undo_steps": _i("Undo history depth."),
            "camera_fov": _n("Camera field of view in radians."),
            "layer_res": _i(
                "Default resolution for NEW layers, as ArmorPaint's index: 0=2048, 1=4096, "
                "2=8192, 3=16384. To resize the project's texture set use "
                "ap_texture_resolution."
            ),
            "brush_live": _b("Live brush preview."),
            "node_previews": _b("Node thumbnails in the node editor."),
            "material_live": _b("Live material preview."),
            "workspace": _i("Workspace index."),
            "workflow": _i("Workflow index."),
        },
    ),
    _tool(
        "ap_get_main_object",
        "Describe the main paint object: name, visibility and transform (location, rotation "
        "quaternion, scale).",
    ),
    _tool(
        "ap_get_object",
        "Describe a scene object by name: visibility and transform. Names are matched "
        "against the paint objects. NOTE: there is no binding that ENUMERATES objects — you "
        "can only look one up by name, or use ap_get_main_object.",
        {"name": _s("Object name.")},
        ["name"],
    ),
    # ---- objects & meshes -------------------------------------------------
    _tool(
        "ap_shape_list",
        "List the built-in primitive shapes that ap_shape_add accepts, from the running "
        f"build. Typically: {KNOWN_SHAPES}.",
    ),
    _tool(
        "ap_shape_add",
        "Add a built-in primitive to the scene (cube, sphere, plane, cylinder, cone, torus, "
        "the bevelled/shared-UV cube variants, the 2048-poly plane/sphere, or 'empty'). Use "
        "ap_shape_list for this build's exact names.",
        {"name": _s(f"Shape name. Typically one of: {KNOWN_SHAPES}.")},
        ["name"],
    ),
    _tool(
        "ap_object_duplicate",
        "Duplicate a scene object by name and return the new object's name.",
        {"name": _s("Name of the object to duplicate.")},
        ["name"],
    ),
    _tool(
        "ap_object_set_transform",
        "Set an object's location, rotation and/or scale, then rebuild its matrix. Rotation "
        "is given as XYZ Euler angles in DEGREES and converted to the quaternion the engine "
        "wants. Omitted components are left alone. ArmorPaint's world is Z-up.",
        {
            "name": _s("Object name."),
            "location": {
                "type": "array",
                "items": {"type": "number"},
                "minItems": 3,
                "maxItems": 3,
                "description": "World location [x, y, z].",
            },
            "rotation_euler_degrees": {
                "type": "array",
                "items": {"type": "number"},
                "minItems": 3,
                "maxItems": 3,
                "description": "XYZ Euler rotation in degrees.",
            },
            "scale": {
                "type": "array",
                "items": {"type": "number"},
                "minItems": 3,
                "maxItems": 3,
                "description": "Scale [x, y, z].",
            },
        },
        ["name"],
    ),
    _tool(
        "ap_object_set_visible",
        "Show or hide a scene object. This is the object's visible flag: a hidden object "
        "stays in the project with its materials intact, it just stops rendering.",
        {"name": _s("Object name."), "visible": _b("True to show, false to hide.")},
        ["name", "visible"],
    ),
    _tool(
        "ap_append_mesh",
        "Append geometry to the current project, either from a mesh file on disk or from "
        "inline Wavefront OBJ text. Give exactly one of 'path' or 'obj_data'. Pass inline "
        "OBJ with ordinary newlines — they are re-encoded for the wire and restored inside "
        "ArmorPaint — but it must contain no double quotes, backslashes or '|' (the "
        "bridge's JSON parser does not decode escapes and '|' is the line separator), and "
        "the whole request is capped at 8 KB. Prefer 'path' for anything non-trivial.",
        {
            "path": _s(f"Mesh file to append. {_PATH_NOTE}"),
            "obj_data": _s("Wavefront OBJ text to append inline."),
        },
    ),
    # ---- materials --------------------------------------------------------
    _tool(
        "ap_material_get_active",
        "Report the active material: its name (a material's name is its node-canvas name) "
        "and its nine per-channel paint flags.",
    ),
    _tool(
        "ap_material_create",
        "Create a new material slot and make it active.",
        {"name": _s("Name for the new material.")},
        ["name"],
    ),
    _tool(
        "ap_material_select",
        "Make an existing material active, by name.",
        {"name": _s("Material name.")},
        ["name"],
    ),
    _tool(
        "ap_material_delete",
        "Delete a material slot by name. It pushes an undo step, so ap_undo takes it back.",
        {"name": _s("Material name.")},
        ["name"],
    ),
    _tool(
        "ap_material_assign",
        "Assign an existing material to a scene object. Both are looked up by name, and a "
        "material's name is its node-canvas name (what ap_material_get_active reports).",
        {"object": _s("Object name."), "material": _s("Material name.")},
        ["object", "material"],
    ),
    _tool(
        "ap_material_set_channels",
        "Enable or disable which channels the active material paints into. These are the "
        "material's own paint_* flags, not texture-set channels (ArmorPaint's plugin API has "
        "no channel add/remove). Omitted channels are left alone.",
        {name: _b(f"Paint into the {name} channel.") for name in PAINT_CHANNELS},
    ),
    _tool(
        "ap_material_list",
        "List material names. With the native extension the list is LIVE (every material "
        "in the project right now, and which is active). On a stock build it falls back to "
        "project_t.material_nodes, a snapshot written only at save/load: empty before the "
        "first save and blind to materials created since; the reply says live=true/false.",
    ),
    _tool(
        "ap_material_update",
        "Recompile the active material after node edits (script_material_update). THIS DOES "
        "NOT CHANGE THE VIEWPORT. ArmorPaint renders the layer stack, and the node graph is "
        "only the paint SOURCE — measured: viewport captures before and after a colour change "
        "plus ap_material_update are byte-identical. To make a graph edit visible you must "
        "apply it: ap_fill_layer (whole layer) or ap_paint_stroke / ap_paint_stroke_world "
        "(where you paint). Call this once after a batch of ap_node_* edits, then apply.",
    ),
    # ---- material nodes ---------------------------------------------------
    _tool(
        "ap_node_list",
        "Read the active material's node graph: every node's id, name, type and canvas "
        "position, its input/output socket names, and every link (from_id/from_socket -> "
        "to_id/to_socket). This is the richest and best-tested part of the API — node work "
        "is where an agent has the most real leverage in ArmorPaint.",
    ),
    _tool(
        "ap_node_add",
        "Add a node to the active material's graph at a canvas position, and return its new "
        "id. Valid types: " + " ".join(sorted(NODE_TYPES)) + ".",
        {
            "type": _s("Node type string, e.g. TEX_NOISE, MIX_RGB, RGB, MATH."),
            "x": _n("Canvas x position.", default=0),
            "y": _n("Canvas y position.", default=0),
        },
        ["type"],
    ),
    _tool(
        "ap_node_remove",
        "Remove a node from the active material's graph by id (from ap_node_list).",
        {"id": _i("Node id.")},
        ["id"],
    ),
    _tool(
        "ap_node_get",
        "Describe one node of the active material: every input socket, output socket and "
        "button as 'index:name:type=default_values;' (e.g. '4:Scale:VALUE=5;'). Use it "
        "before ap_node_set_value / ap_node_connect instead of guessing socket indices. "
        "ap_node_add returns the same tables for the node it creates.",
        {"id": _i("Node id (from ap_node_list or ap_node_add).")},
        ["id"],
    ),
    _tool(
        "ap_node_connect",
        "Link one node's output socket to another node's input socket. Socket numbers are "
        "0-based positions, as reported by ap_node_list; the output node's material socket "
        "order is Base Color, Opacity, Occlusion, Roughness, Metallic, Normal Map, Emission, "
        "Height, Subsurface.",
        {
            "from_id": _i("Source node id."),
            "from_socket": _i("Source output socket index.", default=0),
            "to_id": _i("Destination node id."),
            "to_socket": _i("Destination input socket index.", default=0),
        },
        ["from_id", "to_id"],
    ),
    _tool(
        "ap_node_disconnect",
        "Remove whatever is linked into one input socket of a node.",
        {"to_id": _i("Destination node id."), "to_socket": _i("Input socket index.")},
        ["to_id", "to_socket"],
    ),
    _tool(
        "ap_node_set_value",
        "Set a value on a node. Four kinds: 'float' (one number on a socket), 'color' "
        "(r,g,b,a on a socket), 'vector' (x,y,z on a socket), and 'button' (a node's own "
        "widget — dropdown index, checkbox 0/1, or a slider value; see the node reference "
        "for each type's button list). Socket-based kinds default to the node's INPUT "
        "sockets. Follow a batch of edits with ap_material_update.",
        {
            "id": _i("Node id from ap_node_list."),
            "kind": _s("What to set.", enum=["float", "color", "vector", "button"]),
            "socket": _i("Socket index for float/color/vector kinds.", default=0),
            "is_input": _b("Target an input socket (true) or an output socket.", default=True),
            "value": _n("The number, for kind 'float' or 'button'."),
            "color": {
                "type": "array",
                "items": {"type": "number"},
                "minItems": 3,
                "maxItems": 4,
                "description": "RGBA (or RGB, alpha defaults to 1) in 0..1, for kind 'color'.",
            },
            "vector": {
                "type": "array",
                "items": {"type": "number"},
                "minItems": 3,
                "maxItems": 3,
                "description": "[x, y, z] for kind 'vector'.",
            },
            "button": _i("Button index, for kind 'button'."),
        },
        ["id", "kind"],
    ),
    # ---- whole graphs --------------------------------------------------------
    _tool(
        "ap_node_graph_get",
        "The active material's WHOLE node graph in one call: every node with its input and "
        "output sockets BY NAME and their current values, every button, and every link with "
        "both ends named. Use it instead of ap_node_list + one ap_node_get per node.",
    ),
    _tool(
        "ap_node_graph_apply",
        "Build or change the active material's graph from a declarative spec, as one "
        "operation. The spec names nodes with your own keys and sockets BY NAME, so no ids or "
        "socket indices are needed: {\"nodes\": {\"noise\": {\"type\": \"TEX_NOISE\", "
        "\"inputs\": {\"Scale\": 4}}, \"mix\": {\"type\": \"MIX_RGB\", \"buttons\": "
        "{\"blend_type\": \"Multiply\"}}, \"out\": {\"existing\": \"OUTPUT_MATERIAL_PBR\"}}, "
        "\"links\": [\"noise.Color -> mix.Color 2\", \"mix.Color -> out.Base Color\"]}. "
        "A node is new ({type, optional x/y}), or an existing one ({existing: TYPE} for the "
        "first of that type, or {id: N}). 'inputs'/'outputs' set socket values (a number, or "
        "[r,g,b(,a)] / [x,y,z]); 'buttons' set dropdowns by option name, checkboxes by "
        "true/false, sliders by number. Where several sockets share a name, write "
        "'Value[1]' (the second) or '#1' (index). Everything is validated against the node "
        "catalogue BEFORE anything changes; then nodes are added, values set and links made "
        "in batches, the material is recompiled, and (fill=true, the default) the selected "
        "layer is filled so the result is visible. If any step fails the graph is put back "
        "exactly as it was. The previous graph is kept as a snapshot (snapshot_id) for "
        "ap_node_graph_restore. Missing positions are laid out automatically.",
        {
            "spec": {"type": "object", "description": "The graph spec (see above)."},
            "mode": _s("'merge' adds to the graph; 'replace' first removes every node except "
                       "the output and nodes the spec refers to.", enum=["merge", "replace"],
                       default="merge"),
            "dry_run": _b("Validate and return the plan without changing anything.", default=False),
            "fill": _b("Fill the selected layer afterwards so the change shows.", default=True),
            "label": _s("Label for the automatic before-snapshot."),
        },
        ["spec"],
    ),
    _tool(
        "ap_node_graph_lint",
        "Check the active material's graph for problems that otherwise only show as a wrong "
        "render: link cycles, nodes that feed nothing reaching the output, links into paint "
        "channels the material has switched off, and socket type conversions.",
    ),
    _tool(
        "ap_node_graph_snapshot",
        "Save the active material's graph so it can be put back with ap_node_graph_restore. "
        "ArmorPaint's undo history does not record node edits, so this is how a graph edit "
        "is taken back. With list=true, list the saved snapshots instead.",
        {"label": _s("A label to find it by."), "list": _b("List snapshots instead.", default=False)},
    ),
    _tool(
        "ap_node_graph_restore",
        "Put the active material's graph back to a snapshot: removes nodes added since, "
        "re-creates removed ones (they get new ids; id_map says which), resets socket and "
        "button values and relinks. Node positions and custom node names cannot be written "
        "by the bridge and stay as they are.",
        {
            "snapshot_id": _s("From ap_node_graph_snapshot or ap_node_graph_apply."),
            "force": _b("Restore even if a different material is now active.", default=False),
        },
        ["snapshot_id"],
    ),
    _tool(
        "ap_node_recipe",
        "Ready-made material graphs with parameters (worn_painted_metal, painted_wood, stone, "
        "edge_wear_grunge, ...). No name: list them with their parameters. With a name: "
        "return the filled-in spec, or with apply=true build it (as ap_node_graph_apply).",
        {
            "name": _s("Recipe name."),
            "params": {"type": "object", "description": "Parameter values; defaults for the rest."},
            "apply": _b("Build it into the active material.", default=False),
            "mode": _s("As ap_node_graph_apply.", enum=["merge", "replace"], default="replace"),
            "fill": _b("As ap_node_graph_apply.", default=True),
            "dry_run": _b("As ap_node_graph_apply.", default=False),
        },
    ),
    # ---- painting & viewport ---------------------------------------------
    _tool(
        "ap_select_tool",
        "Select the active tool: " + ", ".join(sorted(TOOL_TYPES)) + ". The selection is "
        "read back from the context so the result confirms it took effect.",
        {"tool": _s("Tool name.", enum=sorted(TOOL_TYPES))},
        ["tool"],
    ),
    _tool(
        "ap_set_brush",
        "Set brush parameters on the painting context. Omitted values are left alone. These "
        "are direct context writes, so they take effect immediately for the next stroke.",
        {
            "radius": _n("Brush radius (context units, typically 0..2).", minimum=0),
            "opacity": _n("Brush opacity 0..1.", minimum=0, maximum=1),
            "hardness": _n("Brush hardness 0..1.", minimum=0, maximum=1),
            "scale": _n("Brush pattern scale.", minimum=0),
            "angle": _n("Brush angle in degrees."),
            "blending": _i("Blend mode index."),
        },
    ),
    _tool(
        "ap_paint_stroke",
        "Paint a stroke in SCREEN space and close it. Coordinates are normalised (0..1) "
        "across the viewport, so 0.5,0.5 is the centre and what gets painted depends on the "
        "current camera. Paints into the selected layer with the active tool/brush; silently "
        "does nothing if no project is open, no layer is selected, or the selected layer is "
        f"a group. At most {MAX_STROKE_POINTS} points — the whole stroke runs inside one "
        "frame.",
        {
            "points": {
                "type": "array",
                "items": {
                    "type": "array",
                    "items": {"type": "number"},
                    "minItems": 2,
                    "maxItems": 2,
                },
                "description": "Stroke path as [[x, y], ...] in normalised 0..1 screen space.",
            }
        },
        ["points"],
    ),
    _tool(
        "ap_paint_stroke_world",
        "Paint a stroke in WORLD space and close it — camera-independent, which makes it the "
        "reliable choice for scripted painting. ArmorPaint's world is Z-up; use "
        "ap_get_main_object for the object's bounds.",
        {
            "points": {
                "type": "array",
                "items": {
                    "type": "array",
                    "items": {"type": "number"},
                    "minItems": 3,
                    "maxItems": 3,
                },
                "description": "Stroke path as [[x, y, z], ...] in world space.",
            }
        },
        ["points"],
    ),
    _tool(
        "ap_fill_layer",
        "Fill the selected layer with the active material and push an undo step. THIS IS THE "
        "STEP THAT MAKES A NODE-GRAPH EDIT VISIBLE — ap_material_update only recompiles the "
        "material; nothing appears in the viewport until you fill or paint. The working loop "
        "is: ap_node_* edits -> ap_material_update -> ap_fill_layer -> ap_capture_window. "
        "The first fill after ap_material_update is repeated automatically on the next "
        "frame (reply: refill_next_frame=true): on its own it leaves the viewport showing "
        "the PREVIOUS material about half the time, and ArmorPaint's own node editor also "
        "re-fills after recompiling. That costs one extra undo step. "
        "Fails with 'no_project'/'bad_args' if no layer is selected. Fills whichever layer is "
        "selected: pick one with ap_layer_select, or make a dedicated fill layer with "
        "ap_layer_new(kind='fill').",
    ),
    _tool(
        "ap_set_display_channel",
        "Set what the 3D viewport displays: " + ", ".join(sorted(VIEWPORT_MODES)) + ". "
        "'lit' is the normal shaded view; the others isolate a channel or a debug output.",
        {"mode": _s("Display mode name.", enum=sorted(VIEWPORT_MODES))},
        ["mode"],
    ),
    _tool(
        "ap_capture_to_project",
        "Capture the 3D viewport into the project as a packed texture asset. The pixels land "
        "INSIDE the project (persisted only when the .arm is saved), so you cannot look at "
        "the result — to see the viewport use ap_capture_window (any build) or "
        "ap_capture_viewport. NOTE: the bridge handler runs inline in one frame and "
        "cannot wait for a re-render, so the capture is of the frame ALREADY drawn and may "
        "include the UI overlay; ArmorPaint's own two-frame settle is not reproducible from "
        "a plugin.",
        {
            "width": _i("Capture width in pixels.", default=1024),
            "height": _i("Capture height in pixels.", default=1024),
        },
    ),
    _tool(
        "ap_capture_viewport",
        "Capture ONLY the shaded 3D viewport (no UI) to a PNG file and return the image. "
        "Works on builds that export viewport_save_texture_to_file (upstream since "
        "2026-09-09) or carry the native extension — the bridge detects either on first use. "
        "On an older stock build this answers 'unsupported'; ap_capture_window works on any "
        "build.",
        {
            "path": _s(f"Destination .png file. {_PATH_NOTE}"),
            "width": _i("Capture width in pixels.", default=1024),
            "height": _i("Capture height in pixels.", default=1024),
            "include_image": _b(
                "Return the PNG inline as an image. Set false to get just the path and keep "
                "the response small.",
                default=True,
            ),
        },
        ["path"],
    ),
    _tool(
        "ap_capture_window",
        "Screenshot ArmorPaint's window and return it as an image — the rendered, shaded "
        "result plus the UI around it (layers, materials, node editor). Works on a STOCK "
        "build: the server reads the window's own pixels from outside the app (Linux: X11 "
        "XGetImage; Windows: PrintWindow; macOS: screencapture, which needs the Screen "
        "Recording permission), so it works while the window is covered by others and never "
        "steals focus; only a minimised window fails. Coordinates in the image are the ones "
        "ap_ui_click / ap_ui_drag take (before any downscale). By "
        "default it first round-trips a ping through the bridge so the frame showing your "
        "last edit has been drawn. The 3D viewport's position is not exposed to plugins, so "
        "to isolate it: capture once uncropped, read off the viewport rectangle, then pass "
        "it as 'crop'. Answered locally; works even if the bridge is off.",
        {
            "crop": {
                "type": "array",
                "items": {"type": "integer"},
                "minItems": 4,
                "maxItems": 4,
                "description": "[x, y, width, height] in window pixels; clamped to the window.",
            },
            "downscale": _i(
                f"Keep every Nth pixel (1..{MAX_DOWNSCALE}) to shrink the response.",
                default=1,
            ),
            "path": _s(f"Also save the PNG here (optional). {_PATH_NOTE}"),
            "settle": _b(
                "Ping the bridge first so the latest edit has rendered. Skipped automatically "
                "if the bridge is not running.",
                default=True,
            ),
            "settle_frames": _i("How many frames to wait when settling (a fill needs 3).",
                                minimum=1, maximum=30, default=3),
            "include_image": _b(
                "Return the PNG inline. Set false (with 'path') to keep the response small.",
                default=True,
            ),
            "diff_against": _s(
                "Compare with an earlier capture: 'last', or a capture_id from an earlier "
                "reply (the server keeps the last 8). Adds the changed-pixel bounding box and "
                "fraction, and a second image zoomed on the change. Both captures need the "
                "same crop and downscale."
            ),
        },
    ),
    _tool(
        "ap_read_image_file",
        "Read an image file from THIS server's filesystem and return it as an image, so you "
        "can look at what ArmorPaint exported. Answered locally — no round trip, and it "
        "works while ArmorPaint is closed. Pair it with ap_export_textures: export, then "
        f"read one of the reported files. PNG/JPEG/GIF/WEBP/BMP only (EXR cannot be "
        f"displayed), up to {MAX_IMAGE_BYTES // 1_000_000} MB.",
        {
            "path": _s(f"Image file to read. {_PATH_NOTE}"),
            "max_bytes": _i(
                f"Refuse files larger than this. Default and ceiling {MAX_IMAGE_BYTES}."
            ),
        },
        ["path"],
    ),
]

_LAYER_TARGET = {
    "index": _i("Target layer by stack index (0 = bottom; see ap_layer_list)."),
    "name": _s("Target layer by name (first match)."),
    "layer_id": _i("Target layer by its id."),
}
_TARGET_NOTE = " Target by index, name or layer_id; with none, the selected layer."
_POINT_LIST = {
    "type": "array",
    "items": {"type": "array", "items": {"type": "integer"}, "minItems": 2, "maxItems": 2},
    "minItems": 2,
    "maxItems": 64,
    "description": "Path as [[x, y], ...] in window pixels.",
}
_MODIFIERS = {
    "type": "array",
    "items": {"type": "string", "enum": ["ctrl", "shift", "alt"]},
    "description": "Modifier keys held during the action.",
}

TOOLS += [
    # ---- batching -----------------------------------------------------------
    _tool(
        "ap_batch",
        "Run several bridge tools as ONE request, in order, and get every result back "
        "together. ArmorPaint runs plugin code inside its render loop, so one request costs "
        "at least one frame; a batch runs several light steps per frame (bounded by the "
        "plugin's per-frame budget), and GPU-heavy steps (fills, strokes, layer changes) one "
        "per frame. Heavy steps (exports, saves, opens, imports, bakes) additionally wait "
        "until no mouse button is held in the app. A failing step does not stop the batch "
        "unless stop_on_error is set. Any bridge tool can be a step except ap_batch and "
        "ap_quit; tools answered by this server (capture_window, ui_*, read_image_file, "
        "resource_search, bridge_status, project_metadata) cannot.",
        {
            "steps": {
                "type": "array",
                "minItems": 1,
                "maxItems": 64,
                "items": {
                    "type": "object",
                    "properties": {
                        "tool": {"type": "string", "description": "Tool name, e.g. ap_node_add."},
                        "args": {"type": "object", "description": "That tool's arguments."},
                    },
                    "required": ["tool"],
                },
                "description": "Steps to run in order.",
            },
            "stop_on_error": _b("Skip the remaining steps after the first failure.", default=False),
        },
        ["steps"],
    ),
    # ---- layers (native extension) -------------------------------------------
    _tool(
        "ap_layer_list",
        "List the layer stack: index (0 = BOTTOM; the Layers panel shows the highest index "
        "at the top), id, name, kind (layer/mask/group/filter), selected, visible, opacity, "
        "blending, parent index, whether it is a fill layer and with which material, object "
        "mask, scale, angle, UV type. Needs the native extension.",
    ),
    _tool(
        "ap_layer_select",
        "Make a layer the selected one — the layer that ap_fill_layer and ap_paint_stroke "
        "act on." + _TARGET_NOTE,
        dict(_LAYER_TARGET),
    ),
    _tool(
        "ap_layer_new",
        "Create a layer exactly as the Layers panel's New menu does, with its undo step: "
        "paint, fill (filled with the active material), decal, group (wraps the selected "
        "layer), or a black/white/fill mask on the selected layer. The new layer becomes the "
        "selection.",
        {
            "kind": _s("Layer kind.", enum=list(LAYER_KINDS), default="paint"),
            "name": _s("Optional name for the new layer."),
        },
    ),
    _tool(
        "ap_layer_delete",
        "Delete a layer (with its masks, as the panel does). ArmorPaint refuses to delete "
        "the last paint layer; the reply says so." + _TARGET_NOTE,
        dict(_LAYER_TARGET),
    ),
    _tool(
        "ap_layer_duplicate",
        "Duplicate a layer; the copy becomes the selection." + _TARGET_NOTE,
        dict(_LAYER_TARGET),
    ),
    _tool(
        "ap_layer_set",
        "Change a layer's properties; each change pushes the same undo step the panel "
        "would. Omitted properties are left alone." + _TARGET_NOTE,
        {
            **_LAYER_TARGET,
            "new_name": _s("Rename the layer."),
            "opacity": _n("Opacity 0..1.", minimum=0, maximum=1),
            "blending": _s("Blend mode.", enum=list(BLEND_MODES)),
            "visible": _b("Show or hide the layer."),
            "object_mask": _i("Restrict to one paint object (1-based index; 0 = all objects)."),
            "scale": _n("Fill/decal texture scale."),
            "angle": _n("Fill/decal texture angle."),
        },
    ),
    _tool(
        "ap_layer_move",
        "Move a layer to another stack position (0 = bottom), with ArmorPaint's own rules: "
        "groups do not nest, masks and filters must sit above a layer, a layer's masks move "
        "with it." + _TARGET_NOTE,
        {**_LAYER_TARGET, "to_index": _i("Destination stack index.")},
        ["to_index"],
    ),
    _tool(
        "ap_layer_action",
        "Layer context-menu actions: clear (paint layers/masks), merge_down, merge_group, "
        "to_fill, to_paint, apply_mask, invert_mask." + _TARGET_NOTE,
        {**_LAYER_TARGET, "action": _s("What to do.", enum=list(LAYER_ACTIONS))},
        ["action"],
    ),
    # ---- history ---------------------------------------------------------------
    _tool(
        "ap_undo",
        "Undo the last step(s) of ArmorPaint's own history: paint strokes, fills, layer "
        "changes and material create/delete. Material NODE edits are NOT in that history "
        "(ArmorPaint's node API records no undo step), and neither are config, camera or "
        "mesh changes: to take back a graph edit, restore a graph snapshot "
        "(ap_node_graph_snapshot / ap_node_graph_restore; ap_node_graph_apply keeps one "
        "automatically). Uses the native extension (exact, and reports the history); on a "
        "stock build it presses the app's own undo shortcut (ctrl+z) through synthetic input "
        "instead, which cannot report what was undone.",
        {"steps": _i("How many steps.", minimum=1, maximum=64, default=1)},
    ),
    _tool(
        "ap_redo",
        "Redo undone step(s) of ArmorPaint's history, which does NOT include node edits (see "
        "ap_undo). Native extension, or the ctrl+shift+z shortcut on a stock build.",
        {"steps": _i("How many steps.", minimum=1, maximum=64, default=1)},
    ),
    _tool(
        "ap_history",
        "The undo history: the last 32 step names, which are undone, and how many "
        "undos/redos are available. Node edits never appear in it (see ap_undo). Needs the "
        "native extension.",
    ),
    # ---- export / bake / render ------------------------------------------------
    _tool(
        "ap_export_presets",
        "List ArmorPaint's texture export presets (generic, unreal, unity, ...) and the "
        "active one. Needs the native extension.",
    ),
    _tool(
        "ap_bake",
        "Bake a mesh map into a Bake Texture node (add one first: ap_node_add type "
        "TEX_BAKE), exactly as the node's Bake button does: curvature, normal, normal_object, "
        "height, derivative, position, texcoord, material_id, object_id, vertex_color, and — "
        "with hardware ray tracing — occlusion, lightmap, bent_normal, thickness. Parameters "
        "given here are applied first. Baking runs over the following frames: poll "
        "ap_bake_status until baking=false. Needs the native extension.",
        {
            "node_id": _i("Id of a TEX_BAKE node in the active material."),
            "type": _s("What to bake.", enum=list(BAKE_TYPES)),
            "samples": _i("Ray-traced bake samples (occlusion/lightmap/bent_normal/thickness).", minimum=1, maximum=4096),
            "axis": _i("Bake axis index."),
            "up_axis": _i("Up axis index (object normal / position / bent normal)."),
            "ao_strength": _n("Occlusion strength."),
            "ao_radius": _n("Occlusion radius."),
            "ao_offset": _n("Occlusion offset."),
            "curv_strength": _n("Curvature strength."),
            "curv_radius": _n("Curvature radius."),
            "curv_offset": _n("Curvature offset."),
            "curv_smooth": _i("Curvature smoothing passes."),
            "high_poly": _i("High-poly source object index for normal/height baking."),
        },
        ["node_id", "type"],
    ),
    _tool(
        "ap_bake_status",
        "Whether a bake is still running, its progress, and the current bake parameters. "
        "Needs the native extension.",
    ),
    _tool(
        "ap_bake_settings",
        "Read, and optionally change, the bake parameters without starting a bake. Needs "
        "the native extension.",
        {
            **{
                "samples": _i("Ray-traced bake samples.", minimum=1, maximum=4096),
                "axis": _i("Bake axis index."),
                "up_axis": _i("Up axis index."),
                "ao_strength": _n("Occlusion strength."),
                "ao_radius": _n("Occlusion radius."),
                "ao_offset": _n("Occlusion offset."),
                "curv_strength": _n("Curvature strength."),
                "curv_radius": _n("Curvature radius."),
                "curv_offset": _n("Curvature offset."),
                "curv_smooth": _i("Curvature smoothing passes."),
                "high_poly": _i("High-poly source object index."),
            }
        },
    ),
    _tool(
        "ap_render_settings",
        "Read, and optionally change, the viewport's tone and post-processing: SSAO, bloom, "
        "contrast, gamma, vignette, grain, supersampling, a .cube colour LUT, texture "
        "filtering and the camera clip range — the Preferences > Viewport settings. Saved to "
        "ArmorPaint's config like the UI does. Needs the native extension.",
        {
            "ssao": _n("0..1", minimum=0, maximum=1),
            "bloom": _n("0..1", minimum=0, maximum=1),
            "contrast": _n("0..2", minimum=0, maximum=2),
            "gamma": _n("0..2", minimum=0, maximum=2),
            "vignette": _n("0..1", minimum=0, maximum=1),
            "grain": _n("0..1", minimum=0, maximum=1),
            "supersample": _n("Render scale: 0.25, 0.5, 1, 1.5, 2 or 4."),
            "lut_path": _s(f"A .cube LUT file, or '' to clear it. {_PATH_NOTE}"),
            "texture_filter": _b("Linear texture filtering."),
            "clip_start": _n("Camera near clip."),
            "clip_end": _n("Camera far clip."),
            "render_mode": _i("0 = deferred, 1 = forward."),
        },
    ),
    _tool(
        "ap_texture_resolution",
        "Read, and optionally change, the texture-set resolution (every layer is resized, "
        "as the Resolution setting in the Layers panel does). Needs the native extension.",
        {"size": _i("New resolution.", enum=[2048, 4096, 8192, 16384])},
    ),
    _tool(
        "ap_project_lists",
        "LIVE lists of everything in the project right now: materials (and which is active), "
        "imported textures, brushes, fonts and paint objects. Unlike the save/load snapshots "
        "a stock plugin can read, these include this session's changes. Needs the native "
        "extension.",
    ),
    _tool(
        "ap_camera",
        "Move the viewport camera: a preset view (front, back, left, right, top, bottom, "
        "reset), an orbit, a zoom, or a new field of view. Returns the camera pose. Pair it "
        "with ap_capture_window / ap_capture_viewport to inspect the model from every side. "
        "Needs the native extension.",
        {
            "view": _s("Preset view.", enum=list(CAMERA_VIEWS)),
            "orbit_x": _n("Orbit around the vertical axis, radians."),
            "orbit_y": _n("Orbit up/down, radians."),
            "zoom": _n("Zoom step (positive = in)."),
            "fov": _n("Field of view, radians."),
        },
    ),
    _tool(
        "ap_console_read",
        "Read back ArmorPaint's console (its last 100 lines): plugin compile/run errors, "
        "import warnings, and whatever ap_console_write left. Needs the native extension.",
        {"max_lines": _i("How many of the latest lines.", minimum=1, maximum=100, default=50)},
    ),
    # ---- UI automation (answered by this server) -------------------------------
    _tool(
        "ap_ui_click",
        "Click in ArmorPaint's window, as a person would — for the parts of the app no "
        "binding reaches (menus, dialogs, panel buttons). Coordinates are window pixels as "
        "in ap_capture_window's image (multiply by its downscale). Delivered to the window "
        "itself: the real pointer does not move and focus is not stolen. Always look first "
        "(ap_capture_window), then click, then look again. Linux (X11/XWayland) verified; "
        "Windows implemented; macOS implemented, untested, and may need Accessibility "
        "permission.",
        {
            "x": _i("Window x."),
            "y": _i("Window y."),
            "button": _s("Mouse button.", enum=["left", "right", "middle"], default="left"),
            "double": _b("Double-click.", default=False),
            "modifiers": _MODIFIERS,
        },
        ["x", "y"],
    ),
    _tool(
        "ap_ui_key",
        "Press a key or shortcut in ArmorPaint (e.g. key 'z' with modifiers ['ctrl']). Keys: "
        "a-z, 0-9, f1-f12, enter, escape, tab, space, backspace, delete, arrows, home, end, "
        "pageup, pagedown. Delivered to the window without focusing it. See ap_ui_click for "
        "platform notes.",
        {"key": _s("Key name."), "modifiers": _MODIFIERS},
        ["key"],
    ),
    _tool(
        "ap_ui_drag",
        "Drag with a mouse button held along a path in window pixels: sliders, node wires, "
        "panel splitters, or painting by hand. See ap_ui_click for coordinates and platform "
        "notes.",
        {
            "points": _POINT_LIST,
            "button": _s("Mouse button.", enum=["left", "right", "middle"], default="left"),
            "modifiers": _MODIFIERS,
        },
        ["points"],
    ),
    _tool(
        "ap_ui_scroll",
        "Scroll the mouse wheel at a window position (zooms the viewport, scrolls panels). "
        "Positive clicks scroll down / zoom out.",
        {"x": _i("Window x."), "y": _i("Window y."), "clicks": _i("Wheel clicks, -50..50.")},
        ["x", "y", "clicks"],
    ),
    # ---- resources & metadata (answered by this server) ------------------------
    _tool(
        "ap_resource_search",
        "Search for resources to use in ArmorPaint — textures, envmaps, meshes, .arm "
        "materials, fonts, LUTs, export presets — by name, across ArmorPaint's own data "
        "folder, the open project's folder, the folders in $ARMORPAINT_LIBRARY and any "
        "'roots' you pass. Returns paths plus the tool that imports each kind. Answered "
        "locally from this server's filesystem.",
        {
            "query": _s("Words that must all appear in the file's path, e.g. 'rust metal'. Empty lists everything."),
            "kinds": {
                "type": "array",
                "items": {"type": "string", "enum": sorted(local_tools.KINDS)},
                "description": "Limit to these kinds.",
            },
            "roots": {"type": "array", "items": {"type": "string"}, "description": "Extra folders to search."},
            "max_results": _i("Cap on returned matches.", minimum=1, maximum=500, default=50),
        },
    ),
    _tool(
        "ap_project_metadata",
        "Read and edit metadata kept WITH a project — notes, a material brief, texture "
        "budgets, anything worth remembering between sessions — as a JSON sidecar next to "
        "the .arm (<project>.arm.mcp.json); the .arm itself is never modified. With no "
        "'set'/'remove' it just reads. The project must have been saved (it needs a path).",
        {
            "set": {"type": "object", "description": "Keys to add or overwrite (any JSON values)."},
            "remove": {"type": "array", "items": {"type": "string"}, "description": "Keys to delete."},
            "project_path": _s(f"Use this .arm instead of the open project. {_PATH_NOTE}"),
        },
    ),
]

TOOL_NAMES = {tool.name for tool in TOOLS}


# ---------------------------------------------------------------------------
# Tool arguments -> wire arguments
# ---------------------------------------------------------------------------


def _build_wire_args(name: str, a: dict[str, Any]) -> dict[str, Any]:
    """Validate one tool call and reduce it to flat scalar wire arguments.

    Every value that reaches the bridge passes through here first. The plugin has no
    exceptions and unchecked pointer dereference, so a malformed request must be stopped on
    this side of the mailbox.
    """
    if name in ("ap_ping", "ap_get_app_info", "ap_project_new", "ap_project_save"):
        return {}
    if name in (
        "ap_project_get_info",
        "ap_project_list_texture_assets",
        "ap_project_list_scripts",
        "ap_get_context",
        "ap_get_config",
        "ap_get_main_object",
        "ap_shape_list",
        "ap_material_get_active",
        "ap_material_list",
        "ap_material_update",
        "ap_node_list",
        "ap_fill_layer",
    ):
        return {}

    if name == "ap_bridge_set_enabled":
        enabled = _opt_bool(a, "enabled")
        if enabled is None:
            raise BadArgs("'enabled' is required.", arg="enabled")
        return {"enabled": enabled}

    if name == "ap_console_write":
        level = (_opt_str(a, "level") or "log").strip().lower()
        if level not in ("log", "info", "error"):
            raise BadArgs("'level' must be one of log, info, error.", arg="level")
        return {"text": _req_str(a, "text"), "level": level}

    if name == "ap_show_message":
        modal = _opt_bool(a, "modal") or False
        out: dict[str, Any] = {"text": _req_str(a, "text"), "modal": modal}
        if modal:
            out["title"] = _opt_str(a, "title") or "ArmorPaint MCP"
        else:
            out["seconds"] = _opt_float(a, "seconds", 0.1, 120.0) or 4.0
        return out

    if name in ("ap_project_open", "ap_project_save_as", "ap_import_asset", "ap_import_envmap"):
        return {"path": _norm_path(a, "path")}

    if name in ("ap_export_mesh", "ap_export_material"):
        return {"path": _norm_path(a, "path")}

    if name in ("ap_fs_list", "ap_fs_stat", "ap_fs_mkdir"):
        return {"path": _norm_path(a, "path")}

    if name == "ap_export_material_bake":
        return {"directory": _norm_path(a, "directory")}

    if name == "ap_export_textures":
        out = {"directory": _norm_path(a, "directory")}
        fmt = _opt_str(a, "format")
        if fmt is not None:
            fmt = fmt.strip().lower()
            if fmt not in ("png", "jpg", "exr"):
                raise BadArgs("'format' must be png, jpg or exr.", arg="format")
        bits = _opt_int(a, "bits")
        if bits is not None and bits not in (8, 16, 32):
            raise BadArgs("'bits' must be 8, 16 or 32.", arg="bits")
        if bits in (16, 32) and fmt not in (None, "exr"):
            raise BadArgs("16- and 32-bit export is EXR only; png and jpg are 8-bit.", arg="bits")
        if bits in (16, 32) and fmt is None:
            fmt = "exr"
        if fmt == "exr" and bits == 8:
            raise BadArgs("EXR export is 16 or 32 bits.", arg="bits")
        layers = _opt_str(a, "layers")
        if layers is not None and layers not in EXPORT_LAYER_MODES:
            raise BadArgs(f"'layers' must be one of {', '.join(EXPORT_LAYER_MODES)}.", arg="layers")
        filename = _opt_str(a, "filename")
        if filename is not None and ("/" in filename or "\\" in filename or not filename.strip()):
            raise BadArgs("'filename' is a base name, not a path.", arg="filename")
        out.update(
            _drop_none(
                {
                    "format": fmt,
                    "bits": bits,
                    "quality": _opt_float(a, "quality", 0.0, 100.0),
                    "preset": _opt_str(a, "preset"),
                    "layers": layers,
                    "filename": filename,
                }
            )
        )
        return out

    if name == "ap_quit":
        if _opt_bool(a, "confirm") is not True:
            raise BadArgs(
                "Refusing to quit ArmorPaint without confirm=true. Unsaved work would be "
                "lost; call ap_project_save first if it matters.",
                arg="confirm",
            )
        return {}

    if name == "ap_set_envmap_params":
        out = _drop_none(
            {
                "strength": _opt_float(a, "strength", 0.0, 1000.0),
                "angle": _opt_float(a, "angle"),
            }
        )
        if not out:
            raise BadArgs("Give at least one of 'strength' or 'angle'.")
        return out

    if name == "ap_set_config":
        out = {}
        for key in CONFIG_INTS:
            value = _opt_int(a, key)
            if value is not None:
                out[key] = value
        for key in CONFIG_FLOATS:
            value = _opt_float(a, key)
            if value is not None:
                out[key] = value
        for key in CONFIG_STRINGS:
            value = _opt_str(a, key)
            if value is not None:
                out[key] = value
        for key in CONFIG_BOOLS:
            value = _opt_bool(a, key)
            if value is not None:
                out[key] = value
        if not out:
            raise BadArgs("Give at least one preference to change.")
        return out

    if name == "ap_get_object":
        return {"name": _req_str(a, "name")}

    if name == "ap_shape_add":
        return {"name": _req_str(a, "name")}

    if name == "ap_object_duplicate":
        return {"name": _req_str(a, "name")}

    if name == "ap_object_set_transform":
        out = {"name": _req_str(a, "name")}
        loc = _vec(a, "location", 3)
        rot = _vec(a, "rotation_euler_degrees", 3)
        scale = _vec(a, "scale", 3)
        if loc is None and rot is None and scale is None:
            raise BadArgs("Give at least one of location, rotation_euler_degrees, scale.")
        if loc is not None:
            out.update({"loc_x": loc[0], "loc_y": loc[1], "loc_z": loc[2]})
        if rot is not None:
            # Convert here: the engine wants a quaternion built from radians, and doing the
            # conversion in Python keeps the plugin's per-frame work small.
            radians = [angle * 3.14159265358979 / 180.0 for angle in rot]
            out.update({"rot_x": radians[0], "rot_y": radians[1], "rot_z": radians[2]})
        if scale is not None:
            out.update({"scale_x": scale[0], "scale_y": scale[1], "scale_z": scale[2]})
        return out

    if name == "ap_object_set_visible":
        visible = _opt_bool(a, "visible")
        if visible is None:
            raise BadArgs("'visible' is required.", arg="visible")
        return {"name": _req_str(a, "name"), "visible": visible}

    if name == "ap_append_mesh":
        path = _norm_path(a, "path", required=False)
        data = _opt_str(a, "obj_data")
        if bool(path) == bool(data):
            raise BadArgs("Give exactly one of 'path' or 'obj_data'.")
        if path:
            return {"path": path}
        assert data is not None
        # Inline OBJ is multi-line by definition, and a raw newline cannot cross this
        # wire, so lines are joined with OBJ_LINE_SEP and the plugin splits them back
        # apart. Tabs get the same treatment (OBJ treats any whitespace run as one
        # separator, so a space is equivalent); anything else encode_value rejects is
        # genuinely not OBJ and should fail loudly.
        if OBJ_LINE_SEP in data:
            raise BadArgs(
                f"'obj_data' contains {OBJ_LINE_SEP!r}, which this transport reserves as the "
                f"line separator. Pass the mesh via 'path' instead.",
                arg="obj_data",
            )
        text = data.replace("\r\n", "\n").replace("\r", "\n").replace("\t", " ")
        encoded = text.replace("\n", OBJ_LINE_SEP)
        # json_sane() in the plugin refuses a request body over 8192 bytes, and the
        # envelope around this value costs about 120 of them.
        if len(encoded.encode("utf-8")) > 7800:
            raise BadArgs(
                f"'obj_data' is {len(encoded)} characters; the bridge refuses a request body "
                f"over 8192 bytes. Write the mesh to a file and pass 'path'.",
                arg="obj_data",
            )
        return {"obj_data": encoded}

    if name in ("ap_material_create", "ap_material_select", "ap_material_delete"):
        return {"name": _req_str(a, "name")}

    if name == "ap_material_assign":
        return {"object": _req_str(a, "object"), "material": _req_str(a, "material")}

    if name == "ap_material_set_channels":
        out = {}
        for channel in PAINT_CHANNELS:
            value = _opt_bool(a, channel)
            if value is not None:
                out[channel] = value
        if not out:
            raise BadArgs("Give at least one channel flag to change.")
        return out

    if name == "ap_node_add":
        node_type = _req_str(a, "type").upper()
        if node_type not in NODE_TYPES:
            raise BadArgs(
                f"Unknown node type {node_type!r}. This list is the socket catalogue "
                f"(data/node_sockets.json, generated from ArmorPaint 1.0's node sources); a node "
                f"added by a newer ArmorPaint needs it regenerated (tools/gen_node_sockets.py). "
                f"Valid types: " + " ".join(sorted(NODE_TYPES)),
                arg="type",
            )
        return {
            "type": node_type,
            "x": _opt_float(a, "x") or 0.0,
            "y": _opt_float(a, "y") or 0.0,
        }

    if name in ("ap_node_remove", "ap_node_get"):
        return {"id": _req_int(a, "id", 0)}

    if name == "ap_node_connect":
        return {
            "from_id": _req_int(a, "from_id", 0),
            "from_socket": _opt_int(a, "from_socket", 0, 63) or 0,
            "to_id": _req_int(a, "to_id", 0),
            "to_socket": _opt_int(a, "to_socket", 0, 63) or 0,
        }

    if name == "ap_node_disconnect":
        return {"to_id": _req_int(a, "to_id", 0), "to_socket": _req_int(a, "to_socket", 0, 63)}

    if name == "ap_node_set_value":
        kind = _req_str(a, "kind").lower()
        out = {"id": _req_int(a, "id", 0), "kind": kind}
        if kind == "float":
            value = _opt_float(a, "value")
            if value is None:
                raise BadArgs("kind 'float' needs 'value'.", arg="value")
            out.update(
                {
                    "socket": _opt_int(a, "socket", 0, 63) or 0,
                    "is_input": _opt_bool(a, "is_input") is not False,
                    "value": value,
                }
            )
        elif kind == "color":
            color = a.get("color")
            if not isinstance(color, (list, tuple)) or len(color) not in (3, 4):
                raise BadArgs("kind 'color' needs 'color' as [r,g,b] or [r,g,b,a].", arg="color")
            comps = [_num(c, "color") for c in color]
            if len(comps) == 3:
                comps.append(1.0)
            out.update(
                {
                    "socket": _opt_int(a, "socket", 0, 63) or 0,
                    "is_input": _opt_bool(a, "is_input") is not False,
                    "r": comps[0],
                    "g": comps[1],
                    "b": comps[2],
                    "alpha": comps[3],
                }
            )
        elif kind == "vector":
            vec = _vec(a, "vector", 3)
            if vec is None:
                raise BadArgs("kind 'vector' needs 'vector' as [x,y,z].", arg="vector")
            out.update(
                {
                    "socket": _opt_int(a, "socket", 0, 63) or 0,
                    "is_input": _opt_bool(a, "is_input") is not False,
                    "x": vec[0],
                    "y": vec[1],
                    "z": vec[2],
                }
            )
        elif kind == "button":
            button = _opt_int(a, "button", 0, 63)
            if button is None:
                raise BadArgs("kind 'button' needs 'button' (the button index).", arg="button")
            value = _opt_float(a, "value")
            if value is None:
                raise BadArgs(
                    "kind 'button' needs 'value' (dropdown index, 0/1 for a checkbox, or a "
                    "slider value).",
                    arg="value",
                )
            out.update({"button": button, "value": value})
        else:
            raise BadArgs("'kind' must be one of float, color, vector, button.", arg="kind")
        return out

    if name == "ap_select_tool":
        return {"tool": _pick(a, "tool", TOOL_TYPES, "tool")}

    if name == "ap_set_brush":
        out = _drop_none(
            {
                "radius": _opt_float(a, "radius", 0.0, 100.0),
                "opacity": _opt_float(a, "opacity", 0.0, 1.0),
                "hardness": _opt_float(a, "hardness", 0.0, 1.0),
                "scale": _opt_float(a, "scale", 0.0, 1000.0),
                "angle": _opt_float(a, "angle", -3600.0, 3600.0),
                "blending": _opt_int(a, "blending", 0, 63),
            }
        )
        if not out:
            raise BadArgs("Give at least one brush parameter to change.")
        return out

    if name == "ap_paint_stroke":
        return {"points": _points(a, "points", 2)}

    if name == "ap_paint_stroke_world":
        return {"points": _points(a, "points", 3)}

    if name == "ap_set_display_channel":
        return {"mode": _pick(a, "mode", VIEWPORT_MODES, "display mode")}

    if name == "ap_capture_to_project":
        return {
            "width": _opt_int(a, "width", MIN_CAPTURE_DIM, MAX_CAPTURE_DIM) or 1024,
            "height": _opt_int(a, "height", MIN_CAPTURE_DIM, MAX_CAPTURE_DIM) or 1024,
        }

    if name == "ap_capture_viewport":
        path = _norm_path(a, "path")
        assert path is not None
        if not path.lower().endswith(".png"):
            raise BadArgs(
                "'path' must end in .png — the capture binding writes PNG only.", arg="path"
            )
        return {
            "path": path,
            "width": _opt_int(a, "width", MIN_CAPTURE_DIM, MAX_CAPTURE_DIM) or 1024,
            "height": _opt_int(a, "height", MIN_CAPTURE_DIM, MAX_CAPTURE_DIM) or 1024,
        }

    # ---- native-extension tools ---------------------------------------------
    if name in ("ap_bridge_set_idle",):
        linger = _opt_float(a, "linger", -1.0, 3600.0)
        if linger is None:
            raise BadArgs("'linger' is required (seconds; -1 = never sleep).", arg="linger")
        return {"linger": linger}

    if name in ("ap_layer_list", "ap_history", "ap_export_presets", "ap_bake_status", "ap_project_lists"):
        return {}

    if name in ("ap_layer_select", "ap_layer_delete", "ap_layer_duplicate", "ap_layer_set", "ap_layer_move", "ap_layer_action"):
        out = _drop_none(
            {
                "index": _opt_int(a, "index", 0, 4096),
                "name": _opt_str(a, "name"),
                "layer_id": _opt_int(a, "layer_id", 0),
            }
        )
        if len(out) > 1:
            raise BadArgs("Give at most one of index, name, layer_id.")
        if name == "ap_layer_set":
            blending = _opt_str(a, "blending")
            if blending is not None and blending not in BLEND_MODES:
                raise BadArgs(f"'blending' must be one of {', '.join(BLEND_MODES)}.", arg="blending")
            changes = _drop_none(
                {
                    "new_name": _opt_str(a, "new_name"),
                    "opacity": _opt_float(a, "opacity", 0.0, 1.0),
                    "blending": blending,
                    "visible": _opt_bool(a, "visible"),
                    "object_mask": _opt_int(a, "object_mask", 0, 1024),
                    "scale": _opt_float(a, "scale", 0.0, 1000.0),
                    "angle": _opt_float(a, "angle", -3600.0, 3600.0),
                }
            )
            if not changes:
                raise BadArgs("Give at least one property to change.")
            out.update(changes)
        elif name == "ap_layer_move":
            out["to_index"] = _req_int(a, "to_index", 0, 4096)
        elif name == "ap_layer_action":
            action = _req_str(a, "action")
            if action not in LAYER_ACTIONS:
                raise BadArgs(f"'action' must be one of {', '.join(LAYER_ACTIONS)}.", arg="action")
            out["action"] = action
        return out

    if name == "ap_layer_new":
        kind = (_opt_str(a, "kind") or "paint").strip()
        if kind not in LAYER_KINDS:
            raise BadArgs(f"'kind' must be one of {', '.join(LAYER_KINDS)}.", arg="kind")
        return _drop_none({"kind": kind, "new_name": _opt_str(a, "name")})

    if name in ("ap_undo", "ap_redo"):
        return {"steps": _opt_int(a, "steps", 1, 64) or 1}

    bake_params = {
        "samples": (int, 1, 4096), "axis": (int, 0, 16), "up_axis": (int, 0, 16),
        "ao_strength": (float, 0.0, 100.0), "ao_radius": (float, 0.0, 100.0),
        "ao_offset": (float, 0.0, 100.0), "curv_strength": (float, 0.0, 100.0),
        "curv_radius": (float, 0.0, 100.0), "curv_offset": (float, -100.0, 100.0),
        "curv_smooth": (int, 0, 64), "high_poly": (int, 0, 1024),
    }
    if name in ("ap_bake", "ap_bake_settings"):
        out = {}
        for key, (typ, lo, hi) in bake_params.items():
            value = _opt_int(a, key, lo, hi) if typ is int else _opt_float(a, key, lo, hi)
            if value is not None:
                out[key] = value
        if name == "ap_bake":
            bake_type = _req_str(a, "type")
            if bake_type not in BAKE_TYPES:
                raise BadArgs(f"'type' must be one of {', '.join(BAKE_TYPES)}.", arg="type")
            out.update({"node_id": _req_int(a, "node_id", 0), "type": bake_type})
        return out

    if name == "ap_render_settings":
        out = _drop_none(
            {
                "ssao": _opt_float(a, "ssao", 0.0, 1.0),
                "bloom": _opt_float(a, "bloom", 0.0, 1.0),
                "contrast": _opt_float(a, "contrast", 0.0, 2.0),
                "gamma": _opt_float(a, "gamma", 0.0, 2.0),
                "vignette": _opt_float(a, "vignette", 0.0, 1.0),
                "grain": _opt_float(a, "grain", 0.0, 1.0),
                "supersample": _opt_float(a, "supersample", 0.25, 4.0),
                "texture_filter": _opt_bool(a, "texture_filter"),
                "clip_start": _opt_float(a, "clip_start", 0.0001, 10.0),
                "clip_end": _opt_float(a, "clip_end", 1.0, 100000.0),
                "render_mode": _opt_int(a, "render_mode", 0, 1),
            }
        )
        if a.get("lut_path") is not None:
            lut = a.get("lut_path")
            if lut == "":
                out["lut_path"] = ""
            else:
                lut_path = _norm_path(a, "lut_path")
                assert lut_path is not None
                if not lut_path.lower().endswith(".cube"):
                    raise BadArgs("'lut_path' must be a .cube file.", arg="lut_path")
                out["lut_path"] = lut_path
        return out

    if name == "ap_texture_resolution":
        size = _opt_int(a, "size")
        if size is not None and size not in (2048, 4096, 8192, 16384):
            raise BadArgs("'size' must be 2048, 4096, 8192 or 16384.", arg="size")
        return _drop_none({"size": size})

    if name == "ap_camera":
        view = _opt_str(a, "view")
        if view is not None and view not in CAMERA_VIEWS:
            raise BadArgs(f"'view' must be one of {', '.join(CAMERA_VIEWS)}.", arg="view")
        return _drop_none(
            {
                "view": view,
                "orbit_x": _opt_float(a, "orbit_x", -100.0, 100.0),
                "orbit_y": _opt_float(a, "orbit_y", -100.0, 100.0),
                "zoom": _opt_float(a, "zoom", -100.0, 100.0),
                "fov": _opt_float(a, "fov", 0.05, 3.0),
            }
        )

    if name == "ap_console_read":
        return {"max_lines": _opt_int(a, "max_lines", 1, 100) or 50}

    raise BadArgs(f"Tool '{name}' has no argument mapping.")


def _wire_op(name: str, wire: dict[str, Any]) -> str:
    """The bridge op a tool call becomes (usually the tool name minus 'ap_')."""
    if name == "ap_export_textures" and set(wire) - {"directory"}:
        return "export_textures_ex"  # the options need the native extension
    return name[len(TOOL_PREFIX) :] if name.startswith(TOOL_PREFIX) else name


# ---------------------------------------------------------------------------
# Responses
# ---------------------------------------------------------------------------


def _text(payload: dict[str, Any]) -> list[types.TextContent]:
    return [types.TextContent(type="text", text=json.dumps(payload, indent=2, default=str))]


def _image_content(path: Path, max_bytes: int) -> types.ImageContent:
    """Inline a PNG the plugin wrote. Bulk data crosses the wire by PATH; only this step
    turns it into bytes, and only for images the caller asked to see."""
    if not path.exists():
        raise BadArgs(
            f"{path} does not exist on this server's filesystem. If ArmorPaint runs on "
            f"another machine or in a container, the file it wrote is not reachable from "
            f"here — use ap_fs_list to confirm where it landed.",
            arg="path",
        )
    if not path.is_file():
        raise BadArgs(f"{path} is not a file.", arg="path")
    suffix = path.suffix.lower()
    if suffix not in IMAGE_MIME:
        raise BadArgs(
            f"{suffix or 'that file'} cannot be shown inline. Supported: "
            f"{', '.join(sorted(IMAGE_MIME))}. (EXR has no MCP image representation.)",
            arg="path",
        )
    size = path.stat().st_size
    if size == 0:
        raise BadArgs(f"{path} is empty — the write probably failed.", arg="path")
    if size > max_bytes:
        raise BadArgs(
            f"{path} is {size} bytes, over the {max_bytes}-byte limit for an inline image. "
            f"Capture at a smaller size, or read the file yourself.",
            arg="path",
        )
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    return types.ImageContent(type="image", data=data, mimeType=IMAGE_MIME[suffix])


# ---------------------------------------------------------------------------
# MCP server definition
# ---------------------------------------------------------------------------

_CAPTURE_RING = 8

# Pings (one per ArmorPaint frame) before a capture. Measured live: a fill's result is
# complete on the 3rd frame after it -- 440, 54 772, 55 011 changed pixels after 1, 2 and 3
# pings -- because the fill is repeated on the next frame and the viewport redraws after.
SETTLE_FRAMES = 3
_CAPTURES: "OrderedDict[str, Any]" = OrderedDict()
_capture_seq = 0

NO_CHANGE_WARNING = (
    "The window shows no visible change (at most a few pixels, as the brush cursor or a UI "
    "widget alone would change). The operation may have been a silent no-op -- no "
    "layer selected, a group layer, paint refused on a fill layer, a stroke off the model or "
    "behind the camera -- or the change lies outside the captured area."
)


def _remember_capture(cap: Any) -> str:
    global _capture_seq
    _capture_seq += 1
    cid = f"c{_capture_seq}"
    _CAPTURES[cid] = cap
    while len(_CAPTURES) > _CAPTURE_RING:
        _CAPTURES.popitem(last=False)
    return cid


def _parse_crop(args: dict[str, Any]) -> tuple[int, int, int, int] | None:
    crop_raw = args.get("crop")
    if crop_raw is None:
        return None
    if (
        not isinstance(crop_raw, list)
        or len(crop_raw) != 4
        or not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in crop_raw)
    ):
        raise BadArgs("'crop' must be [x, y, width, height] as four numbers.", arg="crop")
    return (int(crop_raw[0]), int(crop_raw[1]), int(crop_raw[2]), int(crop_raw[3]))


async def _grab(args: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
    """Settle (a ping, so the last edit has rendered), then capture the window.
    Returns (capture or None, report)."""
    crop = _parse_crop(args)
    downscale = _opt_int(args, "downscale", 1, MAX_DOWNSCALE) or 1
    loop = asyncio.get_running_loop()
    report: dict[str, Any] = {"ok": True}
    started = time.monotonic()

    # Settle: a ping is answered in the frame AFTER any earlier request, and that earlier
    # frame has been rendered by then. The heartbeat's title picks the right window if
    # more than one ArmorPaint is open.
    hb = read_heartbeat()
    title = hb.get("app_title") if isinstance(hb, dict) else None
    frames = _opt_int(args, "settle_frames", 1, 30) or SETTLE_FRAMES
    if _opt_bool(args, "settle") is not False and hb is not None:
        try:
            for _ in range(frames):
                await loop.run_in_executor(None, partial(send_to_armorpaint, "ping", {}, 5.0))
            await asyncio.sleep(0.05)  # let the compositor pick up the presented frame
            report["settled"] = frames
        except BridgeError as exc:
            report["settled"] = False
            report["settle_error"] = exc.message
    else:
        report["settled"] = False

    try:
        cap = await loop.run_in_executor(
            None, partial(capture_window, title or None, crop, downscale)
        )
    except CaptureError as exc:
        return None, {"ok": False, "code": exc.code, "error": exc.message}
    report.update(cap.describe())
    report["capture_id"] = _remember_capture(cap)
    report["capture_ms"] = round((time.monotonic() - started) * 1000)
    return cap, report


def _png_image(png: bytes) -> types.ImageContent:
    return types.ImageContent(type="image", data=base64.b64encode(png).decode("ascii"), mimeType="image/png")


def _compare(before: Any, after: Any) -> tuple[dict[str, Any], bytes | None]:
    """image_diff over two captures; the highlight PNG when something changed."""
    try:
        d = image_diff.diff(before.width, before.height, before.pixels(), after.pixels(),
                            w2=after.width, h2=after.height)
    except ValueError as exc:
        return {"error": str(exc)}, None
    if d["bbox"] is None:
        return d, None
    return d, image_diff.highlight(after.width, after.height, before.pixels(), after.pixels(), d["bbox"])


async def _capture_window_tool(
    args: dict[str, Any],
) -> list[types.TextContent | types.ImageContent]:
    save_to = _norm_path(args, "path", required=False)
    if save_to is not None and not save_to.lower().endswith(".png"):
        raise BadArgs("'path' must end in .png.", arg="path")
    against = _opt_str(args, "diff_against")
    previous = next(reversed(_CAPTURES)) if (against == "last" and _CAPTURES) else against

    cap, report = await _grab(args)
    if cap is None:
        report["tool"] = "ap_capture_window"
        return _text(report)
    if save_to is not None:
        Path(save_to).write_bytes(cap.png)
        report["path"] = save_to
    extra: list[types.ImageContent] = []
    if against is not None:
        if previous is None or previous not in _CAPTURES or previous == report["capture_id"]:
            report["diff"] = {"error": f"no capture {against!r} to compare with (the server keeps the last "
                                       f"{_CAPTURE_RING}: {', '.join(_CAPTURES) or 'none'})"}
        else:
            d, hl = _compare(_CAPTURES[previous], cap)
            report["diff"] = {"against": previous, **d}
            if hl is not None:
                extra.append(_png_image(hl))
    if len(cap.png) > MAX_IMAGE_BYTES:
        report["image_error"] = (
            f"PNG is {len(cap.png)} bytes, over the {MAX_IMAGE_BYTES}-byte inline limit; "
            f"use 'crop' or 'downscale'."
        )
        return _text(report)
    text = types.TextContent(type="text", text=json.dumps(report, indent=2))
    if _opt_bool(args, "include_image") is False:
        return [*extra, text]
    return [_png_image(cap.png), *extra, text]


# Tools that take an optional 'capture' argument: the window is captured after the
# operation (and, for the diff, before it) and returned in the same reply.
CAPTURE_TOOLS = frozenset(
    {"ap_paint_stroke", "ap_paint_stroke_world", "ap_fill_layer", "ap_batch",
     "ap_node_graph_apply", "ap_node_recipe"}
)

_CAPTURE_ARG = {
    "type": "object",
    "description": (
        "Look in the same call: capture ArmorPaint's window after the operation and return "
        "it with the result, saving a round trip. {} for the whole window; optional 'crop' "
        "[x,y,w,h] and 'downscale' as ap_capture_window. By default the window is also "
        "captured BEFORE and the reply says what changed (bounding box, fraction) with a "
        "zoomed image of it, and flags no_visible_change -- the tell-tale of a silent "
        "no-op. 'diff': false skips the before-capture."
    ),
    "properties": {
        "crop": {"type": "array", "items": {"type": "integer"}, "minItems": 4, "maxItems": 4},
        "downscale": {"type": "integer", "minimum": 1, "maximum": MAX_DOWNSCALE},
        "settle_frames": {"type": "integer", "minimum": 1, "maximum": 30, "default": 3},
        "diff": {"type": "boolean", "default": True},
    },
}

for _t in TOOLS:
    if _t.name in CAPTURE_TOOLS:
        _t.inputSchema.setdefault("properties", {})["capture"] = _CAPTURE_ARG


async def _with_capture(
    name: str, args: dict[str, Any]
) -> list[types.TextContent | types.ImageContent]:
    opts = args.get("capture")
    if not isinstance(opts, dict):
        raise BadArgs("'capture' must be an object ({} captures the whole window).", arg="capture")
    grab_args = {k: opts[k] for k in ("crop", "downscale", "settle_frames") if k in opts}
    want_diff = opts.get("diff", True) is not False
    before, before_report = (await _grab(grab_args)) if want_diff else (None, None)
    content = await call_tool(name, {k: v for k, v in args.items() if k != "capture"})
    texts = [c for c in content if getattr(c, "type", "") == "text"]
    others = [c for c in content if getattr(c, "type", "") != "text"]
    payload = json.loads(texts[-1].text) if texts else {}
    after, block = await _grab(grab_args)
    images: list[types.ImageContent] = []
    if after is not None:
        images.append(_png_image(after.png))
        if before is not None:
            d, hl = _compare(before, after)
            block["diff"] = {"against": before_report["capture_id"], **d}
            if hl is not None:
                images.append(_png_image(hl))
            if d.get("no_visible_change"):
                block["warning"] = NO_CHANGE_WARNING
        elif want_diff:
            block["diff"] = {"error": f"no before-capture: {before_report.get('error')}"}
    payload["capture"] = block
    return [*others, *images, types.TextContent(type="text", text=json.dumps(payload, indent=2, default=str))]


def _frame_fence() -> desktop_input.Fence:
    """A bridge ping as a frame fence for synthetic input, when the bridge is reachable."""
    hb = read_heartbeat()
    if not isinstance(hb, dict) or hb.get("enabled") is False:
        return None
    return lambda: send_to_armorpaint("ping", {}, 10.0)


def _ui_tool(name: str, a: dict[str, Any]) -> dict[str, Any]:
    """Synthetic input to ArmorPaint's window (desktop_input)."""
    hb = read_heartbeat()
    title = hb.get("app_title") if isinstance(hb, dict) else None
    title = title or None
    fence = _frame_fence()
    try:
        if name == "ap_ui_click":
            res = desktop_input.click(
                _req_int(a, "x"), _req_int(a, "y"), (_opt_str(a, "button") or "left"),
                bool(_opt_bool(a, "double")), _opt_list_str(a, "modifiers"), title, fence,
            )
        elif name == "ap_ui_key":
            res = desktop_input.key(_req_str(a, "key"), _opt_list_str(a, "modifiers"), title, fence)
        elif name == "ap_ui_drag":
            raw = a.get("points")
            if not isinstance(raw, list) or len(raw) < 2 or len(raw) > 64:
                raise BadArgs("'points' must be 2..64 [x, y] pairs.", arg="points")
            pts: list[tuple[int, int]] = []
            for p in raw:
                if not isinstance(p, (list, tuple)) or len(p) != 2:
                    raise BadArgs("each point must be [x, y].", arg="points")
                pts.append((int(_num(p[0], "points")), int(_num(p[1], "points"))))
            res = desktop_input.drag(pts, (_opt_str(a, "button") or "left"), _opt_list_str(a, "modifiers"), title_hint=title)
        else:
            res = desktop_input.scroll(_req_int(a, "x"), _req_int(a, "y"), _req_int(a, "clicks", -50, 50), title)
    except desktop_input.InputError as exc:
        return {"ok": False, "code": exc.code, "error": exc.message, "tool": name}
    res = dict(res)
    res["ok"] = True
    res["next_step"] = "Call ap_capture_window to see the result."
    return res


def _opt_list_str(a: dict[str, Any], key: str) -> list[str] | None:
    value = a.get(key)
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise BadArgs(f"'{key}' must be an array of strings.", arg=key)
    return value


def _open_project_path() -> str | None:
    hb = read_heartbeat()
    path = hb.get("project") if isinstance(hb, dict) else None
    return path if isinstance(path, str) and path else None


def _resource_search_tool(a: dict[str, Any]) -> dict[str, Any]:
    kinds = _opt_list_str(a, "kinds")
    roots = _opt_list_str(a, "roots")
    try:
        out = local_tools.search_resources(
            _opt_str(a, "query") or "",
            kinds,
            roots,
            _open_project_path(),
            _opt_int(a, "max_results", 1, 500) or 50,
        )
    except ValueError as exc:
        raise BadArgs(str(exc)) from exc
    out["ok"] = True
    return out


def _project_metadata_tool(a: dict[str, Any]) -> dict[str, Any]:
    path = _norm_path(a, "project_path", required=False) or _open_project_path()
    set_values = a.get("set")
    if set_values is not None and not isinstance(set_values, dict):
        raise BadArgs("'set' must be an object.", arg="set")
    try:
        out = local_tools.project_metadata(path or "", set_values, _opt_list_str(a, "remove"))
    except ValueError as exc:
        raise BadArgs(str(exc)) from exc
    out["ok"] = True
    return out


def _material_list_tool() -> dict[str, Any]:
    """Live list from the native extension; the save/load snapshot otherwise."""
    try:
        lists = send_to_armorpaint("project_lists", {}, OP_TIMEOUTS["project_lists"])
        mats = lists.get("materials") or []
        return {
            "ok": True,
            "op": "project_lists",
            "result": {
                "live": True,
                "count": len(mats),
                "materials": mats,
                "names": "|".join(str(m.get("name")) for m in mats),
                "delimiter": "|",
            },
        }
    except OpFailed as exc:
        if exc.code != "unsupported":
            raise
    result = send_to_armorpaint("material_list", {}, OP_TIMEOUTS["material_list"])
    return {"ok": True, "op": "material_list", "result": result, "hint": EXT_HINT}


def _undo_tool(op: str, wire: dict[str, Any]) -> dict[str, Any]:
    """Exact undo/redo through the native extension, else the app's own shortcut."""
    try:
        result = send_to_armorpaint(op, wire, OP_TIMEOUTS[op])
        return {"ok": True, "op": op, "method": "native extension (history_undo/redo)", "result": result}
    except OpFailed as exc:
        if exc.code != "unsupported":
            raise
    # Stock build: press ArmorPaint's own shortcut (keymap edit_undo / edit_redo).
    mods = ["ctrl"] if op == "undo" else ["ctrl", "shift"]
    steps = int(wire.get("steps", 1))
    hb = read_heartbeat()
    title = (hb.get("app_title") if isinstance(hb, dict) else None) or None
    fence = _frame_fence()
    try:
        for _ in range(steps):
            desktop_input.key("z", mods, title, fence)
    except desktop_input.InputError as exc:
        return {
            "ok": False,
            "code": "unsupported",
            "error": f"{op} needs the native extension, or synthetic keyboard input to "
            f"ArmorPaint's window, and the latter failed: {exc.message}",
            "hint": EXT_HINT,
        }
    return {
        "ok": True,
        "op": op,
        "method": f"keyboard shortcut {'+'.join(mods)}+z sent {steps}x (stock build)",
        "note": "Sent as ArmorPaint's default keymap shortcut; if the keymap was changed, or a "
        "text field has focus, it may not act. The history cannot be read back without the "
        "native extension -- verify with ap_capture_window.",
    }


GRAPH_TOOLS = frozenset(
    {"ap_node_graph_get", "ap_node_graph_apply", "ap_node_graph_lint", "ap_node_graph_snapshot",
     "ap_node_graph_restore", "ap_node_recipe"}
)

class _SpoolBridge:
    """node_graph's two-method bridge over the real mailbox."""

    def call(self, op: str, wire: dict[str, Any]) -> dict[str, Any]:
        return send_to_armorpaint(op, wire, OP_TIMEOUTS.get(op, DEFAULT_TIMEOUT_S))

    def batch(self, items: list[tuple[str, dict[str, Any]]], stop_on_error: bool = False) -> dict[str, Any]:
        timeout = sum(OP_TIMEOUTS.get(op, DEFAULT_TIMEOUT_S) for op, _ in items)
        return send_batch(items, min(timeout, 900.0), stop_on_error)


def graph_bridge() -> node_graph.Bridge:
    return _SpoolBridge()


STATE_ENV = "ARMORPAINT_MCP_STATE"


def _state_dir() -> Path:
    """Where the server keeps its own state (graph snapshots, ...): $ARMORPAINT_MCP_STATE,
    else a folder in the spool, which the plugin never touches."""
    env = os.environ.get(STATE_ENV)
    return Path(env) if env else spool_resolution().path / "mcp_state"


def _snapshots() -> node_graph.SnapshotStore:
    return node_graph.SnapshotStore(_state_dir() / "graph_snapshots")


def _apply_report(report: dict[str, Any], label: str | None) -> dict[str, Any]:
    snap = report.pop("snapshot", None)
    if report.get("ok") and snap is not None:
        report["snapshot_id"] = _snapshots().put(snap, label=label or "before ap_node_graph_apply")
    return report


def _graph_tool(name: str, a: dict[str, Any]) -> dict[str, Any]:
    bridge = graph_bridge()
    try:
        if name == "ap_node_graph_get":
            return {"ok": True, "graph": node_graph.read_graph(bridge)}
        if name == "ap_node_graph_lint":
            return {"ok": True, "issues": node_graph.lint(node_graph.read_graph(bridge))}
        if name == "ap_node_graph_snapshot":
            store = _snapshots()
            if _opt_bool(a, "list"):
                return {"ok": True, "snapshots": store.list()}
            sid = store.put(node_graph.snapshot(bridge), label=_opt_str(a, "label"))
            return {"ok": True, "snapshot_id": sid}
        if name == "ap_node_graph_restore":
            sid = _req_str(a, "snapshot_id")
            try:
                entry = _snapshots().get(sid)
            except KeyError:
                return {"ok": False, "code": "not_found", "error": f"no graph snapshot {sid!r}",
                        "hint": "ap_node_graph_snapshot(list=true) lists them."}
            return node_graph.restore(bridge, entry["snapshot"], force=bool(_opt_bool(a, "force")))

        mode = _opt_str(a, "mode")
        dry_run = bool(_opt_bool(a, "dry_run"))
        fill = _opt_bool(a, "fill") is not False
        if name == "ap_node_graph_apply":
            spec = a.get("spec")
            if not isinstance(spec, dict):
                raise BadArgs("'spec' must be an object with 'nodes' (and optionally 'links').", arg="spec")
            report = node_graph.apply(bridge, spec, mode=mode or "merge", dry_run=dry_run, fill=fill)
            return _apply_report(report, _opt_str(a, "label"))
        if name == "ap_node_recipe":
            rname = _opt_str(a, "name")
            if not rname:
                return {"ok": True, "recipes": recipes.list_recipes()}
            params = a.get("params") or {}
            if not isinstance(params, dict):
                raise BadArgs("'params' must be an object.", arg="params")
            spec = recipes.render(rname, params)
            if not _opt_bool(a, "apply") and not dry_run:
                return {"ok": True, "name": rname, "spec": spec,
                        "hint": "apply=true builds it; or edit the spec and pass it to ap_node_graph_apply."}
            report = node_graph.apply(bridge, spec, mode=mode or "replace", dry_run=dry_run, fill=fill)
            return _apply_report(report, f"before recipe {rname}")
    except node_graph.SpecError as exc:
        return {"ok": False, "code": "bad_spec", "problems": exc.problems}
    raise BadArgs(f"unhandled graph tool {name}")


# Tools that cannot be batch steps: answered locally, composed of several requests, or
# would end the session.
_UNBATCHABLE = LOCAL_TOOLS | GRAPH_TOOLS | {
    "ap_batch", "ap_quit", "ap_project_metadata", "ap_material_list", "ap_undo", "ap_redo",
}


def _batch_tool(a: dict[str, Any]) -> dict[str, Any]:
    steps = a.get("steps")
    if not isinstance(steps, list) or not steps:
        raise BadArgs("'steps' must be a non-empty array of {tool, args}.", arg="steps")
    items: list[tuple[str, dict[str, Any]]] = []
    names: list[str] = []
    timeout = 0.0
    for i, step in enumerate(steps):
        if not isinstance(step, dict) or not isinstance(step.get("tool"), str):
            raise BadArgs(f"steps[{i}] must be an object with a 'tool' name.", arg="steps")
        tool = step["tool"].strip()
        if not tool.startswith(TOOL_PREFIX):
            tool = TOOL_PREFIX + tool
        if tool not in TOOL_NAMES:
            raise BadArgs(f"steps[{i}]: unknown tool {tool!r}.", arg="steps")
        if tool in _UNBATCHABLE:
            raise BadArgs(f"steps[{i}]: {tool} cannot run inside a batch.", arg="steps")
        step_args = step.get("args") or {}
        if not isinstance(step_args, dict):
            raise BadArgs(f"steps[{i}].args must be an object.", arg="steps")
        try:
            wire = _build_wire_args(tool, step_args)
        except BadArgs as exc:
            raise BadArgs(f"steps[{i}] ({tool}): {exc.message}", arg="steps") from exc
        op = _wire_op(tool, wire)
        items.append((op, wire))
        names.append(tool)
        timeout += OP_TIMEOUTS.get(op, DEFAULT_TIMEOUT_S)
    result = send_batch(items, min(timeout, 900.0), bool(_opt_bool(a, "stop_on_error")))
    for r in result.get("results") or []:
        i = r.get("i")
        if isinstance(i, int) and 0 <= i < len(names):
            r["tool"] = names[i]
    return {"ok": True, "op": "batch", "result": result}


SERVER_INSTRUCTIONS = (
    "Drives a running ArmorPaint 1.0 (a 3D PBR texture painter) over a file mailbox. "
    "If any tool returns a transport error, call ap_bridge_status first — it diagnoses the "
    "connection without needing ArmorPaint to answer. "
    "The MATERIAL NODE GRAPH is fully scriptable (ap_node_list / ap_node_add / "
    "ap_node_connect / ap_node_set_value + ap_material_update); a graph edit only shows "
    "once you ap_fill_layer or paint. Layers (ap_layer_*), undo history, export format, "
    "bakes, render settings and camera views need the optional native extension — call "
    "ap_get_app_info: ext_state 1 means it is there; on a stock build those tools answer "
    "'unsupported', ap_undo/ap_redo fall back to keyboard shortcuts, and ap_ui_click / "
    "ap_ui_key / ap_ui_drag can drive the UI directly. To SEE results use ap_capture_window "
    "(any build) or ap_capture_viewport; ap_export_textures + ap_read_image_file show the "
    "texture files. Group many small edits into one ap_batch call: it runs several steps "
    "per ArmorPaint frame."
)

server = Server(SERVER_NAME, version=__version__, instructions=SERVER_INSTRUCTIONS)


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return TOOLS


@server.call_tool()
async def call_tool(
    name: str, arguments: dict[str, Any]
) -> list[types.TextContent | types.ImageContent]:
    """Route one MCP tool call through the file mailbox to the ArmorPaint bridge."""
    args = arguments or {}
    started = time.monotonic()

    try:
        if name in CAPTURE_TOOLS and args.get("capture") is not None:
            return await _with_capture(name, args)

        # --- locally answered tools ---------------------------------------
        if name == "ap_bridge_status":
            probe = _opt_bool(args, "probe")
            loop = asyncio.get_running_loop()
            report = await loop.run_in_executor(
                None, partial(bridge_diagnostics, probe=probe is not False)
            )
            return _text(report)

        if name == "ap_capture_window":
            return await _capture_window_tool(args)

        if name == "ap_read_image_file":
            path_text = _norm_path(args, "path")
            assert path_text is not None
            limit = _opt_int(args, "max_bytes", 1, MAX_IMAGE_BYTES) or MAX_IMAGE_BYTES
            path = Path(path_text)
            image = _image_content(path, limit)
            return [
                image,
                types.TextContent(
                    type="text",
                    text=json.dumps(
                        {
                            "ok": True,
                            "path": str(path),
                            "bytes": path.stat().st_size,
                            "mime_type": image.mimeType,
                        },
                        indent=2,
                    ),
                ),
            ]

        if name not in TOOL_NAMES:
            return _text(
                {
                    "ok": False,
                    "code": "unknown_tool",
                    "error": f"Unknown tool: {name}.",
                    "available": sorted(TOOL_NAMES),
                }
            )

        loop = asyncio.get_running_loop()

        if name in UI_TOOLS:
            return _text(await loop.run_in_executor(None, partial(_ui_tool, name, args)))

        if name == "ap_resource_search":
            return _text(await loop.run_in_executor(None, partial(_resource_search_tool, args)))

        if name == "ap_project_metadata":
            return _text(await loop.run_in_executor(None, partial(_project_metadata_tool, args)))

        if name == "ap_batch":
            return _text(await loop.run_in_executor(None, partial(_batch_tool, args)))

        if name in GRAPH_TOOLS:
            return _text(await loop.run_in_executor(None, partial(_graph_tool, name, args)))

        # --- bridge round trip --------------------------------------------
        wire = _build_wire_args(name, args)
        op = _wire_op(name, wire)
        timeout = OP_TIMEOUTS.get(op, DEFAULT_TIMEOUT_S)

        if name == "ap_material_list":
            return _text(await loop.run_in_executor(None, _material_list_tool))

        if name in ("ap_undo", "ap_redo"):
            return _text(await loop.run_in_executor(None, partial(_undo_tool, op, wire)))

        sent_at = time.monotonic()
        try:
            result = await loop.run_in_executor(
                None, partial(send_to_armorpaint, op, wire, timeout)
            )
        except RequestTimeout as exc:
            if name == "ap_quit" and exc.details.get("request_consumed"):
                # Expected: the app exits before it can commit a response.
                return _text(
                    {
                        "ok": True,
                        "op": op,
                        "result": {
                            "note": "Quit was picked up by the bridge; ArmorPaint exited "
                            "before it could reply, which is normal for this op."
                        },
                    }
                )
            raise

        payload: dict[str, Any] = {"ok": True, "op": op, "result": result}
        now = time.monotonic()
        payload["timing"] = {
            "total_ms": round((now - started) * 1000),
            # request written -> reply read: poll latency, frame wait and the handler
            "round_trip_ms": round((now - sent_at) * 1000),
            # the handler alone, as measured inside ArmorPaint
            "handler_ms": result.get("elapsed_ms") if isinstance(result, dict) else None,
        }

        # Echo resolved enums so the caller can see what the name mapped to.
        if name == "ap_select_tool":
            payload["requested_tool_index"] = wire["tool"]
        elif name == "ap_set_display_channel":
            payload["requested_mode_index"] = wire["mode"]

        if name == "ap_capture_viewport" and _opt_bool(args, "include_image") is not False:
            written = result.get("path") or wire["path"]
            try:
                image = _image_content(Path(str(written)), MAX_IMAGE_BYTES)
            except BadArgs as exc:
                payload["image_error"] = exc.message
                return _text(payload)
            return [image, types.TextContent(type="text", text=json.dumps(payload, indent=2))]

        return _text(payload)

    except BridgeError as exc:
        payload = exc.to_dict()
        payload["tool"] = name
        if isinstance(exc, OpFailed):
            payload["from"] = "armorpaint bridge"
            if payload.get("code") == "unsupported" and (
                name in EXT_TOOLS
                or (
                    name == "ap_export_textures"
                    and any(k in args for k in ("format", "bits", "quality", "preset", "layers", "filename"))
                )
            ):
                payload["hint"] = EXT_HINT
            if name == "ap_capture_viewport" and payload.get("code") == "unsupported":
                payload["try_instead"] = (
                    "ap_capture_window: screenshots ArmorPaint's window from outside the app "
                    "on any build."
                )
        elif not isinstance(exc, BadArgs):
            payload.setdefault(
                "next_step", "Call ap_bridge_status for a full connection diagnosis."
            )
        return _text(payload)
    except Exception as exc:  # never let an exception escape into the transport
        return _text(
            {
                "ok": False,
                "code": "server_error",
                "error": f"{type(exc).__name__}: {exc}",
                "tool": name,
            }
        )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def main() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def run() -> None:
    """Console-script entry point (``armorpaint-mcp``)."""
    asyncio.run(main())


if __name__ == "__main__":
    # A one-line sanity note on stderr; stdout belongs to the MCP stream.
    import sys as _sys

    print(
        f"armorpaint-mcp: {len(TOOLS)} tools, spool -> {spool_resolution().path}",
        file=_sys.stderr,
    )
    run()
