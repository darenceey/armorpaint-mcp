# armorpaint-mcp

Drive [ArmorPaint](https://armorpaint.org) 1.0 from an MCP client.

An MCP server (Python) plus a small in-app bridge (an ArmorPaint *plugin*, written in the app's
embedded C dialect) let an agent open projects, inspect and edit materials and node graphs, paint,
and export textures — against a **stock, unmodified ArmorPaint**, including the official paid binary.

> **Not affiliated with, sponsored by, or endorsed by Armory3D or the ArmorPaint project.**
> This is an independent third-party tool. Please do not file ArmorPaint bugs for it, and do not
> file its bugs upstream.

> **This release: bridge 2.1, native extension 2.** It answers a review that listed five
> shortcomings (stroke kinematics, feedback latency, node-graph building, mesh/UV repair, undo and
> state). What could be fixed was, and what cannot be is argued in
> [Review response](#review-response-what-changed-and-what-cannot). The new work was built test
> first and verified **live on Linux** against ArmorPaint 1.0 built from the pinned commit, under
> Xvfb with openbox and Mesa's lavapipe (`tools/live_harness.sh`). Every `tests/test_live*.py`
> suite passes on the stock build (41 passed; the 8 skipped need the extension) and on the
> extension build (47 passed; the 2 skipped are stock-only), alongside the offline suite.
>
> **Earlier status: works, lightly travelled.** The original 58-tool surface is implemented and was exercised against a
> live ArmorPaint 1.0: a 66-call sweep covering 42 of the tools returned **60 OK, 6 structured errors (all
> deliberate bad-input probes), 0 crashes or timeouts**, at 15–513 ms per call. That includes the
> destructive surface — `project_new`, `project_open`, `project_save_as`, `material_delete`,
> `export_*` and the paint ops — run against a scratch project.
>
> **Linux** was then tested separately (Arch/CachyOS, ArmorPaint 1.0 system package, Vulkan/RADV):
> an MCP stdio sweep over all 58 of the original tools returned **77 OK and 5 structured errors (all deliberate
> probes), 0 crashes or timeouts**. Getting there needed Linux-specific plugin fixes; see
> [Linux notes](#linux-notes).
>
> **The known-limitation fixes** were first verified on Linux against ArmorPaint 1.0 built from the
> pinned commit, under Xvfb with a software Vulkan driver, through the same `call_tool` entry point
> an MCP client uses (`tests/test_live.py`), on both the **stock** build and a build carrying the
> **native extension**. That covers an end-to-end sweep of the original tools, doze → automatic
> wake, batching, a heavy op held while a mouse button is down, window capture, synthetic UI input,
> keyboard undo/redo, and every extension tool.
>
> **On a real desktop** (ArmorPaint 1.0 Arch package, stock, KDE Plasma 6 on Wayland with ArmorPaint
> under XWayland, RADV on a Vega iGPU, launched from the desktop menu) that testing found two bugs
> Xvfb had hidden, both now fixed in **bridge 2.0**: polling leaked a file descriptor per poll and
> hung ArmorPaint after ~30 s (see [Linux notes](#linux-notes)), and keyboard undo worked only 6
> times in 10. After the fixes: 13 of the 14 live tests pass (the 14th needs the extension and is
> skipped), keyboard undo and redo 20/20 each, 150 s of continuous requests with a flat descriptor
> count, wakes from 15–90 s dozes in 19–37 ms, and a real MCP stdio session listed every tool of that release (88). 22
> offline tests cover the rest (`tests/test_offline.py`). The extension build has not been re-run
> since the fixes.
>
> What that does **not** cover: **macOS is untested** end to end, and so are the new **Windows**
> code paths (window capture via `PrintWindow`, wake and UI input via `PostMessage`), which were
> written against `windows_system.c` and the Win32 documentation but have not run on Windows
> hardware yet. Long painting sessions, huge meshes and 4K exports are untested. The native
> extension builds against the pinned 1.0 commit named in `docs/UPSTREAM_CHANGES.md`; upstream
> `main` has since changed its UI-handle API, which neither the extension nor the plugin supports
> yet. Expect rough edges outside the tested path, and please report them.

---

## Architecture

One diagram, and it explains most of the design:

```
   ┌───────────────┐    MCP over stdio (JSON-RPC)    ┌──────────────────────┐
   │  MCP client   │ ──────────────────────────────► │   armorpaint-mcp     │
   │ (Claude Code, │ ◄────────────────────────────── │   server (Python)    │
   │  Claude, …)   │                                 └──────────┬───────────┘
   └───────────────┘                                            │
                                     write  req/<id>.json  (via os.replace — atomic)
                                     ring   doorbell       (the id, also via os.replace)
                                     poll   res/<id>.done  (5 ms → 25 ms → 100 ms)
                                                                │
                                                     ┌──────────▼────────────┐
                                                     │     file mailbox      │
                                                     │  <spool>/req/  res/   │
                                                     │  doorbell             │
                                                     │  heartbeat.json       │
                                                     │  bridge.lock          │
                                                     └──────────┬────────────┘
                                                                │
                                     read the doorbell, open that request,
                                     delete it, run it, write
                                     res/<id>.json then res/<id>.done
                                     — at most ONE per frame, inside on_update
                                                                │
 ┌──────────────────────────────────────────────────────────────▼───────────────────────┐
 │ ArmorPaint 1.0  (stock binary — nothing patched, nothing replaced)                   │
 │                                                                                      │
 │   data/plugins/armorpaint_mcp_bridge.c   the bridge: one minic plugin, one dispatcher           │
 │          │ calls                                                                     │
 │          ▼                                                                           │
 │   529 minic bindings ─► project · assets · objects · materials · nodes ·             │
 │                         brush & paint · viewport · export · filesystem               │
 │   + mcp_ext_call       ─► layers · undo · export format · bake · render settings ·   │
 │     (optional patch)      live lists · camera · console        (detected at run time)│
 └──────────────────────────────────────────────────────────────────────────────────────┘
```

The server also talks to ArmorPaint's **window** directly — screenshots, a synthetic pointer
wiggle that wakes a sleeping app, and clicks/keys for UI automation — so those work on any build.

Two things about this shape are worth knowing up front, because they are consequences of what
ArmorPaint's plugin API actually offers, not preferences:

- **The transport is files, not a socket.** ArmorPaint's plugin API exposes no inbound socket
  binding at all; its only network calls are hardcoded HTTPS GETs. A localhost listener is
  unreachable from inside a plugin. File I/O is unrestricted.
- **The bridge is a plugin, not a fork.** It runs on the binary you already bought, survives app
  updates, and needs no compiler.

Both are argued in full, with the source citations, in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).
The byte-level contract is [docs/PROTOCOL.md](docs/PROTOCOL.md).

## Requirements

| | |
|---|---|
| ArmorPaint | **1.0** (the current C/minic generation). The plugin system in the pre-2025 Haxe/Kha builds is a different thing entirely and is not supported. |
| OS | Linux verified, including the new window-level features. Windows 10/11 verified for the original tool set; its new capture, wake and input paths are untested. macOS paths are written but untested. |
| Python | 3.11+ |
| MCP client | Anything that can launch a stdio MCP server (Claude Code, Claude Desktop, …) |
| Compiler | **None** for the core toolkit. Only the optional native extension (layers, undo history, export format, bakes, render settings, camera) needs a self-built ArmorPaint. |

## Install

Short version; the careful one is [docs/INSTALL.md](docs/INSTALL.md).

1. **Copy the bridge into ArmorPaint.** Put `plugin/armorpaint_mcp_bridge.c` in
   `<ArmorPaint>/data/plugins/`. (That directory is beside `ArmorPaint.exe`, and it already
   contains `autosave.c`, `converter.c` and friends — that is how you know you found it.)
2. **Enable it.** In ArmorPaint: **Plugins** tab → **Preferences** button → the **Plugins** list →
   tick `armorpaint_mcp_bridge` (the list shows the filename without its extension). It starts
   immediately; no restart. The choice is remembered, and the plugin
   auto-starts on subsequent launches.
3. **Install the server.** `pip install -e .` in this repo (or point `uv` at the directory).
4. **Register it with your client.** In `.mcp.json`:

   ```json
   {
     "mcpServers": {
       "armorpaint": {
         "command": "python",
         "args": ["-m", "armorpaint_mcp"]
       }
     }
   }
   ```

   `command` must be a Python that has this package installed. For Claude Code working *in* this
   repo on Linux/macOS, a venv is simplest: `python -m venv .venv && .venv/bin/pip install -e .`,
   then set `"command": ".venv/bin/python"` (relative paths resolve against the repo root).
   `.mcp.json` is gitignored here, so that file stays local to your checkout.

   Usually that is all: the server finds the spool by locating your ArmorPaint install (it only
   accepts a candidate directory that really contains `data\plugins`). If your install is somewhere
   unusual — the itch.io app, for instance — add `"env": {"ARMORPAINT_DIR": "C:\\ArmorPaint"}`.

5. **Check the handshake.** Ask the agent to call `ap_ping`. A healthy answer names the app version
   and the open project. If it reports the bridge as absent, see
   [Troubleshooting](#troubleshooting).
6. **Optional: the native extension.** For layer management, exact undo/redo, export format / bit
   depth / preset, bakes, render settings, live project lists, camera views and console read-back,
   build ArmorPaint from source with one added binding:

   ```sh
   git clone https://github.com/armory3d/armorpaint && cd armorpaint
   git checkout 906418acc600132fa927876d208eb452dc5a0967        # ArmorPaint 1.0
   python /path/to/armorpaint-mcp/patch/apply_ext_patch.py .
   cd paint && ../base/make --compile                           # Linux; see UPSTREAM_CHANGES.md for Windows/macOS
   ```

   Use that binary with the same plugin file — the bridge detects the extension by itself
   (`ap_get_app_info` → `ext_state: 1`). Details and the exact diff:
   [docs/UPSTREAM_CHANGES.md](docs/UPSTREAM_CHANGES.md).

## Quickstart

With the bridge enabled and a mesh already imported, a prompt like this exercises the whole path —
read state, mutate the node graph, write files to disk:

> Using ArmorPaint: open `D:/work/goblin.arm`, tell me which material is active and which paint
> channels it has enabled. Then add a Noise node, wire its output into the base-color input of the
> output node, set its scale to 4.0, fill the layer with it, and export the textures to
> `D:/work/out`. List what landed there.

That is `ap_project_open` → `ap_material_get_active` → `ap_node_add` → `ap_node_connect` →
`ap_node_set_value` → `ap_material_update` → **`ap_fill_layer`** → `ap_export_textures` →
`ap_fs_list`. Each step is one request, one frame.

Do not drop the `ap_fill_layer`. Without it every call still returns success and the model, the
viewport and the export are all unchanged — the graph is the paint source, not the render. See
[The loop that actually works](#the-loop-that-actually-works).

A chain like this completes in well under a second: the bridge polls every frame while a
conversation is active. Wrap many small edits in one `ap_batch` call and several of them run per
frame. A bake or a large export still takes as long as ArmorPaint takes.

## Tool index

106 tools, in three groups by what answers them:

- **Stock bridge** — a named minic binding or a registered struct field, on any ArmorPaint 1.0.
  Tabulated in [docs/MINIC_DIALECT_AND_API.md](docs/MINIC_DIALECT_AND_API.md) §2.13.
- **Native extension** (marked ◆) — the one `mcp_ext_call` binding added by
  `patch/apply_ext_patch.py`. On a stock build these answer `unsupported` with a hint.
  §2.14 of the same document lists what the stock API lacks and which remedy covers each gap.
- **This server** (marked ▣) — the window (screenshots, synthetic input) and the filesystem; no
  bridge round trip, any build.

**Bridge & session** (8)

| Tool | Does |
|---|---|
| `ap_bridge_status` | **Call this first when anything fails.** Diagnoses the connection with no round trip: resolved spool path, heartbeat presence and whether its clock is advancing, plus a plain-language diagnosis and next step |
| `ap_ping` | Liveness: app version, uptime, open project, busy flag |
| `ap_get_app_info` | Window geometry, data path, project format version |
| `ap_bridge_set_enabled` | Turn the bridge off (it then refuses everything but this and `ap_ping`) or back on — both work from the agent |
| `ap_bridge_set_idle` | How long the bridge keeps ArmorPaint awake after a request before letting it sleep (default 10 s; `-1` = never). Persisted |
| `ap_batch` | Run up to 64 bridge tools as **one** request, in order, several light steps per frame; per-step results, optional `stop_on_error` |
| `ap_console_write` | Write to ArmorPaint's console at info/error/log level |
| `ap_show_message` | Transient status message, or a modal box |

**Project** (8)

| Tool | Does |
|---|---|
| `ap_project_new` | New project |
| `ap_project_open` | Open an `.arm` (existence-checked first) |
| `ap_project_save` | Save in place; errors with `no_project` if never saved. **Deferred** — see below |
| `ap_project_save_as` | Set the filepath, then save. **Deferred** — ArmorPaint queues the write for the next frame (`sys_notify_on_next_frame`), so a success reply means *queued*, not *written*. Confirm with `ap_fs_stat` if it matters |
| `ap_project_get_info` | Filepath, basepath, version, envmap, camera FOV, BGRA flag |
| `ap_project_list_texture_assets` | Imported texture assets (a save/load snapshot; labelled as such) |
| `ap_project_list_scripts` | Project scripts (a save/load snapshot; labelled as such) |
| `ap_quit` | Quit ArmorPaint |

**Import / export** (7)

| Tool | Does |
|---|---|
| `ap_import_asset` | Import a texture, mesh or `.arm` by path |
| `ap_import_envmap` | Import an environment map |
| `ap_set_envmap_params` | Envmap strength and angle |
| `ap_export_textures` | Export the texture set to a directory, and report the filenames. Plain: ArmorPaint's current settings (8-bit PNG, `generic`, base name from the last export dialog). ◆ With `format` (png/jpg/exr), `bits` (8/16/32), `quality`, `preset`, `layers` (visible/selected/per_object/per_udim_tile) and `filename` |
| `ap_export_presets` ◆ | The export presets (generic, unreal, unity, …) and the active one |
| `ap_export_material_bake` | Bake the material to a plane and export it |
| `ap_export_mesh` | Export the mesh as `.obj` |
| `ap_export_material` | Export the material as `.arm` |

**Filesystem** (4) — `ap_fs_list`, `ap_fs_stat`, `ap_fs_mkdir` run inside ArmorPaint;
`ap_read_image_file` is answered by the server itself and hands back an image, so you can look at
what an export produced (and it still works while ArmorPaint is closed). Enough for an agent to
find its inputs and confirm its outputs, without a second tool server.

**Introspection** (5)

| Tool | Does |
|---|---|
| `ap_get_context` | The workhorse read: tool, brush, layer, material, viewport mode, … (15 fields) |
| `ap_get_config` | The 16 readable config fields |
| `ap_set_config` | Write those same 16 (`layer_res`, `camera_fov`, `workspace`, `workflow`, …) |
| `ap_get_main_object` | The active paint object: name, visibility, transform |
| `ap_get_object` | Any object by name |

**Objects & meshes** (6) — `ap_shape_list`, `ap_shape_add`, `ap_object_duplicate`,
`ap_object_set_transform`, `ap_object_set_visible`, `ap_append_mesh`.

**Materials** (8)

| Tool | Does |
|---|---|
| `ap_material_get_active` | Active material name and its 9 paint-channel flags |
| `ap_material_update` ⚠ | Recompile the material after node edits. **This does not change the viewport** — see "The loop that actually works" below |
| `ap_material_create` | Create a material |
| `ap_material_select` | Make a material active, by name |
| `ap_material_delete` | Delete a material, by name |
| `ap_material_assign` | Assign a material to an object |
| `ap_material_set_channels` | Toggle the per-material paint channels |
| `ap_material_list` | ◆ **Live** list with the extension; on a stock build a save/load snapshot, labelled `live: false` |
| `ap_project_lists` ◆ | Live materials, textures, brushes, fonts and paint objects |

**Material nodes** (7) — `ap_node_list` (nodes *and* link topology), `ap_node_get` (every
socket and button with its index, name, type and default, e.g. `4:Scale:VALUE=5`), `ap_node_add`
(returns the same socket tables), `ap_node_remove`, `ap_node_connect`, `ap_node_disconnect`,
`ap_node_set_value` (float / color / vector / button).

Node types are Blender-style uppercase identifiers — `TEX_NOISE`, `TEX_BRICK`, `RGB`, `MIX_RGB` —
and `ap_node_add` validates against the **socket catalogue** (`armorpaint_mcp/data/node_sockets.json`,
generated from ArmorPaint's own node sources by `tools/gen_node_sockets.py`): 68 types a material
canvas can create here. The hand-written list it replaced also held `BOOL`, `ENUM`, `RGBA`,
`STRING`, `VECTOR` and `CUSTOM`, which are socket and button types that ArmorPaint rejects as
unknown node types (checked live).

**Whole graphs & recipes** (6) — one call instead of dozens, and nothing half-done:

| Tool | Does |
|---|---|
| `ap_node_graph_get` | The whole graph in one call: every node, its sockets **by name** with their values, its buttons, and every link with both ends named |
| `ap_node_graph_apply` | Build or change the graph from a declarative spec: nodes by your own keys, sockets by name (`"noise.Color -> mix.Color 2"`, `"Value[1]"` where names repeat), dropdowns by option name (`"blend_type": "Multiply"`). Validated against the catalogue **before** anything is sent (types, socket names, value shapes, link cycles), then sent as batches, recompiled, and filled so it shows. **Any failing step puts the graph back exactly as it was.** Keeps the previous graph as a snapshot; lays out new nodes automatically |
| `ap_node_graph_lint` | Cycles, nodes that feed nothing, links into paint channels the material has switched off, socket type conversions |
| `ap_node_graph_snapshot` / `ap_node_graph_restore` | Save the graph, and bring it back with the fewest operations (re-created nodes get new ids; `id_map` says which). This is how a node edit is undone: **ArmorPaint's history does not record node edits** |
| `ap_node_recipe` | Parameterised looks: `worn_painted_metal`, `painted_wood`, `stone`, and `edge_wear_grunge` (curvature-driven edge wear masked by Voronoi grunge). List, render the spec, or apply |

**Painting & viewport** (16)

| Tool | Does |
|---|---|
| `ap_select_tool` | Select one of the 14 painting tools, and read back what took |
| `ap_set_brush` | Radius, opacity, hardness, scale, angle, blending |
| `ap_paint_stroke` | A stroke in screen space, **any length**: up to 48 points go in one request, longer strokes are streamed over several frames as one continuous ArmorPaint stroke. Per-point pressure (`[x, y, radius, opacity]` multipliers, verified live: a taper really is thicker at its start), `smooth` (Catmull-Rom), `spacing`, `taper` (in/out/both), seeded `jitter`, or `generate` a scratch, zigzag, spiral or scattered dabs. `record` films it into a contact sheet |
| `ap_paint_stroke_world` | The same in world space (camera-independent aim; the point must still be visible) |
| `ap_stroke_begin` / `ap_stroke_points` / `ap_stroke_end` | Paint one stroke piece by piece and look in between. Any other tool call closes an open stroke (a capture does not); so do 5 s without points |
| `ap_paint_stroke_pointer` ▣ | A real press-drag-release in the window, so ArmorPaint's **own** stroke engine paints (its spacing, lazy mouse, symmetry). Window pixels. Paced to the measured frame time: events faster than a frame broke strokes into pieces (measured) |
| `ap_paint_stroke_uv` | A stroke in **texture space** (u right, v down, as in an exported texture or `ap_mesh_uv_layout`), mapped onto the model through its UVs. Split at island changes so it never cuts across the model; parts on faces turned away from the camera are skipped, because painting them would land on whatever faces the camera (measured). Calibrated live against exported textures |
| `ap_fill_layer` | Fill the active layer. The first fill after `ap_material_update` is repeated on the next frame, because on its own it left the viewport showing the previous material in about half of measured edits |
| `ap_set_display_channel` | Switch the viewport display channel (one of 16) |
| `ap_capture_to_project` | Capture the viewport **into the project as a texture asset** (not a file you can read) |
| `ap_capture_window` ▣ | **Screenshot ArmorPaint's window and return it as an image** — the shaded viewport plus the UI. `diff_against` (`"last"` or a capture id) adds what changed: bounding box, fraction and a zoomed image of the change. Reads the window's own pixels from outside the app, so it works while the window is covered and never steals focus: Linux X11/XWayland `XGetImage` (verified, ~130 ms for 1720×960), Windows `PrintWindow(PW_RENDERFULLCONTENT)`, macOS `screencapture -l` (needs Screen Recording permission). Optional `crop` and `downscale` |
| `ap_capture_viewport` | The 3D viewport alone to a real PNG, returned as an image. Works when the build exports `viewport_save_texture_to_file` (upstream since 2026-09-09, commit `1e14e27e`, or `patch/apply_viewport_patch.py`) **or** carries the native extension; the bridge detects either on first use — no flag to set. Otherwise `unsupported` |
| `ap_capture_sequence` ▣ | Film the window for up to 10 s at up to 10 fps, as one contact sheet |
| `ap_camera` ◆ | Preset views (front/back/left/right/top/bottom/reset), orbit, zoom, FOV |

**Look in the same call.** `ap_paint_stroke`, `_world`, `_uv`, `_pointer`, `ap_stroke_end`,
`ap_fill_layer`, `ap_batch`, `ap_node_graph_apply` and `ap_node_recipe` take `capture: {}`: the
window is captured before and after, and the reply carries the after-image, the changed region and
a `no_visible_change` flag, which is how a silent no-op shows itself. Settling waits 3 frames (a
fill is complete on the 3rd, measured) and a noise floor keeps the brush cursor, which moves on its
own, from counting. Bridge replies also carry `timing`: the round trip, and the handler time
inside ArmorPaint.

**Layers** ◆ (8) — the Layers panel, with the same undo steps it pushes.

| Tool | Does |
|---|---|
| `ap_layer_list` | Every layer: index (0 = bottom), id, name, kind (layer/mask/group/filter), selected, visible, opacity, blending, parent, fill material, object mask, scale, angle |
| `ap_layer_select` | Choose the layer `ap_fill_layer` and the paint ops act on |
| `ap_layer_new` | paint, fill, decal, group, black/white/fill mask |
| `ap_layer_delete` / `ap_layer_duplicate` | As the context menu |
| `ap_layer_set` | Rename, opacity, blending (18 modes), visibility, object mask, scale, angle |
| `ap_layer_move` | Reorder, with ArmorPaint's own nesting rules |
| `ap_layer_action` | clear, merge_down, merge_group, to_fill, to_paint, apply_mask, invert_mask |

**History** (3) — `ap_undo` and `ap_redo` (◆ exact, reporting the history; on a stock build they
press ArmorPaint's own `ctrl+z` / `ctrl+shift+z` through synthetic input and then compare the
context, the material, its graph and the window's pixels before and after, so the reply says
whether anything changed), `ap_history` ◆. ArmorPaint's history covers paint, fills, layers and
material create/delete; it does **not** cover node edits, config, camera or mesh operations
(`script_material_*` push no history step, checked live). The checkpoints below cover those.

**Checkpoints** (3) — `ap_checkpoint`, `ap_rollback`, `ap_checkpoint_list`. A checkpoint records
ArmorPaint's history position by step identity (◆), a snapshot of the node graph, or (◆, `kind:
"project"`) the whole project written to a snapshot file without changing the project's own path.
Rollback is checked against the live history first. If the step fell off the end (more than
`undo_steps` since) or was discarded by a branch, it refuses and says which, instead of undoing to
the wrong place. Destructive tools (fill, layer delete/actions, material delete, texture
resolution, mesh ops, project new/open) take one automatically and name it in their reply;
`ARMORPAINT_MCP_AUTOCHECKPOINT=0` turns that off. `ap_batch(atomic=true)` is all or nothing.

**Mesh & UV** (3) — check the mesh before painting, and fix what ArmorPaint can fix:

| Tool | Does |
|---|---|
| `ap_mesh_inspect` | Exports the mesh (as `ap_export_mesh`) and reports UV islands and seams, overlap, UV triangles mirrored against the rest or collapsed, UDIM tiles, texel-density spread between islands, and boundary / non-manifold edges, each issue with a suggestion; plus the layout as an image. Any build |
| `ap_mesh_uv_layout` | The UV layout as an image, v down like an exported texture: overlaps red, optional texel-density heat map |
| `ap_mesh_op` ◆ | The Meshes tab's edits: UV unwrap, normals (smooth/flat/flip), to origin, rotate, decimate, smooth, subdivide, bevel, and a re-import that keeps the layers. A project checkpoint is taken first (none of these is in ArmorPaint's history); UV or topology changes need `confirm_invalidates_paint` |

**Bake & render** ◆ (5) — `ap_bake` (curvature, normal, object normal, height, derivative,
position, texcoord, material/object id, vertex colour, and — with hardware ray tracing — occlusion,
lightmap, bent normal, thickness, into a `TEX_BAKE` node, with every bake parameter),
`ap_bake_status`, `ap_bake_settings`, `ap_render_settings` (SSAO, bloom, contrast, gamma,
vignette, grain, supersampling, `.cube` LUT, texture filtering, clip range),
`ap_texture_resolution` (resize the texture set). Plus `ap_console_read` ◆: the app's last 100
console lines.

**UI automation** ▣ (4) — `ap_ui_click`, `ap_ui_key` (shortcuts with ctrl/shift/alt),
`ap_ui_drag`, `ap_ui_scroll`, in the pixel coordinates of `ap_capture_window`'s image. Delivered
to ArmorPaint's window, not the desktop: the real pointer does not move and focus is not stolen.
Look, act, look again.

**Resources & metadata** ▣ (2) — `ap_resource_search` finds textures, envmaps, meshes, `.arm`
materials, fonts, LUTs and export presets by name across ArmorPaint's data folder, the project's
folder, `$ARMORPAINT_LIBRARY` and folders you pass, and says which tool imports each.
`ap_project_metadata` keeps notes and settings with a project in a `<project>.arm.mcp.json`
sidecar (the `.arm` is never touched).

### The loop that actually works

This one is worth stating plainly, because every step reports success and the obvious ordering
still shows you nothing:

```
ap_node_add / ap_node_set_value / ap_node_connect   edit the graph
ap_material_update                                  recompile it
ap_fill_layer   (or ap_paint_stroke / _world)       APPLY it   <-- the step people miss
ap_capture_window   (or ap_capture_viewport)        look at it
```

`ap_node_graph_apply` and `ap_node_recipe` do all four steps (the capture with `capture: {}`).

**`ap_material_update` does not render.** ArmorPaint's viewport shows the *layer stack*; the node
graph is only the paint *source*. Measured: viewport captures taken before and after a colour
change plus `ap_material_update` are **byte-identical** — the pixels change only once you fill or
paint. `ap_fill_layer` applies to the selected layer: choose it with `ap_layer_select`, or give
the material its own layer with `ap_layer_new(kind="fill")` (native extension).

## Limitations, and how each is handled

Earlier versions listed the limitations below as properties of ArmorPaint's plugin API that
"no amount of work on this repo removes". Each was a real constraint of the **stock plugin API**,
read out of its source. They are now handled from outside it: by the server acting on the window
and the filesystem, by the bridge scheduling its work differently, and, for the functions that
exist inside ArmorPaint but have no binding, by the optional **native extension** — one added
binding, `mcp_ext_call`, that the bridge detects at run time.

| Limitation (stock plugin API) | Remedy | Works on |
|---|---|---|
| **No 3D viewport capture on a stock binary** — `viewport_save_texture` only writes into the project | `ap_capture_window` screenshots the window from outside the app; `ap_capture_viewport` detects `viewport_save_texture_to_file` or the extension by itself (the `HAVE_VIEWPORT_PATCH` flag is gone) | Window capture: any build — Linux verified; Windows and macOS implemented, not yet run on hardware. Viewport-only: builds from after 2026-09-09, or with a patch |
| **ArmorPaint renders at full rate while the bridge is enabled** | The bridge holds the app awake only for `linger` seconds (default 10) after a request, then lets it sleep. The server wakes it before the next request with a synthetic 1-pixel pointer move (Iron resets its idle counter on any input event): measured 120 frames asleep → wiggle → frames resume, ping answered in 38 ms. `ap_bridge_set_idle` tunes or disables it | Linux verified; Windows implemented; macOS defaults to never sleeping because its wake path (`CGEventPostToPid`) is untested |
| **One request per frame** | Strokes are streamed: `stroke_begin` / `stroke_points` / `stroke_end` keep one ArmorPaint stroke open across frames, so a stroke has no length limit. `ap_batch` sends up to 64 steps as one request; the plugin runs several light steps per frame while its per-frame script-call budget allows (each script call costs ~29 KB of minic's 8 MB arena, measured peak 1.5 MB for a batch frame) and gives GPU-heavy steps a frame each. Between requests of a conversation it polls every frame instead of every 50 ms. Measured: 9 steps in 3 frames | Any build |
| **A slow handler is a visible hitch** | Heavy ops (exports, saves, opens, imports, bakes) wait until no mouse button is held in the app, so they never land mid-stroke, and publish `busy` first. (The old README promised a pending-token mechanism that was never implemented; this replaces it.) A handler still runs inline — ArmorPaint's GPU work belongs to the render thread | Any build |
| **No layer control, layer state unreadable** | `ap_layer_*`: list, select, create (paint/fill/decal/group/masks), delete, duplicate, rename, opacity, blending, visibility, reorder, merge, clear, convert, apply/invert mask — each pushing the same undo step as the Layers panel | Native extension |
| **No undo/redo** | `ap_undo` / `ap_redo` / `ap_history` through the extension; on a stock build `ap_undo` / `ap_redo` press the app's own shortcuts (measured: a created material disappears and comes back, 20 of 20 each way) | Extension: exact. Stock: keystroke |
| **No bake-parameter control** | `ap_bake` runs a bake into a `TEX_BAKE` node with every parameter; `ap_bake_status`, `ap_bake_settings` | Native extension |
| **No export format / bit-depth control** | `ap_export_textures` takes `format`, `bits`, `quality`, `preset`, `layers`, `filename`; `ap_export_presets` | Native extension |
| **No tone mapping or LUT** | `ap_render_settings`: SSAO, bloom, contrast, gamma, vignette, grain, supersampling, `.cube` LUT, filtering, clip range | Native extension |
| **No shelf/resource search** | `ap_resource_search` over ArmorPaint's data folder, the project folder and library folders | Any build |
| **No project metadata** | `ap_project_metadata`: a JSON sidecar next to the `.arm` | Any build |
| **No UI automation** | `ap_ui_click` / `ap_ui_key` / `ap_ui_drag` / `ap_ui_scroll`, delivered to the window without moving the real pointer or stealing focus | Linux verified; Windows implemented; macOS implemented, may need Accessibility permission |
| **macOS is untested** | macOS now has its own capture, wake and input backends and defaults to the conservative never-sleep mode — but it still has not been run on a Mac, and nothing here claims otherwise | — |

**What remains**, stated plainly:

- The extension is a patch to a self-built ArmorPaint. The official paid binary gets everything in
  the "any build" rows, plus keyboard undo/redo and UI automation, but not the ◆ tools.
- A handler still runs on ArmorPaint's render thread; the remedy moves heavy work out of the
  user's strokes, it does not make an export free.
- Windows and macOS code paths for capture, wake and input are unverified on real hardware.
- Some things remain out of reach even with the extension: arbitrary script evaluation, and
  anything the extension does not wrap (see `docs/MINIC_DIALECT_AND_API.md` §2.14).

## Review response: what changed, and what cannot

A review of this project named five things it could not do. Each is taken in turn below: what
was built, and what stays out of reach, with the reason. Everything here is tested; what cannot
be tested is listed at the end, not skipped.

### 1. Stroke kinematics

**Built.** ArmorPaint keeps a script stroke open across frames: upstream's
`script_paint_begin_stroke` runs once per stroke and only `script_paint_end` closes it. So
strokes are now streamed (bridge ops `stroke_begin` / `stroke_points` / `stroke_end`) and have no
length limit. Each point can carry its own pressure, as radius and opacity multipliers per dab.
Shaping (smoothing, spacing, taper, jitter) and generators (scratch, zigzag, spiral, dabs) make
organic strokes from a few points. `ap_paint_stroke_pointer` hands a stroke to ArmorPaint's own
stroke engine, and `ap_paint_stroke_uv` paints in texture space.

**Cannot be done.** Real pen pressure and tilt: synthetic window input (XSendEvent, Win32
PostMessage) carries no tablet axes, and the plugin API has no pen binding. Per-point pressure
and the pointer path are the substitute.

### 2. Visual feedback latency

**Built.** A tool can return what it did in the same reply (`capture`), with the changed region
and a `no_visible_change` flag. `ap_capture_window` diffs against an earlier capture.
Recorded strokes and `ap_capture_sequence` give a filmstrip. Replies carry `timing`.

**Cannot be done.** A model correcting a stroke 30 times a second. A model turn takes seconds; a
request round trip takes tens of milliseconds and a capture about 130 ms. A socket would save
milliseconds, not seconds, and the plugin API has no inbound socket anyway
([ARCHITECTURE.md](docs/ARCHITECTURE.md)). What can be done is to cut the number of turns, which
the items above do. A closed control loop would have to run without the model.

### 3. Node graphs

**Built.** A whole-graph read, a declarative apply that validates before it touches anything and
rolls back on failure, lint, snapshots, recipes, and a socket catalogue generated from
ArmorPaint's source.

**Cannot be done on a stock build.** Editing the canvas inside a `GROUP` node (the node API
reaches only the material's top-level canvas), setting string-valued buttons (the setter writes
only floats), and brush graphs (five node types; custom brush nodes cannot read their inputs).
The extension could reach group canvases; that is not done yet.

### 4. Mesh and UV topology

**Built.** `ap_mesh_inspect` and `ap_mesh_uv_layout` diagnose UV and topology problems on any
build. `ap_mesh_op` (◆) runs ArmorPaint's own unwrap and mesh modifiers, and re-imports a
repaired mesh while keeping the layers.

**Cannot be done.** Seam placement, hand UV editing and arbitrary topology edits: ArmorPaint has
no tools for them, only the whole-mesh operations above, so that work belongs in a 3D tool, with
`ap_mesh_op reimport` to bring the result back. Carrying existing paint over to new UVs is not
possible either: layers are stored in texture space and ArmorPaint has no UV-to-UV transfer. This
is why inspection comes before painting, and why UV-changing ops need confirmation.

### 5. State, undo and rollback

**Built.** Checkpoints that combine the undo history (by step identity), graph snapshots and
project snapshots. Rollback refuses with a reason instead of drifting. Destructive tools take a
checkpoint automatically, batches can be atomic, and stock undo reports whether it changed
anything. The server keeps no mirror of ArmorPaint's state. Every read is live, and a
checkpoint's history part is checked against the live history each time, so the two cannot
silently drift apart.

**Cannot be done.** Undoing node, config, camera or mesh operations through ArmorPaint's own
history (upstream does not record them; checkpoints cover them from outside). Unlimited
history: depth is `undo_steps`, and each step keeps copies of layers in GPU memory. A stock build
cannot snapshot an unsaved project without changing its path, so project checkpoints need the
extension.

### Physically untestable, stated rather than skipped

- **Windows and macOS** paths for pointer strokes and filmstrip capture: no hardware here.
  Offline tests check the event construction with mocks; the code stays labelled "implemented,
  not run on hardware".
- **Whether a stroke looks organic** is taste. Tests check geometry: smoothness, spacing, taper.
- **The unwrap guard for builds without `WITH_PLUGINS`**: every build that can load this plugin
  has plugins, so the `#else` branch cannot run; a static test checks it exists.
- **The GPU memory cost of a larger `undo_steps`** depends on the GPU; documented, not asserted.
- **A model in a 30 Hz loop** is not built; `timing` reports measurements, not pass/fail.

## Testing

```sh
python -m pytest tests/                        # offline: no ArmorPaint needed
tools/live_harness.sh build /tmp/ap            # or: build /tmp/ap-ext --ext
tools/live_harness.sh run /tmp/ap &            # Xvfb :99 + openbox + ArmorPaint on lavapipe
DISPLAY=:99 ARMORPAINT_LIVE=1 python -m pytest tests/test_live*.py
```

Live tests skip only for "this needs the extension" or "this needs a stock build", never to
hide a missing test. `tests/fake_armorpaint.py` answers the node, history and project ops offline,
in the reply shapes recorded from a live ArmorPaint.

## Linux notes

- **The spool is per-user, not under the install.** On Linux (and macOS) the plugin uses
  `~/.local/share/armorpaint-mcp/spool` (macOS: `~/Library/Application Support/armorpaint-mcp/spool`),
  and the server defaults to the same path, so no configuration is needed. A relative spool cannot
  work there: ArmorPaint resolves relative *reads* against the executable's directory but relative
  *writes*, `mkdir` and `rm` against the working directory, so requests would land in one
  directory and be looked for in another. `getenv` is not bound, so the plugin finds the home
  directory by probing `/root`, `/home/*`, `/var/home/*` and `/Users/*` for the one it can write to.
  The path it chose is in the ArmorPaint console (`armorpaint-mcp bridge ... listening on ...`); if
  your home lives elsewhere, set `ARMORPAINT_SPOOL` to that path.
- **Distro packages put the plugins directory under root.** For example, Arch's `armorpaint`
  package installs to `/usr/lib/armorpaint`, so `data/plugins` is root-owned and neither a copy nor
  the in-app **Import** button works without privileges. Symlink the bridge in once, and later
  updates to the repo take effect when you reload the plugin:

  ```sh
  sudo ln -s "$PWD/plugin/armorpaint_mcp_bridge.c" /usr/lib/armorpaint/data/plugins/armorpaint_mcp_bridge.c
  ```
- **Use bridge 2.0 or later; 1.x hangs ArmorPaint.** On Linux and macOS every directory listing in
  ArmorPaint 1.0 leaks a file descriptor (`close_dir` in Iron's `kong/dir.c` is an empty function),
  and bridge 1.x listed `req/` to poll, up to 60 times a second. Launched from the KDE Plasma menu
  (a systemd user unit, so systemd's default soft limit of 1024 descriptors) ArmorPaint ran out
  after about 30 s of activity and froze for good inside a Vulkan present: grey window, no error.
  Launched from a terminal with a higher limit, the same leak just took longer. macOS runs the same
  POSIX code, by reading the source, with a default limit of 256; it has not been observed there. Bridge 2.0 learns
  request ids from a `doorbell` file instead and does not list anything while polling
  ([PROTOCOL.md](docs/PROTOCOL.md#the-doorbell)). `ap_bridge_status` warns if it finds a 1.x bridge.
  Exports and `ap_fs_list` still list one directory each, so they still leak one descriptor per
  call.
- **The window does not need focus.** The bridge kept answering throughout testing while
  ArmorPaint was an unfocused background window, on Xvfb and on KDE Plasma 6 (XWayland). Waking it, window capture and UI input all go to
  the window through the X server (`XSendEvent`, `XGetImage`), so the server needs `DISPLAY` for
  ArmorPaint's display; on Wayland desktops ArmorPaint runs under XWayland, which is enough.

## Troubleshooting

**ArmorPaint freezes — grey window, no error — after a minute or so of agent activity.**
That is bridge 1.x on Linux or macOS running out of file descriptors (see
[Linux notes](#linux-notes)). Update `armorpaint_mcp_bridge.c` in the plugins folder and restart
ArmorPaint; `ap_ping` should report `bridge_version` 2.0.0 or later. To confirm the diagnosis on a
frozen app: `ls /proc/$(pgrep -x ArmorPaint)/fd | wc -l` near 1024, mostly entries for the spool's
`req` directory.

**`bridge_version_mismatch`.**
The server and the plugin are from different releases: a server that predates bridge 2.0 cannot
drive it, because it never rings the doorbell. Update the server, and restart your MCP client so
it relaunches it; a long-running client keeps the old server process.

**The agent says the bridge is not detected.**
Check, in order: (1) `armorpaint_mcp_bridge.c` is in `<ArmorPaint>/data/plugins/` — the directory beside the
executable, *not* the config directory; (2) its checkbox is ticked in Preferences → Plugins;
(3) `bridge.lock` and `heartbeat.json` exist in the spool directory; (4) the server and the plugin
agree on where the spool *is*.

That last one is the common case and the confusing one, because both halves are individually
healthy. The server reports the spool path it resolved and how it decided, in every transport
error — read that path and compare it to where the plugin is actually writing. If the server fell
back to its per-user default (`%LOCALAPPDATA%\armorpaint-mcp\spool`) it means it could not find an
ArmorPaint install at all; set `ARMORPAINT_DIR`.

If `heartbeat.json` exists but its `t` value is not advancing between two reads, look at its
`dozing` field. `"dozing": true` is **normal**: the bridge lets ArmorPaint sleep between requests,
and the server wakes it with a synthetic pointer move when it has something to send
(`ap_bridge_status` does this for its probe). If waking fails — reported in the error's `wake`
field; on Linux the server needs `DISPLAY` pointing at ArmorPaint's display — move the pointer over
ArmorPaint's window, or call `ap_bridge_set_idle(linger=-1)` so it never sleeps. `"dozing": false`
with a frozen `t` means the plugin loaded but is not being ticked: a modal dialog, or a hang.

**Does the ArmorPaint window have to be in the foreground?**
**No.** ArmorPaint has two sleep gates — a Windows-background gate (3 frames) and an idle gate
(120 frames) — and both increment the *same* counter, which the bridge resets on every frame while
it is holding the app awake (traced in `iron.h`; `docs/MINIC_DIALECT_AND_API.md` §0.3). Between
requests it stops resetting it and the app sleeps; any input event — including the synthetic
pointer move the server sends — resets the counter, so a background window wakes without being
focused. The tolerance is narrow (at most 3 missed frames on a backgrounded Windows window), so if
you modify the plugin, keep `iron_delay_idle_sleep()` ahead of every early return in `on_update`.
A *minimized* window has not been separately measured.

**The plugin is not listed in Preferences → Plugins.**
The list shows only files ending in `.c`, read directly from `<ArmorPaint>/data/plugins`. A `.txt`
extension added by a browser download, or the file sitting one directory too high, both produce an
empty row. The list is also cached until the panel is reopened, so close and reopen Preferences
after copying the file. Alternatively use the **Import** button in that same panel, which copies a
`.c` or `.zip` into the right place for you.

**It is listed, ticks on, and nothing happens.**
Look at ArmorPaint's **Console**. minic reports compile and run errors as
`armorpaint_mcp_bridge.c:<line>: error: <message>` and does not pop a dialog — a plugin with a syntax error
fails quietly. Note also that some minic failures are entirely silent by design (the catalogue is
§1.12), which is why the bridge logs a version banner on load: no banner in the console means the
script did not reach the end of `main`.

**ArmorPaint is installed under `C:\Program Files`.**
Then `data\plugins\` needs elevation to write, and ArmorPaint itself redirects its `config.json` to
`%USERPROFILE%\Saved Games\ArmorPaint\` — while still loading plugins from the install directory.
Use the in-app **Import** button rather than copying by hand, and point the spool somewhere
writable with `ARMORPAINT_SPOOL`. Installing ArmorPaint outside `Program Files` avoids the whole
class of problem. Details in [docs/INSTALL.md](docs/INSTALL.md).

## Documentation

| File | Contents |
|---|---|
| [docs/INSTALL.md](docs/INSTALL.md) | Step-by-step Windows install |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Why a file mailbox, why a plugin, and the constraints that forced both |
| [docs/PROTOCOL.md](docs/PROTOCOL.md) | The wire contract — either half can be reimplemented against it |
| [docs/MINIC_DIALECT_AND_API.md](docs/MINIC_DIALECT_AND_API.md) | The minic dialect and the whole plugin API, read out of the source. If you are writing a plugin, this is the document. |
| [docs/API_REFERENCE.md](docs/API_REFERENCE.md) | The generated binding list |
| [docs/UPSTREAM_CHANGES.md](docs/UPSTREAM_CHANGES.md) | The optional native extension and viewport patch, exactly |
| [tests/](tests/) | Offline tests (no app needed) and `test_live*.py` (`ARMORPAINT_LIVE=1`, against a running ArmorPaint); see [Testing](#testing) |
| [tools/](tools/) | `live_harness.sh` (build and run ArmorPaint headless for the live tests) and `gen_node_sockets.py` (regenerate the socket catalogue) |

Everything in `docs/` was derived by reading ArmorPaint 1.0 at commit
`906418acc600132fa927876d208eb452dc5a0967`. Public web documentation for ArmorPaint's scripting
describes the pre-2025 Haxe/Kha version and is wrong for this generation — a trap worth naming.

## Credits and licensing

**armorpaint-mcp** is MIT licensed — see [LICENSE](LICENSE).

**ArmorPaint** is by the Armory3D project and is licensed **zlib/libpng**:
<https://armorpaint.org> · <https://github.com/armory3d/armorpaint> ·
[manual](https://armorpaint.org/manual)

This repository contains no ArmorPaint source and is not a fork. The optional patch in `patch/`
edits *your own* checkout in place, marks every line it inserts, and is reversible.

ArmorPaint's development is funded by sales of its binaries. They are **paid** — from
<https://armorpaint.org/download> and the itch.io store at
<https://armorpaint.itch.io/armorpaint>. Building from source is explicitly supported by upstream
and is free, but if this tool is useful to you, the project it drives is worth paying for.
