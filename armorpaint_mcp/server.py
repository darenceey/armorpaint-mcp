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

Scope note, because it shapes the whole tool surface: ArmorPaint's plugin API is 529
bindings, and **layers are almost absent from it** — ``script_fill_layer`` is the only layer
operation in the entire table, and ``slot_layer_t`` is not a registered struct, so layers
cannot be created, listed, renamed, reordered, masked or blended from a plugin. There are
also no bake-run, undo/redo, camera-pose or UI-automation bindings. Those tools are not
missing here by oversight; they are impossible. See ``docs/MINIC_DIALECT_AND_API.md`` §2.14.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
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
        send_to_armorpaint,
        spool_resolution,
    )
except ImportError:  # running server.py as a loose script
    __version__ = "1.0.0"
    from transport import (  # type: ignore[no-redef]
        BadArgs,
        BridgeError,
        OpFailed,
        RequestTimeout,
        bridge_diagnostics,
        send_to_armorpaint,
        spool_resolution,
    )

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
}

# Tools answered entirely by this process — they work even when ArmorPaint is closed.
LOCAL_TOOLS = frozenset({"ap_bridge_status", "ap_read_image_file"})

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

# Valid script_material_create_node(type) strings: nodes_material/*.c plus the pre-created
# OUTPUT_MATERIAL_PBR. Validated here so a typo cannot reach the binding.
NODE_TYPES = frozenset(
    """ATTRIBUTE BAKE_CURVATURE BLUR BOOL BRIGHTCONTRAST BUMP CLAMP COLMASK COMBINE_COLOR
    COMBXYZ CURVE_RGB CURVE_VEC CUSTOM DIRECT_WARP ENUM FLOAT_CURVE GAMMA GROUP GROUP_INPUT
    GROUP_OUTPUT HUE_SAT INVERT_COLOR LAYER LAYER_MASK MAPPING MAPRANGE MATERIAL MATH
    MIX_NORMAL_MAP MIX_RGB NEURAL_EDIT_IMAGE NEURAL_IMAGE_TO_3D_MESH NEURAL_IMAGE_TO_PBR
    NEURAL_REPEAT NEURAL_SAVE_IMAGE NEURAL_TEXT_TO_IMAGE NEURAL_UPSCALE_IMAGE NEW_GEOMETRY
    NORMAL NORMAL_MAP OBJECT_INFO OUTPUT_MATERIAL_PBR PICKER QUANTIZE REPLACECOL RGB RGBA
    RGBTOBW SCRIPT_CPU SEPARATE_COLOR SEPXYZ SHADER_GPU STRING TEX_BAKE TEX_BRICK TEX_CAMERA
    TEX_CHECKER TEX_COORD TEX_GABOR TEX_GRADIENT TEX_IMAGE TEX_MAGIC TEX_NOISE TEX_TEXT
    TEX_VORONOI TEX_WAVE TILESHEET TILESHEET_ANIM UVMAP VALTORGB VALUE VECTOR VECT_MATH
    VECT_ROTATE VECT_TRANSFORM WIREFRAME""".split()
)

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
        "Turn the bridge's per-frame polling on or off. This has a real cost: while enabled "
        "the plugin calls iron_delay_idle_sleep() every frame, which keeps ArmorPaint "
        "rendering at full rate even when unfocused (that is also what makes remote control "
        "of a background window possible). Disable it when no agent is working. WARNING: "
        "disabling it stops the bridge from reading requests, so this is the last tool that "
        "will work until someone re-enables it from the Plugins tab.",
        {"enabled": _b("True to poll every frame; false to let the app idle.")},
        ["enabled"],
    ),
    _tool(
        "ap_console_write",
        "Write a line to ArmorPaint's own console. Write-only: no binding can read the "
        "console back, so this is for leaving a trail for the human, not for logging you "
        "intend to read.",
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
        "Export the project's texture channels to real image files on disk — the only "
        "binding in ArmorPaint's plugin API that writes images to disk. Returns the files "
        "found in the target directory afterwards; feed one to ap_read_image_file to look at "
        "it. IMPORTANT LIMITS, none of which are settable from a plugin: 'directory' is a "
        "DIRECTORY, not a filename; the base filename comes from the last name used in "
        "ArmorPaint's own export dialog, falling back to 'untitled'; the channel suffixes "
        "come from the active export preset ('generic' is auto-selected on first use); and "
        "the format/bit depth is whatever the UI is set to (8-bit PNG by default).",
        {"directory": _s(f"Output directory. {_PATH_NOTE}")},
        ["directory"],
    ),
    _tool(
        "ap_export_material_bake",
        "Bake the ACTIVE MATERIAL onto a plane and export the result as images "
        "(export_texture_run with bake_material=1). This is not mesh map baking — there is "
        "no bake-run binding for normal/AO/curvature maps.",
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
        "that only whether a layer is selected can be reported: the layer object itself is "
        "an opaque pointer with no readable fields.",
    ),
    _tool(
        "ap_get_config",
        "Application preferences that are readable from a plugin: window size/scale, "
        "supersampling, keymap, theme, undo steps, camera FOV, default layer resolution, "
        "live-brush/live-material/node-preview toggles, workspace and workflow, plus the "
        "recent-project and plugin lists. Post-processing settings (SSAO, bloom, LUT, "
        "gamma...) are not exposed by the API.",
    ),
    _tool(
        "ap_set_config",
        "Change application preferences. Only the listed fields are writable; anything else "
        "in ArmorPaint's preferences is not exposed to plugins. layer_res is the DEFAULT "
        "resolution for new layers — changing it does not resize existing ones.",
        {
            "window_w": _i("Window width in pixels."),
            "window_h": _i("Window height in pixels."),
            "window_scale": _n("UI scale factor, e.g. 1.0 or 1.5."),
            "rp_supersample": _n("Render supersampling factor, e.g. 1.0 or 2.0."),
            "keymap": _s("Keymap preset name."),
            "theme": _s("Theme name."),
            "undo_steps": _i("Undo history depth."),
            "camera_fov": _n("Camera field of view in radians."),
            "layer_res": _i("Default layer resolution for NEW layers (e.g. 2048)."),
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
        "Delete a material slot by name. Irreversible from here — ArmorPaint's plugin API "
        "has no undo/redo binding, so this cannot be taken back except by the user pressing "
        "Ctrl+Z in the app.",
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
        "List material names. DEGRADED — read the caveat before trusting it: this reads "
        "project_t.material_nodes, which is a SNAPSHOT written only at save and load. It is "
        "null in a project that has never been saved, and it misses materials created during "
        "this session. There is no live material enumeration binding. For the material you "
        "are actually working on, use ap_material_get_active.",
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
        "is: ap_node_* edits -> ap_material_update -> ap_fill_layer -> ap_capture_viewport. "
        "Fails with 'no_project'/'bad_args' if no layer is selected. This is also the ONLY "
        "layer operation in ArmorPaint's plugin API — there is no create/delete/rename/mask/"
        "opacity/blend binding, so layer management has to be done by hand in the UI.",
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
        "Capture the 3D viewport into the project as a packed texture asset. Works on a "
        "stock ArmorPaint, but the pixels land INSIDE the project (persisted only when the "
        ".arm is saved) — nothing outside ArmorPaint can read them, so you cannot look at "
        "the result. To actually see the viewport, use ap_capture_viewport (needs the "
        "optional viewport patch). NOTE: the bridge handler runs inline in one frame and "
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
        "Capture the shaded 3D viewport to a PNG file and return the image so you can look "
        "at your own work. REQUIRES the optional viewport patch (docs/UPSTREAM_CHANGES.md) "
        "which adds the viewport_save_texture_to_file binding — on a stock build this "
        "returns code 'unsupported', and the fallbacks are ap_capture_to_project (in-project "
        "only) or ap_export_textures (writes real files, but flat textures rather than the "
        "shaded view).",
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

    if name in ("ap_export_textures", "ap_export_material_bake"):
        return {"directory": _norm_path(a, "directory")}

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
                f"Unknown node type {node_type!r}. This list comes from the running build's "
                f"own --api dump; a node added by a newer ArmorPaint would need this server "
                f"updated. Valid types: " + " ".join(sorted(NODE_TYPES)),
                arg="type",
            )
        return {
            "type": node_type,
            "x": _opt_float(a, "x") or 0.0,
            "y": _opt_float(a, "y") or 0.0,
        }

    if name == "ap_node_remove":
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

    raise BadArgs(f"Tool '{name}' has no argument mapping.")


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

SERVER_INSTRUCTIONS = (
    "Drives a running ArmorPaint 1.0 (a 3D PBR texture painter) over a file mailbox. "
    "If any tool returns a transport error, call ap_bridge_status first — it diagnoses the "
    "connection without needing ArmorPaint to answer. "
    "Two things about this app are worth knowing before planning work: (1) LAYERS are "
    "essentially absent from ArmorPaint's plugin API — ap_fill_layer is the only layer "
    "operation that exists, and layers cannot be listed, created, renamed, masked or "
    "blended from here, so ask the user to do layer setup in the UI; (2) the MATERIAL NODE "
    "GRAPH is fully scriptable (ap_node_list / ap_node_add / ap_node_connect / "
    "ap_node_set_value + ap_material_update), which is where an agent has real leverage. "
    "Bulk data moves by path: ap_export_textures writes real PNGs, then ap_read_image_file "
    "shows you one."
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

    try:
        # --- locally answered tools ---------------------------------------
        if name == "ap_bridge_status":
            probe = _opt_bool(args, "probe")
            loop = asyncio.get_running_loop()
            report = await loop.run_in_executor(
                None, partial(bridge_diagnostics, probe=probe is not False)
            )
            return _text(report)

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

        # --- bridge round trip --------------------------------------------
        op = name[len(TOOL_PREFIX) :] if name.startswith(TOOL_PREFIX) else name
        wire = _build_wire_args(name, args)
        timeout = OP_TIMEOUTS.get(op, DEFAULT_TIMEOUT_S)

        loop = asyncio.get_running_loop()
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
