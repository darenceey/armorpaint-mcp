# armorpaint-mcp

Drive [ArmorPaint](https://armorpaint.org) 1.0 from an MCP client.

An MCP server (Python) plus a small in-app bridge (an ArmorPaint *plugin*, written in the app's
embedded C dialect) let an agent open projects, inspect and edit materials and node graphs, paint,
and export textures — against a **stock, unmodified ArmorPaint**, including the official paid binary.

> **Not affiliated with, sponsored by, or endorsed by Armory3D or the ArmorPaint project.**
> This is an independent third-party tool. Please do not file ArmorPaint bugs for it, and do not
> file its bugs upstream.

> **Status: works, lightly travelled.** All 58 tools are implemented and were exercised against a
> live ArmorPaint 1.0: a 66-call sweep covering 42 tools returned **60 OK, 6 structured errors (all
> deliberate bad-input probes), 0 crashes or timeouts**, at 15–513 ms per call. That includes the
> destructive surface — `project_new`, `project_open`, `project_save_as`, `material_delete`,
> `export_*` and the paint ops — run against a scratch project.
>
> **Linux** was then tested separately (Arch/CachyOS, ArmorPaint 1.0 system package, Vulkan/RADV):
> an MCP stdio sweep over all 58 tools returned **77 OK and 5 structured errors (all deliberate
> probes), 0 crashes or timeouts**. Getting there needed Linux-specific plugin fixes; see
> [Linux notes](#linux-notes).
>
> What that does **not** cover: **macOS is untested.** Long painting sessions, huge meshes and 4K exports are untested, and
> the optional viewport patch has only been built against the pinned commit named in
> `docs/UPSTREAM_CHANGES.md`. Expect rough edges outside the tested path, and please report them.

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
                                     poll   res/<id>.done  (5 ms → 25 ms → 100 ms)
                                                                │
                                                     ┌──────────▼────────────┐
                                                     │     file mailbox      │
                                                     │  <spool>/req/  res/   │
                                                     │  heartbeat.json       │
                                                     │  bridge.lock          │
                                                     └──────────┬────────────┘
                                                                │
                                     read request, delete it, run it, write
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
 └──────────────────────────────────────────────────────────────────────────────────────┘
```

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
| OS | Windows 10/11 and Linux verified. macOS paths are written but untested. |
| Python | 3.11+ |
| MCP client | Anything that can launch a stdio MCP server (Claude Code, Claude Desktop, …) |
| Compiler | **None.** Not for the core toolkit. Only the optional viewport patch needs a self-built ArmorPaint. |

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

   Usually that is all: the server finds the spool by locating your ArmorPaint install (it only
   accepts a candidate directory that really contains `data\plugins`). If your install is somewhere
   unusual — the itch.io app, for instance — add `"env": {"ARMORPAINT_DIR": "C:\\ArmorPaint"}`.

5. **Check the handshake.** Ask the agent to call `ap_ping`. A healthy answer names the app version
   and the open project. If it reports the bridge as absent, see
   [Troubleshooting](#troubleshooting).

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

Because the bridge answers at most one request per frame, a chain like this completes in well under
a second — but a bake or a large export takes as long as ArmorPaint takes.

## Tool index

58 tools. Each is backed by a named minic binding or a registered struct field; the implementation
of each is tabulated in [docs/MINIC_DIALECT_AND_API.md](docs/MINIC_DIALECT_AND_API.md) §2.13, and
what was deliberately **left out, with the reason**, is §2.14 — read that before assuming a missing
capability is an oversight.

**Bridge & session** (6)

| Tool | Does |
|---|---|
| `ap_bridge_status` | **Call this first when anything fails.** Diagnoses the connection with no round trip: resolved spool path, heartbeat presence and whether its clock is advancing, plus a plain-language diagnosis and next step |
| `ap_ping` | Liveness: app version, uptime, open project, busy flag |
| `ap_get_app_info` | Window geometry, data path, project format version |
| `ap_bridge_set_enabled` | Turn the bridge off (and let the app idle again) or back on |
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
| `ap_export_textures` | Export the texture set to a directory, and report the filenames. 8-bit PNG; the format is not settable, and the base name comes from ArmorPaint's own state (the last export dialog, else `untitled`) — so read the returned filenames rather than predicting them |
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
| `ap_material_list` ⚠ | **Degraded.** Reads a save/load snapshot: empty before the first save, and blind to materials created this session. Use `ap_material_get_active` for ground truth. |

**Material nodes** (6) — `ap_node_list` (nodes *and* link topology), `ap_node_add`,
`ap_node_remove`, `ap_node_connect`, `ap_node_disconnect`, `ap_node_set_value` (float / color /
vector / button).

Node types are Blender-style uppercase identifiers — `TEX_NOISE`, `TEX_BRICK`, `RGB`, `MIX_RGB` —
and `ap_node_add` validates against the 76 legal names rather than passing an unknown string into
the app. The list is in `docs/MINIC_DIALECT_AND_API.md` §2.6.

**Painting & viewport** (7)

| Tool | Does |
|---|---|
| `ap_select_tool` | Select one of the 14 tools, and read back what took |
| `ap_set_brush` | Radius, opacity, hardness, scale, angle, blending |
| `ap_paint_stroke` | A stroke in screen space, as a point list |
| `ap_paint_stroke_world` | A stroke in world space |
| `ap_fill_layer` | Fill the active layer |
| `ap_set_display_channel` | Switch the viewport display channel (one of 16) |
| `ap_capture_to_project` | Capture the viewport **into the project as a texture asset** — see Limitations; this does *not* produce a file you can read |

**Optional, patched builds only** (1) — `ap_capture_viewport` writes the 3D viewport to a
real PNG *and returns it as an image*, which is what closes the see → adjust → see loop. It
requires the opt-in native patch in `patch/` and therefore a self-built ArmorPaint; see
[docs/UPSTREAM_CHANGES.md](docs/UPSTREAM_CHANGES.md). On a stock binary the tool reports
`unsupported` and says why. Measured on a patched 1.0 build: 11–15 ms in-app, ~140 ms round trip
for an 800×600 PNG.

### The loop that actually works

This one is worth stating plainly, because every step reports success and the obvious ordering
still shows you nothing:

```
ap_node_add / ap_node_set_value / ap_node_connect   edit the graph
ap_material_update                                  recompile it
ap_fill_layer   (or ap_paint_stroke / _world)       APPLY it   <-- the step people miss
ap_capture_viewport                                 look at it
```

**`ap_material_update` does not render.** ArmorPaint's viewport shows the *layer stack*; the node
graph is only the paint *source*. Measured: viewport captures taken before and after a colour
change plus `ap_material_update` are **byte-identical** — the pixels change only once you fill or
paint. There is no scriptable layer CRUD (see Limitations), so `ap_fill_layer` applies to whichever
layer the user has selected.

## Limitations

These are properties of ArmorPaint's plugin API, verified by reading its source. They are not
temporary gaps, and no amount of work on this repo removes them.

- **No 3D viewport capture on a stock binary.** A plugin *can* capture the viewport to a GPU
  texture, but the only save path (`viewport_save_texture`) encodes it into the project's in-memory
  asset list — persisted inside the `.arm`, unreachable from another process. `iron_encode_png` and
  `gpu_get_texture_pixels` are not exposed to plugins. **What an agent can actually see is its work
  product:** `ap_export_textures` writes real PNGs, which the server reads and returns as images.
  For the shaded viewport itself, the optional patch (12 added lines, one new binding) closes the
  gap on a self-built ArmorPaint.
- **ArmorPaint renders at full rate while the bridge is enabled.** The app normally sleeps after
  ~120 idle frames, and a sleeping app does not dispatch plugin callbacks — so a polling bridge must
  keep it awake, and pays for it in GPU and power. `ap_bridge_set_enabled` (and a toggle in the
  Plugins tab) turns it off when no agent is working. This is a real cost, not a rounding error.
- **One request per frame.** minic has no threads, so every handler runs inline on the render
  thread. Batching a backlog into one frame is not just slow, it risks the interpreter's 8 MB
  per-frame arena. Throughput is therefore bounded by frame rate; a hundred-op plan is a hundred
  frames.
- **A slow handler is a visible hitch** in the user's painting, for the same reason. Long
  operations (bake, large export) return immediately with a pending token and are polled.
- **No layer control.** Not a single layer binding exists beyond "fill the active layer" — no add,
  delete, reorder, rename, opacity, blend mode, or mask. Layer state is not readable either.
- **No undo/redo, no bake-parameter control, no export format/bit-depth control, no tone
  mapping or LUT, no shelf/resource search, no project metadata, no UI automation.** Each of these
  is a missing binding, itemised with its evidence in `docs/MINIC_DIALECT_AND_API.md` §2.14.
- **macOS is untested.** Windows and Linux have been measured; macOS shares the Linux code path
  but nothing there has been run.

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
- **The window does not need focus.** The bridge kept answering throughout testing while
  ArmorPaint was an unfocused background window.

## Troubleshooting

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

If `heartbeat.json` exists but its `t` value is not advancing between two reads, the plugin loaded
but is not being ticked — the app is idle, or the bridge is disabled.

**Does the ArmorPaint window have to be in the foreground?**
**No.** ArmorPaint has two sleep gates — a Windows-background gate (3 frames) and an idle gate
(120 frames) — and it turns out both increment the *same* counter, which the bridge resets on every
frame. Neither ever trips. This was traced in `iron.h` rather than assumed; the trace is in
`docs/MINIC_DIALECT_AND_API.md` §0.3. The tolerance is narrow (at most 3 consecutive missed frames),
so if you modify the plugin, keep `iron_delay_idle_sleep()` as the first statement of `on_update`.
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
| [docs/UPSTREAM_CHANGES.md](docs/UPSTREAM_CHANGES.md) | The optional viewport patch, exactly |

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
