# PBR Channels, Export Presets, And The Unity HDRP Handoff

Primary sources, all local and verified against ArmorPaint 1.0 (`906418ac`):
- `paint/sources/io/export_texture.c` — the channel router; every token below is a `string_equals`
  branch at lines 341–430
- `paint/sources/render/make_paint.c:575-605` — the three render targets and the per-channel write masks
- `paint/assets/export_presets/*.json` — the eight stock presets, quoted verbatim
- `paint/sources/ui/box_export.c` — how a preset is chosen, saved, imported, created

## How ArmorPaint Actually Stores A Surface

Not as named channels you can add or remove — as three fixed RGBA targets:

| Target | R | G | B | A |
|---|---|---|---|---|
| 0 (`texpaint`) | base colour R | base colour G | base colour B | opacity |
| 1 (`texpaint_nor`) | normal X | normal Y | normal Z | material id |
| 2 (`texpaint_pack`) | occlusion | roughness | metallic | height |

Consequences you will hit:

- **There is no "add channel".** The nine material `paint_*` flags are write masks over these slots,
  nothing more. A flag set false turns off the GPU colour write for that component
  (`make_paint.c:581+`), so the existing pixels survive untouched — that is what makes multi-pass
  fills safe.
- **Emission and subsurface are not scalars.** They are encoded in the material-id byte of target 1,
  and export recovers them as a **binary mask** (`matid % 3 == 1` ⇒ emissive, `== 2` ⇒ subsurface;
  `export_texture_extract_channel`). A "half-strength emission" does not survive an export — it is
  255 or 0.
- **Height is the alpha of the packed target**, which is why height maps cost you nothing extra and
  why a preset that wants height must read `height`, not a separate map.

## Export Channel Tokens

An export preset is a JSON file listing output textures; each names four channel tokens, one per
output RGBA component. These are the only tokens the router understands:

| Token | Source | Notes |
|---|---|---|
| `base_r` `base_g` `base_b` | target 0 RGB | |
| `opac` | target 0 A | |
| `nor_r` `nor_g` `nor_b` | target 1 RGB | OpenGL convention (+Y up) |
| `nor_g_directx` | target 1 G, **inverted** | the DirectX green flip, `255 - g` |
| `occ` | target 2 R | |
| `rough` | target 2 G | |
| `smooth` | target 2 G, **inverted** | `255 - roughness`; there is no separate smoothness buffer |
| `metal` | target 2 B | |
| `height` | target 2 A | |
| `emis` `subs` | target 1 A, decoded | **binary 0/255 masks**, see above |
| `diff_r` `diff_g` `diff_b` | computed | metal/rough → specular-workflow diffuse |
| `spec_r` `spec_g` `spec_b` | computed | metal/rough → specular-workflow specular |
| `0.0` `1.0` | constant | writes 0 or 255 into that component |

`color_space` per texture: `"linear"` copies the bytes through unchanged. **Any other value applies
`pow(v, 1/2.2)`** on write (`export_texture_gamma`, `export_texture.c:4`). Every stock preset uses
`"linear"`, so what the viewport's channel view shows is what lands in the file. Leave it linear
unless you have a specific reason and have looked at the result.

## The Eight Stock Presets

Verbatim from `paint/assets/export_presets/`. Output filename is
`<stem>_<name>.<ext>`; a preset entry with an empty `name` drops the suffix.

| Preset | Outputs (name → channels) |
|---|---|
| `generic` | `base`→base RGB · `nor`→normal · `occ`→occ×3 · `rough`→rough×3 · `metal`→metal×3 |
| `unity` | `base`→base RGB · `nor`→normal · **`mos`→[metal, occ, 1.0, smooth]** · `height`→[0, height, 0, 1] |
| `unreal` | `base`→base RGB · `nor`→[nor_r, **nor_g_directx**, nor_b] · `orm`→[occ, rough, metal] |
| `minecraft_mer` | (no suffix)→base RGB · `normal`→normal · `mer`→[metal, emis, rough] |
| `base_color` | `base`→base RGB only |
| `specular` | `diff` · `spec` · `nor` · `occ` · `smooth` |
| `unigine` | `base` · `nor`→[nor_r, nor_g, 0, 1] · `sh`→[metal, rough, 0, 1] · `occ`→[occ, 0, 0, 1] |
| `xplane` | `base`→[base RGB, opac] · `nor`→[nor_r, nor_g, metal, smooth] |

`generic` is the fallback the exporter auto-selects when the Export dialog has never been opened this
session (`export_texture.c:500-504`).

## Which Preset Gets Used, And Why You Cannot Choose It

`export_texture_run(dir, bake)` uses whatever preset is currently selected on the export UI handle:

- Dialog never opened this session ⇒ it fetches the preset list and selects `generic`.
- Dialog opened, or a preset picked ⇒ **that** selection persists for the rest of the session, and
  every plugin-driven export uses it.

`box_export_hpreset` is a UI handle and is not registered, so **no plugin and therefore no MCP tool
can select a preset**. Three honest ways to get the packing you want:

1. **Use `unity` (recommended for Unity/HDRP).** Ask the human, once: *Export → Presets → `unity`*.
   Then every export you drive emits the right maps. Confirm afterwards by reading the filenames back
   — a `_mos` file proves the `unity` preset is live; a `_rough` file proves it is `generic`.
2. **Ship a custom preset file and have them select it.** Presets are plain JSON in
   `<data path>/export_presets/`, where `<data path>` is `data/` beside the ArmorPaint executable
   (`ap_get_app_info` reports it). The server can write the file; the human still has to pick it in
   the combo. The list is built by `box_export_fetch_presets`, which runs **once per app run** — at
   the first export or the first time the Export dialog opens — and again when the Presets tab
   imports or creates a preset. So a file written mid-session may not appear until they restart
   ArmorPaint or import it through the dialog. Write it early, or tell them to restart.
3. **Export with `generic` and pack outside ArmorPaint.** Five clean single-purpose maps, packed into
   a MaskMap by whatever image tooling the server has. Slowest, but preset-independent and the only
   route that can put a real detail mask in B.

Never overwrite the stock `generic.json`. The app treats it as constant (it refuses to save over it
from the UI) and other work on that machine depends on it.

## Unity HDRP Handoff

HDRP's Lit shader wants exactly three textures:

| Unity slot | Content | Colour space |
|---|---|---|
| Base Map | albedo RGB (+ alpha) | **sRGB** |
| Mask Map | **R = metallic, G = occlusion, B = detail mask, A = smoothness** | linear (sRGB off) |
| Normal Map | tangent-space normal, **OpenGL +Y** | normal-map import type |

ArmorPaint's stock `unity` preset emits precisely this:

```json
{ "name": "mos", "channels": ["metal", "occ", "1.0", "smooth"], "color_space": "linear" }
```

R metallic, G occlusion, B constant 1.0, A smoothness (inverted roughness). That **is** an HDRP
MaskMap. Two notes:

- **B = 1.0 is a full detail mask.** HDRP only consults it when a detail map is assigned, so a
  constant 1.0 is harmless for materials without detail maps and correct-by-default for materials
  with one. If you need a real detail mask, you must pack outside ArmorPaint — no export token reads
  an arbitrary channel into B.
- **Normal green is OpenGL.** `unity` uses `nor_g` (not `nor_g_directx`), which matches Unity. Do not
  flip it. `unreal` is the preset that flips green; if you see `nor_g_directx` in a preset you were
  told is for Unity, that preset is wrong.

If you want HDRP-shaped filenames rather than `_base`/`_nor`/`_mos`, write a preset with the same
channels and better names — same packing, clearer handoff:

```json
{
  "textures": [
    { "name": "BaseColor", "channels": ["base_r", "base_g", "base_b", "1.0"], "color_space": "linear" },
    { "name": "Normal",    "channels": ["nor_r", "nor_g", "nor_b", "1.0"],    "color_space": "linear" },
    { "name": "MaskMap",   "channels": ["metal", "occ", "1.0", "smooth"],     "color_space": "linear" },
    { "name": "Height",    "channels": ["height", "height", "height", "1.0"], "color_space": "linear" }
  ]
}
```

Save as `<data path>/export_presets/hdrp.json`, then ask the human to select `hdrp` once. Output for
project `goblin.arm`: `goblin_BaseColor.png`, `goblin_Normal.png`, `goblin_MaskMap.png`,
`goblin_Height.png`.

### Unity-side import settings to state in the handoff

- BaseColor: sRGB **on** (default).
- MaskMap: sRGB **off**, compression that preserves alpha (do not use a format that discards A —
  smoothness lives there).
- Normal: texture type **Normal map**; do not tick "Create from grayscale".
- Height (if used): sRGB **off**, single channel; HDRP's parallax expects height in a dedicated slot.
- Nothing here needs a green-channel flip.

### For URP / Built-in

URP's Lit metallic map is `R = metallic, A = smoothness` and ambient occlusion is a separate texture.
The `unity` preset's `mos` still works — URP reads R and A and ignores G — but you must supply
occlusion separately (add an `occ` texture entry, or use `generic`).

## Bit Depth And Format

`export_texture_run` takes only `(path, bake_material)`. Format (PNG/JPG) and bit depth (8/16) come
from `g_context->format_type` and the bits UI handle, **neither of which is registered**. So:

- default output is **8-bit PNG**
- 16-bit output is `.exr`, and only the human can switch to it
- there is no plugin-side control at all — if a job needs 16-bit height or EXR, say so up front and
  ask them to set it in the Export dialog

## PBR Guardrails

The physics does not change because the tool did.

- Base colour describes surface response, not lighting. No baked shadows, no painted highlights.
- Roughness is the strongest lever for how a surface *feels*. Reach for it before normal or height.
- Metallic is effectively binary for most game assets. Dielectrics stay at 0; contamination between
  0.1 and 0.9 across a whole surface is almost always a mistake.
- Occlusion is contact shadowing, not dirt. Do not use it to darken a colour.
- Normal and height add structure, not silhouette. If the object reads as the wrong shape, that is
  geometry, and no map will fix it.
- Height in ArmorPaint is the packed alpha, cheap to author and easy to overdo. Keep it subtle unless
  the target renderer actually does parallax or tessellation with it.
- Smoothness and roughness are the same buffer inverted. Never export both and expect them to be
  independent — and never author "smoothness" thinking it is a separate channel.

## Verifying A Packing Without A Viewport

1. Export to a fresh directory, `ap_fs_list` it, and confirm the **filenames** match the preset you
   expect (`_mos` ⇒ unity, `_orm` ⇒ unreal, `_rough` + `_metal` ⇒ generic).
2. Read the packed map back as an image and look at it as a colour image first: a MaskMap usually
   reads as a strange teal/olive field. Wholly black or wholly white means a channel is dead.
3. Reason channel by channel against the table above: is R (metallic) binary-ish? Is G (occlusion)
   bright with dark creases? Is A present at all?
4. If a channel is wrong, the fault is almost always upstream in the material graph or in a channel
   write mask — not in the export. Fix it there and re-export to a new directory.
