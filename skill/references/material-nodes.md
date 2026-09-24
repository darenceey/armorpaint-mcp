# Driving The Material Node Graph

Primary sources, all local and verified against ArmorPaint 1.0 (`906418ac`):
- `paint/sources/minic_impl.c:502-690` — every `script_material_*` binding, including its silent guards
- `paint/sources/nodes_material/*.c` — the node definitions; socket order below is read from these
- `paint/assets/plugins/dev/test.c:99-176` — upstream's own worked example of building a graph
- `paint/assets/data/default_material.arm` — what a new material contains
- `docs/MINIC_DIALECT_AND_API.md` §2.6 — the binding table with line numbers

This is the best-served corner of the API and, because layers are closed, it is where the work
happens. Everything a Substance user would build as a stack of masked layers is built here as one
graph and applied with one fill.

## Mechanics

| Tool | Binding | Behaviour |
|---|---|---|
| `ap_node_add` | `script_material_create_node_at(type, x, y)` | returns the new node (id assigned by the app) |
| `ap_node_list` | walks `ctx.material.canvas.nodes` and `.links` | ids, types, names, positions, topology |
| `ap_node_get` | one node's `inputs` / `outputs` / `buttons` | every socket and button: index, name, type, default value |
| `ap_node_remove` | `script_material_remove_node` | refuses to remove `OUTPUT_MATERIAL_PBR` |
| `ap_node_connect` | `script_material_connect(from, from_socket, to, to_socket)` | **removes any existing link into that input first** — idempotent per input |
| `ap_node_disconnect` | `script_material_disconnect(to, to_socket)` | by target input |
| `ap_node_set_value` | `_set_float` / `_set_color` / `_set_vector` / `_set_button` | writes a socket's `default_value` |
| — | `script_material_update()` | re-parses the material, rebuilds previews |

Tool names follow this project's planned surface (`docs/MINIC_DIALECT_AND_API.md` §2.13); the server's
own tool list is the authority, and the update may be a tool of its own or folded into the node ops —
check before you assume. The **bindings** in the middle column are fixed and verified.

Seven rules that decide whether your graph works:

1. **Always finish with update.** Nothing you changed affects the viewport, a fill, or an export
   until `script_material_update()` runs. A whole graph that "did nothing" is usually a missing
   update.
2. **All writes are silently ignored when out of range.** `script_material_socket` returns `NULL` for
   a bad index and the setter just returns. Same for a `NULL` node. **Read the graph back** and
   confirm the value landed.
3. **Socket indices are positional**, in the order the node defines them. Names are display strings
   (translated!) — never match on them, use the tables below.
4. **`is_input` matters, and for constant nodes the value lives on the OUTPUT.** `RGB` and `VALUE`
   have no inputs; their value is `default_value` of output socket 0. That is
   `script_material_set_color(rgb, 0, 0, r, g, b, a)` — `is_input = 0`. Getting this backwards is the
   most common way to build a graph that quietly stays grey.
5. **Buttons are enums and toggles**, indexed separately from sockets, set with `_set_button`. Their
   options are ordered lists; the indices are in the tables below.
6. **Connections are not type-checked.** Prefer matching types (VALUE→VALUE, RGBA→RGBA,
   VECTOR→VECTOR) and convert explicitly with `RGBTOBW`, `SEPARATE_COLOR`, `COMBINE_COLOR`.
7. **Type lookup returns the first match.** `script_material_get_node(type)` scans the canvas and
   returns the first node of that type, so it is only safe for singletons like
   `OUTPUT_MATERIAL_PBR`. For everything else, keep the id you got from `ap_node_add`.

Placement (`x`, `y`) is cosmetic but not pointless — a human will open this graph. Lay out columns
right-to-left from the output at `(0, 0)`: sources around `x = -900`, mixing around `x = -450`.

## The Output Node

`OUTPUT_MATERIAL_PBR`, one per material, cannot be deleted. Inputs, in order:

| # | Input | Type | Default |
|---|---|---|---|
| 0 | Base Color | RGBA | 0.8, 0.8, 0.8, 1.0 |
| 1 | Opacity | VALUE | 1.0 |
| 2 | Occlusion | VALUE | 1.0 |
| 3 | Roughness | VALUE | 0.1 |
| 4 | Metallic | VALUE | 0.0 |
| 5 | Normal Map | VECTOR | 0.5, 0.5, 1.0 |
| 6 | Emission | VALUE | 0.0 |
| 7 | Height | VALUE | 0.0 |
| 8 | Subsurface | VALUE | 0.0 |

These map one-to-one onto the three render targets in `references/pbr-channels.md`. Note the stock
roughness default of 0.1 — a material you never wire will read as near-mirror plastic, which is a
useful tell that your graph is not reaching the output.

**A new material is not empty.** `default_material.arm` contains two nodes and one link: an `RGB`
node named "Color" wired into Base Color. Your first move on a fresh material is usually to grab that
RGB node (find it in `ap_node_list` by type) and set its colour, or to disconnect input 0 and put
your own chain there.

## Verified Socket Tables

Read from the node definitions in `paint/sources/nodes_material/`. Inputs and outputs are numbered
from 0 in definition order; buttons likewise.

**TEX_NOISE** — the workhorse breakup source
```
in  0 Vector VECTOR | 1 Scale 5.0 | 2 Detail 2.0 [0..8] | 3 Roughness 0.5 | 4 Lacunarity 2.0 | 5 Distortion 0.0
out 0 Factor VALUE  | 1 Color RGBA
btn 0 Dimensions (3D) | 1 Type (fBM) | 2 Normalize BOOL
```

**TEX_VORONOI** — cells, chips, stone grain
```
in  0 Vector | 1 Scale 5.0 | 2 Detail 0.0 | 3 Roughness 0.5 | 4 Lacunarity 2.0 | 5 Randomness 1.0
out 0 Distance VALUE | 1 Color RGBA | 2 Position VECTOR
btn 0 Dimensions [0=2D, 1=3D] default 1 | 1 Feature [0=F1, 1=F2] | 2 Normalize BOOL
```

**TEX_WAVE** — grain, planks, banding
```
in  0 Vector | 1 Scale 5.0 | 2 Distortion 0.0 | 3 Detail Scale 1.0 | 4 Detail Roughness 0.5 | 5 Phase Offset 0.0
out 0 Color RGBA | 1 Factor VALUE
btn 0 Wave Type [0=Bands, 1=Rings] | 1 Direction [0=X, 1=Y, 2=Z, 3=Diagonal] | 2 Profile [0=Sine, 1=Saw, 2=Triangle]
```

**TEX_BRICK** — masonry
```
in  0 Vector | 1 Color 1 | 2 Color 2 | 3 Mortar | 4 Scale 5.0 | 5 Mortar Size 0.02 | 6 Mortar Smooth 0.1
    | 7 Bias 0.0 | 8 Brick Width 0.5 | 9 Row Height 0.25 | 10 Offset 0.5 | 11 Frequency 2.0
    | 12 Squash 1.0 | 13 Frequency 2.0
out 0 Color RGBA | 1 Factor VALUE
```
With input 0 unlinked, the pattern does not follow the UVs: measured on the default cube, it shows
as thin stripes on some faces and flat mortar on others. Wire `TEX_COORD` output 2 (UV) into input 0
for real bricks, optionally through `MAPPING` (input 3 is Scale) to change proportions. The default
cube's UV islands are rotated per face, so the bricks run sideways on some faces. Check the exported
base-colour texture, where they are laid out flat.

**BAKE_CURVATURE** — the one bake an agent can actually trigger
```
in  0 Strength 1.0 [0..2] | 1 Radius 1.0 [0..2] | 2 Offset 0.0 [-2..2]
out 0 Value VALUE
```
The general bake tool is unreachable (no bake-run binding), but this node is different: the node
sampled by the shader is a **node preview**, and `script_material_update()` re-bakes it. The update
path calls `make_material_parse_paint_material(true)`, which walks the canvas and, for every
`BAKE_CURVATURE` node, switches to the bake tool, runs a curvature bake into an R8 preview target,
and dilates it (`render/make_material.c:222-266`). Two consequences:
- **Without an update, curvature reads as `empty_black`** (`uniforms.c:503`) — your wear mask is
  silently zero and the material looks untouched. This is the most confusing silent failure in the
  whole graph API.
- **The re-bake happens at document resolution on the render thread.** It is the most expensive
  single thing you can ask for. Do not call update in a loop while a human is painting.

**MIX_RGB**
```
in  0 Factor 0.5 | 1 Color 1 | 2 Color 2
out 0 Color RGBA
btn 0 blend_type: 0 Mix, 1 Darken, 2 Multiply, 3 Burn, 4 Lighten, 5 Screen, 6 Dodge, 7 Add,
      8 Overlay, 9 Soft Light, 10 Linear Light, 11 Difference, 12 Exclusion, 13 Subtract,
      14 Divide, 15 Hue, 16 Saturation, 17 Color, 18 Value
btn 1 Clamp Factor BOOL | 2 Clamp Result BOOL
```

**MATH**
```
in  0 Value 0.5 | 1 Value 0.5
out 0 Value
btn 0 operation: 0 Add, 1 Subtract, 2 Multiply, 3 Divide, 4 Power, 5 Logarithm, 6 Square Root,
      7 Inverse Square Root, 8 Absolute, 9 Exponent, 10 Minimum, 11 Maximum, 12 Less Than,
      13 Greater Than, 14 Sign, 15 Round, 16 Floor, 17 Ceil, 18 Truncate, 19 Fraction,
      20 Truncated Modulo, 21 Floored Modulo, 22 Snap, 23 Ping-Pong, 24 Sine, 25 Cosine, ...
btn 1 Clamp BOOL
```

**MAPRANGE** — the remapper you will use constantly
```
in  0 Value 0.5 | 1 From Min 0.0 | 2 From Max 1.0 | 3 To Min 0.0 | 4 To Max 1.0
out 0 Value
btn 0 Clamp BOOL
```
Straight lerp, plus an optional `clamp(out, To Min, To Max)`. **Never enable Clamp on an inverted
range** (To Min > To Max) — the generated `clamp(x, hi, lo)` is undefined and you get garbage
(`map_range_node.c:11-15`). For an inverted mapping either leave Clamp off and clamp the *input*
upstream, or keep To Min < To Max and invert the signal with `MATH` set to Subtract (1 − x).

**Other nodes worth knowing**
```
RGB           out 0 Color RGBA (0.5,0.5,0.5,1)      -- value lives on the OUTPUT
VALUE         out 0 Value VALUE (0.0)               -- value lives on the OUTPUT
VALTORGB      in 0 Factor | out 0 Color, 1 Alpha    -- ramp stops are a CUSTOM button, not scriptable
BUMP          in 0 Strength 1.0, 1 Distance 0.001, 2 Height 1.0, 3 Normal | out 0 Normal Map | btn 0 Invert
NORMAL_MAP    in 0 Strength 1.0 [0..2], 1 Normal Map | out 0 Normal Map
BRIGHTCONTRAST in 0 Color, 1 Brightness, 2 Contrast | out 0 Color
HUE_SAT       in 0 Hue 0.5, 1 Saturation 1.0, 2 Value 1.0, 3 Factor 1.0, 4 Color | out 0 Color
INVERT_COLOR  in 0 Factor 1.0, 1 Color | out 0 Color
GAMMA         in 0 Color, 1 Gamma | out 0 Color
CLAMP         in 0 Value, 1 Min, 2 Max | out 0 Value | btn 0 [0=Min Max, 1=Range]
RGBTOBW       in 0 Color | out 0 Val
SEPARATE_COLOR in 0 Color | out 0 R, 1 G, 2 B
COMBINE_COLOR in 0 R, 1 G, 2 B | out 0 Color
COLMASK       in 0 Color, 1 Mask Color, 2 Radius 0.1, 3 Fuzziness | out 0 Mask
TEX_CHECKER   in 0 Vector, 1 Color 1, 2 Color 2, 3 Scale | out 0 Color, 1 Factor
TEX_GRADIENT  in 0 Vector | out 0 Color, 1 Factor | btn 0 [0=Linear, 1=Diagonal, 2=Radial, 3=Spherical]
TEX_MAGIC     in 0 Vector, 1 Scale, 2 Distortion | out 0 Color, 1 Factor | btn 0 Depth
TEX_COORD     out 0 Generated, 1 Normal, 2 UV, 3 Object, 4 Camera, 5 Window
MAPPING       in 0 Vector, 1 Location, 2 Rotation [0..360], 3 Scale (1,1,1) | out 0 Vector
TEX_IMAGE     in 0 Vector | out 0 Color, 1 Alpha | btn 0 File (asset index), 1 Color Space
              [0=Auto, 1=Linear, 2=sRGB, 3=DirectX Normal Map]
LAYER         out 0..8 mirror the output node's inputs | btn 0 Layer (layer index)
LAYER_MASK    out 0 Value | btn 0 Layer (layer index)
PICKER        out 0..8 mirror the output node's inputs (the picked surface)
```

The full list of 68 valid `type` strings is in `docs/MINIC_DIALECT_AND_API.md` §2.6. A type that is
not on it returns `NULL` from `create_node` — which, again, looks exactly like success unless you
check.

## What A Useful Graph Looks Like

Shallow, named by role, and readable by the human who owns the file:

```
  [source]        [shaping]            [routing]              [output]
  TEX_NOISE  -->  MAPRANGE      -->    MIX_RGB      -->   Base Color
  BAKE_CURV  -->  MATH(mult)    -->    (mask)       -->   Metallic
                                       MAPRANGE     -->   Roughness
                  BUMP                              -->   Normal Map
```

One source of breakup, one remap to put it in a sensible range, one mix per output channel. Resist
stacking five noises: with no viewport you cannot judge the result, and every extra node is another
place a silent no-op can hide.

Rules of thumb that survive the blindness:
- **Roughness carries the material's identity.** Wire it before base colour.
- **Metallic should be a mask, not a gradient.** Feed it something near-binary.
- **Normal last, and weak.** `BUMP` strength above ~0.3 usually reads as noise in the exported map.
- **Occlusion is optional.** Leaving input 2 at 1.0 is honest; a fake AO from noise is not.

## Recipes

Each is an ordered op list. Node references are the ids returned by `ap_node_add`. Every recipe ends
with update, then a bake-to-plane so you can look at it.

### Worn painted metal

```
ap_material_create "worn_metal"
n_paint  = ap_node_add RGB            at (-950,  400)   set_color  out 0 = (0.10, 0.13, 0.16, 1)
n_metal  = ap_node_add RGB            at (-950,  650)   set_color  out 0 = (0.62, 0.60, 0.57, 1)
n_curv   = ap_node_add BAKE_CURVATURE at (-950,    0)   set_float  in 0 = 1.2   (Strength)
                                                        set_float  in 1 = 0.8   (Radius)
                                                        set_float  in 2 = 0.0   (Offset)
n_edge   = ap_node_add MAPRANGE       at (-700,    0)   in 1=0.45  in 2=0.75  in 3=0.0  in 4=1.0
                                                        button 0 = 1  (Clamp)
n_grime  = ap_node_add TEX_NOISE      at (-950, -300)   in 1=14.0 (Scale)  in 2=5.0 (Detail)
n_mask   = ap_node_add MATH           at (-450,    0)   button 0 = 2  (Multiply)
n_mix    = ap_node_add MIX_RGB        at (-200,  400)   button 0 = 0  (Mix)
n_rough  = ap_node_add MAPRANGE       at (-200,  100)   in 3=0.62  in 4=0.28
                                                        button 0 = 0  (Clamp OFF - inverted range;
                                                        the input is already clamped 0..1 by n_edge)
n_bump   = ap_node_add BUMP           at (-200, -300)   in 0=0.12 (Strength)
out      = the OUTPUT_MATERIAL_PBR node (find it in ap_node_list by type)

connect n_curv:0  -> n_edge:0
connect n_edge:0  -> n_mask:0
connect n_grime:0 -> n_mask:1        (Factor output, not Color)
connect n_mask:0  -> n_mix:0         (wear mask drives the mix factor)
connect n_paint:0 -> n_mix:1
connect n_metal:0 -> n_mix:2
connect n_mix:0   -> out:0           Base Color
connect n_mask:0  -> out:4           Metallic  (near-binary: paint 0, exposed metal 1)
connect n_mask:0  -> n_rough:0
connect n_rough:0 -> out:3           Roughness (0.62 painted -> 0.28 polished metal)
connect n_grime:0 -> n_bump:2        Height
connect n_bump:0  -> out:5           Normal Map
update
```

Dial it: wear amount is `n_edge` inputs 1/2 (raise From Min for less wear); grime scale is
`n_grime` input 1; paint sheen is `n_rough` input 3.

### Painted wood

```
ap_material_create "painted_wood"
n_grain  = ap_node_add TEX_WAVE       at (-950,    0)   in 1=3.0 (Scale)  in 2=1.8 (Distortion)
                                                        in 3=2.5 (Detail Scale)
                                                        button 0 = 0 (Bands)  1 = 0 (X)  2 = 1 (Saw)
n_fibre  = ap_node_add TEX_NOISE      at (-950, -300)   in 1=40.0  in 2=6.0
n_dark   = ap_node_add RGB            at (-950,  400)   set_color out 0 = (0.20, 0.12, 0.06, 1)
n_light  = ap_node_add RGB            at (-950,  650)   set_color out 0 = (0.45, 0.30, 0.16, 1)
n_wood   = ap_node_add MIX_RGB        at (-600,  400)   button 0 = 0 (Mix)
n_paint  = ap_node_add RGB            at (-600,  700)   set_color out 0 = (0.55, 0.15, 0.13, 1)
n_curv   = ap_node_add BAKE_CURVATURE at (-950,  200)   in 0=1.0  in 1=0.7
n_bare   = ap_node_add MAPRANGE       at (-700,  200)   in 1=0.55 in 2=0.8  in 3=0.0 in 4=1.0
                                                        button 0 = 1 (Clamp)  -- 1 = paint chipped off
n_top    = ap_node_add MIX_RGB        at (-300,  500)   button 0 = 0 (Mix)
n_rough  = ap_node_add MAPRANGE       at (-300,  150)   in 3=0.42  in 4=0.75
                                                        button 0 = 1 (Clamp)
n_bump   = ap_node_add BUMP           at (-300, -200)   in 0=0.18
out      = the OUTPUT_MATERIAL_PBR node (find it in ap_node_list by type)

connect n_grain:1 -> n_wood:0        (Wave Factor)
connect n_dark:0  -> n_wood:1
connect n_light:0 -> n_wood:2
connect n_curv:0  -> n_bare:0
connect n_bare:0  -> n_top:0         (0 = painted, 1 = bare wood on the edges)
connect n_paint:0 -> n_top:1         Color 1 is the factor-0 end
connect n_wood:0  -> n_top:2         Color 2 is the factor-1 end
connect n_top:0   -> out:0
connect n_bare:0  -> n_rough:0
connect n_rough:0 -> out:3           painted sheen 0.42 -> bare wood 0.75
connect n_fibre:0 -> n_bump:2
connect n_bump:0  -> out:5
update
```

Leave Metallic at 0. If the planks read too regular, raise `n_grain` input 2 (Distortion) before
adding another node.

### Stone

```
ap_material_create "stone"
n_cell   = ap_node_add TEX_VORONOI    at (-950,    0)   in 1=6.0 (Scale)  in 5=0.9 (Randomness)
                                                        button 0 = 1 (3D)  1 = 0 (F1)
n_grit   = ap_node_add TEX_NOISE      at (-950, -350)   in 1=45.0  in 2=7.0  in 3=0.65
n_pale   = ap_node_add RGB            at (-950,  400)   set_color out 0 = (0.46, 0.45, 0.42, 1)
n_dark   = ap_node_add RGB            at (-950,  650)   set_color out 0 = (0.26, 0.25, 0.24, 1)
n_tone   = ap_node_add MAPRANGE       at (-650,    0)   in 1=0.0 in 2=0.6 in 3=0.0 in 4=1.0
                                                        button 0 = 1 (Clamp)
n_mix    = ap_node_add MIX_RGB        at (-350,  400)   button 0 = 0 (Mix)
n_rough  = ap_node_add MAPRANGE       at (-350,  100)   in 3=0.88  in 4=0.72
                                                        button 0 = 0  (Clamp OFF - inverted range)
n_occ    = ap_node_add MAPRANGE       at (-350, -100)   in 1=0.0 in 2=0.35 in 3=0.55 in 4=1.0
                                                        button 0 = 1 (Clamp)
n_bump   = ap_node_add BUMP           at (-350, -350)   in 0=0.25
out      = the OUTPUT_MATERIAL_PBR node (find it in ap_node_list by type)

connect n_cell:0  -> n_tone:0        (Distance output)
connect n_tone:0  -> n_mix:0
connect n_pale:0  -> n_mix:1
connect n_dark:0  -> n_mix:2
connect n_mix:0   -> out:0
connect n_tone:0  -> n_rough:0
connect n_rough:0 -> out:3
connect n_cell:0  -> n_occ:0
connect n_occ:0   -> out:2           crevice darkening where cells meet
connect n_grit:0  -> n_bump:2
connect n_bump:0  -> out:5
update
```

Metallic stays 0. For masonry rather than rock, swap `TEX_VORONOI` for `TEX_BRICK` and drive the mix
from its Factor output (socket 1), with mortar width on inputs 5/6.

### Wiring an imported image

`TEX_IMAGE` picks its file by **index into the project's texture asset list**, not by path
(`parser_material_make_texture` reads button 0 and indexes `base_combo_enum_texts`). That list is the
runtime asset array, and the plugin-visible `project_t.assets` is a **save/load snapshot** — it is
written by `export_arm` at save and read by `import_arm` at load, and is *not* updated when you
import a texture mid-session. So the index is not directly readable. Work it like this:

```
1. ap_project_list_texture_assets      -> record the count N (label it as a snapshot)
2. ap_import_asset "D:/tex/rust_albedo.png"
3. the new asset is appended last, so its index is N (then N+1 for the next import, ...)
4. n_img = ap_node_add TEX_IMAGE
   ap_node_set_value kind=button node=n_img button=0 value=<index>
   ap_node_set_value kind=button node=n_img button=1 value=2      (sRGB for albedo; 1 Linear for
                                                                   data maps; 3 for DirectX normals)
5. connect n_img:0 -> out:0
6. update, bake to plane, LOOK: a pink or blank swatch means the index was wrong
```

Caveats to state out loud when you use this: the count is only trustworthy if the human does not
import or delete assets in between, and the snapshot can be stale in a never-saved project (it may be
`NULL`/empty even when assets exist). If accuracy matters more than autonomy, add the node and ask
the human to pick the file in its combo — one click, zero ambiguity. And never re-import an edited
image at the same path: the blob cache and the "asset already imported" guard will both serve you the
old pixels.

### Reading an existing layer

`LAYER` and `LAYER_MASK` are the only way a graph can see the human's layer stack. Their layer choice
is button 0, an index into the layer list the human sees in the UI — which you cannot enumerate. Add
the node, wire its outputs (0..8 mirror the output node's inputs), and ask them to pick the layer.
This is the sanctioned way to build "grunge over their base coat" without touching their pixels.

## Verifying A Graph

1. `ap_node_list` — confirm every node you added exists with the type you asked for, and that the
   link table contains each connection with the right `from_socket` / `to_socket`. A missing link
   means the connect was rejected (index out of range) or overwritten by a later connect into the
   same input.
2. Read the socket `default_value`s back. A value that did not change means the write was dropped —
   usually `is_input` inverted, or a socket index past the end.
3. Confirm the chain actually reaches `OUTPUT_MATERIAL_PBR`. An orphan subgraph parses to nothing and
   the material renders as the stock defaults (0.8 grey, roughness 0.1) — a very recognisable "my
   graph is not connected" look.
4. `ap_export_material_bake <fresh dir>` and read the resulting maps back. This shows the material on
   a plane, independent of the mesh UVs — the fastest honest look at what you built.
5. Only then fill a layer with it.

## Persisting And Reusing

- `ap_export_material <path>.arm` writes the active material; `ap_import_asset` on that file brings it
  back as a material (a `.arm` with material nodes and no layer data routes to the material importer).
- Materials created this session are invisible to `ap_material_list` until the project is saved and
  reloaded — keep your own list of names you made.
- `script_material_delete` refuses to delete the last remaining material.
