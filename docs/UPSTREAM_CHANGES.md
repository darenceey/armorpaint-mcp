# Upstream changes

This project is **not a fork of ArmorPaint** and ships no modified ArmorPaint source. The core
toolkit — the minic plugin bridge and the Python MCP server — runs on a **stock, unmodified
ArmorPaint**, including the official paid binary.

This document exists for one **optional** feature: 3D viewport capture.

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

## The change — 3 hunks, 12 added lines

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
