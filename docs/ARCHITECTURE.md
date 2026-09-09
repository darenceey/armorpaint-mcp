# Architecture

Why this tool has the shape it has. Almost every structural decision here was forced by something
in ArmorPaint's plugin API rather than chosen, and the ones that were chosen are marked as such.

Everything below was verified by reading ArmorPaint 1.0 at commit
`906418acc600132fa927876d208eb452dc5a0967`. File and line references point into that tree.

## The shape

```
MCP client ──stdio JSON-RPC──► Python server ──files──► minic plugin ──► ArmorPaint internals
```

Three processes' worth of concerns, but only two artifacts: a Python package and a single `.c` file
that ArmorPaint interprets. The seam between them — the file mailbox — is specified byte-for-byte in
[PROTOCOL.md](PROTOCOL.md), so either half can be replaced independently.

## Why a file mailbox and not a socket

A socket would be the obvious transport. It is not available.

ArmorPaint exposes a fixed table of functions to plugins: `paint/sources/minic_api_list.h`, 529
active entries. **If a function is not in that table, and not a struct field registered in
`minic_api.c`, a plugin cannot reach it.** There is no `dlopen`, no FFI, no escape hatch — minic is
an interpreter over a whitelist.

Searching that table for anything network-shaped yields exactly two entries:

```c
X4(iron_file_download, "v(p:char url,p callback,i size,p:char dst_path)", v, p, p, i, p)
X4(file_download_to,   "v(p:char url,p:char dst_path,p done,i size)",     v, p, p, p, i)
```

Both are outbound, and both are narrower than they look. `iron_file_download`
(`base/sources/iron_file.c:450`) parses the URL by **blindly skipping the first 8 characters** —
i.e. it assumes the literal prefix `https://` — and then calls:

```c
iron_net_request(url_base, url_path, NULL, 443, IRON_HTTPS_GET, &_https_callback, cbd, dst_path);
```

Port `443` and `IRON_HTTPS_GET` are hardcoded constants, not parameters. There is no way to reach
`http://127.0.0.1:8765` through it, no way to POST, and — the decisive point — **no inbound listener
binding of any kind.** `ui_get_socket_id` is a node-graph socket, a false positive.

File I/O, by contrast, is unrestricted: read, write, delete, `mkdir`, directory listing, absolute
paths. So the transport is files. This is not a fallback that happens to work; it is the only
bidirectional channel that exists.

The cost is honest and small: latency is a poll interval rather than a wakeup, and the spool
directory is visible state on disk. The benefit beyond mere availability is that the whole
conversation is inspectable — when something misbehaves, the failing request is a file you can open.

## Why a plugin and not a source fork

We could have forked ArmorPaint and compiled a real server into it. That would have removed most of
the constraints in the next section. It was rejected, and this one *is* a choice:

- **It runs on the binary you bought.** ArmorPaint's distributed builds are paid, and they are what
  most users actually have. A fork is useless to them unless they rebuild — which means installing
  Visual Studio with clang tools, and maintaining a build.
- **Users need no compiler.** Installation is "copy a text file into `data/plugins`, tick a
  checkbox". The plugin is *interpreted*: `plugin_start()` reads the `.c` file at runtime and hands
  it to `minic_eval_named` (`paint/sources/plugin.c:13`). Nothing is built, ever.
- **It survives ArmorPaint updates.** A new ArmorPaint release replaces the executable. The plugin
  is data beside it, and keeps working as long as the bindings it uses still exist. A fork must be
  rebased against every release, forever, by someone.
- **The blast radius is bounded.** A plugin can only call what the table permits and cannot alter
  ArmorPaint's own behaviour. Disabling it is a checkbox; removing it is deleting a file. There is
  no version of this tool that leaves a modified ArmorPaint on someone's machine.
- **It stays inside upstream's intent.** Plugins are a supported extension point with eight bundled
  examples. We are a ninth, not a patch set.

The price is that we inherit every limitation of the plugin sandbox — which is the rest of this
document.

## The five constraints that shaped the implementation

### 1. A plugin cannot write atomically → the two-file commit

There is **no rename or move binding** anywhere in the 529. (`ui_mouse_move` is the only grep hit,
and is unrelated.) The single write primitive, `iron_file_save_bytes`, is a plain truncating
`_wfopen(path, "wb")` (`base/sources/iron_file.c:412`).

Therefore a reader **can** observe a half-written response file. The standard fix — write a temp,
rename over the target — is unavailable to the plugin. It is available to Python.

So the protocol is deliberately **asymmetric**:

| Direction | Writer | Atomicity |
|---|---|---|
| server → plugin | Python | `os.replace()` — a genuine atomic rename |
| plugin → server | minic | **two-file commit**: write `res/<id>.json`, *then* write `res/<id>.done` |

The reader ignores any response body with no matching `.done` marker. Because the body write
completes before the marker write begins, a visible marker implies a complete body. The marker
carries the body's byte length, so a torn write that still returned is detected rather than parsed
as garbage.

This is the reason the response side of the protocol looks over-engineered. It is not.

### 2. minic caps a script at 32 functions → one dispatcher, not 54 handlers

`minic.c:2099`:

```c
e->func_cap = 32;
e->funcs    = minic_alloc(e->func_cap * (int)sizeof(minic_func_t));
```

Registration walks the file and stops when `func_count == func_cap`. **There is no `else` branch —
the 33rd function is silently dropped**, and calling it fails at runtime as `unknown function`. No
warning at load.

A tool-per-function design would therefore break, invisibly, at tool 32 — and the natural symptom
(a few tools mysteriously unavailable) points nowhere near the cause.

So the bridge is **one `dispatch()` function containing an `if / else if` chain**, one arm per
operation. Arms are not functions; the chain can grow past 54 without approaching the cap. The
other caps are respected the same way: 32 arrays with 512 total elements, 128 locals per scope, 64
globals, 20 parameters.

There are more silent failures where that came from — no `#define`, no `switch`, no ternary, no
casts, non-short-circuiting `&&` and `||`, `main` must be the last function in the file — all
catalogued in [MINIC_DIALECT_AND_API.md](MINIC_DIALECT_AND_API.md) §1.12 and §1.13. Anyone editing
`plugin/armorpaint_mcp_bridge.c` should read those two sections first; the dialect looks like C and is not.

### 3. `data_get_blob` caches by path forever → mandatory eviction, never-reused ids

`base/sources/engine.c:1879`:

```c
buffer_t *cached = any_map_get(data_cached_blobs, file);
if (cached != NULL) return cached;
```

Keyed by path string, with **no expiry**. Read a file twice by the same path and you get the *first*
bytes, permanently. `data_delete_blob(path)` is the only eviction.

Two rules fall out, and both are load-bearing:

- The bridge calls `data_delete_blob` after **every** read. Omit it and the bridge appears to work
  for exactly one request per filename, then replays that first request forever — a failure that
  looks like a protocol bug and is not.
- Request ids are **monotonic and never reused**. A recycled filename risks serving a cached stale
  request, so ids are seeded from the wall clock at server start and only ever increase, across
  restarts.

### 4. The app sleeps, and a sleeping app does not tick plugins → full-rate rendering

`base/sources/iron.h:215`:

```c
void _update() {
#ifdef IRON_WINDOWS
    if (in_background && ++paused_frames > 3) { Sleep(1); return; }
#endif
#ifdef IDLE_SLEEP
    if (++paused_frames > start_sleep && !input_down) { Sleep(1); return; }  // start_sleep = 120
#endif
    ...
    iron_update();   // <- eventually dispatches every plugin's on_update
```

Both early returns are **above** `iron_update()`, which is what dispatches plugin callbacks. Once
either gate trips, `on_update` stops running — so it cannot un-trip them. **The gates are sticky.**
A polling bridge would go deaf after two seconds of inactivity.

`iron_delay_idle_sleep()` is one line (`iron.h:1011`): `paused_frames = 0;`. Calling it as the first
statement of `on_update` gives a steady state of 0 → 2 → 0, and neither threshold (3 and 120) is
ever reached.

**A useful surprise:** the two gates increment the *same* counter, so one reset defeats both —
including the Windows background gate. Live control of a **backgrounded** ArmorPaint works, and does
not require the window to be focused. This had been the design's biggest open question; it was
settled by tracing the counter rather than by testing, and the trace is in
[MINIC_DIALECT_AND_API.md](MINIC_DIALECT_AND_API.md) §0.3. The tolerance is exact: at most **3
consecutive missed frames**, so the reset must precede any early return in `on_update`.

**The cost is real and unavoidable.** While the bridge is enabled, ArmorPaint never sleeps: full-rate
rendering, GPU load, and battery, whether or not an agent is doing anything. That is why the bridge
ships with an enable/disable flag exposed three ways — a toggle in the Plugins tab, the
`ap_bridge_set_enabled` tool, and a persisted file the plugin reads at start. The toggle alone would
not be enough, because `on_ui` runs *only while the Plugins tab is being drawn*
(`paint/sources/ui/tab_plugins.c:27`), so a user on any other tab could not reach it.

### 5. No threads → one request per frame

minic exposes no threading primitive. Every handler therefore runs **inline on the render thread**,
inside `on_update`. Two consequences are designed around rather than worked around:

- **A slow handler is a visible hitch** in the user's painting. Long operations (bake, large export)
  return a pending token immediately and are polled through a status file, rather than blocking a
  frame for seconds.
- **At most one request is handled per frame.** This is a latency decision *and* a memory-safety
  one. minic allocates from an 8 MB arena with **no bounds check** (`minic.c:287`), rewound only at
  the outermost host→script boundary — i.e. once per `on_update`. Each script function call costs
  ~29 KB of it, giving roughly **280 calls per frame before the arena overflows into heap
  corruption**. Draining a backlog of requests in one frame would be a genuine crash risk, not just
  a stutter.

Throughput is thus bounded by frame rate: an *n*-step plan takes *n* frames. At 60 fps that is fast
enough to feel instant for interactive work, and it is the honest ceiling.

### And one more: the JSON parser cannot handle nested objects or arrays

`json_parse_to_map` **flattens** a nested object into the top level (the containing key simply
disappears), and **any JSON array corrupts the rest of the parse**
(`base/sources/iron_json.c:297-320`). Every value comes back as a string with escapes undecoded, and
there is no `atoi`/`atof` binding to convert them.

So the on-the-wire request shape is deliberately **flat, compact, and string-typed**, with list
arguments (stroke point lists, for instance) passed as delimited strings rather than JSON arrays.
The Python side presents a normal typed MCP schema and does the conversion; the ugliness stops at
the mailbox. Details in [MINIC_DIALECT_AND_API.md](MINIC_DIALECT_AND_API.md) §2.9.

## Safety posture

minic has **no exceptions**, and pointer dereference is **unchecked** — a null `->` is a segfault
inside the user's paint session, potentially with unsaved work open. The interpreter's own
`minic_error` path reports to the console and sets an error flag, but a bad pointer never gets that
far.

The bridge is therefore written defensively as a matter of policy:

- **Every argument is validated before it reaches a binding.** Paths are existence-checked, indices
  range-checked, enum values checked against their legal sets, names looked up before use.
- **A request the plugin cannot serve returns `unsupported`, naming the missing binding.** Never a
  silent no-op, never a crash. An agent can tell "this is impossible here" from "this failed".
- **Requests are deleted before they execute.** If a handler ever did crash the app, a request left
  on disk would be replayed at next launch — running a destructive operation into the user's project
  a second time, unasked.

## Optional: the native-patch power mode

**Opt-in. Requires building ArmorPaint from source. Everything else in this tool works without it.**

On a stock binary, an agent cannot see the shaded 3D viewport. It can *capture* it —
`viewport_capture_screenshot_to()` is bound — but it cannot get the pixels out:

- `viewport_save_texture()` encodes the PNG into `g_project->packed_assets`
  (`paint/sources/viewport.c:96`), an in-memory list persisted only inside the `.arm` on save.
  Another process cannot read it.
- `iron_encode_png` and `gpu_get_texture_pixels` are **not in the binding table.**

What the agent *can* see is its actual work product: `export_texture_run(path, bake_material)` is
bound (`minic_api_list.h:540`) and writes real PNG/EXR files to disk. For most tasks — "did the
material come out right" — that is the more useful artifact anyway.

For the viewport itself, the gap turns out to be one missing binding rather than missing
functionality: **`iron_write_png(path, bytes, w, h, format)` already exists upstream**
(`base/sources/iron_image.h:7`) and writes straight to a path. The patch in `patch/` adds a
three-line wrapper beside `viewport_save_texture`, its declaration, and the one `X2` line that
exposes it — **3 hunks, 12 added lines, no socket, no thread, no dependency, no change to existing
behaviour.**

Deliberate properties:

- **Idempotent and reversible** (`--revert`).
- **Anchored structurally, not by line number** — the script finds `viewport_save_texture()` and
  walks its braces. If upstream renames or removes it, the script **fails loudly and refuses to
  patch** rather than corrupting the file.
- **Every inserted line is marked `armorpaint-mcp`**, so a patched tree is plainly identifiable as
  altered — which is what ArmorPaint's zlib licence requires of altered source versions.
- **This repository ships no ArmorPaint source and is not a fork.** The script edits *your* checkout
  in place.

With the patch, `ap_capture_viewport` returns a real image. Without it, that one tool
reports `unsupported` and names the missing binding; the other 54 are unaffected.

Full rationale, the exact diff, the rebuild recipe, and the licence position:
[UPSTREAM_CHANGES.md](UPSTREAM_CHANGES.md).

## What is deliberately absent

A capability missing from the tool index is usually a missing *binding*, not an unfinished feature —
layers, undo/redo, bake parameters, export format control, tone mapping, texture sets, UV tiles,
shelf search, project metadata, UI automation. Each is listed with the evidence for its exclusion in
[MINIC_DIALECT_AND_API.md](MINIC_DIALECT_AND_API.md) §2.14. That table is the right place to check
before opening a feature request: if the answer is "no binding exists", the fix is upstream, in
`minic_api_list.h`, and not here.
