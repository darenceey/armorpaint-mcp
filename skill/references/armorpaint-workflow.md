# ArmorPaint Texturing Workflow

Primary sources, all local and verified against ArmorPaint 1.0 (`906418ac`):
- `paint/sources/minic_api_list.h` — the 529 bindings; nothing else exists
- `paint/sources/minic_impl.c` — what each `script_*` binding actually does, including its silent guards
- `paint/sources/io/export_texture.c` — export naming, presets, channel routing
- `paint/sources/util/util_layer.c` — what a fill really does
- `paint/assets/plugins/dev/test.c` — upstream's own end-to-end drive of the API, step by step
- `docs/MINIC_DIALECT_AND_API.md` §2.2–2.10 — the tool surface, with line numbers

## The Document Model

Understand this before planning any session.

- **There is always a document.** ArmorPaint boots with a default paint object (a bevelled cube) and
  one layer. "No project open" is not a state. What varies is whether the document has a **filepath**
  (`ap_project_get_info.filepath == ""` ⇒ never saved) and whether it is **dirty** (window title
  contains `*`).
- **Pixels live in three RGBA render targets**, not in per-channel images:
  - target 0 — base colour RGB, opacity A
  - target 1 — normal RGB, material id A
  - target 2 — occlusion R, roughness G, metallic B, height A

  Every fill, stroke and export is a read or write of those three. This is why "channels" are not
  objects you can create or delete: they are fixed slots.
- **Materials are node graphs.** A material's identity is its canvas name
  (`ctx.material.canvas.name`), and its content is a `ui_node_t` graph you can fully author.
- **Layers are opaque.** You can learn only that one is selected. Not its name, type, opacity,
  visibility, blend mode, or resolution. See "The layer wall".

## Session Start

```
ap_ping                     -> bridge alive? heartbeat t advancing?
ap_get_app_info             -> window title (dirty marker), window size, data path
ap_project_get_info         -> filepath, basepath, envmap, camera fov
ap_get_context              -> material name, layer != NULL, tool, brush_*, viewport_mode, xray
ap_get_main_object          -> paint object name, visibility, transform
```

Answer these four questions before you plan:

1. Is this the human's real project, or a scratch document? (filepath + title)
2. Is there unsaved work? (`*` in the title ⇒ yes ⇒ nothing destructive without asking)
3. Is a layer selected? (`layer == NULL` ⇒ no fill, no paint, ask them to select one)
4. Which material is active, and do I want to touch it at all? (usually: no — make my own)

## Getting Geometry In

| Intent | Call | Effect |
|---|---|---|
| add a mesh alongside what is there | `ap_append_mesh <path>` | `import_mesh_run(path, false, false, true)` — appends, keeps layers |
| replace the paint object | `ap_import_asset <mesh path>` | `import_mesh_run(path, true, true, false)` — **replaces and clears layers** |
| add a primitive | `ap_shape_add <name>` | name must come from `ap_shape_list` or it returns NULL |
| duplicate what is there | `ap_object_duplicate <name>` | |

The default is `ap_append_mesh`. Reach for `ap_import_asset` on a mesh only when the user asked to
start from that mesh, and say so first.

Blender owns silhouette, UVs, material slots and mesh splits. If the texture problem is really a UV
or slot problem, stop and send it back to Blender — ArmorPaint cannot fix it and painting over it
wastes the session.

**You cannot enumerate paint objects.** `context_main_object()` and `ap_get_object <name>` are the
only ways in; there is no list. Ask the human for object names when you need them.

## Materials: The Real Control Surface

Because layers are closed, everything expressive happens in a material graph plus a fill.

```
ap_material_create "wear_pass"          -> new material, becomes active
ap_node_add / ap_node_set_value / ap_node_connect ...
ap_node_set_value ... (kind=float|color|vector|button)
<update>                                -> re-parse; nothing is live until this runs
ap_material_set_channels base=false roughness=true metallic=false normal=false ...
                                        (booleans, and the full channel names: base,
                                         opacity, occlusion, roughness, metallic, normal,
                                         height, emission, subsurface. An abbreviation is
                                         silently ignored, leaving that channel unchanged.)
ap_fill_layer                           -> writes only the enabled channels
```

Four things to internalise:

- **A material's name is its canvas name.** `ap_material_select "wear_pass"` matches
  `materials[i]->canvas->name`. A newly created material is auto-named `Material N` unless you pass a
  name; pass one.
- **The channel flags are a write mask.** The nine `paint_base|opac|occ|rough|met|nor|height|emis|subs`
  booleans on the material become GPU colour-write masks at paint time (`render/make_paint.c:581+`).
  A channel with its flag off is **left untouched**, not cleared. This is the single most useful
  additive tool you have: fill roughness only, and the human's base color survives.
- **`ap_material_list` is degraded.** It reads `project_t.material_nodes`, a snapshot taken at
  save/load — `NULL` in a never-saved project, and blind to materials created this session. Prefer
  `ap_material_get_active` plus your own bookkeeping of names you created.
- **Deleting the last material is refused** (`script_material_delete` returns if
  `materials->length <= 1`).

Read `references/material-nodes.md` for the graph itself.

## Filling

`ap_fill_layer` → `script_fill_layer()` → `layers_update_fill_layer(true)`:

1. clears the selected layer to transparent (with roughness defaulted),
2. re-fills it with the active material across the **whole object** (`FILL_TYPE_OBJECT`),
3. pushes one undo step,
4. respects the material's channel write mask.

So: it is whole-object, it is destructive to that layer, and it is the most reproducible operation in
the whole API — no camera, no window size, no mouse. **Prefer it to strokes for anything that must be
repeatable.**

Guards that make it a silent no-op: no layer selected, or the layer is a group.

## Painting

Strokes exist, and they are the least reliable thing you can do blind: always pass `capture: {}`
and look at `no_visible_change`. They are no longer limited to one frame: any length is streamed
as one stroke, and each point can carry pressure (`[x, y, radius, opacity]` multipliers).

```
ap_select_tool tool=brush           (a NAME, not an index: bake blur brush clone colorid
                                     cursor decal eraser fill material particle picker
                                     select text)
ap_set_brush radius=0.5 opacity=1.0 hardness=0.8 scale=1.0 angle=0 blending=0
ap_paint_stroke_world  "x,y,z; x,y,z; ..."      -> N x script_paint_world + paint_end
ap_paint_stroke        "x,y; x,y; ..."          -> N x script_paint + paint_end
```

- `script_paint(x, y)` takes **window-normalised** coordinates in `[0,1]`. It is "click at this
  fraction of the window": what it marks depends on the camera, the window size, and the panel
  layout, and points outside the paint bounds are dropped.
- `script_paint_world(x, y, z)` projects a world point through the camera's view-projection and
  paints at the resulting screen point. Better, because you can name a point on the model — but it is
  still camera-dependent: a point behind the camera is silently dropped (`clip.w <= 0`), and an
  occluded point paints whatever surface is actually visible at that pixel.
- Every stroke must be terminated. The tools do this for you; if you drive the bindings directly,
  `script_paint_end()` is what dilates and commits.
- **Paint is refused on a fill layer** unless the tool is picker, material or colorid
  (`script_paint_allowed`, `minic_impl.c:187`). A no-op here looks identical to success.

Aiming by texture coordinates: `ap_paint_stroke_uv` maps UV points (read off `ap_mesh_uv_layout`,
v down) onto the model and paints them; a path crossing between UV islands is split, and parts on
faces turned away from the camera are skipped and reported by the direction they face — turn the
view that way (`ap_camera`, extension) and paint them again.

Practical rule: use strokes only when the user has framed the camera and asked for a stroke, or for
deliberately loose hand-work they will review. Everything else goes through a material plus a fill.

## Export

`ap_export_textures <dir>` → `export_texture_run(dir, 0)`. Read `references/pbr-channels.md` for the
channel semantics; the mechanics you must respect:

- **`dir` is a directory, not a filename.** Create it first (`ap_fs_mkdir`).
- **You do not choose the filenames.** The stem is `ui_files_filename`: the opened project's basename,
  or the translated `"untitled"` when nothing has been opened (`base.c:492`,
  `io/import_arm.c:536`). Then a preset suffix per map, then the extension. A project
  `goblin.arm` exported with the stock `unity` preset yields
  `goblin_base.png`, `goblin_nor.png`, `goblin_mos.png`, `goblin_height.png`.
  With more than one paint object an object-name suffix is appended too.
- **You do not choose the format.** 8-bit PNG unless the human changed the format or bit-depth
  handles in the UI; those live on unregistered state. 16-bit means `.exr`.
- **You do not choose the preset.** It is whatever the human last selected in the Export dialog this
  session. If the dialog has never been opened, `export_texture_run` auto-selects `generic`
  (`export_texture.c:500-504`).
- **Always export into a fresh directory**, then `ap_fs_list` it. That gets you the real filenames,
  makes the export atomic-ish from the user's point of view, and stops a bake from overwriting a
  layer export (they derive the same names).

Sibling exports:
- `ap_export_material_bake <dir>` — bakes the **active material** onto a plane and exports that. The
  material-swatch view. Same naming, so give it its own directory.
- `ap_export_mesh <path>` — writes `<path>.obj`.
- `ap_export_material <path>` — writes a `.arm` material you can re-import with `ap_import_asset`.

## Close The Loop

An export you did not read is not a delivery. After every export:

1. `ap_fs_list <dir>` — count and names. Zero files means the export never ran (no preset? no
   layers?).
2. Have the server read the maps back as images and **look at them**:
   - base colour: is the albedo the intended hue, and is it free of baked lighting?
   - the packed map: are the channels where you expect (see `references/pbr-channels.md`)?
   - roughness: does it have variation, or is it a flat grey field?
   - normal: is it mostly `(128,128,255)` with detail where you added it, or garbage?
   - coverage: any unwritten regions, hard UV seams, or a whole-flat map (the classic "fill went to
     the wrong channel" signature)?
3. Compare to the intent in words before you change anything. Name the defect, then fix the one node
   that causes it.
4. Re-export to a **new** directory. Never overwrite the evidence you just judged.

If the look question is about the shaded read rather than the map content, set the viewport channel
for the human (`ap_set_display_channel`) and ask one specific question.

## Saving

- `ap_project_save` requires a filepath. If `filepath == ""`, `project_save(false)` falls through to
  Save-As and pops a file browser in the human's face — guard, and ask for a path.
- `ap_project_save_as <path>` = `project_filepath_set(path)` then `project_save(false)`, exactly as
  `dev/test.c:238` does it. Verify with `ap_project_get_info.filepath`.
- Saving clears the `*` from the window title, which is also how you prove it worked.
- Do not save someone's project on your own initiative. Their `*` is their decision.

## The Layer Wall

Exhaustive grep of the 529 bindings for `layer` returns exactly one operation: `script_fill_layer`.
There is no create, delete, duplicate, rename, reorder, show/hide, group, mask, opacity, blend mode,
resolution, or enumerate. `slot_layer_t` is not a registered struct, so `context_t.layer` cannot be
dereferenced; `project_t.layer_datas` is nulled immediately after save (`export_arm.c:415`) and is
therefore always `NULL` live. `ui_*` bindings draw *your* widgets in the Plugins tab; they cannot
click ArmorPaint's UI.

What to do instead, in order of preference:

1. **Do it in one material graph.** Mixes, masks and remaps that would be separate layers in
   Substance are nodes here. One fill, one layer.
2. **Use per-channel write masks** to compose across several fills without destroying earlier work.
3. **Use `LAYER` / `LAYER_MASK` nodes** to *read* an existing layer inside a material — the layer is
   chosen by an index button whose list the human sees in the UI.
4. **Ask the human**, precisely: "add a layer above `base`, select it, and say go" — one sentence,
   one action, then you continue.

With the native extension none of this is needed: `ap_layer_*` manages the stack directly. On a
stock build a fifth option exists: drive the Layers panel yourself with `ap_ui_click` /
`ap_ui_drag`, after an `ap_capture_window` shows you where things are — slower and more fragile
than asking, so prefer asking for anything beyond one or two clicks.

## Other Limits Worth Knowing Early

"ext" = needs the native extension (`ap_get_app_info` → `ext_state: 1`).

| Wanted | Reality |
|---|---|
| bake AO / thickness / normals to a map | ext: `ap_bake` into a `TEX_BAKE` node (AO, bent normal, thickness and lightmap need hardware ray tracing), poll `ap_bake_status`. Stock: no bake-run binding |
| bake curvature | **reachable** — put a `BAKE_CURVATURE` node in a material; `script_material_update()` re-bakes its preview automatically (`render/make_material.c:222-266`). Without an update it samples black. Expensive: a full-resolution bake on the render thread |
| set document resolution | ext: `ap_texture_resolution`. Stock: `config_t.layer_res` is a preference for the next new project (0=2048, 1=4096, 2=8192, 3=16384) and does not resize the current document |
| list layers | ext: `ap_layer_list` |
| list channels / texture sets / UV tiles | no bindings, even with the extension |
| undo / redo | ext: `ap_undo` / `ap_redo` exact, `ap_history`. Stock: the same tools press ctrl+z / ctrl+shift+z |
| read ArmorPaint's console | ext: `ap_console_read` (last 100 lines) |
| set the camera pose | ext: `ap_camera` (front/back/left/right/top/bottom/reset, orbit, zoom, FOV) |
| tone mapping / LUT / post FX | ext: `ap_render_settings` |
| export format / bit depth / preset | ext: `ap_export_textures` options, `ap_export_presets` |
| arbitrary script eval in-app | no `minic_eval` binding |
| project metadata | any build: `ap_project_metadata` (a sidecar JSON next to the `.arm`) |
| find textures / materials / envmaps on disk | any build: `ap_resource_search` |

Full list with reasons: `docs/MINIC_DIALECT_AND_API.md` §2.14.

## Triage Order

When a result is wrong or absent:

1. **Transport** — `ap_ping`; is the heartbeat advancing; is the bridge enabled? A deaf bridge is the
   only failure that makes *every* op silent at once.
2. **Contract** — did the op return `ok:false`? `unsupported` names the missing binding; `bad_args`
   means your argument never reached the app.
3. **Precondition** — no layer selected; layer is a group; layer is a fill layer and you tried to
   paint; material name did not match its canvas name; socket index out of range; `_update` not
   called; the stroke missed the mesh.
4. **Mask** — the op ran but the channel write mask blocked the channel you were looking at.
5. **Wrong place** — you exported before `_update`, or read an old directory, or looked at the map
   from the previous iteration.
6. **Geometry / UV** — flat or smeared results with correct channels usually mean the mesh or UVs are
   wrong. Back to Blender.
7. **The app** — last. Write a console marker and ask the human what the UI shows.

## Staleness Traps

- `data_get_blob` caches by path **forever** (`engine.c:1879`). Anything the host has read once, it
  will keep serving from memory.
- `import_texture_run` refuses a path already in the project's asset list ("asset already imported")
  and returns; the texture cache would serve the old pixels anyway.
- `project_t.material_nodes` / `brush_nodes` / `mesh_datas` / `camera_world` / `script_datas` are
  save/load **snapshots**, not live state.
- `project_t.assets` is written at save and read at load — treat it as a snapshot too, and do not
  trust its length as a live asset count (see `references/material-nodes.md`, "Wiring an image").
- **Never reuse a filename across iterations.** New path every time.
