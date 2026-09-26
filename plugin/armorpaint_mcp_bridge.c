#include "global.h"

// ============================================================================
// armorpaint-mcp — in-app bridge (plugin half of docs/PROTOCOL.md v1)
//
// Drop this file into ArmorPaint's plugins folder and enable it in the Plugins
// tab. It watches a spool directory for request files written by the Python MCP
// server, executes them in order (a batch request runs several items per frame
// inside an arena budget), and writes replies back with a two-file commit.
// Between requests it lets ArmorPaint sleep; the server wakes it (see linger).
//
// WHY A FILE MAILBOX: minic exposes no inbound sockets (the only network
// bindings are HTTPS GET downloads), so files are the only transport a plugin
// can offer. See docs/PROTOCOL.md.
//
// ---------------------------------------------------------------------------
// THE OP VOCABULARY IS THE SERVER'S, NOT THIS FILE'S
//
// Every `op` string below is an `ap_*` tool from armorpaint_mcp/server.py with
// the `ap_` prefix stripped, and every argument name below is exactly what
// server.py's _build_wire_args() emits for that tool. That pairing IS the
// contract. A rename on either side does not fail loudly: an unknown op comes
// back as `unsupported`, and an unknown ARGUMENT is simply absent, so an op
// like material_set_channels answers `ok` while changing nothing. If you add or
// rename an op here, change server.py in the same commit, and vice versa.
//
// ---------------------------------------------------------------------------
// SPOOL LAYOUT (all paths use forward slashes; backslash escapes truncate
// minic string literals)
//
//   <spool>/req/<id>.json     server -> plugin   (server writes atomically)
//   <spool>/doorbell          server -> plugin   (the id of the newest request)
//   <spool>/res/<id>.json     plugin -> server   (body)
//   <spool>/res/<id>.done     plugin -> server   (commit marker: byte length)
//   <spool>/heartbeat.json    plugin -> server   (~1 Hz liveness)
//   <spool>/bridge.lock       plugin -> server   (written once at start)
//
// <spool> is, on Windows, data_path() + "mcp_spool", i.e. "./data/mcp_spool"
// relative to ArmorPaint's working directory. On Linux and macOS it is an
// ABSOLUTE per-user path (see find_home()), because there a relative path does
// not name one directory: reads resolve it against the executable's directory
// and writes against the working directory. The resolved path is logged to the
// console at start and is reported by get_app_info.
//
// ---------------------------------------------------------------------------
// REQUEST SHAPE — READ THIS BEFORE CHANGING THE SERVER
//
// Requests are parsed with json_parse_to_map(), whose behaviour is fixed and
// hostile (iron_json.c:297). It must be fed a FLAT, COMPACT object whose values
// are all JSON strings:
//
//   {"v":"1","id":"42","op":"select_tool","a_tool":"10"}
//
//   * every value comes back as a char* (numbers and booleans included);
//   * nested objects are FLATTENED into the same map -- the "args" key itself
//     disappears, so nested arg names share the envelope namespace. The "a_"
//     prefix keeps them apart. arg() also accepts a bare name so a server that
//     literally sends PROTOCOL.md's {"args":{...}} still works;
//   * the ENVELOPE key "op" is therefore read with any_map_get DIRECTLY, never
//     through arg(). Going through arg() would let a request carrying an
//     argument named `op` (legal: server.py's _ARG_RE accepts it) send "a_op"
//     and hijack the dispatcher;
//   * ARRAYS CORRUPT THE REST OF THE PARSE. Never send one. List-shaped args
//     (stroke points) arrive as delimited strings;
//   * key detection is s[end]==':' -- no space may sit before a colon, so the
//     server must emit compact separators (json.dumps(o, separators=(",",":"))).
//
// REPLY SHAPE (parsed by Python, so it may nest freely):
//
//   {"v":1,"id":"42","ok":true,"result":{...},"elapsed_ms":12}
//   {"v":1,"id":"42","ok":false,"error":{"code":"...","message":"..."},"elapsed_ms":1}
//
// Result/error objects are built with the json_encode_* family; the envelope
// around them is assembled with string(). That split is forced: json_encode_key
// is NOT a bound function and json_encode_begin_object() takes no key, so the
// json_encode_* family physically cannot emit a nested object under a key. It
// also never escapes string values, hence jesc() on every dynamic string below.
//
// ---------------------------------------------------------------------------
// DIALECT CONSTRAINTS OBSERVED HERE (see docs/MINIC_DIALECT_AND_API.md)
//   * main() is the LAST function; anything after it is never registered.
//   * 31 functions + main, against a silent hard cap of 32 -- ONE slot left.
//     Every operation is an else-if ARM inside dispatch(), never its own
//     function. Adding a helper means removing one.
//   * no switch, no ternary, no casts, no i++ inside an expression, no #define,
//     no fixed-size local arrays (they leak from a shared 512-slot pool).
//   * && and || do NOT short-circuit, so every null check is its own nested if.
//   * minic evaluates EVERY else-if CONDITION even after one has matched
//     (minic.c:1592) though it does skip the bodies. The conditions here are
//     pure string_equals calls, so that is ~55 strcmps and no side effects.
//     Never put a side effect in a dispatcher condition.
//   * ~29 KB of the 8 MB arena per SCRIPT function call, rewound only at the
//     on_update boundary => ~280 script calls per frame. The list and stroke
//     caps below keep the worst single op well inside that, and ncalls /
//     CALL_BUDGET keep a batch from stacking ops past it.
//   * a string LITERAL inside a function lives in that arena too, so it dies
//     with the frame: never park one in a global (see EMPTY_STR).
// ============================================================================

char *BRIDGE_VERSION = "2.1.0";
int   ENVELOPE_V     = 1;

// Per-frame script-call budget. Every number in a stroke request costs one
// to_float script call: 48 screen points are 96, 48 world points 144, and a
// point with pressure (radius and opacity multipliers) adds two more. So a
// request is capped at MAX_STROKE_POINTS points AND MAX_STROKE_VALUES numbers;
// 64 list items cost 64*2 jesc calls. All leave margin under ~280.
// Both limits must equal MAX_POINTS / MAX_VALUES in armorpaint_mcp/strokes.py.
int MAX_STROKE_POINTS = 48;
int MAX_STROKE_VALUES = 150;
int MAX_LIST_ITEMS    = 64;

// A STREAMED STROKE: stroke_begin, then any number of stroke_points requests
// (one frame each), then stroke_end. It works because ArmorPaint keeps a script
// stroke open across frames: script_paint_begin_stroke runs once per stroke
// (guarded by script_paint_active, minic_impl.c) and only script_paint_end
// dilates and closes it. So a stroke is no longer limited to what fits in one
// frame. An open stroke is closed by end_stroke() before any other op runs, and
// after STROKE_IDLE_S without points, so a stroke can never be left dangling.
// stroke_r0/o0 are the brush radius and opacity to restore at the end: per-point
// pressure multiplies them.
int   stroke_open    = 0;
int   stroke_world   = 0;
int   stroke_total   = 0;
float stroke_touched = 0.0;
float stroke_r0      = 0.0;
float stroke_o0      = 0.0;
float STROKE_IDLE_S  = 5.0;

// OPTIONAL BINDINGS ARE DETECTED, NOT CONFIGURED.
//
// Calling a function the build does not export is a minic runtime error -- but
// it aborts only the SCRIPT FUNCTION it happens in: every call runs in a fresh
// child env (minic.c:823) whose error flag never reaches the caller, which just
// receives 0. MEASURED on stock 1.0: a helper whose body calls an unknown
// binding returns 0 and its caller carries on. So each optional binding is
// reached only through a one-line try_* wrapper that returns non-zero when the
// call really happened. States: -1 not probed yet, 0 absent, 1 present.
//
//   ext_state     mcp_ext_call -- the native extension from patch/apply_ext_patch.py
//                 (layers, undo/redo, export format, bake, render settings, ...)
//   png_state     viewport_save_texture_to_file -- upstream since 2026-09-09
//                 (commit 1e14e27e), or patch/apply_viewport_patch.py
//
// The one cost: the first probe on a build without the binding prints ONE
// "unknown function" error line to the ArmorPaint console. main() says so first.
int   ext_state   = -1;
int   png_state   = -1;
char *ext_ops     = "";
int   ext_version = 0;

// Script calls made in the current frame (every helper bumps it). minic charges
// ~29 KB of its 8 MB per-context arena per script call and only rewinds at the
// host->script boundary, i.e. once per on_update. A batch keeps running items
// in one frame only while this stays under CALL_BUDGET, so the worst item that
// can follow (a 48-point stroke, ~150 calls) still fits. See step_job().
int ncalls      = 0;
int CALL_BUDGET = 90;
// ...and only while the frame has spent less than this in the bridge.
float FRAME_BUDGET_S = 0.008;

// 1 on Windows, set first thing in main(). No binding reports the platform, but
// data_path() is "." PATH_SEP "data" PATH_SEP (engine.c), so its separator does.
int is_windows = 0;
int is_macos   = 0;

void        *plugin;
ui_handle_t *h_panel;
ui_handle_t *h_enabled;

char *spool_root;
char *dir_req;
char *dir_res;
char *path_heartbeat;
char *path_lock;

// THE DOORBELL. req/ is never listed while polling, because on Linux and macOS
// every directory listing LEAKS a file descriptor: Iron's POSIX close_dir() is an
// empty function (kong/dir.c), so the DIR* from opendir() is never closed. At a
// listing per poll that is 20-60 fds a second; under the 1024-fd soft limit a
// desktop launch gets (systemd units), ArmorPaint ran out in ~32 s and hung
// forever inside gpu_present -- the Vulkan driver could not get a sync fd.
// MEASURED on ArmorPaint 1.0 / KDE Plasma 6 / RADV: 1006 of 1022 open fds were
// handles on req/.
//
// Instead the server writes the request's id into <spool>/doorbell after the
// request itself. A poll is one iron_file_exists() (fopen + fclose, no leak);
// when it rings, the bridge reads the id, deletes the doorbell and opens
// req/<id>.json directly. Two servers ringing at once lose one ring (last write
// wins), so a waiting server re-rings while its request is still unclaimed.
//
// A server that predates the doorbell never rings, so this is bridge MAJOR 2:
// such a server refuses it by version instead of timing out silently.
char *path_bell;

int   enabled       = 1;
int   busy          = 0;
float poll_interval = 0.05;
float poll_accum    = 0.0;
float hb_accum      = 0.0;
int   req_count     = 0;
int   err_count     = 0;
char *last_op       = "-";
char *last_id       = "-";
char *last_error    = "-";

// DOZE. While the bridge holds ArmorPaint awake (iron_delay_idle_sleep every
// frame) the app renders at full rate. Instead it holds it awake only for
// `linger` seconds after the last request, then lets it sleep. A sleeping app
// runs no on_update, so the SERVER wakes it before writing a request: Iron
// resets its idle counter on any input event (iron.h _mouse_move etc.), and the
// server posts a synthetic 1-pixel pointer move to the window (X11 XSendEvent,
// Win32 PostMessage; see armorpaint_mcp/desktop_input.py). heartbeat.json says
// "dozing":true so the server knows to do that. linger < 0 = never doze.
// Defaults: 10 s; never on macOS, where the wake path is untested. Persisted in
// <spool>/bridge_settings.json by bridge_set_idle.
float linger        = 10.0;
float last_activity = 0.0;
int   dozing        = 0;
char *path_settings;

// THE CURRENT JOB: one request, or one ordered batch of sub-requests, possibly
// spanning several frames. Requests are strictly sequential: nothing new is
// read from req/ while a job is open.
void *job_map    = NULL;
char *job_id     = "";
int   job_n      = 0;  // items (1 for a plain request)
int   job_i      = 0;  // next item to run
int   job_batch  = 0;  // 1 = batch: keys are b<i>_op / b<i>_a_<name>
int   job_stop   = 0;  // batch: stop at the first failed item
int   job_errs   = 0;
int   job_frames = 0;
int   job_held   = 0;  // a heavy item is waiting for the mouse to be released
char *job_acc    = "";
char *job_inner  = "{}";
int   job_ok     = 1;
float job_t0     = 0.0;

// Argument-key prefix of the item being dispatched ("" or "b<i>_").
char *arg_prefix = "";

// ARENA LITERALS DO NOT OUTLIVE THEIR FRAME. A string literal inside a function
// body is lexed into the context arena when that line runs, and the arena is
// rewound after every on_update -- so a GLOBAL assigned from such a literal
// dangles from the next frame on. Globals that must persist are assigned from
// these (initialised in pass 2, below the watermark) or from heap strings.
char *EMPTY_STR = "";
char *EMPTY_OBJ = "{}";
char *HANDLER_ABORTED = "internal: handler aborted";
char *NO_ERROR = "-";

// capture_viewport's render target, kept between calls.
//
// gpu_create_render_target is bound, but NO destructor is: gpu_delete_texture
// exists in the engine (iron.h:854) and is simply absent from minic's binding
// table, so a plugin can never free a target it allocates. Allocating one per
// request would leak ~4 MB of VRAM per 1024x1024 capture, and a see -> adjust
// -> see loop is the whole point of the op. Reusing one target makes the steady
// state free. Changing the capture SIZE still abandons the previous target, so
// callers should keep the size stable within a session.
void *capture_tex;
int   capture_w = 0;
int   capture_h = 0;

// Pre-formatted panel lines. on_ui runs every frame the Plugins tab is open and
// string() allocates from a heap nothing ever collects, so formatting there
// would leak steadily for as long as the tab is visible. These are rebuilt only
// when the heartbeat ticks (~1 Hz and around each request).
char *ui_stats   = "requests 0 / errors 0";
char *ui_version = "-";

// dispatch() return channel: the arms return the inner JSON object and set r_ok.
int r_ok = 1;

// Set by the quit arm. The app exits at the end of this frame without calling
// on_delete, so handle_one removes the heartbeat itself once the reply is
// committed; otherwise the server sees a frozen heartbeat and blames a modal
// dialog instead of reporting that ArmorPaint is not running.
int quitting = 0;

// Set by material_update, consumed by fill_layer. MEASURED on 1.0/Linux: the
// first script_fill_layer() after script_material_update() leaves the viewport
// showing the PREVIOUS material about half the time (4/8 value edits). Waiting
// does not fix it (3 frames or 250 ms first: still 2/10 stale) and neither does
// a second fill in the SAME frame (4/12), but a second fill on the NEXT frame
// was never stale (0/10). ArmorPaint's own node editor likewise re-runs the fill
// layers after its final recompile (util_nodes.c ui_nodes_recompile_mat_final).
// So fill_layer schedules refill_pending and on_update performs it next frame,
// before any queued request -- a settle ping is therefore answered after it.
int fill_after_update = 0;
int refill_pending    = 0;

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

// JSON-escape a string. json_encode_string() dumps its value verbatim between
// quotes, so an unescaped Windows path or a quote in a name would produce a
// response the server cannot parse. Also flattens control characters, which
// JSON forbids raw. Never returns NULL -- json_encode_string(NULL) would strlen
// a null pointer inside the host.
char *jesc(char *s) {
	ncalls = ncalls + 1;
	if (s == NULL) {
		return "";
	}
	char *r = string_replace_all(s, "\\", "\\\\");
	r       = string_replace_all(r, "\"", "\\\"");
	r       = string_replace_all(r, "\n", " ");
	r       = string_replace_all(r, "\r", " ");
	r       = string_replace_all(r, "\t", " ");
	return r;
}

// Convert a path to the spelling Iron's Windows helpers actually accept.
//
// Iron is inconsistent about separators on Windows: functions that go through
// Win32 directly (iron_file_save_bytes -> _wfopen) accept '/' happily, but two
// of them do not, and BOTH FAIL SILENTLY:
//
//   iron_delete_file  -- shells  del /f "<path>"  to cmd.exe, where '/' is the
//                        switch prefix (iron_file.c:394, iron_system.c:596)
//   iron_file_exists  -- opens via IRON_FILE_TYPE_ASSET (iron_file.c:385)
//
// MEASURED on ArmorPaint 1.0 / Windows 11:
//   iron_delete_file  "E:/x/f"->fail  "E:\\x\\f"->ok   "./x/f"->fail
//   iron_file_exists  "E:/x/f"->0     "E:\\x\\f"->1    "./x/f"->1
//
// Relative paths are fine for exists() but not for delete(), and backslashes
// satisfy both, so on Windows normalising unconditionally is the one safe rule.
// The rest of the plugin keeps '/' because minic string literals treat '\' as an
// escape introducer and an unrecognised escape truncates the literal -- so the
// conversion lives here, at the boundary, and nowhere else.
//
// Windows ONLY. On Linux iron_delete_file shells  rm "<path>"  and a backslash
// is an ordinary filename character there, so converting made every delete
// fail -- and an undeleted request replays forever (see del_file).
char *win_path(char *p) {
	ncalls = ncalls + 1;
	if (p == NULL) {
		return NULL;
	}
	if (!is_windows) {
		return p;
	}
	return string_replace_all(p, "/", "\\");
}

// Delete a file. ALWAYS use this rather than iron_delete_file() -- see win_path().
//
// This is more load-bearing than it looks: an undeleted request file is re-read
// every frame forever, so the bridge answers request #1 on repeat and NEVER SEES
// request #2. Observed before the fix: 1625 replays of the first request while
// four later ones sat unread in req/ and timed out.
void del_file(char *p) {
	ncalls = ncalls + 1;
	if (p == NULL) {
		return;
	}
	iron_delete_file(win_path(p));
}

// Does a file exist? ALWAYS use this rather than iron_file_exists() -- see win_path().
// A false negative here makes an agent believe its own export never happened.
int file_here(char *p) {
	ncalls = ncalls + 1;
	if (p == NULL) {
		return 0;
	}
	return iron_file_exists(win_path(p));
}

// Can this process create a file directly inside directory d? Probes with a
// real write, because no binding reports permissions or ownership.
int writable_dir(char *d) {
	ncalls = ncalls + 1;
	if (!iron_is_directory(d)) {
		return 0;
	}
	char *probe = string("%s/.armorpaint_mcp_probe", d);
	iron_file_save_bytes(probe, sys_string_to_buffer("1"), 0);
	int ok = file_here(probe);
	if (ok) {
		del_file(probe);
	}
	return ok;
}

// POSIX only: this user's home directory, or NULL.
//
// The spool must be an ABSOLUTE path on Linux, because Iron resolves a relative
// one two different ways: file reads (data_get_blob, iron_file_exists) prefix
// the executable's directory (iron_file.c, fileslocation), while writes, mkdir,
// rm and directory listings use the process working directory. A relative
// spool therefore splits in two -- requests land in one directory and are read
// from another, and a system-wide install such as /usr/lib/armorpaint is not
// writable at all. getenv is not bound, so HOME cannot be read; instead, look
// for the one home directory under `root` that this process can write into.
// Other users' homes refuse the probe write. macOS's /Users/Shared is
// world-writable, so it is skipped by name.
char *find_home(char *root) {
	ncalls = ncalls + 1;
	if (!iron_is_directory(root)) {
		return NULL;
	}
	any_array_t *names = file_read_directory(root);
	if (names == NULL) {
		return NULL;
	}
	char *found = NULL;
	char *cand;
	char *nm;
	int   n = names->length;
	for (int hi = 0; hi < n; ++hi) {
		nm = names->buffer[hi];
		if (nm != NULL) {
			if (!string_equals(nm, "Shared")) {
				cand = string("%s/%s", root, nm);
				if (writable_dir(cand)) {
					found = cand;
					break;
				}
			}
		}
	}
	array_free(names);
	free(names);
	return found;
}

// BOOL FIELDS REGISTERED AS INT. ArmorPaint registers several C `bool` struct
// fields with MINIC_I rather than MINIC_B (minic_api.c): the nine
// slot_material_t paint_* channels, config_t brush_live / node_previews /
// material_live, context_t xray and project_t is_bgra. minic then loads and
// stores them as 4-byte int32 (minic.c minic_load/minic_store), so
//   * a READ picks up the three bytes that follow -- mask with `& 255`;
//   * a WRITE clobbers them -- measured: setting paint_base alone turned
//     paint_opac off. Always write  x->f = (x->f & ~255) | v.

// There is no atoi binding anywhere in the table, so integers arrive as text and
// are converted here. Unparseable input yields 0, never an error.
int to_int(char *s) {
	ncalls = ncalls + 1;
	if (s == NULL) {
		return 0;
	}
	int len = string_length(s);
	int i   = 0;
	int neg = 0;
	int c   = 0;
	if (len > 0) {
		c = char_code_at(s, 0);
		if (c == 45) { // '-'
			neg = 1;
			i   = 1;
		}
		else if (c == 43) { // '+'
			i = 1;
		}
	}
	int n = 0;
	while (i < len) {
		c = char_code_at(s, i);
		if (c < 48) {
			break;
		}
		if (c > 57) {
			break;
		}
		n = n * 10 + (c - 48);
		i = i + 1;
	}
	if (neg) {
		n = -n;
	}
	return n;
}

// Same story for floats. Plain decimal only: sign, digits, optional fraction.
// Exponent notation is not accepted -- server.py's _fmt_float never emits it.
float to_float(char *s) {
	ncalls = ncalls + 1;
	if (s == NULL) {
		return 0.0;
	}
	int   len   = string_length(s);
	int   i     = 0;
	int   c     = 0;
	float sign  = 1.0;
	float whole = 0.0;
	float frac  = 0.0;
	float scale = 1.0;
	if (len > 0) {
		c = char_code_at(s, 0);
		if (c == 45) { // '-'
			sign = -1.0;
			i    = 1;
		}
		else if (c == 43) { // '+'
			i = 1;
		}
	}
	while (i < len) {
		c = char_code_at(s, i);
		if (c < 48) {
			break;
		}
		if (c > 57) {
			break;
		}
		whole = whole * 10.0 + (c - 48);
		i     = i + 1;
	}
	if (i < len) {
		if (char_code_at(s, i) == 46) { // '.'
			i = i + 1;
			while (i < len) {
				c = char_code_at(s, i);
				if (c < 48) {
					break;
				}
				if (c > 57) {
					break;
				}
				scale = scale * 10.0;
				frac  = frac * 10.0 + (c - 48);
				i     = i + 1;
			}
		}
	}
	return sign * (whole + frac / scale);
}

// Booleans arrive as the literal text "true"/"false" (server.py encode_value).
// to_int returns 0 for BOTH of those, so they need their own conversion.
int to_bool(char *s) {
	ncalls = ncalls + 1;
	if (s == NULL) {
		return 0;
	}
	if (string_equals(s, "true")) {
		return 1;
	}
	if (string_equals(s, "1")) {
		return 1;
	}
	return 0;
}

// Fetch an argument. Prefers the "a_" prefixed key; falls back to the bare name
// so a server sending PROTOCOL.md's nested "args" object (which json_parse_to_map
// flattens into the top level) still works. NEVER use this for envelope keys.
char *arg(void *m, char *name) {
	ncalls = ncalls + 1;
	if (m == NULL) {
		return NULL;
	}
	if (name == NULL) {
		return NULL;
	}
	char *v = any_map_get(m, string("%sa_%s", arg_prefix, name));
	if (v != NULL) {
		return v;
	}
	if (string_length(arg_prefix) > 0) {
		return NULL; // a batch item never falls back to the bare, envelope-level name
	}
	return any_map_get(m, name);
}

int arg_i(void *m, char *name, int dflt) {
	ncalls = ncalls + 1;
	char *v = arg(m, name);
	if (v == NULL) {
		return dflt;
	}
	if (string_length(v) < 1) {
		return dflt;
	}
	if (string_equals(v, "true")) {
		return 1;
	}
	if (string_equals(v, "false")) {
		return 0;
	}
	return to_int(v);
}

float arg_f(void *m, char *name, float dflt) {
	ncalls = ncalls + 1;
	char *v = arg(m, name);
	if (v == NULL) {
		return dflt;
	}
	if (string_length(v) < 1) {
		return dflt;
	}
	return to_float(v);
}

// Errors are values, never crashes. Codes are PROTOCOL.md's set:
// no_project, bad_args, not_found, unsupported, app_busy, internal.
char *fail(char *code, char *msg) {
	ncalls = ncalls + 1;
	r_ok       = 0;
	err_count  = err_count + 1;
	last_error = string("%s: %s", code, msg);
	json_encode_begin();
	json_encode_string("code", jesc(code));
	json_encode_string("message", jesc(msg));
	return json_encode_end();
}

// An id becomes a filename, so a hostile one ("../../x") would write outside the
// spool. Accept only [0-9A-Za-z._-], reject dot-runs and a leading dot.
int id_ok(char *s) {
	ncalls = ncalls + 1;
	if (s == NULL) {
		return 0;
	}
	int len = string_length(s);
	if (len < 1) {
		return 0;
	}
	if (len > 64) {
		return 0;
	}
	int c    = 0;
	int good = 0;
	int bad  = 0;
	for (int k = 0; k < len; ++k) {
		c    = char_code_at(s, k);
		good = 0;
		if (c >= 48) {
			if (c <= 57) {
				good = 1; // 0-9
			}
		}
		if (c >= 65) {
			if (c <= 90) {
				good = 1; // A-Z
			}
		}
		if (c >= 97) {
			if (c <= 122) {
				good = 1; // a-z
			}
		}
		if (c == 45) {
			good = 1; // '-'
		}
		if (c == 46) {
			good = 1; // '.'
		}
		if (c == 95) {
			good = 1; // '_'
		}
		if (!good) {
			bad = 1;
			break;
		}
	}
	if (bad) {
		return 0;
	}
	if (char_code_at(s, 0) == 46) {
		return 0;
	}
	if (string_index_of(s, "..") >= 0) {
		return 0;
	}
	return 1;
}

// Screen a filesystem path before it reaches a binding.
//
// iron_create_directory() is not a syscall: on every platform it builds a shell
// command line and hands it to system() (iron_file.c:353). A path containing a
// double quote closes the quoting, and everything after it executes. The server
// already refuses quotes (server.py _UNSAFE_STR_RE) but a request is just a
// file -- anything able to write into the spool bypasses the server entirely,
// so the check has to exist on this side too.
//
// On Linux and macOS that shell is sh, which still expands $(...), `...` and
// $VAR inside double quotes, so  /tmp/$(cmd)  would run cmd. Those characters
// are refused on every platform: they are vanishingly rare in real paths.
//
// The length cap is the same call's other hazard: it copies into a 1024-byte
// stack buffer with strcpy/strcat and no bounds check.
int path_ok(char *p) {
	ncalls = ncalls + 1;
	if (p == NULL) {
		return 0;
	}
	int len = string_length(p);
	if (len < 1) {
		return 0;
	}
	if (len > 900) {
		return 0;
	}
	if (string_index_of(p, "\"") >= 0) {
		return 0;
	}
	if (string_index_of(p, "\n") >= 0) {
		return 0;
	}
	if (string_index_of(p, "\r") >= 0) {
		return 0;
	}
	if (string_index_of(p, "$") >= 0) {
		return 0;
	}
	if (string_index_of(p, "`") >= 0) {
		return 0;
	}
	return 1;
}

// Screen a request body before json_parse_to_map() ever sees it.
//
// WHY THIS EXISTS: jsmn_parse returns a NEGATIVE token count for malformed
// input, and load_tokens (iron_json.c:270) feeds that straight into
// malloc(sizeof(jsmntok_t) * count) -- which yields NULL for a negative size --
// and then writes through it. A truncated write, an empty file or any stray
// .json dropped into req/ would therefore take ArmorPaint down mid-paint. The
// host offers no "is this valid" call, so the check has to happen here.
//
// This is a proxy, not a parser. It catches the realistic failure modes and, as
// a bonus, rejects JSON arrays outright: json_parse_to_map skips an array's
// opening token and then consumes its elements as key/value pairs, silently
// corrupting everything after it (iron_json.c:311).
//
// Returns 1 = usable, 0 = malformed, -1 = contains an array.
int json_sane(char *s) {
	ncalls = ncalls + 1;
	if (s == NULL) {
		return 0;
	}
	int len = string_length(s);
	if (len < 2) {
		return 0;
	}
	if (len > 16384) {
		return 0; // room for a 64-item batch; far larger than any single op
	}
	if (char_code_at(s, 0) != 123) { // '{'
		return 0;
	}
	if (char_code_at(s, len - 1) != 125) { // '}'
		return 0;
	}
	int depth = 0;
	int instr = 0;
	int esc   = 0;
	int c     = 0;
	int bad   = 0;
	for (int q = 0; q < len; ++q) {
		c = char_code_at(s, q);
		if (esc) {
			esc = 0;
		}
		else if (instr) {
			if (c == 92) { // backslash
				esc = 1;
			}
			else if (c == 34) { // '"'
				instr = 0;
			}
		}
		else if (c == 34) {
			instr = 1;
		}
		else if (c == 123) {
			depth = depth + 1;
		}
		else if (c == 125) {
			depth = depth - 1;
			if (depth < 0) {
				bad = 1;
				break;
			}
		}
		else if (c == 91) { // '[' outside a string
			bad = 2;
			break;
		}
		else if (c == 93) { // ']' outside a string
			bad = 2;
			break;
		}
	}
	if (bad == 2) {
		return -1;
	}
	if (bad) {
		return 0;
	}
	if (instr) {
		return 0; // unterminated string: the write was almost certainly torn
	}
	if (depth != 0) {
		return 0;
	}
	return 1;
}

// Join up to `max` entries of a buffer/length array into one pipe-delimited,
// escaped string. Names go out this way rather than as a JSON array because
// json_encode_string_array() cannot escape its elements, and one stray quote
// there would make the whole reply unparseable. A stray '|' only costs an extra
// split entry, which degrades gracefully.
//
// The parameter is any_array_t but string_array_t is passed to it too: every
// *_array_t in this codebase is {void *buffer; int length; int capacity;}
// (types.h:781, minic_api.c:331) and minic stamps the declared deref type onto
// the parameter, so the re-typing is exact rather than lucky.
char *sa_names(any_array_t *a, int max) {
	ncalls = ncalls + 1;
	if (a == NULL) {
		return "";
	}
	int n = a->length;
	if (n > max) {
		n = max;
	}
	char *acc = "";
	char *s;
	for (int i = 0; i < n; ++i) {
		s = a->buffer[i];
		if (s != NULL) {
			if (i > 0) {
				acc = string("%s|%s", acc, jesc(s));
			}
			else {
				acc = jesc(s);
			}
		}
	}
	return acc;
}

// Describe a node's sockets (or buttons) as "index:name:type=v0,v1,...;" so an
// agent can address them by index without guessing. ArmorPaint's node catalogue
// uses no ':' ';' or '=' in socket or button names. default_value is 1 float for
// VALUE, 3 for VECTOR, 4 for RGBA; it is what the parser uses when the socket is
// unlinked. ui_node_socket_t and ui_node_button_t lay their fields out
// differently, so each gets its own typed pointer.
char *socket_table(any_array_t *a, int is_button) {
	ncalls = ncalls + 1;
	if (a == NULL) {
		return "";
	}
	char             *acc  = "";
	char             *vals = "";
	char             *nm   = "";
	char             *ty   = "";
	f32_array_t      *dv   = NULL;
	ui_node_socket_t *so;
	ui_node_button_t *bt;
	int               n = a->length;
	if (n > MAX_LIST_ITEMS) {
		n = MAX_LIST_ITEMS;
	}
	for (int k = 0; k < n; ++k) {
		if (is_button) {
			bt = a->buffer[k];
			nm = bt->name;
			ty = bt->type;
			dv = bt->default_value;
		}
		else {
			so = a->buffer[k];
			nm = so->name;
			ty = so->type;
			dv = so->default_value;
		}
		vals = "";
		if (dv != NULL) {
			for (int q = 0; q < dv->length; ++q) {
				if (q > 0) {
					vals = string("%s,", vals);
				}
				vals = string("%s%s", vals, f32_to_string(dv->buffer[q]));
			}
		}
		acc = string("%s%d:%s:%s=%s;", acc, k, jesc(nm), jesc(ty), vals);
	}
	return acc;
}

// Emit an object's readable state into the reply currently being encoded.
// Rotation goes out as the raw quaternion: there is no quat->euler binding, and
// inventing one here would be a lossy guess the caller could not check.
void emit_object(object_t *o) {
	ncalls = ncalls + 1;
	if (o == NULL) {
		return;
	}
	json_encode_string("name", jesc(o->name));
	json_encode_bool("visible", o->visible);
	json_encode_bool("is_empty", o->is_empty);
	transform_t *t = o->transform;
	if (t != NULL) {
		json_encode_f32("loc_x", t->loc.x);
		json_encode_f32("loc_y", t->loc.y);
		json_encode_f32("loc_z", t->loc.z);
		json_encode_f32("rot_x", t->rot.x);
		json_encode_f32("rot_y", t->rot.y);
		json_encode_f32("rot_z", t->rot.z);
		json_encode_f32("rot_w", t->rot.w);
		json_encode_f32("scale_x", t->scale.x);
		json_encode_f32("scale_y", t->scale.y);
		json_encode_f32("scale_z", t->scale.z);
		json_encode_string("rotation_format", "quaternion xyzw");
	}
}

// Walk a "x,y;x,y;..." (or "x,y,z;..." when is_world) point list and paint it.
// The ';' between points and the ',' within one are strokes.py's format; a JSON
// array cannot cross this wire because it corrupts json_parse_to_map.
//
// A point may carry PRESSURE after its coordinates: a radius multiplier and an
// opacity multiplier ("x,y,r" or "x,y,r,o"; world "x,y,z,r,o"). They scale the
// brush radius/opacity the stroke started with, for that dab: upstream renders
// each script_paint call immediately (render_path_paint_commands_paint), so the
// value in the context at the call is the one that paints. With close = 1 the
// stroke is ended and the brush restored; with close = 0 (stroke_points) it stays
// open for the next request.
//
// Returns the number of points painted, or -1 if the list is over
// MAX_STROKE_POINTS points or MAX_STROKE_VALUES numbers. That is detected by a
// counting pass BEFORE anything is painted: silently truncating would leave a
// half-drawn stroke on the user's model, and painting first and erroring
// afterwards is worse than either. The counting pass is char_code_at only --
// host calls, no arena frames.
int do_stroke(char *pts, int is_world, int close) {
	ncalls = ncalls + 1;
	if (pts == NULL) {
		return 0;
	}
	int len = string_length(pts);
	if (len < 1) {
		return 0;
	}
	int i   = 0;
	int c   = 0;
	int cnt = 1;
	int cms = 0;
	while (i < len) {
		c = char_code_at(pts, i);
		if (c == 59) { // ';'
			cnt = cnt + 1;
		}
		if (c == 44) { // ','
			cms = cms + 1;
		}
		i = i + 1;
	}
	if (cnt > MAX_STROKE_POINTS) {
		return -1;
	}
	if (cnt + cms > MAX_STROKE_VALUES) {
		return -1;
	}

	context_t *sc = script_get_context();
	float      r0 = sc->brush_radius;
	float      o0 = sc->brush_opacity;
	if (stroke_open) {
		r0 = stroke_r0;
		o0 = stroke_o0;
	}
	int   need = 2 + is_world;
	int   pos  = 0;
	int   n    = 0;
	int   sp   = 0;
	int   q    = 0;
	int   e    = 0;
	int   nv   = 0;
	int   tlen = 0;
	float v    = 0.0;
	float f0   = 0.0;
	float f1   = 0.0;
	float f2   = 0.0;
	float f3   = 0.0;
	float f4   = 0.0;
	char *tok;
	while (pos < len) {
		sp = string_index_of_pos(pts, ";", pos);
		if (sp < 0) {
			sp = len;
		}
		if (sp > pos) {
			tok  = substring(pts, pos, sp);
			tlen = string_length(tok);
			nv   = 0;
			q    = 0;
			while (q < tlen) {
				e = string_index_of_pos(tok, ",", q);
				if (e < 0) {
					e = tlen;
				}
				v = to_float(substring(tok, q, e));
				if (nv == 0) {
					f0 = v;
				}
				if (nv == 1) {
					f1 = v;
				}
				if (nv == 2) {
					f2 = v;
				}
				if (nv == 3) {
					f3 = v;
				}
				if (nv == 4) {
					f4 = v;
				}
				nv = nv + 1;
				q  = e + 1;
			}
			if (nv >= need) {
				// Pressure, when present: the numbers after the coordinates.
				if (is_world) {
					if (nv > 3) {
						sc->brush_radius = r0 * f3;
					}
					if (nv > 4) {
						sc->brush_opacity = o0 * f4;
					}
					script_paint_world(f0, f1, f2);
				}
				else {
					if (nv > 2) {
						sc->brush_radius = r0 * f2;
					}
					if (nv > 3) {
						sc->brush_opacity = o0 * f3;
					}
					script_paint(f0, f1);
				}
				n = n + 1;
			}
		}
		pos = sp + 1;
	}
	if (close) {
		if (n > 0) {
			script_paint_end(); // a stroke that is never ended stays open in the tool
		}
		sc->brush_radius  = r0;
		sc->brush_opacity = o0;
	}
	return n;
}

// Close the open streamed stroke, if any: end it (dilate + commit, as a normal
// stroke's release does) and put the brush radius/opacity back. Returns 1 if a
// stroke was open. The 32nd and last function minic allows (minic.c:2099); any
// further logic must be a dispatch() arm.
int end_stroke() {
	ncalls = ncalls + 1;
	if (!stroke_open) {
		return 0;
	}
	script_paint_end();
	context_t *ec = script_get_context();
	if (ec != NULL) {
		ec->brush_radius  = stroke_r0;
		ec->brush_opacity = stroke_o0;
	}
	stroke_open = 0;
	return 1;
}

// Rewritten roughly once per second. `t` is sys_time(): seconds since app start,
// monotonic WITHIN A RUN and unrelated to the wall clock -- liveness is judged
// by t advancing between two reads, never by comparing it to time.time().
//
// There is no rename binding, so this single file is written truncating and a
// reader can catch it torn. That is unavoidable and harmless: a torn heartbeat
// fails to parse and the server simply reads again a moment later.
void write_heartbeat() {
	ncalls = ncalls + 1;
	project_t *pr   = script_get_project();
	char      *pver = "";
	if (pr != NULL) {
		if (pr->version != NULL) {
			pver = pr->version;
		}
	}
	json_encode_begin();
	json_encode_i32("v", ENVELOPE_V);
	json_encode_string("pid_hint", "armorpaint");
	json_encode_string("bridge_version", BRIDGE_VERSION);
	// The app's own version string is not bound. project_t.version is the
	// nearest reachable thing: the .arm format version stamped by this build.
	json_encode_string("app_version", jesc(pver));
	json_encode_string("app_title", jesc(sys_title()));
	json_encode_f32("t", sys_time());
	json_encode_string("project", jesc(project_filepath_get()));
	json_encode_string("spool", jesc(spool_root));
	json_encode_bool("busy", busy);
	json_encode_bool("enabled", enabled);
	json_encode_i32("requests", req_count);
	json_encode_i32("errors", err_count);
	json_encode_f32("poll_interval", poll_interval);
	json_encode_bool("dozing", dozing);
	json_encode_f32("linger", linger);
	json_encode_bool("job_open", job_map != NULL);
	json_encode_bool("job_held", job_held);
	json_encode_bool("stroke_open", stroke_open);
	json_encode_i32("ext", ext_state);
	char *body = json_encode_end();
	iron_file_save_bytes(path_heartbeat, sys_string_to_buffer(body), 0);

	// Refresh the panel's counter line here rather than in on_ui, which draws at
	// frame rate. This is the natural throttle point: ~1 Hz plus once either side
	// of every request.
	ui_stats = string("requests %d / errors %d", req_count, err_count);
}

// TWO-FILE COMMIT (PROTOCOL.md). A plugin cannot write atomically -- there is no
// rename or move binding in the whole binding table and iron_file_save_bytes is
// a truncating fopen("wb"). So: write the body, let that call RETURN (the file
// is closed), and only then write a tiny marker holding the body's byte length.
// The server ignores any res/<id>.json without a matching .done, so a visible
// marker proves a complete body; the length lets it detect a torn one. Writing
// the marker first, or in the same call, defeats the entire scheme.
void reply(char *id, int okflag, char *inner, int ms) {
	ncalls = ncalls + 1;
	char *body;
	if (okflag) {
		body = string("{\"v\":%d,\"id\":\"%s\",\"ok\":true,\"result\":%s,\"elapsed_ms\":%d}", ENVELOPE_V, jesc(id), inner, ms);
	}
	else {
		body = string("{\"v\":%d,\"id\":\"%s\",\"ok\":false,\"error\":%s,\"elapsed_ms\":%d}", ENVELOPE_V, jesc(id), inner, ms);
	}
	iron_file_save_bytes(string("%s/%s.json", dir_res, id), sys_string_to_buffer(body), 0);
	// Marker LAST, in its own call. string_length() is a byte count (strlen),
	// sys_string_to_buffer() writes exactly that many bytes (iron_system.c:1189)
	// and iron_file_save_bytes with length 0 writes bytes->length
	// (iron_file.c:413), so the number the server compares against the file size
	// is exact.
	iron_file_save_bytes(string("%s/%s.done", dir_res, id), sys_string_to_buffer(i32_to_string(string_length(body))), 0);
}

// ---------------------------------------------------------------------------
// Optional bindings -- see ext_state. Each wrapper is ONE call so that a missing
// binding aborts only the wrapper, which then returns NULL / 0 to its caller.
// ---------------------------------------------------------------------------
char *try_ext(char *op, void *m, char *prefix) {
	ncalls = ncalls + 1;
	return mcp_ext_call(op, m, prefix);
}

int try_save_png(void *tex, char *path) {
	ncalls = ncalls + 1;
	viewport_save_texture_to_file(tex, path);
	return 1;
}

// How an op is scheduled (step_job): 1 = may share a frame with other batch
// items; 2 = starts a frame of its own (GPU work, or up to ~150 script calls);
// 3 = heavy -- its own frame AND only once no mouse button is held, because it
// stalls the render thread for as long as it runs; -1 = refused because the
// bridge is disabled. Unknown op names cost 1: dispatch() rejects them cheaply.
int op_cost(char *op) {
	ncalls = ncalls + 1;
	if (op == NULL) {
		return 1;
	}
	if (!enabled) {
		if (string_equals(op, "bridge_set_enabled")) {
			return 1;
		}
		if (string_equals(op, "ping")) {
			return 1;
		}
		return -1;
	}
	if (starts_with(op, "export_")) {
		return 3;
	}
	if (starts_with(op, "project_")) {
		if (string_equals(op, "project_get_info")) {
			return 1;
		}
		if (starts_with(op, "project_list")) {
			return 1;
		}
		return 3; // new, open, save, save_as
	}
	if (starts_with(op, "import_")) {
		return 3;
	}
	if (string_equals(op, "append_mesh")) {
		return 3;
	}
	if (string_equals(op, "bake")) {
		return 3;
	}
	if (string_equals(op, "texture_resolution")) {
		return 3;
	}
	if (string_equals(op, "quit")) {
		return 3;
	}
	if (starts_with(op, "paint_stroke")) {
		return 2;
	}
	if (starts_with(op, "stroke_")) {
		return 2;
	}
	if (starts_with(op, "capture_")) {
		return 2;
	}
	if (starts_with(op, "layer_")) {
		if (string_equals(op, "layer_list")) {
			return 1;
		}
		return 2;
	}
	if (string_equals(op, "fill_layer")) {
		return 2;
	}
	if (string_equals(op, "material_update")) {
		return 2;
	}
	if (string_equals(op, "undo")) {
		return 2;
	}
	if (string_equals(op, "redo")) {
		return 2;
	}
	if (string_equals(op, "shape_add")) {
		return 2;
	}
	if (string_equals(op, "object_duplicate")) {
		return 2;
	}
	return 1;
}

// ---------------------------------------------------------------------------
// THE DISPATCHER — one function, one else-if arm per operation.
//
// minic silently DROPS the 33rd function in a script (minic.c:2054, no else
// branch, no diagnostic), so operations must never become functions.
//
// Every arm validates its arguments before touching a binding: minic has no
// exceptions and dereferences pointers unchecked, so one bad request would
// otherwise take down the user's session mid-paint.
//
// Op names and argument names are server.py's. See the header comment.
// ---------------------------------------------------------------------------
char *dispatch(void *m, char *op) {
	ncalls = ncalls + 1;
	// `op` is the envelope key (or a batch item's b<i>_op), read by step_job
	// DIRECTLY -- never through arg(). See the header comment.
	if (op == NULL) {
		return fail("bad_args", "missing 'op'");
	}
	last_op = op;

	context_t        *c  = script_get_context();
	project_t        *pr = script_get_project();
	config_t         *cf = script_get_config();
	slot_material_t  *mt;
	ui_node_canvas_t *cv;
	ui_node_t        *nd;
	ui_node_t        *nd2;
	object_t         *ob;
	mesh_object_t    *mo;
	any_array_t      *ar;
	char             *s1;
	char             *s2;
	char             *acc;
	int               i1;
	int               i2;
	int               i3;
	int               i4;
	int               n;

	// An open streamed stroke is closed before any other op touches the app: the
	// op might change the layer, tool or brush under it. Pings and context reads
	// leave it open, so a capture can settle between two stroke_points.
	if (stroke_open) {
		if (!starts_with(op, "stroke_")) {
			if (!string_equals(op, "ping")) {
				if (!string_equals(op, "get_context")) {
					end_stroke();
				}
			}
		}
	}

	// ---- bridge & session -------------------------------------------------
	if (string_equals(op, "ping")) {
		json_encode_begin();
		json_encode_string("bridge_version", BRIDGE_VERSION);
		json_encode_i32("envelope_v", ENVELOPE_V);
		json_encode_f32("t", sys_time());
		json_encode_string("app_title", jesc(sys_title()));
		json_encode_string("project", jesc(project_filepath_get()));
		if (pr == NULL) {
			json_encode_null("app_version");
		}
		else {
			json_encode_string("app_version", jesc(pr->version));
		}
		json_encode_bool("busy", busy);
		json_encode_bool("enabled", enabled);
		json_encode_i32("requests", req_count);
		json_encode_i32("errors", err_count);
		json_encode_bool("ext", ext_state == 1);
		return json_encode_end();
	}
	else if (string_equals(op, "get_app_info")) {
		json_encode_begin();
		json_encode_string("app_title", jesc(sys_title()));
		json_encode_i32("window_w", sys_w());
		json_encode_i32("window_h", sys_h());
		json_encode_i32("window_x", sys_x());
		json_encode_i32("window_y", sys_y());
		json_encode_string("data_path", jesc(data_path()));
		json_encode_string("spool", jesc(spool_root));
		json_encode_string("bridge_version", BRIDGE_VERSION);
		json_encode_i32("envelope_v", ENVELOPE_V);
		json_encode_f32("t", sys_time());
		json_encode_f32("poll_interval", poll_interval);
		json_encode_i32("max_stroke_points", MAX_STROKE_POINTS);
		json_encode_i32("max_list_items", MAX_LIST_ITEMS);
		// -1 = not probed yet, 0 = absent, 1 = present (see ext_state).
		json_encode_i32("viewport_file_binding", png_state);
		json_encode_i32("ext_state", ext_state);
		json_encode_i32("ext_version", ext_version);
		json_encode_string("ext_ops", ext_ops);
		json_encode_bool("dozing", dozing);
		json_encode_f32("linger", linger);
		json_encode_i32("call_budget", CALL_BUDGET);
		if (is_windows) {
			json_encode_string("os", "windows");
		}
		else if (is_macos) {
			json_encode_string("os", "macos");
		}
		else {
			json_encode_string("os", "linux");
		}
		json_encode_i32("requests", req_count);
		json_encode_i32("errors", err_count);
		json_encode_string("last_op", jesc(last_op));
		json_encode_string("last_error", jesc(last_error));
		if (pr == NULL) {
			json_encode_null("format_version");
		}
		else {
			json_encode_string("format_version", jesc(pr->version));
		}
		return json_encode_end();
	}
	else if (string_equals(op, "bridge_set_enabled")) {
		s1 = arg(m, "enabled");
		if (s1 == NULL) {
			return fail("bad_args", "missing 'enabled'");
		}
		i1      = to_bool(s1);
		enabled = i1;
		if (h_enabled != NULL) {
			h_enabled->b = i1; // keep the Plugins-tab checkbox in sync
		}
		json_encode_begin();
		json_encode_bool("enabled", enabled);
		// Disabled = never hold the app awake and refuse every op except
		// bridge_set_enabled and ping. The bridge still reads req/ on whatever
		// frames run, and the server wakes a sleeping app before writing a
		// request, so re-enabling from the server works.
		json_encode_bool("reenable_needs_ui", 0);
		return json_encode_end();
	}
	else if (string_equals(op, "bridge_set_idle")) {
		s1 = arg(m, "linger");
		if (s1 == NULL) {
			return fail("bad_args", "missing 'linger' (seconds to stay awake after a request; -1 = never doze)");
		}
		linger = to_float(s1);
		if (linger < 0.0) {
			linger = -1.0;
		}
		if (linger > 3600.0) {
			linger = 3600.0;
		}
		// Persist, so the choice survives a restart. Flat, compact, all-string:
		// the same shape json_parse_to_map reads back in main().
		iron_file_save_bytes(path_settings, sys_string_to_buffer(string("{\"linger\":\"%s\"}", f32_to_string(linger))), 0);
		json_encode_begin();
		json_encode_f32("linger", linger);
		json_encode_bool("never_doze", linger < 0.0);
		return json_encode_end();
	}
	else if (string_equals(op, "bridge_set_poll_ms")) {
		i1 = arg_i(m, "value", -1);
		if (i1 < 5) {
			return fail("bad_args", "value must be 5..1000 ms");
		}
		if (i1 > 1000) {
			return fail("bad_args", "value must be 5..1000 ms");
		}
		poll_interval = i1 / 1000.0;
		json_encode_begin();
		json_encode_f32("poll_interval", poll_interval);
		return json_encode_end();
	}
	else if (string_equals(op, "console_write")) {
		s1 = arg(m, "text");
		if (s1 == NULL) {
			return fail("bad_args", "missing 'text'");
		}
		s2 = arg(m, "level");
		if (s2 == NULL) {
			s2 = "log";
		}
		if (string_equals(s2, "error")) {
			console_error(s1);
		}
		else if (string_equals(s2, "info")) {
			console_info(s1);
		}
		else {
			console_log(s1);
		}
		json_encode_begin();
		json_encode_string("level", jesc(s2));
		return json_encode_end();
	}
	else if (string_equals(op, "show_message")) {
		s1 = arg(m, "text");
		if (s1 == NULL) {
			return fail("bad_args", "missing 'text'");
		}
		i1 = arg_i(m, "modal", 0);
		if (i1) {
			s2 = arg(m, "title");
			if (s2 == NULL) {
				s2 = "ArmorPaint MCP";
			}
			// A modal blocks ArmorPaint's UI thread, which also stalls this
			// bridge until the user dismisses it. That is the documented
			// behaviour of the tool, not a bug.
			ui_box_show_message(s2, s1, 0);
		}
		else {
			script_show_message(s1, arg_f(m, "seconds", 4.0));
		}
		json_encode_begin();
		json_encode_bool("modal", i1);
		return json_encode_end();
	}
	else if (string_equals(op, "quit")) {
		// iron_stop() only clears the run-loop flag (iron_system.c:209), so this
		// frame finishes normally and handle_one still commits the reply below.
		script_quit();
		quitting = 1;
		json_encode_begin();
		json_encode_bool("quitting", 1);
		return json_encode_end();
	}

	// ---- project ----------------------------------------------------------
	else if (string_equals(op, "project_new")) {
		script_project_new();
		return "{}";
	}
	else if (string_equals(op, "project_open")) {
		s1 = arg(m, "path");
		if (s1 == NULL) {
			return fail("bad_args", "missing 'path'");
		}
		if (!path_ok(s1)) {
			return fail("bad_args", "'path' is empty, over-long, or contains a quote, newline, $ or backtick");
		}
		if (!file_here(s1)) {
			return fail("not_found", string("no such file: %s", s1));
		}
		script_project_open(s1);
		json_encode_begin();
		json_encode_string("filepath", jesc(project_filepath_get()));
		return json_encode_end();
	}
	else if (string_equals(op, "project_save")) {
		s1 = project_filepath_get();
		if (s1 == NULL) {
			return fail("no_project", "project has never been saved; use project_save_as");
		}
		if (string_equals(s1, "")) {
			return fail("no_project", "project has never been saved; use project_save_as");
		}
		project_save(0);
		json_encode_begin();
		json_encode_string("filepath", jesc(s1));
		json_encode_bool("deferred", 1);
		json_encode_string("note", "project_save only QUEUES the write (sys_notify_on_next_frame, project.c:39-61); the file appears one frame later. This reply is not proof the save landed -- confirm with fs_stat.");
		return json_encode_end();
	}
	else if (string_equals(op, "project_save_as")) {
		s1 = arg(m, "path");
		if (s1 == NULL) {
			return fail("bad_args", "missing 'path'");
		}
		if (!path_ok(s1)) {
			return fail("bad_args", "'path' is empty, over-long, or contains a quote, newline, $ or backtick");
		}
		// "Save as" is filepath_set + save; there is no dedicated binding.
		project_filepath_set(s1);
		// project_filepath_set() is the ONLY thing standing between us and a modal
		// file picker: project_save() with an empty filepath calls project_save_as(),
		// which opens iron_save_dialog and blocks the UI thread -- and with it the
		// bridge -- until a human clicks something. Never call project_save() here
		// without confirming the path took.
		s2 = project_filepath_get();
		if (s2 == NULL) {
			return fail("internal", "project_filepath_set did not take; refusing to call project_save because an empty filepath opens a modal file dialog that would block the bridge");
		}
		if (string_equals(s2, "")) {
			return fail("internal", "project_filepath_set did not take; refusing to call project_save because an empty filepath opens a modal file dialog that would block the bridge");
		}
		project_save(0);
		json_encode_begin();
		json_encode_string("filepath", jesc(s2));
		// NO "exists" field here. project_save() is deferred by a frame, so a
		// same-frame file_here() is false for every first save and true only when
		// something was ALREADY at that path -- i.e. it reports the state BEFORE
		// this save. Measured: first save_as -> false, one round trip later -> true,
		// second save_as to the same path -> true. An agent reading that field would
		// conclude its own save had failed.
		json_encode_bool("deferred", 1);
		json_encode_string("note", "the write is queued for the next frame; confirm with fs_stat rather than trusting this reply");
		return json_encode_end();
	}
	else if (string_equals(op, "project_get_info")) {
		json_encode_begin();
		json_encode_string("filepath", jesc(project_filepath_get()));
		json_encode_string("basepath", jesc(project_basepath_get()));
		if (pr == NULL) {
			json_encode_null("format_version");
		}
		else {
			json_encode_string("format_version", jesc(pr->version));
			json_encode_i32("is_bgra", (pr->is_bgra & 255) != 0);
			json_encode_string("envmap", jesc(pr->envmap));
			json_encode_f32("envmap_strength", pr->envmap_strength);
			json_encode_f32("envmap_angle", pr->envmap_angle);
			json_encode_f32("camera_fov", pr->camera_fov);
			if (pr->assets == NULL) {
				json_encode_i32("asset_count", 0);
			}
			else {
				json_encode_i32("asset_count", pr->assets->length);
			}
		}
		if (c != NULL) {
			if (c->material != NULL) {
				if (c->material->canvas != NULL) {
					json_encode_string("active_material", jesc(c->material->canvas->name));
				}
			}
		}
		mo = context_main_object();
		if (mo != NULL) {
			if (mo->base != NULL) {
				json_encode_string("main_object", jesc(mo->base->name));
			}
		}
		return json_encode_end();
	}
	else if (string_equals(op, "project_list_texture_assets")) {
		if (pr == NULL) {
			return fail("no_project", "no project runtime");
		}
		// project_t.assets is the SERIALISED texture_assets list, filled at
		// save/load. The live list is g_project->_->assets, and project_t's `_`
		// is not a registered field. Measured: new project -> import -> this
		// list stays NULL until the first save, then shows the asset.
		json_encode_begin();
		json_encode_bool("live", 0);
		json_encode_string("caveat", "project_t.assets is written only at save/load: empty before the first save and blind to imports made since the last save");
		if (pr->assets == NULL) {
			json_encode_i32("count", 0);
			json_encode_i32("returned", 0);
			json_encode_string("names", "");
		}
		else {
			n = pr->assets->length;
			json_encode_i32("count", n);
			if (n > MAX_LIST_ITEMS) {
				n = MAX_LIST_ITEMS;
			}
			json_encode_i32("returned", n);
			json_encode_string("names", sa_names(pr->assets, MAX_LIST_ITEMS));
		}
		json_encode_string("delimiter", "|");
		return json_encode_end();
	}
	else if (string_equals(op, "project_list_scripts")) {
		if (pr == NULL) {
			return fail("no_project", "no project runtime");
		}
		json_encode_begin();
		json_encode_bool("live", 0);
		json_encode_string("caveat", "project_t.script_datas is written only at save/load: empty in a fresh project and stale after this session's changes");
		if (pr->script_datas == NULL) {
			json_encode_i32("count", 0);
			json_encode_i32("returned", 0);
			json_encode_string("names", "");
		}
		else {
			n = pr->script_datas->length;
			json_encode_i32("count", n);
			if (n > MAX_LIST_ITEMS) {
				n = MAX_LIST_ITEMS;
			}
			json_encode_i32("returned", n);
			json_encode_string("names", sa_names(pr->script_datas, MAX_LIST_ITEMS));
		}
		json_encode_string("delimiter", "|");
		return json_encode_end();
	}

	// ---- import / export --------------------------------------------------
	else if (string_equals(op, "import_asset")) {
		s1 = arg(m, "path");
		if (s1 == NULL) {
			return fail("bad_args", "missing 'path'");
		}
		if (!path_ok(s1)) {
			return fail("bad_args", "'path' is empty, over-long, or contains a quote, newline, $ or backtick");
		}
		if (!file_here(s1)) {
			return fail("not_found", string("no such file: %s", s1));
		}
		// Dispatched by extension inside ArmorPaint: texture, mesh, or .arm material.
		script_import_asset(s1, 0);
		// No asset count here: pr->assets is a save/load snapshot and would
		// report the import as missing (see project_list_texture_assets).
		json_encode_begin();
		json_encode_string("path", jesc(s1));
		return json_encode_end();
	}
	else if (string_equals(op, "import_envmap")) {
		s1 = arg(m, "path");
		if (s1 == NULL) {
			return fail("bad_args", "missing 'path'");
		}
		if (!path_ok(s1)) {
			return fail("bad_args", "'path' is empty, over-long, or contains a quote, newline, $ or backtick");
		}
		if (!file_here(s1)) {
			return fail("not_found", string("no such file: %s", s1));
		}
		script_import_asset(s1, 1); // hdr_as_envmap
		json_encode_begin();
		json_encode_string("path", jesc(s1));
		if (pr != NULL) {
			json_encode_string("envmap", jesc(pr->envmap));
			json_encode_f32("envmap_strength", pr->envmap_strength);
		}
		return json_encode_end();
	}
	else if (string_equals(op, "set_envmap_params")) {
		if (pr == NULL) {
			return fail("no_project", "no project runtime");
		}
		s1 = arg(m, "strength");
		if (s1 != NULL) {
			pr->envmap_strength = to_float(s1);
		}
		s2 = arg(m, "angle");
		if (s2 != NULL) {
			pr->envmap_angle = to_float(s2);
		}
		if (c != NULL) {
			c->ddirty = 2; // the viewport will not repaint on its own
		}
		json_encode_begin();
		json_encode_f32("envmap_strength", pr->envmap_strength);
		json_encode_f32("envmap_angle", pr->envmap_angle);
		return json_encode_end();
	}
	else if (string_equals(op, "export_textures")) {
		s1 = arg(m, "directory");
		if (s1 == NULL) {
			return fail("bad_args", "missing 'directory' (export_texture_run takes a DIRECTORY, not a filename)");
		}
		if (!path_ok(s1)) {
			return fail("bad_args", "'directory' is empty, over-long, or contains a quote, newline, $ or backtick");
		}
		iron_create_directory(s1);
		if (!iron_is_directory(s1)) {
			return fail("bad_args", string("not a directory and could not be created: %s", s1));
		}
		i2 = 0;
		ar = file_read_directory(s1);
		if (ar != NULL) {
			i2 = ar->length;
			array_free(ar); // frees the backing buffer; the name strings are separate
			free(ar);
		}
		// The only binding in the table that writes real image files to disk.
		// Filenames come from the last export-dialog name (else "untitled") plus
		// the active preset's per-channel suffixes; format and bit depth live on
		// unregistered fields and are NOT settable from a plugin (8-bit PNG).
		export_texture_run(s1, 0);
		i3 = 0;
		ar = file_read_directory(s1);
		if (ar != NULL) {
			i3 = ar->length;
			array_free(ar); // frees the backing buffer; the name strings are separate
			free(ar);
		}
		json_encode_begin();
		json_encode_string("directory", jesc(s1));
		json_encode_i32("bake_material", 0);
		json_encode_i32("files_before", i2);
		json_encode_i32("files_after", i3);
		json_encode_i32("files_added", i3 - i2);
		ar = file_read_directory(s1);
		if (ar != NULL) {
			json_encode_string("files", sa_names(ar, MAX_LIST_ITEMS));
			json_encode_string("delimiter", "|");
			array_free(ar);
			free(ar);
		}
		json_encode_string("note", "names derive from the last export-dialog name or 'untitled'; the format is not settable from a plugin");
		return json_encode_end();
	}
	else if (string_equals(op, "export_material_bake")) {
		s1 = arg(m, "directory");
		if (s1 == NULL) {
			return fail("bad_args", "missing 'directory'");
		}
		if (!path_ok(s1)) {
			return fail("bad_args", "'directory' is empty, over-long, or contains a quote, newline, $ or backtick");
		}
		iron_create_directory(s1);
		if (!iron_is_directory(s1)) {
			return fail("bad_args", string("not a directory and could not be created: %s", s1));
		}
		i2 = 0;
		ar = file_read_directory(s1);
		if (ar != NULL) {
			i2 = ar->length;
			array_free(ar); // frees the backing buffer; the name strings are separate
			free(ar);
		}
		export_texture_run(s1, 1); // bake_material
		i3 = 0;
		ar = file_read_directory(s1);
		if (ar != NULL) {
			i3 = ar->length;
			array_free(ar); // frees the backing buffer; the name strings are separate
			free(ar);
		}
		json_encode_begin();
		json_encode_string("directory", jesc(s1));
		json_encode_i32("bake_material", 1);
		json_encode_i32("files_before", i2);
		json_encode_i32("files_after", i3);
		json_encode_i32("files_added", i3 - i2);
		ar = file_read_directory(s1);
		if (ar != NULL) {
			json_encode_string("files", sa_names(ar, MAX_LIST_ITEMS));
			json_encode_string("delimiter", "|");
			array_free(ar);
			free(ar);
		}
		return json_encode_end();
	}
	else if (string_equals(op, "export_mesh")) {
		s1 = arg(m, "path");
		if (s1 == NULL) {
			return fail("bad_args", "missing 'path' (the extension is added: <path>.obj)");
		}
		if (!path_ok(s1)) {
			return fail("bad_args", "'path' is empty, over-long, or contains a quote, newline, $ or backtick");
		}
		script_export_mesh(s1);
		s2 = string("%s.obj", s1);
		json_encode_begin();
		json_encode_string("path", jesc(s2));
		json_encode_bool("exists", file_here(s2));
		return json_encode_end();
	}
	else if (string_equals(op, "export_material")) {
		s1 = arg(m, "path");
		if (s1 == NULL) {
			return fail("bad_args", "missing 'path' (should end in .arm)");
		}
		if (!path_ok(s1)) {
			return fail("bad_args", "'path' is empty, over-long, or contains a quote, newline, $ or backtick");
		}
		script_export_material(s1);
		json_encode_begin();
		json_encode_string("path", jesc(s1));
		json_encode_bool("exists", file_here(s1));
		return json_encode_end();
	}

	// ---- filesystem (as the ArmorPaint process sees it) --------------------
	else if (string_equals(op, "fs_list")) {
		s1 = arg(m, "path");
		if (s1 == NULL) {
			return fail("bad_args", "missing 'path'");
		}
		if (!path_ok(s1)) {
			return fail("bad_args", "'path' is empty, over-long, or contains a quote, newline, $ or backtick");
		}
		if (!iron_is_directory(s1)) {
			return fail("not_found", string("not a directory: %s", s1));
		}
		ar = file_read_directory(s1);
		if (ar == NULL) {
			return fail("internal", string("could not list %s", s1));
		}
		n   = ar->length;
		acc = sa_names(ar, MAX_LIST_ITEMS);
		// The entry strings come from string_split (iron_string.c:173), which
		// allocates each one separately, so they outlive the array header.
		array_free(ar);
		free(ar);
		json_encode_begin();
		json_encode_string("path", jesc(s1));
		json_encode_i32("count", n);
		if (n > MAX_LIST_ITEMS) {
			n = MAX_LIST_ITEMS;
		}
		json_encode_i32("returned", n);
		json_encode_string("names", acc);
		json_encode_string("delimiter", "|");
		return json_encode_end();
	}
	else if (string_equals(op, "fs_stat")) {
		s1 = arg(m, "path");
		if (s1 == NULL) {
			return fail("bad_args", "missing 'path'");
		}
		if (!path_ok(s1)) {
			return fail("bad_args", "'path' is empty, over-long, or contains a quote, newline, $ or backtick");
		}
		i1 = 0;
		if (starts_with(s1, "/")) {
			i1 = 1;
		}
		if (string_length(s1) > 1) {
			if (char_code_at(s1, 1) == 58) { // "C:" style
				i1 = 1;
			}
		}
		json_encode_begin();
		json_encode_string("path", jesc(s1));
		json_encode_bool("exists", file_here(s1));
		json_encode_bool("is_directory", iron_is_directory(s1));
		json_encode_bool("is_absolute", i1);
		return json_encode_end();
	}
	else if (string_equals(op, "fs_mkdir")) {
		s1 = arg(m, "path");
		if (s1 == NULL) {
			return fail("bad_args", "missing 'path'");
		}
		if (!path_ok(s1)) {
			return fail("bad_args", "'path' is empty, over-long, or contains a quote, newline, $ or backtick");
		}
		iron_create_directory(s1);
		json_encode_begin();
		json_encode_string("path", jesc(s1));
		json_encode_bool("is_directory", iron_is_directory(s1));
		if (is_windows) {
			json_encode_string("note", "intermediate directories are NOT created; make each parent first");
		}
		else {
			json_encode_string("note", "mkdir -p: intermediate directories are created too");
		}
		return json_encode_end();
	}

	// ---- introspection ----------------------------------------------------
	else if (string_equals(op, "get_context")) {
		if (c == NULL) {
			return fail("internal", "no context");
		}
		json_encode_begin();
		json_encode_i32("tool", c->tool);
		json_encode_i32("viewport_mode", c->viewport_mode);
		json_encode_i32("xray", (c->xray & 255) != 0);
		json_encode_i32("brush_blending", c->brush_blending);
		json_encode_f32("brush_radius", c->brush_radius);
		json_encode_f32("brush_opacity", c->brush_opacity);
		json_encode_f32("brush_hardness", c->brush_hardness);
		json_encode_f32("brush_scale", c->brush_scale);
		json_encode_f32("brush_angle", c->brush_angle);
		json_encode_i32("ddirty", c->ddirty);
		json_encode_i32("pdirty", c->pdirty);
		// context_t.layer and .brush are untyped void* -- slot_layer_t is not a
		// registered struct, so only their non-nullness is knowable.
		json_encode_bool("has_layer", c->layer != NULL);
		json_encode_bool("has_brush", c->brush != NULL);
		if (c->material != NULL) {
			if (c->material->canvas != NULL) {
				json_encode_string("material", jesc(c->material->canvas->name));
			}
		}
		if (c->paint_object != NULL) {
			if (c->paint_object->base != NULL) {
				json_encode_string("paint_object", jesc(c->paint_object->base->name));
			}
		}
		return json_encode_end();
	}
	else if (string_equals(op, "get_config")) {
		if (cf == NULL) {
			return fail("internal", "no config");
		}
		json_encode_begin();
		json_encode_i32("window_w", cf->window_w);
		json_encode_i32("window_h", cf->window_h);
		json_encode_f32("window_scale", cf->window_scale);
		json_encode_f32("rp_supersample", cf->rp_supersample);
		json_encode_i32("layer_res", cf->layer_res);
		json_encode_i32("undo_steps", cf->undo_steps);
		json_encode_f32("camera_fov", cf->camera_fov);
		json_encode_i32("brush_live", (cf->brush_live & 255) != 0);
		json_encode_i32("node_previews", (cf->node_previews & 255) != 0);
		json_encode_i32("material_live", (cf->material_live & 255) != 0);
		json_encode_i32("workspace", cf->workspace);
		json_encode_i32("workflow", cf->workflow);
		json_encode_string("keymap", jesc(cf->keymap));
		json_encode_string("theme", jesc(cf->theme));
		json_encode_string("recent_projects", sa_names(cf->recent_projects, MAX_LIST_ITEMS));
		json_encode_string("plugins", sa_names(cf->plugins, MAX_LIST_ITEMS));
		json_encode_string("delimiter", "|");
		return json_encode_end();
	}
	else if (string_equals(op, "set_config")) {
		if (cf == NULL) {
			return fail("internal", "no config");
		}
		// Presence, not a sentinel: workspace, workflow and the three live-*
		// toggles are all legitimately 0, so "absent" has to mean "the key is
		// not in the map" rather than "the value is some magic number".
		s1 = arg(m, "window_w");
		if (s1 != NULL) {
			cf->window_w = to_int(s1);
		}
		s1 = arg(m, "window_h");
		if (s1 != NULL) {
			cf->window_h = to_int(s1);
		}
		s1 = arg(m, "undo_steps");
		if (s1 != NULL) {
			cf->undo_steps = to_int(s1);
		}
		s1 = arg(m, "layer_res");
		if (s1 != NULL) {
			cf->layer_res = to_int(s1);
		}
		s1 = arg(m, "workspace");
		if (s1 != NULL) {
			cf->workspace = to_int(s1);
		}
		s1 = arg(m, "workflow");
		if (s1 != NULL) {
			cf->workflow = to_int(s1);
		}
		s1 = arg(m, "window_scale");
		if (s1 != NULL) {
			cf->window_scale = to_float(s1);
		}
		s1 = arg(m, "rp_supersample");
		if (s1 != NULL) {
			cf->rp_supersample = to_float(s1);
		}
		s1 = arg(m, "camera_fov");
		if (s1 != NULL) {
			cf->camera_fov = to_float(s1);
		}
		s1 = arg(m, "brush_live");
		if (s1 != NULL) {
			cf->brush_live = (cf->brush_live & ~255) | to_bool(s1);
		}
		s1 = arg(m, "node_previews");
		if (s1 != NULL) {
			cf->node_previews = (cf->node_previews & ~255) | to_bool(s1);
		}
		s1 = arg(m, "material_live");
		if (s1 != NULL) {
			cf->material_live = (cf->material_live & ~255) | to_bool(s1);
		}
		// The map's value strings are substrings of the request text and the map
		// is dropped at the end of this frame, so copy before handing the host a
		// pointer it will hold indefinitely.
		s1 = arg(m, "keymap");
		if (s1 != NULL) {
			cf->keymap = string_copy(s1);
		}
		s1 = arg(m, "theme");
		if (s1 != NULL) {
			cf->theme = string_copy(s1);
		}
		json_encode_begin();
		json_encode_i32("window_w", cf->window_w);
		json_encode_i32("window_h", cf->window_h);
		json_encode_f32("window_scale", cf->window_scale);
		json_encode_f32("rp_supersample", cf->rp_supersample);
		json_encode_i32("layer_res", cf->layer_res);
		json_encode_i32("undo_steps", cf->undo_steps);
		json_encode_f32("camera_fov", cf->camera_fov);
		json_encode_i32("brush_live", (cf->brush_live & 255) != 0);
		json_encode_i32("node_previews", (cf->node_previews & 255) != 0);
		json_encode_i32("material_live", (cf->material_live & 255) != 0);
		json_encode_i32("workspace", cf->workspace);
		json_encode_i32("workflow", cf->workflow);
		json_encode_string("keymap", jesc(cf->keymap));
		json_encode_string("theme", jesc(cf->theme));
		json_encode_string("note", "keymap and theme are stored but ArmorPaint applies them only on reload");
		return json_encode_end();
	}
	else if (string_equals(op, "get_main_object")) {
		mo = context_main_object();
		if (mo == NULL) {
			return fail("no_project", "no main paint object");
		}
		if (mo->base == NULL) {
			return fail("internal", "main object has no base");
		}
		json_encode_begin();
		emit_object(mo->base);
		return json_encode_end();
	}
	else if (string_equals(op, "get_object")) {
		s1 = arg(m, "name");
		if (s1 == NULL) {
			return fail("bad_args", "missing 'name'");
		}
		ob = script_get_object(s1);
		if (ob == NULL) {
			return fail("not_found", string("no object named %s", s1));
		}
		json_encode_begin();
		emit_object(ob);
		return json_encode_end();
	}
	else if (string_equals(op, "camera_get")) {
		// The viewport camera is a scene object named "Camera" (startup.c), not a
		// paint object, so script_get_object cannot see it; scene_get_child can. Its
		// world position lets the server tell which way a surface faces the view.
		ob = scene_get_child("Camera");
		if (ob == NULL) {
			return fail("not_found", "no scene object named Camera");
		}
		if (ob->transform == NULL) {
			return fail("internal", "the camera has no transform");
		}
		json_encode_begin();
		emit_object(ob);
		json_encode_f32("world_x", transform_world_x(ob->transform));
		json_encode_f32("world_y", transform_world_y(ob->transform));
		json_encode_f32("world_z", transform_world_z(ob->transform));
		return json_encode_end();
	}

	// ---- objects & meshes -------------------------------------------------
	else if (string_equals(op, "shape_list")) {
		// script_shape_list() returns project_default_mesh_list, a host-owned
		// global (minic_impl.c:702). NEVER free it.
		json_encode_begin();
		json_encode_string("names", sa_names(script_shape_list(), MAX_LIST_ITEMS));
		json_encode_string("delimiter", "|");
		return json_encode_end();
	}
	else if (string_equals(op, "shape_add")) {
		s1 = arg(m, "name");
		if (s1 == NULL) {
			return fail("bad_args", "missing 'name'");
		}
		// The host validates the name against its own list and returns NULL for
		// an unknown shape or an empty project (minic_impl.c:707).
		ob = script_shape_add(s1);
		if (ob == NULL) {
			return fail("bad_args", string("unknown shape '%s', or there is no paint object to add it to", s1));
		}
		json_encode_begin();
		emit_object(ob);
		return json_encode_end();
	}
	else if (string_equals(op, "object_duplicate")) {
		s1 = arg(m, "name");
		if (s1 == NULL) {
			return fail("bad_args", "missing 'name'");
		}
		ob = script_get_object(s1);
		if (ob == NULL) {
			return fail("not_found", string("no object named %s", s1));
		}
		ob = script_object_duplicate(ob);
		if (ob == NULL) {
			return fail("bad_args", string("'%s' is not a mesh object and cannot be duplicated", s1));
		}
		json_encode_begin();
		emit_object(ob);
		return json_encode_end();
	}
	else if (string_equals(op, "object_set_transform")) {
		s1 = arg(m, "name");
		if (s1 == NULL) {
			return fail("bad_args", "missing 'name'");
		}
		ob = script_get_object(s1);
		if (ob == NULL) {
			return fail("not_found", string("no object named %s", s1));
		}
		transform_t *tr = ob->transform;
		if (tr == NULL) {
			return fail("internal", "object has no transform");
		}
		s2 = arg(m, "loc_x");
		if (s2 != NULL) {
			tr->loc.x = to_float(s2);
			tr->loc.y = arg_f(m, "loc_y", tr->loc.y);
			tr->loc.z = arg_f(m, "loc_z", tr->loc.z);
			tr->loc.w = 1.0;
		}
		s2 = arg(m, "rot_x");
		if (s2 != NULL) {
			// server.py sends XYZ euler in RADIANS. iron composes its quaternion
			// YZX (quat_from_euler, iron_math.c:404) and that function is not
			// bound, so the formula is reproduced exactly -- any other ordering
			// would silently disagree with what the app itself would produce.
			float ex  = to_float(s2);
			float ey  = arg_f(m, "rot_y", 0.0);
			float ez  = arg_f(m, "rot_z", 0.0);
			float h1  = ex / 2.0;
			float h2  = ey / 2.0;
			float h3  = ez / 2.0;
			float q1  = cosf(h1);
			float p1  = sinf(h1);
			float q2  = cosf(h2);
			float p2  = sinf(h2);
			float q3  = cosf(h3);
			float p3  = sinf(h3);
			tr->rot.x = p1 * q2 * q3 + q1 * p2 * p3;
			tr->rot.y = q1 * p2 * q3 + p1 * q2 * p3;
			tr->rot.z = q1 * q2 * p3 - p1 * p2 * q3;
			tr->rot.w = q1 * q2 * q3 - p1 * p2 * p3;
		}
		s2 = arg(m, "scale_x");
		if (s2 != NULL) {
			tr->scale.x = to_float(s2);
			tr->scale.y = arg_f(m, "scale_y", tr->scale.y);
			tr->scale.z = arg_f(m, "scale_z", tr->scale.z);
			tr->scale.w = 1.0;
		}
		transform_build_matrix(tr);
		if (c != NULL) {
			c->ddirty = 2;
		}
		json_encode_begin();
		emit_object(ob);
		return json_encode_end();
	}
	else if (string_equals(op, "object_set_visible")) {
		s1 = arg(m, "name");
		if (s1 == NULL) {
			return fail("bad_args", "missing 'name'");
		}
		s2 = arg(m, "visible");
		if (s2 == NULL) {
			return fail("bad_args", "missing 'visible'");
		}
		ob = script_get_object(s1);
		if (ob == NULL) {
			return fail("not_found", string("no object named %s", s1));
		}
		ob->visible = to_bool(s2);
		if (c != NULL) {
			c->ddirty = 2;
		}
		json_encode_begin();
		emit_object(ob);
		return json_encode_end();
	}
	else if (string_equals(op, "append_mesh")) {
		s1 = arg(m, "path");
		if (s1 != NULL) {
			if (!path_ok(s1)) {
				return fail("bad_args", "'path' is empty, over-long, or contains a quote, newline, $ or backtick");
			}
			if (!file_here(s1)) {
				return fail("not_found", string("no such file: %s", s1));
			}
			script_append_mesh(s1);
			json_encode_begin();
			json_encode_string("path", jesc(s1));
			json_encode_string("source", "file");
			return json_encode_end();
		}
		s2 = arg(m, "obj_data");
		if (s2 == NULL) {
			return fail("bad_args", "give exactly one of 'path' or 'obj_data'");
		}
		// A real newline cannot cross this wire -- the request JSON is not
		// escape-decoded on this side -- so server.py encodes OBJ line breaks as
		// '|' and they are restored here. Wavefront OBJ never contains a '|'.
		script_append_mesh_obj(string_replace_all(s2, "|", "\n"));
		json_encode_begin();
		json_encode_string("source", "inline");
		json_encode_i32("bytes", string_length(s2));
		return json_encode_end();
	}

	// ---- materials --------------------------------------------------------
	else if (string_equals(op, "material_get_active")) {
		if (c == NULL) {
			return fail("internal", "no context");
		}
		mt = c->material;
		if (mt == NULL) {
			return fail("no_project", "no active material");
		}
		json_encode_begin();
		json_encode_i32("id", mt->id);
		if (mt->canvas != NULL) {
			json_encode_string("name", jesc(mt->canvas->name));
			if (mt->canvas->nodes != NULL) {
				json_encode_i32("node_count", mt->canvas->nodes->length);
			}
			if (mt->canvas->links != NULL) {
				json_encode_i32("link_count", mt->canvas->links->length);
			}
		}
		// Reported under server.py's PAINT_CHANNELS names, not slot_material_t's
		// abbreviations, so what comes back matches what set_channels takes.
		json_encode_bool("base", (mt->paint_base & 255) != 0);
		json_encode_bool("opacity", (mt->paint_opac & 255) != 0);
		json_encode_bool("occlusion", (mt->paint_occ & 255) != 0);
		json_encode_bool("roughness", (mt->paint_rough & 255) != 0);
		json_encode_bool("metallic", (mt->paint_met & 255) != 0);
		json_encode_bool("normal", (mt->paint_nor & 255) != 0);
		json_encode_bool("height", (mt->paint_height & 255) != 0);
		json_encode_bool("emission", (mt->paint_emis & 255) != 0);
		json_encode_bool("subsurface", (mt->paint_subs & 255) != 0);
		return json_encode_end();
	}
	else if (string_equals(op, "material_list")) {
		if (pr == NULL) {
			return fail("no_project", "no project runtime");
		}
		json_encode_begin();
		json_encode_bool("live", 0);
		json_encode_string("caveat", "project_t.material_nodes is written only at save/load: null before the first save and blind to materials created this session; use material_get_active for ground truth");
		// project_t.material_nodes is an untyped void*. Every *_array_t in this
		// codebase shares {buffer,length,capacity}, so re-typing it as
		// any_array_t reads the right offsets (MINIC_DIALECT_AND_API.md 2.1).
		ar = pr->material_nodes;
		if (ar == NULL) {
			json_encode_i32("count", 0);
			json_encode_i32("returned", 0);
			json_encode_string("names", "");
		}
		else {
			n = ar->length;
			json_encode_i32("count", n);
			if (n > MAX_LIST_ITEMS) {
				n = MAX_LIST_ITEMS;
			}
			json_encode_i32("returned", n);
			acc = "";
			for (int mi = 0; mi < n; ++mi) {
				ui_node_canvas_t *cvi = ar->buffer[mi];
				if (cvi != NULL) {
					if (mi > 0) {
						acc = string("%s|%s", acc, jesc(cvi->name));
					}
					else {
						acc = jesc(cvi->name);
					}
				}
			}
			json_encode_string("names", acc);
		}
		json_encode_string("delimiter", "|");
		return json_encode_end();
	}
	else if (string_equals(op, "material_create")) {
		s1 = arg(m, "name");
		if (s1 == NULL) {
			return fail("bad_args", "missing 'name'");
		}
		if (string_length(s1) < 1) {
			return fail("bad_args", "empty 'name'");
		}
		mt = script_material_create(s1);
		if (mt == NULL) {
			return fail("internal", "material could not be created");
		}
		json_encode_begin();
		json_encode_i32("id", mt->id);
		if (mt->canvas != NULL) {
			json_encode_string("name", jesc(mt->canvas->name));
		}
		return json_encode_end();
	}
	else if (string_equals(op, "material_select")) {
		s1 = arg(m, "name");
		if (s1 == NULL) {
			return fail("bad_args", "missing 'name'");
		}
		// A material's identity is its canvas name (minic_impl.c:177).
		mt = script_get_material(s1);
		if (mt == NULL) {
			return fail("not_found", string("no material named %s", s1));
		}
		script_material_set(mt);
		json_encode_begin();
		json_encode_i32("id", mt->id);
		json_encode_string("name", jesc(s1));
		return json_encode_end();
	}
	else if (string_equals(op, "material_delete")) {
		s1 = arg(m, "name");
		if (s1 == NULL) {
			return fail("bad_args", "missing 'name'");
		}
		mt = script_get_material(s1);
		if (mt == NULL) {
			return fail("not_found", string("no material named %s", s1));
		}
		script_material_delete(mt);
		return "{}";
	}
	else if (string_equals(op, "material_assign")) {
		s1 = arg(m, "object");
		s2 = arg(m, "material");
		if (s1 == NULL) {
			return fail("bad_args", "missing 'object'");
		}
		if (s2 == NULL) {
			return fail("bad_args", "missing 'material'");
		}
		ob = script_get_object(s1);
		if (ob == NULL) {
			return fail("not_found", string("no paint object named %s", s1));
		}
		mt = script_get_material(s2);
		if (mt == NULL) {
			return fail("not_found", string("no material named %s", s2));
		}
		script_object_set_material(ob, mt);
		return "{}";
	}
	else if (string_equals(op, "material_set_channels")) {
		if (c == NULL) {
			return fail("internal", "no context");
		}
		mt = c->material;
		if (mt == NULL) {
			return fail("no_project", "no active material");
		}
		// The KEYS are server.py's PAINT_CHANNELS, not slot_material_t's field
		// spelling. They differ (opacity/paint_opac, occlusion/paint_occ,
		// roughness/paint_rough, ...) and reading the wrong name here is silent:
		// the op answers ok and changes nothing.
		s1 = arg(m, "base");
		if (s1 != NULL) {
			mt->paint_base = (mt->paint_base & ~255) | to_bool(s1);
		}
		s1 = arg(m, "opacity");
		if (s1 != NULL) {
			mt->paint_opac = (mt->paint_opac & ~255) | to_bool(s1);
		}
		s1 = arg(m, "occlusion");
		if (s1 != NULL) {
			mt->paint_occ = (mt->paint_occ & ~255) | to_bool(s1);
		}
		s1 = arg(m, "roughness");
		if (s1 != NULL) {
			mt->paint_rough = (mt->paint_rough & ~255) | to_bool(s1);
		}
		s1 = arg(m, "metallic");
		if (s1 != NULL) {
			mt->paint_met = (mt->paint_met & ~255) | to_bool(s1);
		}
		s1 = arg(m, "normal");
		if (s1 != NULL) {
			mt->paint_nor = (mt->paint_nor & ~255) | to_bool(s1);
		}
		s1 = arg(m, "height");
		if (s1 != NULL) {
			mt->paint_height = (mt->paint_height & ~255) | to_bool(s1);
		}
		s1 = arg(m, "emission");
		if (s1 != NULL) {
			mt->paint_emis = (mt->paint_emis & ~255) | to_bool(s1);
		}
		s1 = arg(m, "subsurface");
		if (s1 != NULL) {
			mt->paint_subs = (mt->paint_subs & ~255) | to_bool(s1);
		}
		json_encode_begin();
		json_encode_bool("base", (mt->paint_base & 255) != 0);
		json_encode_bool("opacity", (mt->paint_opac & 255) != 0);
		json_encode_bool("occlusion", (mt->paint_occ & 255) != 0);
		json_encode_bool("roughness", (mt->paint_rough & 255) != 0);
		json_encode_bool("metallic", (mt->paint_met & 255) != 0);
		json_encode_bool("normal", (mt->paint_nor & 255) != 0);
		json_encode_bool("height", (mt->paint_height & 255) != 0);
		json_encode_bool("emission", (mt->paint_emis & 255) != 0);
		json_encode_bool("subsurface", (mt->paint_subs & 255) != 0);
		return json_encode_end();
	}
	else if (string_equals(op, "material_update")) {
		script_material_update();
		fill_after_update = 1;
		return "{}";
	}

	// ---- material nodes ---------------------------------------------------
	else if (string_equals(op, "node_list")) {
		if (c == NULL) {
			return fail("internal", "no context");
		}
		if (c->material == NULL) {
			return fail("no_project", "no active material");
		}
		cv = c->material->canvas;
		if (cv == NULL) {
			return fail("no_project", "the active material has no canvas");
		}
		if (cv->nodes == NULL) {
			return fail("internal", "the canvas has no node array");
		}
		n = cv->nodes->length;
		if (n > MAX_LIST_ITEMS) {
			n = MAX_LIST_ITEMS;
		}
		// "id,TYPE,name,x,y;..." -- a delimited string for the same reason as
		// the other lists, and bounded because string() leaks every intermediate.
		acc = "";
		for (int ni = 0; ni < n; ++ni) {
			ui_node_t *ndi = cv->nodes->buffer[ni];
			if (ndi != NULL) {
				acc = string("%s%d,%s,%s,%f,%f;", acc, ndi->id, jesc(ndi->type), jesc(ndi->name), ndi->x, ndi->y);
			}
		}
		json_encode_begin();
		json_encode_i32("count", cv->nodes->length);
		json_encode_i32("returned", n);
		json_encode_string("nodes", acc);
		json_encode_string("node_format", "id,type,name,x,y;");
		// Graph topology. ui_node_link_t is its own registered struct -- reading a
		// link through ui_node_t would silently read the wrong offsets.
		if (cv->links != NULL) {
			i1 = cv->links->length;
			if (i1 > MAX_LIST_ITEMS) {
				i1 = MAX_LIST_ITEMS;
			}
			acc = "";
			for (int li = 0; li < i1; ++li) {
				ui_node_link_t *lk = cv->links->buffer[li];
				if (lk != NULL) {
					acc = string("%s%d,%d,%d,%d;", acc, lk->from_id, lk->from_socket, lk->to_id, lk->to_socket);
				}
			}
			json_encode_i32("link_count", cv->links->length);
			json_encode_i32("links_returned", i1);
			json_encode_string("links", acc);
			json_encode_string("link_format", "from_id,from_socket,to_id,to_socket;");
		}
		return json_encode_end();
	}
	else if (string_equals(op, "node_add")) {
		s1 = arg(m, "type");
		if (s1 == NULL) {
			return fail("bad_args", "missing 'type'");
		}
		if (string_length(s1) < 1) {
			return fail("bad_args", "empty 'type'");
		}
		if (c == NULL) {
			return fail("internal", "no context");
		}
		if (c->material == NULL) {
			return fail("no_project", "no active material");
		}
		// Returns NULL for an unknown type, which is the only validation
		// available -- the legal type names are not enumerable at runtime.
		nd = script_material_create_node_at(s1, arg_f(m, "x", 0.0), arg_f(m, "y", 0.0));
		if (nd == NULL) {
			return fail("bad_args", string("unknown node type: %s", s1));
		}
		json_encode_begin();
		json_encode_i32("id", nd->id);
		json_encode_string("type", jesc(nd->type));
		json_encode_string("name", jesc(nd->name));
		json_encode_f32("x", nd->x);
		json_encode_f32("y", nd->y);
		if (nd->inputs != NULL) {
			json_encode_i32("inputs", nd->inputs->length);
		}
		if (nd->outputs != NULL) {
			json_encode_i32("outputs", nd->outputs->length);
		}
		json_encode_string("input_sockets", socket_table(nd->inputs, 0));
		json_encode_string("output_sockets", socket_table(nd->outputs, 0));
		json_encode_string("buttons", socket_table(nd->buttons, 1));
		json_encode_string("socket_format", "index:name:type=default_values;");
		return json_encode_end();
	}
	else if (string_equals(op, "node_get")) {
		i1 = arg_i(m, "id", -1);
		if (i1 < 0) {
			return fail("bad_args", "missing or negative 'id'");
		}
		nd = script_material_get_node_id(i1);
		if (nd == NULL) {
			return fail("not_found", string("no node with id %d", i1));
		}
		json_encode_begin();
		json_encode_i32("id", nd->id);
		json_encode_string("type", jesc(nd->type));
		json_encode_string("name", jesc(nd->name));
		json_encode_f32("x", nd->x);
		json_encode_f32("y", nd->y);
		json_encode_string("input_sockets", socket_table(nd->inputs, 0));
		json_encode_string("output_sockets", socket_table(nd->outputs, 0));
		json_encode_string("buttons", socket_table(nd->buttons, 1));
		json_encode_string("socket_format", "index:name:type=default_values;");
		return json_encode_end();
	}
	else if (string_equals(op, "node_remove")) {
		i1 = arg_i(m, "id", -1);
		if (i1 < 0) {
			return fail("bad_args", "missing or negative 'id'");
		}
		nd = script_material_get_node_id(i1);
		if (nd == NULL) {
			return fail("not_found", string("no node with id %d", i1));
		}
		if (string_equals(nd->type, "OUTPUT_MATERIAL_PBR")) {
			return fail("unsupported", "the PBR output node cannot be removed");
		}
		script_material_remove_node(nd);
		return "{}";
	}
	else if (string_equals(op, "node_connect")) {
		i1 = arg_i(m, "from_id", -1);
		i2 = arg_i(m, "from_socket", -1);
		i3 = arg_i(m, "to_id", -1);
		i4 = arg_i(m, "to_socket", -1);
		if (i1 < 0) {
			return fail("bad_args", "missing 'from_id'");
		}
		if (i3 < 0) {
			return fail("bad_args", "missing 'to_id'");
		}
		if (i2 < 0) {
			return fail("bad_args", "missing 'from_socket'");
		}
		if (i4 < 0) {
			return fail("bad_args", "missing 'to_socket'");
		}
		nd = script_material_get_node_id(i1);
		if (nd == NULL) {
			return fail("not_found", string("no node with id %d", i1));
		}
		nd2 = script_material_get_node_id(i3);
		if (nd2 == NULL) {
			return fail("not_found", string("no node with id %d", i3));
		}
		// script_material_connect() no-ops silently on an out-of-range socket,
		// so the range is checked here to report bad_args instead.
		if (nd->outputs == NULL) {
			return fail("bad_args", "the source node has no outputs");
		}
		if (i2 >= nd->outputs->length) {
			return fail("bad_args", string("from_socket %d out of range", i2));
		}
		if (nd2->inputs == NULL) {
			return fail("bad_args", "the target node has no inputs");
		}
		if (i4 >= nd2->inputs->length) {
			return fail("bad_args", string("to_socket %d out of range", i4));
		}
		script_material_connect(nd, i2, nd2, i4);
		return "{}";
	}
	else if (string_equals(op, "node_disconnect")) {
		i1 = arg_i(m, "to_id", -1);
		i2 = arg_i(m, "to_socket", -1);
		if (i1 < 0) {
			return fail("bad_args", "missing 'to_id'");
		}
		if (i2 < 0) {
			return fail("bad_args", "missing 'to_socket'");
		}
		nd = script_material_get_node_id(i1);
		if (nd == NULL) {
			return fail("not_found", string("no node with id %d", i1));
		}
		script_material_disconnect(nd, i2);
		return "{}";
	}
	else if (string_equals(op, "node_set_value")) {
		// One op, four kinds -- server.py's ap_node_set_value. Four separate ops
		// would work too, but the tool surface is the contract and the tool is
		// singular.
		s1 = arg(m, "kind");
		if (s1 == NULL) {
			return fail("bad_args", "missing 'kind' (float, color, vector or button)");
		}
		i1 = arg_i(m, "id", -1);
		if (i1 < 0) {
			return fail("bad_args", "missing 'id'");
		}
		nd = script_material_get_node_id(i1);
		if (nd == NULL) {
			return fail("not_found", string("no node with id %d", i1));
		}
		i3 = arg_i(m, "is_input", 1);
		if (string_equals(s1, "button")) {
			i2 = arg_i(m, "button", -1);
			if (i2 < 0) {
				return fail("bad_args", "kind 'button' needs 'button'");
			}
			if (nd->buttons == NULL) {
				return fail("bad_args", "the node has no buttons");
			}
			if (i2 >= nd->buttons->length) {
				return fail("bad_args", string("button %d out of range", i2));
			}
			script_material_set_button(nd, i2, arg_f(m, "value", 0.0));
			return "{}";
		}
		i2 = arg_i(m, "socket", -1);
		if (i2 < 0) {
			return fail("bad_args", "missing 'socket'");
		}
		// Range-check against the side actually being written: the setters index
		// the socket array without a bounds check of their own.
		if (i3) {
			if (nd->inputs == NULL) {
				return fail("bad_args", "the node has no input sockets");
			}
			if (i2 >= nd->inputs->length) {
				return fail("bad_args", string("input socket %d out of range", i2));
			}
		}
		else {
			if (nd->outputs == NULL) {
				return fail("bad_args", "the node has no output sockets");
			}
			if (i2 >= nd->outputs->length) {
				return fail("bad_args", string("output socket %d out of range", i2));
			}
		}
		if (string_equals(s1, "float")) {
			script_material_set_float(nd, i3, i2, arg_f(m, "value", 0.0));
			return "{}";
		}
		else if (string_equals(s1, "color")) {
			// 'alpha', not 'a' -- server.py sends the fourth component spelled out.
			script_material_set_color(nd, i3, i2, arg_f(m, "r", 0.0), arg_f(m, "g", 0.0), arg_f(m, "b", 0.0), arg_f(m, "alpha", 1.0));
			return "{}";
		}
		else if (string_equals(s1, "vector")) {
			script_material_set_vector(nd, i3, i2, arg_f(m, "x", 0.0), arg_f(m, "y", 0.0), arg_f(m, "z", 0.0));
			return "{}";
		}
		return fail("bad_args", string("unknown kind '%s'; expected float, color, vector or button", s1));
	}

	// ---- painting & viewport ----------------------------------------------
	else if (string_equals(op, "select_tool")) {
		i1 = arg_i(m, "tool", -1);
		if (i1 < 0) {
			return fail("bad_args", "tool must be 0..13 (brush..bake)");
		}
		if (i1 > 13) {
			return fail("bad_args", "tool must be 0..13 (brush..bake)");
		}
		context_select_tool(i1);
		json_encode_begin();
		if (c != NULL) {
			json_encode_i32("tool", c->tool); // read back, do not assume
		}
		return json_encode_end();
	}
	else if (string_equals(op, "set_display_channel")) {
		i1 = arg_i(m, "mode", -99);
		if (i1 < -1) {
			return fail("bad_args", "mode must be -1..15");
		}
		if (i1 > 15) {
			return fail("bad_args", "mode must be -1..15");
		}
		context_set_viewport_mode(i1);
		json_encode_begin();
		if (c != NULL) {
			json_encode_i32("viewport_mode", c->viewport_mode);
		}
		return json_encode_end();
	}
	else if (string_equals(op, "set_brush")) {
		if (c == NULL) {
			return fail("internal", "no context");
		}
		// Brush parameters are context_t fields, not bindings. Presence rather
		// than a sentinel: brush_angle may legitimately hold any value.
		s1 = arg(m, "radius");
		if (s1 != NULL) {
			c->brush_radius = to_float(s1);
		}
		s1 = arg(m, "opacity");
		if (s1 != NULL) {
			c->brush_opacity = to_float(s1);
		}
		s1 = arg(m, "hardness");
		if (s1 != NULL) {
			c->brush_hardness = to_float(s1);
		}
		s1 = arg(m, "scale");
		if (s1 != NULL) {
			c->brush_scale = to_float(s1);
		}
		s1 = arg(m, "angle");
		if (s1 != NULL) {
			c->brush_angle = to_float(s1);
		}
		s1 = arg(m, "blending");
		if (s1 != NULL) {
			c->brush_blending = to_int(s1);
		}
		json_encode_begin();
		json_encode_f32("radius", c->brush_radius);
		json_encode_f32("opacity", c->brush_opacity);
		json_encode_f32("hardness", c->brush_hardness);
		json_encode_f32("scale", c->brush_scale);
		json_encode_f32("angle", c->brush_angle);
		json_encode_i32("blending", c->brush_blending);
		return json_encode_end();
	}
	else if (string_equals(op, "paint_stroke")) {
		s1 = arg(m, "points");
		if (s1 == NULL) {
			return fail("bad_args", "missing 'points' (\"x,y;x,y;...\", normalised screen coords)");
		}
		if (c == NULL) {
			return fail("internal", "no context");
		}
		if (c->layer == NULL) {
			return fail("no_project", "no layer is selected; script_paint would no-op");
		}
		n = do_stroke(s1, 0, 1);
		if (n < 0) {
			return fail("bad_args", string("too many points for one request (at most %d points and %d numbers); stream a longer stroke with stroke_begin / stroke_points / stroke_end", MAX_STROKE_POINTS, MAX_STROKE_VALUES));
		}
		if (n < 1) {
			return fail("bad_args", "no parseable points; expected \"x,y;x,y;...\"");
		}
		json_encode_begin();
		json_encode_i32("points", n);
		json_encode_i32("max_points", MAX_STROKE_POINTS);
		return json_encode_end();
	}
	else if (string_equals(op, "paint_stroke_world")) {
		s1 = arg(m, "points");
		if (s1 == NULL) {
			return fail("bad_args", "missing 'points' (\"x,y,z;x,y,z;...\", world coords)");
		}
		if (c == NULL) {
			return fail("internal", "no context");
		}
		if (c->layer == NULL) {
			return fail("no_project", "no layer is selected; script_paint_world would no-op");
		}
		n = do_stroke(s1, 1, 1);
		if (n < 0) {
			return fail("bad_args", string("too many points for one request (at most %d points and %d numbers); stream a longer stroke with stroke_begin / stroke_points / stroke_end", MAX_STROKE_POINTS, MAX_STROKE_VALUES));
		}
		if (n < 1) {
			return fail("bad_args", "no parseable points; expected \"x,y,z;x,y,z;...\"");
		}
		json_encode_begin();
		json_encode_i32("points", n);
		json_encode_i32("max_points", MAX_STROKE_POINTS);
		return json_encode_end();
	}
	else if (string_equals(op, "stroke_begin")) {
		// Streamed stroke (see stroke_open). Opens it; the first stroke_points
		// starts the stroke in ArmorPaint (script_paint_begin_stroke is lazy).
		if (c == NULL) {
			return fail("internal", "no context");
		}
		if (c->layer == NULL) {
			return fail("no_project", "no layer is selected; script_paint would no-op");
		}
		i1 = end_stroke(); // a stroke left open by an earlier caller
		stroke_open    = 1;
		stroke_world   = to_bool(arg(m, "world"));
		stroke_total   = 0;
		stroke_touched = sys_time();
		stroke_r0      = c->brush_radius;
		stroke_o0      = c->brush_opacity;
		json_encode_begin();
		json_encode_bool("open", 1);
		json_encode_bool("world", stroke_world);
		json_encode_bool("closed_previous", i1);
		json_encode_i32("max_points", MAX_STROKE_POINTS);
		json_encode_i32("max_values", MAX_STROKE_VALUES);
		json_encode_f32("idle_close_s", STROKE_IDLE_S);
		return json_encode_end();
	}
	else if (string_equals(op, "stroke_points")) {
		if (!stroke_open) {
			return fail("no_stroke", string("no stroke is open: call stroke_begin first (an open stroke is closed by any other op, and after %f s without points)", STROKE_IDLE_S));
		}
		s1 = arg(m, "points");
		if (s1 == NULL) {
			return fail("bad_args", "missing 'points'");
		}
		n = do_stroke(s1, stroke_world, 0);
		if (n < 0) {
			return fail("bad_args", string("too many points for one request (at most %d points and %d numbers); send them in more stroke_points requests", MAX_STROKE_POINTS, MAX_STROKE_VALUES));
		}
		if (n < 1) {
			return fail("bad_args", "no parseable points");
		}
		stroke_total   = stroke_total + n;
		stroke_touched = sys_time();
		json_encode_begin();
		json_encode_i32("points", n);
		json_encode_i32("total", stroke_total);
		return json_encode_end();
	}
	else if (string_equals(op, "stroke_end")) {
		i1 = stroke_open;
		if (stroke_open) {
			script_paint_end();
			if (c != NULL) {
				c->brush_radius  = stroke_r0;
				c->brush_opacity = stroke_o0;
			}
			stroke_open = 0;
		}
		json_encode_begin();
		json_encode_bool("was_open", i1);
		json_encode_i32("total", stroke_total);
		return json_encode_end();
	}
	else if (string_equals(op, "fill_layer")) {
		// The ONLY layer operation in the stock binding table. Everything else
		// (list, create, delete, rename, reorder, mask, opacity, blending) is in
		// the native extension: the layer_* ops at the end of dispatch().
		if (c == NULL) {
			return fail("internal", "no context");
		}
		if (c->layer == NULL) {
			return fail("no_project", "no layer is selected");
		}
		if (c->material == NULL) {
			return fail("no_project", "no active material");
		}
		script_fill_layer(); // fills the selected layer with the active material
		refill_pending    = fill_after_update; // see fill_after_update
		fill_after_update = 0;
		json_encode_begin();
		json_encode_bool("refill_next_frame", refill_pending);
		return json_encode_end();
	}
	else if (string_equals(op, "capture_to_project")) {
		if (pr == NULL) {
			return fail("no_project", "viewport capture needs an open project");
		}
		if (c == NULL) {
			return fail("internal", "no context");
		}
		i1 = arg_i(m, "width", 1024);
		i2 = arg_i(m, "height", 1024);
		if (i1 < 16) {
			return fail("bad_args", "width must be 16..4096");
		}
		if (i1 > 4096) {
			return fail("bad_args", "width must be 16..4096");
		}
		if (i2 < 16) {
			return fail("bad_args", "height must be 16..4096");
		}
		if (i2 > 4096) {
			return fail("bad_args", "height must be 16..4096");
		}
		// The upstream recipe is make_tilesheet.c:38-58. It copies the render
		// path's "last" target, i.e. the frame ALREADY RENDERED. A handler runs
		// inline and cannot wait for a re-render, so the UI-hiding handshake
		// (capturing_screenshot plus a two-frame settle) cannot complete inside
		// one request -- this captures whatever was last drawn.
		void *tex = gpu_create_render_target(i1, i2, GPU_TEXTURE_FORMAT_RGBA32);
		if (tex == NULL) {
			return fail("internal", "gpu_create_render_target returned null");
		}
		viewport_capture_screenshot_to(tex, 0.0, 0.0, i1, i2);
		viewport_save_texture(tex);
		c->capturing_screenshot = 0;
		c->ddirty               = 2;
		json_encode_begin();
		json_encode_i32("width", i1);
		json_encode_i32("height", i2);
		json_encode_string("destination", "project packed asset (/packed/screenshotN.png)");
		json_encode_string("note", "the pixels live inside the project and are persisted only when the .arm is saved; nothing outside ArmorPaint can read them");
		json_encode_string("settle", "none: taken from the previously rendered frame, so the UI overlay state is whatever was last drawn");
		return json_encode_end();
	}
	else if (string_equals(op, "capture_viewport")) {
		if (ext_state == 1) {
			// The native extension captures and writes the PNG itself, and can
			// free the render target it reallocates on a size change.
			s1 = try_ext(op, m, arg_prefix);
			if (s1 != NULL) {
				if (!starts_with(s1, "1")) {
					r_ok      = 0;
					err_count = err_count + 1;
				}
				return substring(s1, 1, string_length(s1));
			}
		}
		if (png_state == 0) {
			return fail("unsupported",
			            "this build exports neither viewport_save_texture_to_file (upstream since 2026-09-09) nor mcp_ext_call (patch/apply_ext_patch.py). Use ap_capture_window, which screenshots the window from outside on any build.");
		}
		if (pr == NULL) {
			return fail("no_project", "viewport capture needs an open project");
		}
		s1 = arg(m, "path");
		if (s1 == NULL) {
			return fail("bad_args", "missing 'path'");
		}
		if (!path_ok(s1)) {
			return fail("bad_args", "'path' is empty, over-long, or contains a quote, newline, $ or backtick");
		}
		i1 = arg_i(m, "width", 1024);
		i2 = arg_i(m, "height", 1024);
		if (i1 < 16) {
			return fail("bad_args", "width must be 16..4096");
		}
		if (i1 > 4096) {
			return fail("bad_args", "width must be 16..4096");
		}
		if (i2 < 16) {
			return fail("bad_args", "height must be 16..4096");
		}
		if (i2 > 4096) {
			return fail("bad_args", "height must be 16..4096");
		}
		// Reuse the cached target; only a size change reallocates. See the
		// capture_tex declaration for why this cannot be freed instead.
		if (capture_tex == NULL || capture_w != i1 || capture_h != i2) {
			capture_tex = gpu_create_render_target(i1, i2, GPU_TEXTURE_FORMAT_RGBA32);
			capture_w   = i1;
			capture_h   = i2;
		}
		if (capture_tex == NULL) {
			capture_w = 0;
			capture_h = 0;
			return fail("internal", "gpu_create_render_target returned null");
		}
		viewport_capture_screenshot_to(capture_tex, 0.0, 0.0, i1, i2);
		// The first call doubles as the probe: try_save_png returns 0 when the
		// binding is missing, having written nothing.
		png_state = try_save_png(capture_tex, s1);
		if (c != NULL) {
			c->capturing_screenshot = 0;
			c->ddirty               = 2;
		}
		if (png_state == 0) {
			return fail("unsupported",
			            "this build exports neither viewport_save_texture_to_file (upstream since 2026-09-09) nor mcp_ext_call (patch/apply_ext_patch.py). Use ap_capture_window, which screenshots the window from outside on any build.");
		}
		json_encode_begin();
		json_encode_string("path", jesc(s1));
		json_encode_i32("width", i1);
		json_encode_i32("height", i2);
		json_encode_bool("exists", file_here(s1));
		json_encode_string("method", "viewport_save_texture_to_file");
		return json_encode_end();
	}

	// ---- everything else: the native extension ------------------------------
	// Layers, undo/redo, export format, bake, render settings, live lists and
	// camera views have no minic binding at all. On a build carrying
	// patch/apply_ext_patch.py they are implemented natively behind the single
	// mcp_ext_call binding, which also rejects op names it does not know.
	if (ext_state != 0) {
		s1 = try_ext(op, m, arg_prefix);
		if (s1 == NULL) {
			ext_state = 0;
		}
		else {
			ext_state = 1;
			if (!starts_with(s1, "1")) {
				r_ok       = 0;
				err_count  = err_count + 1;
				last_error = string("%s: native extension refused", op);
			}
			return substring(s1, 1, string_length(s1));
		}
	}
	return fail("unsupported", string("unknown op '%s', or an op that needs the optional native extension (patch/apply_ext_patch.py), which this ArmorPaint build does not carry", op));
}

// ---------------------------------------------------------------------------
// Request lifecycle
//
// A request becomes a JOB: one op, or an ordered batch of sub-ops. step_job()
// runs as many items as fit in the current frame and resumes on the next one:
//
//   * items run strictly in order, and nothing new is read from req/ until the
//     job's single reply has been committed;
//   * a second item starts in the same frame only while the frame has used
//     fewer than CALL_BUDGET script calls (the arena limit, see ncalls) and
//     less than FRAME_BUDGET_S of wall time;
//   * op_cost() >= 2 items always start a frame of their own, and >= 3
//     ("heavy": exports, saves, opens, imports, bakes) additionally wait until
//     no mouse button is down, so a long handler never lands mid-stroke.
// ---------------------------------------------------------------------------
void handle_one(char *name) {
	ncalls  = ncalls + 1;
	int nlen = string_length(name);
	if (nlen < 6) {
		return; // ".json" with no id at all; substring below would go negative
	}
	char *id   = substring(name, 0, nlen - 5); // strip ".json"
	char *path = string("%s/%s", dir_req, name);

	// The id becomes the reply filename, so it is validated before anything
	// else. An unusable one is deleted rather than retried forever.
	if (!id_ok(id)) {
		del_file(path);
		err_count  = err_count + 1;
		last_error = string("bad_args: unusable request id in %s", name);
		return;
	}

	buffer_t *blob = data_get_blob(path);
	if (blob == NULL) {
		// A miss is cached as a NULL entry, which data_delete_blob will not
		// remove -- harmless, since a NULL entry reads back as a miss and the
		// file is reloaded next time.
		del_file(path);
		err_count  = err_count + 1;
		last_error = string("internal: could not read %s", name);
		return;
	}

	// Copy the bytes out FIRST. This ordering is not stylistic: data_delete_blob
	// frees blob->buffer and blob itself (engine.c:2000), so converting after the
	// eviction would read freed memory.
	char *text = sys_buffer_to_string(blob);

	// MANDATORY. data_get_blob memoises by path in data_cached_blobs
	// (engine.c:1879) with no expiry. Skip this and the bridge answers the first
	// request seen at a given filename correctly and then silently replays those
	// same bytes for every later request that reuses the name.
	data_delete_blob(path);

	// Consume BEFORE executing. If a handler ever took the app down, a request
	// still sitting on disk would be replayed into the user's project on the next
	// start -- re-running a destructive op they never asked for twice.
	del_file(path);

	req_count     = req_count + 1;
	last_id       = id;
	last_activity = sys_time();

	// Screen before parsing: a malformed body would crash the host parser, not
	// merely fail. See json_sane().
	int sane = json_sane(text);
	if (sane < 0) {
		reply(id, 0, fail("bad_args", "request contains a JSON array; json_parse_to_map corrupts on arrays -- send list args as a delimited string"), 0);
		return;
	}
	if (sane < 1) {
		reply(id, 0, fail("bad_args", "body is not a well-formed compact JSON object (<= 16384 bytes)"), 0);
		return;
	}

	// json_parse_to_map flattens nested objects, skips arrays and returns every
	// value as a char*. See the header comment for what the server must send.
	// The map is heap-allocated, so it outlives this frame for a multi-frame job.
	job_map    = json_parse_to_map(text);
	job_id     = id;
	job_i      = 0;
	job_errs   = 0;
	job_frames = 0;
	job_held   = 0;
	job_acc    = EMPTY_STR;
	job_inner  = EMPTY_OBJ;
	job_ok     = 1;
	job_t0     = sys_time();
	char *op0  = any_map_get(job_map, "op"); // envelope key: read directly, never via arg()
	if (op0 == NULL) {
		job_map = NULL;
		reply(id, 0, fail("bad_args", "missing 'op'"), 0);
		return;
	}
	job_batch = string_equals(op0, "batch");
	job_n     = 1;
	job_stop   = 0;
	if (job_batch) {
		arg_prefix = EMPTY_STR;
		job_n      = arg_i(job_map, "count", 0);
		job_stop   = arg_i(job_map, "stop_on_error", 0);
		if (job_n < 1) {
			job_map = NULL;
			reply(id, 0, fail("bad_args", "batch needs a_count >= 1 and items b0_op, b1_op, ..."), 0);
			return;
		}
		if (job_n > 64) {
			job_map = NULL;
			reply(id, 0, fail("bad_args", "a batch holds at most 64 items"), 0);
			return;
		}
	}
	step_job();
}

// Run what fits of the open job this frame; commit its reply once it is done.
void step_job() {
	ncalls = ncalls + 1;
	if (job_map == NULL) {
		return;
	}
	job_frames  = job_frames + 1;
	float f0    = sys_time();
	int   ran   = 0;
	char *pfx   = "";
	char *op    = NULL;
	char *inner = NULL;
	int   cost  = 1;
	while (job_i < job_n) {
		pfx = EMPTY_STR;
		if (job_batch) {
			pfx = string("b%d_", job_i);
		}
		op   = any_map_get(job_map, string("%sop", pfx));
		cost = op_cost(op);
		if (ran > 0) {
			if (cost > 1) {
				break;
			}
			if (ncalls > CALL_BUDGET) {
				break;
			}
			if (sys_time() - f0 > FRAME_BUDGET_S) {
				break;
			}
		}
		if (cost > 2) {
			// Heavy ops run inline on the render thread and stall it for their
			// whole duration. Never start one while the user is holding a mouse
			// button (mid-stroke, mid-drag); wait for the release instead.
			if (mouse_down_any()) {
				if (!job_held) {
					job_held = 1;
					write_heartbeat();
				}
				return;
			}
			job_held = 0;
			busy     = 1;
			write_heartbeat(); // publish busy=1: the server must not mistake the stall for a hang
		}

		arg_prefix = pfx;
		r_ok       = 1;
		if (cost < 0) {
			// Disabled bridge: op_cost() marks everything but the two ops that
			// must keep working (bridge_set_enabled, ping) with -1.
			inner = fail("bridge_disabled", "the bridge is disabled; call bridge_set_enabled(true) or tick it in the Plugins tab");
		}
		else {
			inner = dispatch(job_map, op);
		}
		arg_prefix = EMPTY_STR;
		busy       = 0;

		// A minic runtime error inside dispatch -- a parse error in an arm, say --
		// aborts THAT function and returns 0 without touching this one, because
		// every call gets a fresh env (minic.c:823). Without this guard the reply
		// would carry a literal `null` result while r_ok still said true.
		if (inner == NULL) {
			r_ok       = 0;
			err_count  = err_count + 1;
			last_error = HANDLER_ABORTED;
			inner      = "{\"code\":\"internal\",\"message\":\"the handler aborted; look in the ArmorPaint console for a '<plugin>.c:<line>: error:' line\"}";
		}
		if (job_batch) {
			if (job_i > 0) {
				job_acc = string("%s,", job_acc);
			}
			if (r_ok) {
				job_acc = string("%s{\"i\":%d,\"op\":\"%s\",\"ok\":true,\"result\":%s}", job_acc, job_i, jesc(op), inner);
			}
			else {
				job_acc  = string("%s{\"i\":%d,\"op\":\"%s\",\"ok\":false,\"error\":%s}", job_acc, job_i, jesc(op), inner);
				job_errs = job_errs + 1;
			}
		}
		else {
			job_inner = inner;
			job_ok    = r_ok;
		}
		job_i = job_i + 1;
		ran   = ran + 1;
		if (quitting) {
			break;
		}
		if (!r_ok) {
			if (job_stop) {
				break;
			}
		}
		// A heavy/expensive item ends the frame, and so does a fill that queued
		// its next-frame refill: later items must see the settled layer.
		if (cost > 1) {
			break;
		}
		if (refill_pending) {
			break;
		}
	}
	if (job_i < job_n) {
		if (!quitting) {
			if (r_ok) {
				return; // more next frame
			}
			if (!job_stop) {
				return;
			}
		}
	}

	int ms = (sys_time() - job_t0) * 1000.0;
	if (job_batch) {
		pfx = "false";
		if (job_i < job_n) {
			pfx = "true";
		}
		job_inner = string("{\"count\":%d,\"executed\":%d,\"errors\":%d,\"frames\":%d,\"stopped_early\":%s,\"results\":[%s]}", job_n, job_i, job_errs,
		                   job_frames, pfx, job_acc);
		job_ok = 1; // per-item status lives in results[]
	}
	job_map       = NULL;
	job_held      = 0;
	last_activity = sys_time();
	reply(job_id, job_ok, job_inner, ms);
	if (quitting) {
		del_file(path_heartbeat);
		del_file(path_lock);
		return;
	}
	write_heartbeat(); // clear busy promptly rather than waiting for the 1 Hz tick
}

void on_update() {
	ncalls    = 0;
	float now = sys_time();

	// Hold the app awake only while there is a reason to. Dozing lets Iron's own
	// idle gate put ArmorPaint to sleep (after 120 idle frames), and a sleeping
	// app dispatches no on_update at all -- the server wakes it with a synthetic
	// pointer move before it writes a request (see linger).
	//
	// When awake, iron_delay_idle_sleep() must be reached on every frame before
	// any early return: base_update() skips iron_update() -- which dispatches this
	// callback -- once paused_frames exceeds 3 (Windows background) or 120
	// (idle), and BOTH gates read the counter this call zeroes (iron.h:1011).
	int awake = 0;
	if (enabled) {
		if (linger < 0.0) {
			awake = 1;
		}
		if (now - last_activity < linger) {
			awake = 1;
		}
	}
	if (job_map != NULL) {
		awake = 1;
	}
	if (refill_pending) {
		awake = 1;
	}
	if (stroke_open) {
		awake = 1;
	}
	if (awake) {
		iron_delay_idle_sleep();
	}
	if (dozing == awake) {
		dozing = !awake;
		write_heartbeat(); // the server reads "dozing" to decide whether to wake the app
	}

	// The deferred second pass of a fill after material_update (see
	// fill_after_update). Checked before the poll throttle so it lands on the very
	// next frame. The layer may have been deselected since; re-check.
	if (refill_pending) {
		refill_pending = 0;
		context_t *rc = script_get_context();
		if (rc != NULL) {
			if (rc->layer != NULL) {
				if (rc->material != NULL) {
					script_fill_layer();
				}
			}
		}
	}

	// A streamed stroke nobody finished (the server died, or never sent
	// stroke_end) is closed here rather than left open in the paint tool.
	if (stroke_open) {
		if (now - stroke_touched > STROKE_IDLE_S) {
			end_stroke();
			write_heartbeat();
		}
	}

	float dt = sys_real_delta();

	hb_accum = hb_accum + dt;
	if (hb_accum >= 1.0) {
		hb_accum = 0.0;
		write_heartbeat();
	}

	// An open job continues before anything new is read.
	if (job_map != NULL) {
		step_job();
		return;
	}

	// Throttle idle polling, but not a conversation in progress: within a second
	// of the last request, poll every frame, so a chain of sequential tool calls
	// costs a frame each rather than a poll interval each.
	poll_accum = poll_accum + dt;
	if (now - last_activity > 1.0) {
		if (poll_accum < poll_interval) {
			return;
		}
	}
	poll_accum = 0.0;

	// The doorbell (see path_bell): no directory listing on this path.
	if (file_here(path_bell)) {
		buffer_t *bb  = data_get_blob(path_bell);
		char     *rid = NULL;
		if (bb != NULL) {
			rid = sys_buffer_to_string(bb); // copy out before the eviction frees bb
			data_delete_blob(path_bell);    // mandatory: data_get_blob memoises by path
		}
		del_file(path_bell);
		if (rid == NULL) {
			return;
		}
		char *bell_name = string("%s.json", trim_end(rid));
		if (!file_here(string("%s/%s", dir_req, bell_name))) {
			return; // already answered, or abandoned by its server
		}
		// One REQUEST is opened per frame; a batch request runs several items per
		// frame inside the call budget (see step_job).
		handle_one(bell_name);
	}
}

// Drawn ONLY while the Plugins tab is visible (tab_plugins.c:27), so it can show
// state but must never be the only way to observe it -- everything here is also
// in heartbeat.json and get_app_info.
void on_ui() {
	if (ui_panel(h_panel, "MCP Bridge", 0, 0, 0)) {
		// The checkbox owns the flag. bridge_set_enabled writes h_enabled->b too,
		// so the widget and the server never disagree.
		enabled = ui_check(h_enabled, "Enabled", "");
		if (enabled) {
			ui_text("status: listening", UI_ALIGN_LEFT, 0);
		}
		else {
			ui_text("status: stopped, app may sleep", UI_ALIGN_LEFT, 0);
		}
		// Everything below draws a PRE-BUILT string -- no string() at frame rate.
		ui_text(ui_stats, UI_ALIGN_LEFT, 0);
		ui_row2();
		ui_text("last op", UI_ALIGN_LEFT, 0);
		ui_text(last_op, UI_ALIGN_LEFT, 0);
		ui_row2();
		ui_text("last id", UI_ALIGN_LEFT, 0);
		ui_text(last_id, UI_ALIGN_LEFT, 0);
		ui_text("last error", UI_ALIGN_LEFT, 0);
		ui_text(last_error, UI_ALIGN_LEFT, 0);
		ui_separator(4, 0);
		ui_text("spool", UI_ALIGN_LEFT, 0);
		ui_text(spool_root, UI_ALIGN_LEFT, 0);
		ui_text(ui_version, UI_ALIGN_LEFT, 0);
		if (ui_button("Open spool folder", UI_ALIGN_CENTER, "")) {
			file_start(spool_root);
		}
		if (ui_button("Clear error", UI_ALIGN_CENTER, "")) {
			last_error = NO_ERROR;
		}
	}
}

void on_delete() {
	enabled = 0;
	busy    = 0;
	// Leave a final heartbeat saying so, so the server reports "bridge stopped"
	// rather than "bridge crashed" when t stops advancing.
	write_heartbeat();
	del_file(path_lock);
}

// main MUST be last: minic's registration pass stops dead at main (minic.c:2051)
// and any function defined below it is never registered.
void main() {
	plugin       = plugin_create();
	h_panel      = ui_handle_create();
	h_enabled    = ui_handle_create();
	h_enabled->b = enabled;

	// data_path() is "./data" plus the platform separator, which is a backslash
	// on Windows. Normalise it: backslashes inside minic string literals are
	// escape sequences and any unrecognised one truncates the string at that
	// point, so every path this plugin handles stays forward-slashed.
	is_windows = string_index_of(data_path(), "\\") >= 0;
	if (!is_windows) {
		is_macos = iron_is_directory("/System/Library");
	}
	char *base = string_replace_all(data_path(), "\\", "/");
	spool_root = string("%smcp_spool", base);

	// Linux/macOS: an absolute per-user spool, the same path the server derives
	// from $HOME (transport.py _per_user_spool). Why it cannot stay relative:
	// see find_home().
	char *home = NULL;
	if (!is_windows) {
		if (writable_dir("/root")) {
			home = "/root";
		}
		if (home == NULL) {
			home = find_home("/home");
		}
		if (home == NULL) {
			home = find_home("/var/home");
		}
		if (home == NULL) {
			home = find_home("/Users");
		}
		if (home == NULL) {
			console_error(string("armorpaint-mcp: could not find a writable home directory; falling back to %s, which will not work on this platform", spool_root));
		}
		else if (iron_is_directory(string("%s/Library/Application Support", home))) {
			spool_root = string("%s/Library/Application Support/armorpaint-mcp/spool", home);
		}
		else {
			spool_root = string("%s/.local/share/armorpaint-mcp/spool", home);
		}
	}
	dir_req    = string("%s/req", spool_root);
	dir_res    = string("%s/res", spool_root);

	// Created parent-first: on Windows iron_create_directory does not build
	// intermediates. On POSIX it is mkdir -p, so the first call builds the whole
	// per-user path.
	iron_create_directory(spool_root);
	iron_create_directory(dir_req);
	iron_create_directory(dir_res);

	path_heartbeat = string("%s/heartbeat.json", spool_root);
	path_lock      = string("%s/bridge.lock", spool_root);
	path_bell      = string("%s/doorbell", spool_root);
	path_settings  = string("%s/bridge_settings.json", spool_root);

	// Doze default: on, except on macOS where waking a sleeping app from the
	// server is implemented but untested. bridge_set_idle overrides and persists.
	if (is_macos) {
		linger = -1.0;
	}
	if (file_here(path_settings)) {
		buffer_t *sb = data_get_blob(path_settings);
		if (sb != NULL) {
			char *st = sys_buffer_to_string(sb);
			data_delete_blob(path_settings);
			if (json_sane(st) == 1) {
				void *sm = json_parse_to_map(st);
				char *lv = any_map_get(sm, "linger");
				if (lv != NULL) {
					linger = to_float(lv);
				}
			}
		}
	}
	last_activity = sys_time();
	ui_version     = string("bridge %s", BRIDGE_VERSION);

	// Drain anything still sitting in req/ from a previous run.
	//
	// The server refuses to write a request unless it can already see a live
	// heartbeat, so nothing found here at start can belong to a live session --
	// these are requests whose server was killed before its own timeout could
	// unlink them. Without this they would be executed now, replaying a
	// destructive op nobody asked for (project_open discarding unsaved work,
	// say) minutes or days after it was abandoned.
	any_array_t *stale = file_read_directory(dir_req);
	if (stale != NULL) {
		int   sn = stale->length;
		char *snm;
		for (int si = 0; si < sn; ++si) {
			snm = stale->buffer[si];
			if (snm != NULL) {
				if (ends_with(snm, ".json")) {
					del_file(string("%s/%s", dir_req, snm));
				}
			}
		}
		array_free(stale);
		free(stale);
	}
	del_file(path_bell); // may name one of the requests just drained

	json_encode_begin();
	json_encode_i32("v", ENVELOPE_V);
	json_encode_string("bridge_version", BRIDGE_VERSION);
	json_encode_string("spool", jesc(spool_root));
	json_encode_f32("started_t", sys_time());
	iron_file_save_bytes(path_lock, sys_string_to_buffer(json_encode_end()), 0);

	// Probe for the optional native extension (patch/apply_ext_patch.py). On a
	// stock build this prints exactly one "unknown function" error, announced
	// first so nobody chases it.
	console_info("armorpaint-mcp: probing for the optional native extension (mcp_ext_call). On a stock ArmorPaint the next line is an 'unknown function' error -- that is expected and harmless.");
	char *probe = try_ext("ext_info", NULL, "");
	if (probe == NULL) {
		ext_state = 0;
	}
	else {
		ext_state = 1;
		if (starts_with(probe, "1")) {
			void *pm  = json_parse_to_map(substring(probe, 1, string_length(probe)));
			char *eo  = any_map_get(pm, "ops");
			if (eo != NULL) {
				ext_ops = eo; // a heap substring owned by the map, which is never freed
			}
			ext_version = to_int(any_map_get(pm, "ext_version"));
		}
		console_info(string("armorpaint-mcp: native extension v%d present", ext_version));
	}

	write_heartbeat();

	// The spool path is relative to ArmorPaint's working directory, so log it:
	// this line is how a user tells the server where to look.
	console_info(string("armorpaint-mcp bridge %s listening on %s", BRIDGE_VERSION, spool_root));

	plugin_notify_on_ui(plugin, on_ui);
	plugin_notify_on_update(plugin, on_update);
	plugin_notify_on_delete(plugin, on_delete);
}
