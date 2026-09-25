# ArmorPaint MCP — wire protocol v1

The MCP server (Python) and the in-app bridge (a minic plugin) talk over a **file mailbox**. This
document is the contract; either side may be reimplemented against it.

## Why files and not a socket

ArmorPaint's plugin API (`paint/sources/minic_api_list.h`, 529 bindings) exposes **no inbound
sockets**. The only network bindings are `iron_file_download` / `file_download_to`, both hardcoded
to `port 443, HTTPS GET`. A localhost listener is unreachable from a plugin. File I/O, by contrast,
is unrestricted. So: files.

## The awkward constraint that shapes everything

**A plugin cannot perform an atomic write.** There is no `rename`/`move` binding anywhere in the
529 (the only grep hit, `ui_mouse_move`, is a false positive), and `iron_file_save_bytes` is a plain
truncating `_wfopen(path, "wb")`. A reader can therefore observe a **half-written response**.

The usual fix — write to a temp name, atomically rename over the target — is unavailable **to the
plugin**. It *is* available to Python (`os.replace`). Hence the protocol is **asymmetric**:

| Direction | Writer | Atomicity mechanism |
|---|---|---|
| Server → plugin (request) | Python | `os.replace()` — genuinely atomic |
| Plugin → server (response) | minic plugin | **two-file commit**: body first, then a marker |

**Two-file commit:** the plugin writes the full body to `res/<id>.json`, and only *after that call
returns* writes a tiny `res/<id>.done`. The reader ignores any `res/<id>.json` that has no matching
`.done`. Since the body write completes before the marker write begins, a visible marker implies a
complete body. The marker doubles as a checksum carrier (below) so a reader can also detect the
pathological case of a truncated write that still returned.

## Layout

```
<spool>/                     default: Windows <ArmorPaint data dir>/mcp_spool;
                             Linux ~/.local/share/armorpaint-mcp/spool;
                             macOS ~/Library/Application Support/armorpaint-mcp/spool
  req/<id>.json              server → plugin   (written via os.replace)
  doorbell                   server → plugin   (the newest request's id; written via os.replace)
  res/<id>.json              plugin → server   (body)
  res/<id>.done              plugin → server   (commit marker; see below)
  heartbeat.json             plugin → server   (liveness, rewritten ~1 Hz)
  bridge.lock                plugin → server   (written once at plugin start)
```

`<id>` is a monotonic, **never-reused** decimal string minted by the server (`1`, `2`, …, seeded
from the wall clock at server start to survive restarts). Never reusing an id matters: `data_get_blob`
caches by path forever (see below), so a recycled filename risks serving stale bytes.

## Envelopes

**Request** — `req/<id>.json`
```json
{"v":"1","id":"1730-42","op":"select_tool","deadline_ms":"30000","a_tool":"0"}
```

The request envelope is **flat, compact, and all-string**, and that is forced by the plugin's
parser rather than chosen. `json_parse_to_map` (`base/sources/iron_json.c:297`):

* returns every value as a `char *`, so numbers and booleans travel as text (`"0"`, `"true"`)
  and the plugin converts them with its own `to_int` / `to_float` / `to_bool` — there is no
  `atoi` binding;
* **flattens** a nested object into the same map: the `"args"` key itself disappears and its
  members land at the top level. Arguments therefore carry an **`a_` prefix** so an argument
  named `id` or `op` cannot collide with the envelope. The plugin reads envelope keys with
  `any_map_get` directly and never through its `arg()` helper, so `a_op` cannot hijack the
  dispatcher;
* **an array anywhere in the document corrupts the remainder of the parse** — the opening
  token is skipped and the elements are consumed as key/value pairs. No request may contain
  one. List-shaped arguments cross as delimited strings: stroke points are `"x,y;x,y"` (or
  `"x,y,z;..."`), inline OBJ uses `|` for line breaks;
* detects a key by testing for `:` immediately after the closing quote, so the writer must use
  compact separators (`json.dumps(o, separators=(",", ":"))`);
* does **not** decode escapes, so no value may contain a quote, a backslash or a control
  character. Paths use forward slashes.

`deadline_ms` is advisory: the plugin runs every handler inline and cannot abandon one.

**Response** — `res/<id>.json`
```json
{ "v": 1, "id": "1730-42", "ok": true, "result": { "index": 3 }, "elapsed_ms": 12 }
```
```json
{ "v": 1, "id": "1730-42", "ok": false, "error": { "code": "no_project", "message": "No project is open." } }
```

**Commit marker** — `res/<id>.done`: the decimal byte length of `res/<id>.json`, ASCII, no newline.
A reader that finds a length mismatch treats the response as torn and retries (bounded), rather than
parsing garbage.

**Heartbeat** — `heartbeat.json`, rewritten roughly once per second:
```json
{ "v": 1, "pid_hint": "armorpaint", "bridge_version": "2.0.0", "app_version": "1.0",
  "t": 1234.567, "project": "C:/work/goblin.arm", "busy": false }
```
`t` is `sys_time()` seconds since app start — **monotonic within a run, not wall clock.** Liveness is
judged by `t` *advancing* between two reads, never by comparing it to the system clock.

## Server algorithm

1. Mint `id`. Write `req/<id>.json` to `req/<id>.json.tmp`, then `os.replace()` onto the final name.
2. **Ring the doorbell:** write `<id>` (ASCII, no newline) to a temp file and `os.replace()` it onto
   `doorbell`. Always *after* step 1, so a ring never names a missing file.
3. Poll for `res/<id>.done` (see cadence below). On appearance: read `.done` → expected length; read
   `res/<id>.json`; verify length; parse. While `req/<id>.json` still exists, ring again every
   250 ms: two servers ringing at once lose one ring (last write wins), and the re-ring recovers it.
4. Delete `res/<id>.json` and `res/<id>.done`. (The plugin already deleted the request.)
5. On timeout, delete `req/<id>.json` best-effort and return a transport error naming the op.

**Poll cadence:** 5 ms for the first 200 ms, then 25 ms, then 100 ms after 2 s. Most ops answer in
one or two frames; bakes and exports take seconds. This keeps latency low without spinning.

## Plugin algorithm (`on_update`, every frame)

```
on_update():
    if awake: iron_delay_idle_sleep()   # before any early return — see "The idle gate"
    accumulate sys_real_delta(); return early unless >= poll_interval

    if not iron_file_exists(spool + "/doorbell"): return     # never list req/ — see below
    id = contents of doorbell; data_delete_blob; iron_delete_file(doorbell)
    name = id + ".json"
    if not iron_file_exists(spool + "/req/" + name): return  # answered already, or abandoned
    if not id_is_safe(id): delete and return    # it becomes a reply filename

    path = spool + "/req/" + name
    blob = data_get_blob(path)
    text = sys_buffer_to_string(blob)   # copy out BEFORE the eviction frees the buffer
    data_delete_blob(path)     # MUST — the cache is keyed by path and never expires
    iron_delete_file(path)     # consume before executing: a crash must not replay

    if not looks_like_a_json_object(text): reply(bad_args); return   # see below
    reply = dispatch(json_parse_to_map(text))
    iron_file_save_bytes(res + "/" + id + ".json", sys_string_to_buffer(reply), 0)
    iron_file_save_bytes(res + "/" + id + ".done", length_of(reply), 0)   # commit
```

Six things in there are load-bearing and easy to get wrong:

- **Never list `req/` to poll.** See *The doorbell*: on Linux and macOS every directory listing
  leaks a file descriptor, and a bridge that polls by listing hangs ArmorPaint within a minute.

- **`data_delete_blob` is mandatory.** `data_get_blob` memoises by path in `data_cached_blobs`
  (`base/sources/engine.c:1879`) with no expiry. Omit the eviction and the bridge appears to work
  for exactly one request per filename, then silently replays the first one forever.
- **Delete the request *before* executing it.** If a handler crashes the app, a request left on disk
  would be re-executed on restart — replaying a destructive op into the user's project.
- **The `.done` marker is written last, as a separate call.** Writing it in the same buffer, or
  first, defeats the whole commit scheme.
- **Budget script calls per frame.** This is a memory-safety rule, not only a latency one: minic
  charges ~29 KB of its 8 MB context arena per script function call and rewinds it only at the
  `on_update` boundary, giving roughly 280 calls per frame, and `minic_alloc` has no bounds check
  (`minic.c:287`). Draining an arbitrary backlog in one frame overflows the arena and corrupts the
  heap. The bridge therefore opens **one request per frame**, and a `batch` request (below) runs
  further items in the same frame only while its call counter stays under a budget sized from
  measurement: a plain request peaks at ~650–800 KB, a batch frame at ~1.5 MB.
- **Screen the body before parsing it.** `jsmn_parse` returns a *negative* token count for
  malformed input and `load_tokens` (`iron_json.c:270`) passes that straight into
  `malloc(sizeof(jsmntok_t) * count)` — `NULL` for a negative size — and then writes through it.
  An empty file, a torn write, or any stray `.json` dropped into `req/` takes ArmorPaint down
  mid-paint. The host exposes no validity check, so the plugin does a bracket/quote balance scan
  itself and rejects arrays outright.

## The doorbell

Why the bridge learns request ids from a file instead of listing `req/`: **on Linux and macOS,
every directory listing inside ArmorPaint 1.0 leaks a file descriptor.** `iron_read_directory` and
`file_read_directory` both go through `open_dir` / `close_dir` in `base/sources/kong/dir.c`, and
the POSIX `close_dir` is an empty function — the `DIR *` from `opendir()` is never closed, so its
descriptor and its buffer live until the process exits. (The Windows branch calls `FindClose` and
does not leak.)

Bridge 1.x polled by listing `req/` — up to 60 times a second during a conversation, 20 while
lingering. MEASURED on ArmorPaint 1.0 (Arch package) under KDE Plasma 6 / XWayland / RADV,
launched from the desktop, where a systemd user unit gives it a soft limit of **1024** descriptors:
after ~32 s of activity the process held 1022 descriptors, 1006 of them on `req/`, and its main
thread hung for good inside `gpu_present` — the Vulkan driver could no longer get a sync-file
descriptor. No error anywhere: the window goes grey and stops answering. Launched from a terminal
with a high limit, the same leak runs for hours before it bites, which is how it went unnoticed.

Bridge 2 therefore polls one known file with `iron_file_exists` (an `fopen` + `fclose`, no leak).
The cost of an idle poll is one failed `fopen`. What still lists directories, one descriptor per
listing: the stale-request drain at plugin load, `fs_list`, and exports (which list the target
directory to report the files they added). A long session of exports therefore still leaks slowly;
`tests/test_live.py::test_polling_does_not_leak_file_descriptors` guards the polling path.

The real fix is upstream: `close_dir` should call `closedir(dir->handle)`.

## Batches

One request can carry an ordered list of ops. Arrays cannot cross this wire, so item `i` is
spelled out with prefixed keys:

```json
{"v":"1","id":"7-3","op":"batch","deadline_ms":"90000","a_count":"3","a_stop_on_error":"false",
 "b0_op":"select_tool","b0_a_tool":"2","b1_op":"fill_layer","b2_op":"node_list"}
```

The plugin runs the items **in order**, reading item arguments as `b<i>_a_<name>` (never falling
back to envelope-level keys), and answers once:

```json
{"v":1,"id":"7-3","ok":true,"result":{"count":3,"executed":3,"errors":0,"frames":2,
 "stopped_early":false,"results":[{"i":0,"op":"select_tool","ok":true,"result":{...}}, ...]}}
```

Scheduling (`step_job` in the plugin): light items share a frame while the frame has used fewer
than `CALL_BUDGET` script calls and less than 8 ms; GPU-heavy items (strokes, fills, layer ops,
undo, captures) start a frame of their own; **heavy** items — exports, saves, opens, imports,
bakes — also wait until no mouse button is held in the app, so they never stall the render thread
in the middle of a stroke. A fill that schedules its next-frame refill ends the frame too.
Nothing new is read from `req/` until the open request's reply is committed. A batch holds at most
64 items and, like every request, at most 16 KB.

## Dozing and waking

The bridge no longer holds ArmorPaint awake for as long as it is enabled. It calls
`iron_delay_idle_sleep()` only while there is a reason to: an open request, a pending refill, or
less than `linger` seconds (default 10; `bridge_set_idle`) since the last request. Otherwise it
lets the idle gate below put the app to sleep, and writes `"dozing": true` into the heartbeat.

A sleeping app runs no `on_update`, so the **server** wakes it: before writing a request to a
dozing bridge, and again every second while such a request sits unread, it sends ArmorPaint's
window a synthetic pointer move — two, one pixel apart, ending where the real pointer already is,
because Iron ignores a move with a zero delta and has no delta for the first move it sees. Any
input event zeroes `paused_frames` (`iron.h` `_mouse_move` et al.), so the app runs ~120 frames,
the bridge sees the request, and holds itself awake again for `linger`. Delivery is `XSendEvent`
to the window on X11 (empty event mask: delivered to the creating client, no focus change),
`PostMessageW` on Windows, `CGEventPostToPid` on macOS. Measured on Linux: asleep at frame 120,
wiggle, frames resume; a ping to a dozing bridge answers in ~40 ms.

A disabled bridge (`bridge_set_enabled false`) is simply a bridge that never holds the app awake
and refuses every op except `bridge_set_enabled` and `ping` with `bridge_disabled`; since the
server wakes it like any dozing bridge, it can be re-enabled remotely.

Heartbeat fields added for this: `dozing` (bool), `linger` (s), `job_open` (a request is being
worked on), `job_held` (a heavy item is waiting for the mouse to be released), `ext` (-1 unknown,
0 absent, 1 native extension present). The server treats a frozen `t` as a fault only when the
bridge is not dozing, the request is still unread, and the freeze has lasted `STALL_LIMIT_S`
(8 s) — a single frame can legitimately take seconds.

## The idle gate

`base_update()` returns **before** `iron_update()` — which is what dispatches every plugin
`on_update` — under two conditions:

- **Windows background gate:** `in_background && ++paused_frames > 3`.
- **Idle gate:** ~120 frames without input (`flags.idle_sleep = true` for ArmorPaint,
  `paint/project.js:18`).

**Both are sticky**: once tripped, `on_update` no longer runs, so it cannot un-trip them. A polling
bridge would go deaf precisely when an agent drives an unfocused app.

`iron_delay_idle_sleep()` resets the idle counter; while the bridge wants the app awake it must be
reached before any early return in `on_update` — upstream's own `make_tilesheet.c` uses exactly
this pattern. **The cost is real:** an app held awake renders at full rate, which is why the
bridge only holds it awake around requests and otherwise lets it sleep (see *Dozing and waking*).

**Resolved — it defeats both.** The background gate is *not* a separate counter. `iron.h:215`
increments the single `paused_frames` in each gate and `iron_delay_idle_sleep()` is one line,
`paused_frames = 0` (`iron.h:1011`). Tracing one frame with the reset inside `on_update`: enter at
0 → background gate `++` → 1 (not > 3) → idle gate `++` → 2 (not > 120) → `iron_update()` →
`on_update` → back to 0. Steady state is 0→2→0 and neither gate ever trips, so live control of a
**backgrounded** ArmorPaint works.

The precondition is exact: the background gate tolerates at most **three** consecutive missed
frames, so the reset must be reached unconditionally on every frame — **above the poll-interval
early return**, and below nothing except the enable check. (`make_tilesheet.c:26` puts its own call
*after* `if (!baking) return;` precisely because it is happy to go idle when not baking. A bridge
that must stay awake is not.)

## Concurrency

minic exposes no threads. **Every handler runs inline on the render thread inside `on_update`**, so
a slow handler is a visible hitch in the user's painting. Consequences baked into the design:

- The plugin opens **at most one request per frame**; a `batch` request runs several items per
  frame within the call budget (see *Batches*), and heavy items wait for the mouse to be released.
- Long ops (bake, export) *may* return `{"ok": true, "result": {"status": "pending", "token": …}}`
  immediately, and the server then polls with a `job_status` op. **The current bridge never emits
  this shape** — every op it implements completes inline, and export/save simply block for their
  whole duration with `busy: true` in the heartbeat. The client side is implemented and inert; it
  is reserved, not live. Which ops would actually need it is a measurement, not a guess.
- Payloads stay small. Bulk data (textures, screenshots) is passed **by path**, never inline: the
  plugin writes a PNG via `export_texture_run`, replies with the path, and the Python side reads and
  base64s it into the MCP image response.

## Error codes

`no_project` · `bad_args` · `not_found` · `unsupported` · `app_busy` · `internal` ·
`bridge_disabled`. Ops served by the optional native extension answer `unsupported` on a build
without it.
Anything the plugin cannot do is `unsupported` with a message naming the missing binding — never a
silent no-op, and never a crash. minic has no exceptions and unchecked pointer deref, so **the
dispatcher validates every argument before touching a binding**; one bad request must not take down
the user's session.

## The op and argument vocabulary

This protocol carries names; it does not define them. **The names are
`armorpaint_mcp/server.py`'s tool surface**: an `op` is an `ap_*` tool with the `ap_` prefix
stripped, and the argument names are exactly what that tool's `_build_wire_args()` branch emits.
`plugin/armorpaint_mcp_bridge.c` has one `else if` arm per op reading exactly those keys; ops it
does not know go to the native extension (`patch/mcp_ext.c`) when the build has it. The pairing is
checked by `tests/test_offline.py::test_every_bridge_tool_maps_to_a_handled_op`.

Treat that pairing as part of the contract, because **neither kind of drift fails loudly**:

| Drift | Symptom |
|---|---|
| Op renamed on one side | the tool returns `unsupported` — looks like a missing capability |
| Argument renamed on one side | the key is simply absent, so the arm takes its default and answers **`ok`**. `material_set_channels` toggling nothing, `set_brush` ignoring a radius, `bridge_set_enabled` never disabling — all silent |
| Delimiter changed on one side | `paint_stroke` paints one point instead of a stroke, and still reports success |

So: change both halves in the same commit, and keep the two shared constants — the stroke-point
cap and the inline-OBJ line separator — equal on both sides. Each is commented as such where it is
defined.

## Versioning

`v` is the envelope version. The server refuses a bridge whose `bridge_version` major it does not
support and says so plainly (`bridge_version_mismatch`), rather than failing later in a confusing way.

| Bridge major | Poll mechanism | Server that speaks it |
|---|---|---|
| 1 | lists `req/` (leaks a descriptor per listing on Linux/macOS) | 1.x, and the current server — which also warns in `ap_bridge_status` |
| 2 | the doorbell | the current server only. An older server never rings, so it would time out silently; the major bump makes it refuse the bridge instead |
