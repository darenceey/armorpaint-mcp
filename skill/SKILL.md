---
name: armorpaint-mcp
description: Use when driving ArmorPaint 1.0 through the armorpaint-mcp bridge for PBR texturing, procedural material authoring, Blender-to-ArmorPaint handoff, Unity/HDRP texture handoff, and debugging export, material-graph, or bridge problems. Covers the file-mailbox transport, the material node graph as the primary control surface, the absent layer API, export-and-look verification when there is no viewport capture, and the etiquette of working inside an app a human has open. Ground truth is the local ArmorPaint source and this project's docs; web documentation describes the obsolete pre-2025 Haxe/Kha ArmorPaint and must not be trusted.
---

# ArmorPaint MCP

Use this skill for any work that drives ArmorPaint through the bridge, and for judging whether a
texturing job belongs in ArmorPaint at all.

Default outcome:
- inspect the live session before changing anything
- decide whether the request is additive or destructive, and get consent for destructive
- build the look in a **material node graph**, not in layers
- fill or paint into the layer the human has selected
- export to a **fresh directory**, then **read the exported maps back and look at them**
- iterate on evidence, and hand the app back when done

## Use This Skill When

Use it for:
- procedural PBR material authoring in ArmorPaint (worn metal, painted wood, stone, fabric)
- wiring an imported texture set into a material graph
- exporting a texture set and validating the channel packing
- Unity / HDRP handoff (`BaseColor` / `MaskMap` / `Normal`)
- diagnosing "my export looks wrong", "nothing happened", "the bridge went deaf"
- deciding what is impossible in ArmorPaint and must be asked of the human or done elsewhere

Do not use it as the primary workflow for:
- layer-stack construction, masks, groups, blend modes, layer opacity — **no bindings exist**, this is
  human work in the UI (see "The layer wall")
- baking mesh maps (AO, curvature, thickness) — no bake-run binding
- modelling, UVs, or silhouette work — that is Blender's job
- anything needing a shaded 3D view as the deliverable, on a stock build

## What Makes ArmorPaint Different (read before your first call)

Six facts shape every decision. All are verified against `paint/sources/minic_api_list.h`
(529 active bindings) and the interpreter in `base/sources/libs/minic.c`.

1. **The tool surface is closed.** A plugin can only call those 529 bindings, and the MCP tools are a
   thin, checked wrapper over them. If a capability is not in the tool list, it does not exist —
   `docs/MINIC_DIALECT_AND_API.md` §2.14 records what was excluded and exactly why. Do not invent
   tool names, and do not promise the user a capability you have not found in the server's own list.
2. **There is no layer API.** One layer operation exists in the whole binding table:
   fill the selected layer. Layers are the human's.
3. **There is no undo binding.** Nothing you do can be revoked by you. The human's Ctrl+Z still
   works, but you cannot drive it, and you cannot count on it after a fill.
4. **You are blind by default.** On a stock build there is no plugin path from the GPU to an image
   file for the viewport. Your only real eyes are the PNGs that `export_texture_run` writes to disk.
   On a **patched** build `ap_capture_viewport` gives you the shaded 3D view as an image, which
   changes the workflow substantially — check `ap_get_app_info` → `viewport_patch` once at the start
   and say which mode you are in, rather than assuming either way.
5. **You are a guest on the render thread.** Every handler runs inline in `on_update`, at most one
   request per frame, and the bridge holds the app awake at full frame rate to stay reachable. A slow
   op is a visible hitch in someone's brush stroke.
6. **Recompiling a material is not rendering it.** `ap_material_update` makes a graph edit take
   effect *as a paint source*; the viewport keeps showing the layer stack. Measured: captures before
   and after a colour change plus `ap_material_update` come back **byte-identical**. Nothing you do
   to the graph is visible — in the viewport, in an export, or to the human — until `ap_fill_layer`
   or a paint stroke applies it. Every call in the wrong order still reports success.

## Core Rules

- **Inspect first.** `ap_ping`, `ap_get_app_info`, `ap_project_get_info`, `ap_get_context`. Never
  assume which material is active, whether a layer is selected, or whether a project has a filepath.
- **Read back every write.** minic has no exceptions and every setter is bounds-checked to a silent
  `return`. A bad socket index, a missing node, a wrong material name: all of them look exactly like
  success. Verify by reading the state back, then by looking at output.
- **Additive by default.** Prefer creating a new material over editing the active one, appending a
  mesh over importing one, a new export directory over reusing one, a new file path over overwriting.
- **Never open or new over someone's work.** `ap_project_new` and `ap_project_open` discard the live
  session with no prompt (`project_new(true)` / `import_arm_run_project`). Check the dirty marker
  first (below) and ask.
- **Announce destructive actions before you take them**, in one line, naming what is lost:
  fill (clears the selected layer), material delete, project new/open, `ap_quit`, export into a
  directory that already holds maps.
- **The material graph is your control surface.** Where a Substance workflow reaches for a layer
  stack, here you build nodes and fill. Read `references/material-nodes.md`.
- **Look at your output.** An export you did not read back is not a finished job. Read
  `references/armorpaint-workflow.md` §"Close the loop".
- **One thing per frame, small payloads.** Bulk data moves by path, never inline.
- **Forward slashes in every path.** Backslashes truncate string literals on the plugin side.
- **Ground truth is local.** `docs/MINIC_DIALECT_AND_API.md`, `docs/API_REFERENCE.md`,
  `docs/PROTOCOL.md`, and the ArmorPaint source. Web docs describe a version of ArmorPaint that no
  longer exists (Haxe/Kha, pre-2025) and will send you after bindings that were never in this build.

## Default MCP Workflow

Follow this order unless the task is clearly narrower.

1. **Confirm the bridge is alive**
   - `ap_ping` — is the heartbeat's `t` advancing between two reads? (It is app-uptime seconds, not
     wall clock; never compare it to the system clock.)
   - if it is dead: `ap_bridge_set_enabled true`, and check the plugin is loaded at all
2. **Read the session**
   - `ap_get_app_info` — window title carries the **dirty marker** (see etiquette)
   - `ap_project_get_info` — filepath (`""` means never saved), basepath, envmap, fov
   - `ap_get_context` — active material name, `layer != NULL`, tool, brush params, viewport mode
   - `ap_get_main_object` — the paint object
3. **Classify the request**
   - additive (new material, new nodes, export to a new dir) — proceed
   - destructive (fill, new/open project, delete) — announce, and ask if the project is dirty
   - impossible (layers, masks, bakes, per-set resolution) — say so now and offer the human-side step
4. **Set up the material**
   - `ap_material_create` a named material rather than mutating theirs
   - build the graph: `ap_node_add` → `ap_node_set_value` → `ap_node_connect` → **`_update`**
   - `ap_material_set_channels` to mask which channels a fill is allowed to touch
5. **Apply it**
   - confirm a layer is selected (`ctx.layer != NULL`); if not, ask the human to add/select one
   - `ap_fill_layer` for whole-object coverage (this **clears** the layer first)
   - `ap_paint_stroke_world` only when a stroke is genuinely needed, and only with the camera framed
6. **Export**
   - `ap_fs_mkdir` a fresh, timestamped directory
   - `ap_export_textures <dir>` (layers) or `ap_export_material_bake <dir>` (material swatch on a plane)
   - `ap_fs_list <dir>` to learn the filenames — you do not control them
7. **Look at what you made**
   - have the server read the PNGs back as images and **inspect them**
   - judge base color, then roughness/metallic, then normal; compare against the intent
8. **Iterate, then release**
   - fix in the graph, re-export to a **new** directory (never re-import over an old path)
   - report the exact paths you wrote
   - `ap_bridge_set_enabled false` when the session is over, so the app can sleep again

## Verifying Your Own Work Without A Viewport

There is no `screenshot` tool on a stock build. `viewport_save_texture` PNG-encodes into
`packed_assets` **in memory**, and `iron_encode_png` / `gpu_get_texture_pixels` are not bound. So
build evidence in layers, cheapest first:

**Level 1 — state readback (free, proves nothing about looks).**
`ap_get_context` for tool/brush/viewport mode; `ap_node_list` for node ids, types and every socket's
`default_value`; `ap_material_get_active` for the material name and channel flags. Use this to catch
silent no-ops: a value you set that reads back unchanged means the write was rejected.

**Level 2 — filesystem truth (cheap, proves an op ran).**
`ap_fs_list` / `ap_fs_stat` on the export directory; file count and names after `ap_export_textures`;
`iron_file_exists` for a mesh or material export. This is how upstream's own `dev/test.c` checks
itself — it counts files in the export directory.

**Level 3 — read the exported maps and look at them (the real check).**
`export_texture_run` is the only binding that writes real image files, and it writes 8-bit PNG by
default. Those files are your work product and your eyes. What they do and do not tell you:
- they **do** show base color, roughness, metallic, normal content, seams, missing coverage, wrong
  channel packing, and a fill that landed on the wrong channel
- they are **UV-space**, not a shaded render: they cannot tell you how the material reads on the
  silhouette, under the envmap, or at distance
- a whole-black or whole-white map is the single most common signal that a fill went to the wrong
  channel or that the graph never reached the output node

**Level 4 — bake the material to a plane and look at the swatch.**
`ap_export_material_bake <dir>` (`export_texture_run(dir, 1)`) renders the *active material* onto a
plane and exports that. It is the closest thing to a material preview an agent can obtain, and it is
independent of the mesh's UVs — ideal for judging a procedural look before committing a fill. It
writes the **same filenames** as a layer export, so always bake into its own directory.

**Level 5 — the human's eyes.** For anything about read, feel, or art direction, set the viewport
channel for them (`ap_set_display_channel`, `0` lit / `1` base color / `4` roughness / `5` metallic /
`2` normal) and ask a specific question: "does the wear read as edge wear or as noise?" Do not ask
"does it look good".

**Level 0 — check which of these you actually need.** `ap_get_app_info` reports `viewport_patch`.
When it is `true` (`docs/UPSTREAM_CHANGES.md`, source builds only) `ap_capture_viewport` writes a
real PNG of the shaded viewport and hands it straight back as an image, and Levels 3–5 collapse into
"look at the render": measured 11–15 ms in-app, ~140 ms round trip at 800×600. Set the display
channel first (`ap_set_display_channel`) to isolate base color, roughness or normal in the same way
Level 3 does, but shaded and in silhouette. When it is `false`, you are on the ladder above. Check
once at the start of a session and say which mode you are in — never assume either way.

Two things the capture does **not** do, on any build. It photographs the frame **already drawn**, so
apply your change, let a frame or two pass, then capture — a capture taken in the same breath as the
edit shows you the state before it. And it shows the *layer stack*: if you changed the graph and did
not `ap_fill_layer`, the capture will faithfully show you the old surface while every call reports
success.

## Working In An App A Human Is Using

The bridge exists because ArmorPaint has no socket. It puts you inside someone's live session, on
their render thread, while they may be holding a brush.

**Detect unsaved work before anything destructive.** Any undoable action retitles the window to
`"<name>* - ArmorPaint"` (`history.c:882`); `project_save` clears the star (`project.c:55`). So the
`*` in `ap_get_app_info`'s title is the dirty flag, and it is the only one a plugin can see. Star
present ⇒ the human has unsaved work ⇒ do not new, open, or fill without asking.

**Know the destructive set.** These lose work with no prompt and no undo:
- `ap_project_new`, `ap_project_open` — replace the whole session
- `ap_import_asset` with a `.arm` path — a project `.arm` routes to the *project* importer and
  replaces the session (`path_is_project` is just "ends with .arm"). Only pass `.arm` files you
  exported yourself, and use `ap_project_open` when you actually mean to open a project
- `ap_fill_layer` — clears the selected layer, then fills it
- `ap_material_delete`, `ap_quit`
- a second export into a directory that already holds maps — filenames are derived, so it overwrites

**Prefer the additive form of every op.** New material instead of editing theirs. `ap_append_mesh`
instead of `ap_import_asset` for geometry. Channel flags off (`ap_material_set_channels`) so a fill
touches only roughness and leaves their base color intact — the flags become GPU colour-write masks,
so untouched channels are genuinely untouched, not zeroed.

**Do not surprise them with UI.** `ap_project_save` on a project that has never been saved falls
through to Save-As, which pops a file browser in their face. Guard on
`ap_project_get_info.filepath != ""` and ask for a path instead.

**Stay cheap.** One request per frame; keep handlers short; never poll in a tight loop. The bridge
defeats the idle-sleep gate to stay reachable, which means ArmorPaint renders at full rate — real
power and GPU cost — for as long as it is enabled. Turn it off when you are done.

**Say what you did.** End with the concrete list: material name, nodes added, layer filled or not,
files written (absolute paths), and anything you deliberately did not do because it needed consent.

## High-Value Decision Rules

### The Layer Wall

The request mentions layers, masks, groups, opacity, blend modes, or per-layer resolution.

- Say plainly that none of it is reachable: `script_fill_layer` is the only layer binding in the 529,
  `slot_layer_t` is not a registered struct so the active layer cannot even be inspected, and
  `project_t.layer_datas` is `NULL` in a live session.
- Then convert the request into what *is* reachable: a material graph plus a fill, per-channel write
  masks, a `LAYER` / `LAYER_MASK` node that reads a layer the human selects in the node's combo, or
  an explicit ask ("add a layer above and select it, then tell me").

Read: `references/armorpaint-workflow.md`, `references/material-nodes.md`

### Building A Look

- Author it as a node graph and judge it as a **plane bake** before you touch a layer.
- Roughness first, then metallic, then base color breakup, then normal/height last and lightly.
- Keep the graph shallow and named by role: one texture source, one remap, one mix, into the output.
- Always finish with `_update` — nothing you set is visible or fillable until the material re-parses.

Read: `references/material-nodes.md`, `references/pbr-channels.md`

### Handing Off To Unity

- HDRP wants `BaseColor`, `MaskMap` (R metallic, G occlusion, B detail, A smoothness) and `Normal`.
- ArmorPaint's stock `unity` preset already emits exactly that as its `mos` map. Prefer it.
- You **cannot select an export preset from a plugin** — the selection lives on a UI handle. Either
  the human picks it once in the Export dialog, or you write a preset JSON and ask them to select it.
- Verify the packing by reading the exported `mos` map back and checking the channels separately.

Read: `references/pbr-channels.md`

### Nothing Happened

Work in this order, because each step is cheaper than the next:

1. Is the bridge alive? (`ap_ping`; heartbeat `t` advancing; bridge enabled?)
2. Did the op return `ok`? An `unsupported` error names the missing binding — believe it and stop.
3. Was a precondition silently false?
   - fill/paint: `ctx.layer == NULL`, layer is a group, or the layer is a **fill layer** (paint is
     refused on fill layers unless the tool is picker/material/colorid)
   - paint: the screen point missed the mesh, or the world point is behind the camera (dropped
     silently); strokes are camera-dependent
   - material op: the name did not match — a material's name is its **canvas** name
   - node op: socket index out of range, or you never called `_update`
4. Did it work but land somewhere you did not look? Check the channel write masks
   (`ap_material_get_active`), then export and look.
5. Only then suspect the app: `ap_console_write` a marker, and ask the human what the UI shows.

Read: `references/armorpaint-workflow.md`

### Re-Importing Something You Just Wrote

Do not write to a path ArmorPaint has already read. Two independent caches make it stale:
`data_get_blob` memoises by path forever (`engine.c:1879`), and `import_texture_run` refuses a path
already in the project's asset list, logging "asset already imported" and returning. **Use a new
filename every iteration.**

## Reference Map

- Session shape, geometry, fills, strokes, export, iteration loop, triage:
  `references/armorpaint-workflow.md`
- Channel layout, export tokens, presets, Unity/HDRP handoff, PBR guardrails:
  `references/pbr-channels.md`
- Node graph mechanics, verified socket tables, look recipes: `references/material-nodes.md`
- Wire protocol contract (transport, ids, two-file commit, error codes): `../docs/PROTOCOL.md`
- Binding-level authority, what is excluded and why: `../docs/MINIC_DIALECT_AND_API.md`
- Generated API dump from this exact build: `../docs/API_REFERENCE.md`
- Optional viewport-capture patch: `../docs/UPSTREAM_CHANGES.md`
