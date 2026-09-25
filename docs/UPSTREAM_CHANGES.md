# Upstream changes

This project is **not a fork of ArmorPaint** and ships no modified ArmorPaint source. The core
toolkit — the minic plugin bridge and the Python MCP server — runs on a **stock, unmodified
ArmorPaint**, including the official paid binary.

This document describes the two **optional** native changes:

1. **The native extension** (`patch/apply_ext_patch.py`) — one binding, `mcp_ext_call`, that
   gives the bridge layer management, undo/redo, export format / bit depth / preset, bakes,
   render settings, the texture-set resolution, live project lists, camera views, console
   read-back and a file-based viewport capture. **Start here**: it supersedes the viewport patch.
2. **The viewport patch** (`patch/apply_viewport_patch.py`) — the smaller, older change that only
   adds file-based viewport capture. It is upstream since 2026-09-09.

Neither is needed for the core toolkit, and the same plugin file works with or without them: the
bridge **detects** each binding at run time (a call to a missing binding inside a one-line
wrapper fails only that wrapper — see `docs/MINIC_DIALECT_AND_API.md` §1.12a), so there is no flag
to set.

# 1. The native extension

## Why

ArmorPaint already implements everything the extension exposes — the Layers panel, the history,
the export dialog, the Bake Texture node, the Preferences viewport settings — but the minic
binding table (`paint/sources/minic_api_list.h`) has no entry for any of it, and the structs
involved (`slot_layer_t`, `project_runtime_t`, ...) are not registered, so a plugin cannot reach
them at all. The only way in is a binding.

Rather than one binding per function (dozens of lines in the table, each a separate API surface to
keep in sync), the extension adds **one**: `char *mcp_ext_call(char *op, any_map_t *args, char
*prefix)`. `op` is the bridge op name, `args` the request map the bridge already parsed, `prefix`
the key prefix of a batch item. It returns `"1{json}"` or `"0{error json}"`. Everything behind it
is ordinary C in `patch/mcp_ext.c`, which mirrors what ArmorPaint's own UI does for each action,
**including the undo step it pushes** — so `ap_undo` takes back an agent's layer change exactly as
it would a person's.

## The change — 1 new file, 3 one-line insertions

`apply_ext_patch.py <checkout>` (idempotent; `--revert` undoes it):

| File | Change |
|---|---|
| `paint/sources/mcp_ext.c` | **new** — copied from `patch/mcp_ext.c` |
| `paint/sources/main.c` | `#include "mcp_ext.c" // armorpaint-mcp` after the last `.c` include of the unity build, so every function and file-static it calls is already defined |
| `paint/sources/functions.h` | `char *mcp_ext_call(char *op, any_map_t *args, char *prefix); // armorpaint-mcp` |
| `paint/sources/minic_api_list.h` | `X3(mcp_ext_call, "p:char(p:char op,p:any_map_t args,p:char prefix)", p, p, p, p) // armorpaint-mcp`, after `iron_delay_idle_sleep` |

No existing function changes meaning, nothing runs unless a request asks for it, and there is no
socket, thread or new dependency.

## Ops

| Op | Implemented with |
|---|---|
| `layer_list` | `g_project->_->layers`, `slot_layer_is_*`, `slot_layer_has_masks` |
| `layer_select` | `context_set_layer` |
| `layer_new` | `layers_new_layer` + `history_new_layer`; fill/decal as `layers_create_fill_layer_on_next_frame`; group, black/white/fill masks as `tab_layers_button_new_menu` |
| `layer_delete` | `tab_layers_can_delete` + `tab_layers_delete_layer` |
| `layer_duplicate` | `history_duplicate_layer` + `layers_duplicate_layer` |
| `layer_set` | `history_layer_name/visible/opacity/blending/object/scale/angle` + the field |
| `layer_move` | `slot_layer_can_move` + `slot_layer_move` |
| `layer_action` | the context-menu handlers: clear, merge down/group, to fill/paint, apply/invert mask |
| `undo`, `redo`, `history` | `history_undo`, `history_redo`, `history_steps` |
| `export_presets`, `export_textures_ex` | `box_export_*` presets; `g_context->format_type/format_quality/layers_export`, `base_bits_handle` + `layers_set_bits` (as the export dialog's Color combo), `ui_files_filename`, then `export_texture_run` |
| `bake`, `bake_status`, `bake_settings` | the `context_t` bake fields; `bake` is `bake_texture_node_run` minus its mid-draw `draw_end`/`draw_begin` bracket (a request runs in the update phase, with no draw pass open) |
| `render_settings` | `g_config->rp_*`, `box_preferences_lut_picked` / `import_lut_free`, `context_set_render_path`, `config_apply`, camera clip planes |
| `texture_resolution` | `base_res_handle` + `config_set_texture_res` + `layers_on_resized` |
| `project_lists` | `g_project->_->materials/assets/brushes/fonts/paint_objects` |
| `camera` | `viewport_set_view` (the numpad views), `viewport_reset`, `viewport_orbit`, `viewport_zoom` |
| `console_read` | `console_last_traces` |
| `capture_viewport` | `viewport_capture_screenshot_to` + `iron_write_png(gpu_get_texture_pixels(...))`, reusing one target and **freeing** it on a size change (`gpu_delete_texture` is not bound for plugins) |

## Verification

Applied to the baseline commit below and built on Linux (Ubuntu 24.04, clang 18, `../base/make
--compile`): 0 errors, 0 warnings from `mcp_ext.c`. Run under Xvfb with Mesa's software Vulkan
driver, every op above was exercised through the bridge by `tests/test_live.py`; the full suite
passes on the patched build and on the stock build (where the extension tools answer
`unsupported`).

Not covered by that run: a real desktop. On KDE Plasma 6 with the stock Arch package, bridge 1.x
turned out to hang ArmorPaint by leaking a file descriptor per poll (below, and `PROTOCOL.md`
"The doorbell"); bridge 2.0 fixes that on the plugin side. The extension build has not been re-run
against bridge 2.0.

**An upstream bug worth reporting:** `close_dir()` in `base/sources/kong/dir.c` is empty on POSIX,
so every `iron_read_directory` / `file_read_directory` leaks an `opendir()` handle. The one-line
fix is `closedir(dir->handle);` in the POSIX branch (with a `NULL` check). Neither patch here
carries it: the bridge avoids the leak by not listing directories, which also works on the stock
binary.

**Upstream `main`** (as of 2026-09-24) replaced the `ui_handle_t` globals the export and
resolution ops use (`base_bits_handle`, `box_export_hpreset`, `base_res_handle`), so the extension
does not compile there yet. Neither does the bridge plugin itself load there (`ui_handle_create`
is gone). Both target ArmorPaint 1.0.

# 2. The viewport patch

## Baseline

| | |
|---|---|
| Upstream | https://github.com/armory3d/armorpaint |
| Commit | `906418acc600132fa927876d208eb452dc5a0967` (2026-09-04, "fix: map length") |
| Version | ArmorPaint 1.0 |
| Licence | zlib/libpng — modification and redistribution permitted |

## Why a patch is needed at all

A minic plugin can already *capture* the viewport into a GPU texture via the bound
`viewport_capture_screenshot_to()`. It cannot get those pixels onto disk:

- `viewport_save_texture()` encodes the PNG into `g_project->packed_assets` — an in-memory list
  persisted only inside the `.arm` when the project is saved. An external process cannot read it.
- `iron_encode_png` and `gpu_get_texture_pixels` are **not** in the minic binding table
  (`paint/sources/minic_api_list.h`).

So an agent driving ArmorPaint through a plugin is blind to the shaded 3D result. It can still see
its actual work product — `export_texture_run()` **does** write real PNG/EXR files — but not the
viewport.

The useful discovery: **`iron_write_png(path, bytes, w, h, format)` already exists**
(`base/sources/iron_image.h:7`) and writes straight to a path. Nothing needed implementing. The
only thing missing was a *binding*.

## The change — 3 hunks, 12 added lines (viewport patch)

Apply/revert with `patch/apply_viewport_patch.py` (idempotent; `--revert` undoes it). The exact
diff is in `patch/viewport_capture.diff`.

**1. `paint/sources/viewport.c`** — a wrapper beside the existing `viewport_save_texture`:

```c
void viewport_save_texture_to_file(gpu_texture_t *screenshot, char *path) {
	iron_write_png(path, gpu_get_texture_pixels(screenshot), screenshot->width, screenshot->height, 0);
}
```

**2. `paint/sources/functions.h`** — its declaration.

**3. `paint/sources/minic_api_list.h`** — the binding that exposes it to plugins:

```c
X2(viewport_save_texture_to_file, "v(p:gpu_texture_t screenshot,p:char path)", v, p, p)
```

That is the whole change. No socket, no thread, no new dependency, no altered existing behaviour —
`viewport_save_texture()` is untouched, so nothing upstream changes meaning.

## Verification

Applied to the baseline commit, then rebuilt (ClangCL, clang 22.1.8, 6.8 s, 0 errors). The patched
binary's **own** `--api` output lists the new function at line 1022, which is proof it is genuinely
reachable from a plugin rather than merely compiled in:

```
void viewport_capture_screenshot_to(gpu_texture_t *target, float x, float y, float w, float h);
void viewport_save_texture(gpu_texture_t *screenshot);
void viewport_save_texture_to_file(gpu_texture_t *screenshot, char *path);   <-- added
```

## Licence position

ArmorPaint is zlib/libpng, which permits altering and redistributing the software, subject to:
origin must not be misrepresented; **altered source versions must be plainly marked as such**; the
notice must not be removed from source distributions.

We stay clear of all three by not redistributing ArmorPaint source at all. `apply_viewport_patch.py`
edits **your own checkout**, in place, and every inserted line carries an `armorpaint-mcp` marker —
so a patched tree is plainly marked as altered, both in the source text and by this document.

If you do publish a patched tree, keep those markers and this file with it.

## Maintenance

The patch anchors on `viewport_save_texture()` by walking its braces rather than matching line
numbers, so ordinary upstream churn will not silently corrupt it. If that function is ever renamed
or removed the script **fails loudly and refuses to patch** — re-derive it by hand at that point
rather than forcing it.

Binaries are paid and fund ArmorPaint's development. Building from source is explicitly supported
by upstream, but if you use this tool seriously, consider buying a copy.
