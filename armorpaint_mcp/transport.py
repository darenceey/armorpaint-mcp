"""
ArmorPaint MCP transport — the file-mailbox client.
===================================================

This is the **server half** of ``docs/PROTOCOL.md`` (wire protocol v1). The other half is a
minic plugin running inside ArmorPaint. They never share a socket: ArmorPaint's plugin API
exposes no inbound listener, so the two halves talk through files in a spool directory.

Everything in here exists because of one of four facts about the plugin side:

1. **The plugin cannot write atomically** (no rename/move binding exists in the 529). So a
   response arrives as a *two-file commit*: ``res/<id>.json`` (body) then ``res/<id>.done``
   (a marker holding the body's decimal byte length). We ignore a body with no marker, and
   we verify the length — a mismatch means a torn write, which we retry, bounded.
2. **Python *can* write atomically**, so requests go out via ``os.replace()``.
3. **``json_parse_to_map`` on the plugin side is flat, string-typed and array-hostile**
   (``iron_json.c:297``): nested objects are flattened into the top-level namespace, arrays
   corrupt the remainder of the parse, every value comes back as a ``char *`` with escapes
   *undecoded*, and key detection requires no whitespace before the colon. Therefore this
   module emits **compact, flat, all-string JSON with no arrays**, and argument keys carry
   an ``a_`` prefix so an argument named ``id`` or ``op`` cannot clobber the envelope.
   (See ``docs/MINIC_DIALECT_AND_API.md`` §2.9 and Appendix A, which corrects the nested
   ``"args": {...}`` shown in ``PROTOCOL.md`` §Envelopes.)
4. **The plugin runs inline on the render thread**, with no threads and no exceptions. It
   opens one request per frame, and a ``batch`` request runs several of its items per frame
   inside an arena budget (:func:`send_batch`). So: small payloads, bulk data by path, and
   every argument validated *here* before it can reach an unchecked binding over there.

Request envelope actually written::

    {"v":"1","id":"1757280000-3","op":"select_tool","deadline_ms":"30000","a_tool":"0"}

Response envelope expected (the plugin builds it with ``string()``, so real JSON — nesting
and numbers are fine coming back; we parse leniently)::

    {"v":1,"id":"1757280000-3","ok":true,"result":{...},"elapsed_ms":12}
    {"v":1,"id":"1757280000-3","ok":false,"error":{"code":"no_project","message":"..."}}

Liveness
--------
``heartbeat.json``'s ``t`` is ``sys_time()`` — **seconds since app start, monotonic within a
run, NOT wall clock**. It is never compared to the system clock. Liveness means: read the
file twice and see ``t`` change. A *decrease* means ArmorPaint restarted (still alive).

While waiting for a reply we use a sharper signal than a bare timeout:

* heartbeat file missing            -> the bridge was never started (fail immediately)
* heartbeat says ``"dozing": true`` -> the bridge let ArmorPaint sleep between requests (it
  no longer holds the app awake at full frame rate). A sleeping app runs no plugin code, so
  this server WAKES it with a synthetic 1-pixel pointer move before writing the request, and
  again if the request is not picked up (``desktop_input.wake``).
* heartbeat frozen AND our request still on disk, and not dozing -> the plugin is not
  polling: a modal dialog, a hang, or a very long frame. Reported after ``STALL_LIMIT_S``.
* heartbeat frozen BUT our request was consumed -> a long handler is blocking the render
  thread inline, which also freezes the heartbeat. This is expected; keep waiting.

Spool directory discovery (first hit wins)
------------------------------------------
1. ``$ARMORPAINT_SPOOL`` — an absolute path to the spool directory itself.
2. A config file: ``$ARMORPAINT_MCP_CONFIG``, else ``%APPDATA%/armorpaint-mcp/config.json``,
   ``~/.config/armorpaint-mcp/config.json``, ``~/.armorpaint-mcp.json``. Recognised keys:
   ``{"spool": "<abs path>"}`` or ``{"armorpaint_dir": "<install root>"}``.
3. **Linux / macOS:** the per-user spool, ``~/.local/share/armorpaint-mcp/spool`` (macOS:
   ``~/Library/Application Support/armorpaint-mcp/spool``). This is the plugin's own
   default there: it cannot use a relative path, because Iron resolves relative *reads*
   against the executable's directory and relative *writes* against the working
   directory, so it finds the user's home and uses this absolute path instead.
4. **Windows:** ``$ARMORPAINT_DIR`` / ``$ARMORPAINT_EXE``, then a short list of common
   install roots, each accepted only if it really contains ``data/plugins``. The spool is
   ``<install>/data/mcp_spool`` — the plugin's own default, because ``data_path()``
   (``engine.c:1782``) is the only stable directory a plugin can name.
5. Windows last resort: a per-user directory (``%LOCALAPPDATA%/armorpaint-mcp/spool``).
   This only works if the plugin is pointed at the same path, so errors say so out loud.

Nothing in this module blocks without a deadline.
"""

from __future__ import annotations

import json
import math
import os
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Protocol constants (PROTOCOL.md)
# ---------------------------------------------------------------------------

ENVELOPE_VERSION = "1"
# Bridge 2 polls the doorbell instead of listing req/; bridge 1 lists (and leaks a file
# descriptor per listing on Linux/macOS) but still works with this server.
SUPPORTED_BRIDGE_MAJORS = (1, 2)
ARG_PREFIX = "a_"
RESERVED_ENVELOPE_KEYS = frozenset({"v", "id", "op", "deadline_ms"})

SPOOL_LEAF = "mcp_spool"
REQ_DIR = "req"
RES_DIR = "res"
HEARTBEAT_FILE = "heartbeat.json"
LOCK_FILE = "bridge.lock"
# The id of the newest request (PROTOCOL.md "The doorbell"). The bridge polls this one
# file instead of listing req/, because on Linux/macOS every directory listing inside
# ArmorPaint leaks a file descriptor (Iron's POSIX close_dir() is empty).
DOORBELL_FILE = "doorbell"

# Poll cadence — PROTOCOL.md "Poll cadence": 5 ms for the first 200 ms, then 25 ms,
# then 100 ms after 2 s.
POLL_FAST_S = 0.005
POLL_FAST_WINDOW_S = 0.200
POLL_MEDIUM_S = 0.025
POLL_MEDIUM_WINDOW_S = 2.0
POLL_SLOW_S = 0.100

# Timeouts. Every call has one; nothing waits forever.
DEFAULT_TIMEOUT_S = 30.0
MIN_TIMEOUT_S = 0.5
MAX_TIMEOUT_S = 900.0

# Liveness gates while waiting for a reply.
LIVENESS_FIRST_CHECK_S = 1.0   # don't bother the filesystem before this
LIVENESS_WINDOW_S = 2.5        # heartbeat is ~1 Hz, so this is >2 rewrites
# How long a frozen heartbeat with our request still unread is tolerated before it is
# reported. A single ArmorPaint frame can legitimately take seconds (a normal-map bake on a
# software GPU measured ~3 s), so one frozen window is not proof of anything.
STALL_LIMIT_S = 8.0
WAKE_RETRY_S = 1.0
# Re-ring the doorbell this often while our request is still unclaimed: two servers
# ringing at once lose one ring (last write wins).
RERING_S = 0.25
WAKE_MAX_TRIES = 5
HEARTBEAT_PROBE_TIMEOUT_S = 4.0
HEARTBEAT_PROBE_POLL_S = 0.05

# Torn-read handling for the two-file commit.
TORN_READ_RETRIES = 6
TORN_READ_BACKOFF_S = 0.005

# Pending-job follow-up (PROTOCOL.md §Concurrency).
PENDING_STATUS_OP = "job_status"
PENDING_POLL_S = 0.25

# Housekeeping: orphaned response bodies from a crashed run.
ORPHAN_MAX_AGE_S = 24 * 3600

_ID_RE = re.compile(r"^[0-9]+-[0-9]+$")
_OP_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_ARG_RE = re.compile(r"^[a-z][a-z0-9_]{0,47}$")
# The plugin does not decode JSON escapes, so anything needing one cannot cross the wire.
_UNSAFE_STR_RE = re.compile(r'["\\\x00-\x1f\x7f]')


# ---------------------------------------------------------------------------
# Errors — every one is actionable and names the spool
# ---------------------------------------------------------------------------


class BridgeError(Exception):
    """Base class. ``to_dict()`` is what an MCP tool hands back to the caller."""

    code = "transport_error"

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.message = message
        self.details = {k: v for k, v in details.items() if v is not None}

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"ok": False, "error": self.message, "code": self.code}
        out.update(self.details)
        return out


class SpoolNotFound(BridgeError):
    code = "spool_not_found"


class BridgeNotRunning(BridgeError):
    code = "bridge_not_running"


class BridgeNotResponding(BridgeError):
    code = "bridge_not_responding"


class BridgeVersionMismatch(BridgeError):
    code = "bridge_version_mismatch"


class RequestTimeout(BridgeError):
    code = "timeout"


class ProtocolError(BridgeError):
    code = "protocol_error"


class BadArgs(BridgeError):
    """Rejected before anything is written — a bad request must never reach the plugin."""

    code = "bad_args"


class OpFailed(BridgeError):
    """The bridge answered ``ok:false``. ``code`` is the plugin's own error code."""

    def __init__(self, op: str, code: str, message: str, **details: Any) -> None:
        super().__init__(message or f"'{op}' failed with code '{code}'.", op=op, **details)
        self.code = code or "internal"


# ---------------------------------------------------------------------------
# Spool discovery
# ---------------------------------------------------------------------------

SPOOL_ENV = "ARMORPAINT_SPOOL"
CONFIG_ENV = "ARMORPAINT_MCP_CONFIG"
INSTALL_DIR_ENV = "ARMORPAINT_DIR"
INSTALL_EXE_ENV = "ARMORPAINT_EXE"


@dataclass
class SpoolResolution:
    path: Path
    source: str
    trace: list[str] = field(default_factory=list)
    is_fallback: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "spool": str(self.path),
            "resolved_from": self.source,
            "is_last_resort_default": self.is_fallback,
            "resolution_trace": self.trace,
        }


def _config_file_candidates() -> list[Path]:
    out: list[Path] = []
    explicit = os.environ.get(CONFIG_ENV, "").strip()
    if explicit:
        out.append(Path(explicit).expanduser())
    appdata = os.environ.get("APPDATA", "").strip()
    if appdata:
        out.append(Path(appdata) / "armorpaint-mcp" / "config.json")
    out.append(Path.home() / ".config" / "armorpaint-mcp" / "config.json")
    out.append(Path.home() / ".armorpaint-mcp.json")
    return out


def _install_root_candidates() -> list[Path]:
    """Windows only: off Windows the spool does not live under the install."""
    out: list[Path] = []
    env_dir = os.environ.get(INSTALL_DIR_ENV, "").strip()
    if env_dir:
        out.append(Path(env_dir).expanduser())
    env_exe = os.environ.get(INSTALL_EXE_ENV, "").strip()
    if env_exe:
        out.append(Path(env_exe).expanduser().parent)
    local = os.environ.get("LOCALAPPDATA", "").strip()
    if local:
        out.append(Path(local) / "Programs" / "ArmorPaint")
    for var in ("PROGRAMFILES", "PROGRAMFILES(X86)"):
        base = os.environ.get(var, "").strip()
        if base:
            out.append(Path(base) / "ArmorPaint")
    out.append(Path("C:/ArmorPaint"))
    out.append(Path.home() / "ArmorPaint")
    out.append(Path.home() / "Documents" / "ArmorPaint")
    return out


def _per_user_spool() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA", "").strip()
        if base:
            return Path(base) / "armorpaint-mcp" / "spool"
    elif sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "armorpaint-mcp" / "spool"
    return Path.home() / ".local" / "share" / "armorpaint-mcp" / "spool"


def resolve_spool(explicit: str | os.PathLike[str] | None = None) -> SpoolResolution:
    """Work out where the mailbox lives. Never raises; records how it decided."""
    trace: list[str] = []

    if explicit:
        p = Path(explicit).expanduser()
        trace.append(f"explicit argument -> {p}")
        return SpoolResolution(p, "explicit argument", trace)

    env_spool = os.environ.get(SPOOL_ENV, "").strip()
    if env_spool:
        p = Path(env_spool).expanduser()
        trace.append(f"${SPOOL_ENV} -> {p}")
        return SpoolResolution(p, f"${SPOOL_ENV}", trace)
    trace.append(f"${SPOOL_ENV} not set")

    for cfg in _config_file_candidates():
        if not cfg.is_file():
            trace.append(f"config {cfg}: absent")
            continue
        try:
            data = json.loads(cfg.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            trace.append(f"config {cfg}: unreadable ({exc})")
            continue
        if not isinstance(data, dict):
            trace.append(f"config {cfg}: not a JSON object")
            continue
        spool = data.get("spool")
        if isinstance(spool, str) and spool.strip():
            p = Path(spool).expanduser()
            trace.append(f"config {cfg} key 'spool' -> {p}")
            return SpoolResolution(p, f"config file {cfg}", trace)
        root = data.get("armorpaint_dir")
        if isinstance(root, str) and root.strip() and sys.platform != "win32":
            trace.append(
                f"config {cfg} key 'armorpaint_dir' ignored: off Windows the plugin does not "
                f"keep its spool under the install directory"
            )
        elif isinstance(root, str) and root.strip():
            p = Path(root).expanduser() / "data" / SPOOL_LEAF
            trace.append(f"config {cfg} key 'armorpaint_dir' -> {p}")
            return SpoolResolution(p, f"config file {cfg}", trace)
        trace.append(f"config {cfg}: no 'spool' or 'armorpaint_dir' key")

    if sys.platform != "win32":
        # Must match main() in plugin/armorpaint_mcp_bridge.c, which derives the
        # same absolute path from the user's home directory.
        p = _per_user_spool()
        trace.append(f"per-user spool (the plugin's default on this platform) -> {p}")
        return SpoolResolution(p, "per-user default", trace)

    for root in _install_root_candidates():
        if (root / "data" / "plugins").is_dir():
            p = root / "data" / SPOOL_LEAF
            trace.append(f"install root {root} (has data/plugins) -> {p}")
            return SpoolResolution(p, f"ArmorPaint install at {root}", trace)
        trace.append(f"install root {root}: no data/plugins")

    p = _per_user_spool()
    trace.append(f"last-resort per-user default -> {p}")
    return SpoolResolution(p, "last-resort default", trace, is_fallback=True)


_spool_lock = threading.Lock()
_spool_cache: SpoolResolution | None = None


def spool_resolution(refresh: bool = False) -> SpoolResolution:
    global _spool_cache
    with _spool_lock:
        if _spool_cache is None or refresh:
            _spool_cache = resolve_spool()
        return _spool_cache


def spool_dir(refresh: bool = False) -> Path:
    return spool_resolution(refresh).path


def _hint_where_to_point() -> str:
    if sys.platform != "win32":
        return (
            f"The plugin logs 'armorpaint-mcp bridge ... listening on <path>' to the ArmorPaint "
            f"console at start. If that path differs from {_per_user_spool()}, set ${SPOOL_ENV} "
            f"to it."
        )
    return (
        f"Set ${SPOOL_ENV} to the spool directory the plugin created (it prints the path to the "
        f"ArmorPaint console at start), or write {{\"armorpaint_dir\": \"<install root>\"}} into "
        f"%APPDATA%/armorpaint-mcp/config.json."
    )


# ---------------------------------------------------------------------------
# Heartbeat
# ---------------------------------------------------------------------------


def read_heartbeat(spool: Path | None = None) -> dict[str, Any] | None:
    """Read ``heartbeat.json``, tolerating a torn write. ``None`` if absent/unreadable.

    The plugin cannot write this file atomically either, so a partial read is normal and
    is retried rather than reported.
    """
    root = spool or spool_dir()
    path = root / HEARTBEAT_FILE
    for attempt in range(TORN_READ_RETRIES):
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError:
            return None
        if raw:
            try:
                data = json.loads(raw.decode("utf-8", errors="strict"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                data = None
            if isinstance(data, dict):
                return data
        if attempt + 1 < TORN_READ_RETRIES:
            time.sleep(TORN_READ_BACKOFF_S)
    return None


def _hb_t(hb: dict[str, Any] | None) -> float | None:
    if not hb:
        return None
    t = hb.get("t")
    if isinstance(t, bool):
        return None
    if isinstance(t, (int, float)):
        return float(t)
    if isinstance(t, str):
        try:
            return float(t.strip())
        except ValueError:
            return None
    return None


def _check_bridge_version(hb: dict[str, Any], spool: Path) -> None:
    ver = hb.get("bridge_version")
    if not isinstance(ver, str) or not ver.strip():
        return
    head = ver.strip().split(".", 1)[0]
    try:
        major = int(head)
    except ValueError:
        return
    if major not in SUPPORTED_BRIDGE_MAJORS:
        raise BridgeVersionMismatch(
            f"The ArmorPaint bridge reports version {ver}, but this server speaks bridge "
            f"majors {', '.join(map(str, SUPPORTED_BRIDGE_MAJORS))}. Update whichever half is "
            f"older; they will not interoperate.",
            bridge_version=ver,
            server_supports_major=list(SUPPORTED_BRIDGE_MAJORS),
            spool=str(spool),
        )


def probe_liveness(
    spool: Path | None = None,
    timeout_s: float = HEARTBEAT_PROBE_TIMEOUT_S,
) -> dict[str, Any]:
    """Prove the bridge is alive by watching ``t`` change between two reads.

    ``t`` is monotonic within a run only, so it is never compared to the wall clock. A
    decrease is treated as evidence of an ArmorPaint restart, which is still alive.
    Raises :class:`BridgeNotRunning` / :class:`BridgeNotResponding` / version mismatch.
    """
    root = spool or spool_dir()
    res = spool_resolution()
    first = read_heartbeat(root)
    if first is None:
        raise BridgeNotRunning(
            f"No {HEARTBEAT_FILE} in {root}. Either ArmorPaint is not running, the MCP "
            f"bridge plugin is not enabled (Plugins tab), or this server is looking at the "
            f"wrong spool directory. " + _hint_where_to_point(),
            spool=str(root),
            resolved_from=res.source,
            resolution_trace=res.trace if res.is_fallback else None,
        )
    _check_bridge_version(first, root)
    t0 = _hb_t(first)
    woke: dict[str, Any] | None = None
    if _is_dozing(first):
        # Asleep between requests by design; t only advances once it is awake again.
        woke = wake_armorpaint(first)

    deadline = time.monotonic() + max(0.1, timeout_s)
    while time.monotonic() < deadline:
        time.sleep(HEARTBEAT_PROBE_POLL_S)
        again = read_heartbeat(root)
        t1 = _hb_t(again)
        if again is None:
            continue
        if t0 is None or t1 is None:
            # A heartbeat without a usable 't' cannot prove liveness.
            continue
        if t1 != t0:
            return {
                "alive": True,
                "restarted": t1 < t0,
                "t_first": t0,
                "t_second": t1,
                "bridge_version": again.get("bridge_version"),
                "app_version": again.get("app_version"),
                "project": again.get("project"),
                "busy": again.get("busy"),
                "dozing_before_probe": _is_dozing(first),
                "wake": woke,
                "spool": str(root),
            }

    raise BridgeNotResponding(
        f"{HEARTBEAT_FILE} exists in {root} but its 't' has not advanced in "
        f"{timeout_s:.1f}s, so the plugin's on_update is not running. Likely causes: "
        f"ArmorPaint is dozing and could not be woken (see 'wake'; move the pointer over its "
        f"window, or call ap_bridge_set_idle with linger -1 so it never dozes), a modal "
        f"dialog is open, or the app is hung. A stale heartbeat also looks like this after "
        f"a crash.",
        spool=str(root),
        heartbeat=first,
        wake=woke,
    )


def _is_dozing(hb: dict[str, Any] | None) -> bool:
    """True when the bridge is not holding ArmorPaint awake (so the app may be asleep)."""
    if not hb:
        return False
    return _as_bool(hb.get("dozing")) is True or _as_bool(hb.get("enabled")) is False


def wake_armorpaint(hb: dict[str, Any] | None = None) -> dict[str, Any]:
    """Wake a dozing ArmorPaint. Never raises; the result says what happened."""
    try:
        try:
            from .desktop_input import InputError, wake
        except ImportError:
            from desktop_input import InputError, wake  # type: ignore[no-redef]
    except Exception as exc:  # pragma: no cover - import failure is environmental
        return {"woke": False, "error": f"no input backend: {exc}"}
    title = (hb or {}).get("app_title") or None
    try:
        return wake(title if isinstance(title, str) else None)
    except InputError as exc:
        return {"woke": False, "code": exc.code, "error": exc.message}
    except Exception as exc:  # never let a wake attempt take a request down
        return {"woke": False, "error": f"{type(exc).__name__}: {exc}"}


# ---------------------------------------------------------------------------
# Request id minting
# ---------------------------------------------------------------------------

_id_lock = threading.Lock()
_id_seed = int(time.time())
_id_counter = 0


def mint_id() -> str:
    """Monotonic, never-reused id, seeded from the wall clock at import.

    Never reusing a name matters: ``data_get_blob`` memoises by path forever
    (``engine.c:1879``), so a recycled filename risks serving stale bytes to the plugin.
    """
    global _id_counter
    with _id_lock:
        _id_counter += 1
        n = _id_counter
    return f"{_id_seed}-{n}"


# ---------------------------------------------------------------------------
# Argument encoding
# ---------------------------------------------------------------------------


def _fmt_float(value: float, arg: str) -> str:
    if math.isnan(value) or math.isinf(value):
        raise BadArgs(f"'{arg}' must be a finite number (got {value!r}).", arg=arg)
    text = f"{value:.6f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    if text in ("", "-0", "-"):
        text = "0"
    return text


def encode_value(value: Any, arg: str) -> str:
    """Coerce one argument to the flat string form the plugin can actually parse.

    * ``bool`` -> ``"true"``/``"false"`` (the plugin compares with ``string_equals``)
    * ``int``  -> decimal (the plugin converts with its own ``to_int``; there is no ``atoi``)
    * ``float``-> fixed-point, 6 dp, **never exponent notation** (``1e-05`` would defeat a
      hand-written ``to_float``)
    * ``str``  -> verbatim, but rejected if it would need a JSON escape, because the
      plugin's parser does not decode escapes (``iron_json.c``)
    * lists/dicts -> rejected: an array anywhere in the document corrupts the rest of the
      plugin-side parse. Callers flatten to a delimited string instead.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return _fmt_float(value, arg)
    if isinstance(value, str):
        bad = _UNSAFE_STR_RE.search(value)
        if bad:
            ch = bad.group(0)
            raise BadArgs(
                f"'{arg}' contains a character that cannot cross this wire ({ch!r}). The "
                f"plugin's JSON parser does not decode escapes, so quotes, backslashes and "
                f"control characters are forbidden. Use forward slashes in paths.",
                arg=arg,
            )
        return value
    if isinstance(value, (list, tuple, dict)):
        raise BadArgs(
            f"'{arg}' must be a scalar. JSON arrays and nested objects corrupt the plugin's "
            f"parse; pass a delimited string instead.",
            arg=arg,
        )
    raise BadArgs(f"'{arg}' has unsupported type {type(value).__name__}.", arg=arg)


def build_request(op: str, args: dict[str, Any] | None, rid: str, timeout_s: float) -> bytes:
    """Serialise the request envelope: flat, compact, all-string, no arrays."""
    if not isinstance(op, str) or not _OP_RE.match(op):
        raise BadArgs(f"Illegal op name {op!r}; expected lowercase snake_case.", op=op)

    body: dict[str, str] = {
        "v": ENVELOPE_VERSION,
        "id": rid,
        "op": op,
        "deadline_ms": str(int(timeout_s * 1000)),
    }
    for name, value in (args or {}).items():
        if value is None:
            continue  # an omitted optional argument, not an error
        if not isinstance(name, str) or not _ARG_RE.match(name):
            raise BadArgs(f"Illegal argument name {name!r}; expected lowercase snake_case.", arg=name)
        key = ARG_PREFIX + name
        if key in RESERVED_ENVELOPE_KEYS:
            raise BadArgs(f"Argument {name!r} collides with the envelope.", arg=name)
        body[key] = encode_value(value, name)

    return _serialise(body)


# The plugin's json_sane() refuses a body larger than this.
MAX_REQUEST_BYTES = 16384
MAX_BATCH_ITEMS = 64


def _serialise(body: dict[str, str]) -> bytes:
    # Compact separators are mandatory: the plugin detects a key by testing for ':'
    # immediately after the closing quote (iron_json.c:91).
    data = json.dumps(body, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    if len(data) > MAX_REQUEST_BYTES:
        raise BadArgs(
            f"The request is {len(data)} bytes; the bridge refuses bodies over "
            f"{MAX_REQUEST_BYTES}. Split the batch, or pass bulk data by path."
        )
    return data


def build_batch_request(
    items: list[tuple[str, dict[str, Any]]], rid: str, timeout_s: float, stop_on_error: bool
) -> bytes:
    """Serialise an ordered batch into ONE flat request.

    JSON arrays cannot cross this wire (they corrupt the plugin's parse), and nested objects
    are flattened into one namespace, so item ``i`` is spelled out as ``b<i>_op`` plus
    ``b<i>_a_<name>`` keys. The plugin runs the items in order, several per frame while its
    per-frame script-call budget allows, and answers once with every item's result.
    """
    if not items:
        raise BadArgs("A batch needs at least one step.")
    if len(items) > MAX_BATCH_ITEMS:
        raise BadArgs(f"A batch holds at most {MAX_BATCH_ITEMS} steps (got {len(items)}).")
    body: dict[str, str] = {
        "v": ENVELOPE_VERSION,
        "id": rid,
        "op": "batch",
        "deadline_ms": str(int(timeout_s * 1000)),
        ARG_PREFIX + "count": str(len(items)),
        ARG_PREFIX + "stop_on_error": "true" if stop_on_error else "false",
    }
    for i, (op, args) in enumerate(items):
        if not isinstance(op, str) or not _OP_RE.match(op) or op == "batch":
            raise BadArgs(f"Step {i}: illegal op name {op!r}.", step=i)
        body[f"b{i}_op"] = op
        for name, value in (args or {}).items():
            if value is None:
                continue
            if not isinstance(name, str) or not _ARG_RE.match(name):
                raise BadArgs(f"Step {i}: illegal argument name {name!r}.", step=i, arg=name)
            body[f"b{i}_{ARG_PREFIX}{name}"] = encode_value(value, name)
    return _serialise(body)


# ---------------------------------------------------------------------------
# The mailbox
# ---------------------------------------------------------------------------


def _ensure_dirs(spool: Path) -> None:
    try:
        (spool / REQ_DIR).mkdir(parents=True, exist_ok=True)
        (spool / RES_DIR).mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise SpoolNotFound(
            f"Cannot create the request/response directories under {spool}: {exc}. "
            + _hint_where_to_point(),
            spool=str(spool),
        ) from exc


def ring_doorbell(spool: Path, rid: str) -> None:
    """Tell the bridge which request to open. Best effort: a failed ring is retried."""
    bell = spool / DOORBELL_FILE
    tmp = spool / f"{DOORBELL_FILE}.{os.getpid()}.tmp"
    try:
        tmp.write_bytes(rid.encode("ascii"))
        os.replace(tmp, bell)
    except OSError:
        # Windows refuses to replace a file ArmorPaint has open at that instant.
        _unlink(tmp)


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


_swept = False


def sweep_orphans(spool: Path, max_age_s: float = ORPHAN_MAX_AGE_S) -> int:
    """Remove long-dead response bodies and temp files from a crashed run.

    Conservative on purpose: only files older than a day, so it can never race a live
    request (the longest op here is capped well below that).
    """
    removed = 0
    now = time.time()
    # req/*.json is swept too: a server killed before its own timeout leaves one
    # behind, and the bridge would otherwise execute it on ArmorPaint's next
    # start. The bridge drains req/ at startup for the same reason; this is the
    # other half, for when the bridge is already running. The 24h floor keeps it
    # clear of any live request (the longest op deadline here is 900 s).
    for sub, patterns in ((RES_DIR, ("*.json", "*.done")), (REQ_DIR, ("*.json.tmp", "*.json"))):
        directory = spool / sub
        if not directory.is_dir():
            continue
        for pattern in patterns:
            for path in directory.glob(pattern):
                try:
                    if now - path.stat().st_mtime > max_age_s:
                        path.unlink()
                        removed += 1
                except OSError:
                    pass
    return removed


def _poll_interval(waited_s: float) -> float:
    if waited_s < POLL_FAST_WINDOW_S:
        return POLL_FAST_S
    if waited_s < POLL_MEDIUM_WINDOW_S:
        return POLL_MEDIUM_S
    return POLL_SLOW_S


def _read_committed_response(res_dir: Path, rid: str) -> dict[str, Any] | None:
    """Read a two-file commit. ``None`` if not committed yet; raises if it stays torn.

    The marker holds the body's decimal byte length. Because the plugin's body write
    completes before the marker write begins, a visible marker implies a complete body —
    but a truncated write that still returned would also produce a marker, so the length
    is verified rather than trusted.
    """
    done_path = res_dir / f"{rid}.done"
    body_path = res_dir / f"{rid}.json"

    last_problem = ""
    for attempt in range(TORN_READ_RETRIES):
        try:
            marker = done_path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError as exc:
            last_problem = f"marker unreadable: {exc}"
            time.sleep(TORN_READ_BACKOFF_S)
            continue

        marker_text = marker.decode("ascii", errors="replace").strip()
        if not marker_text:
            last_problem = "marker is empty (write not flushed yet)"
            time.sleep(TORN_READ_BACKOFF_S)
            continue
        try:
            expected = int(marker_text)
        except ValueError:
            last_problem = f"marker is not a decimal length: {marker_text[:32]!r}"
            time.sleep(TORN_READ_BACKOFF_S)
            continue

        try:
            raw = body_path.read_bytes()
        except FileNotFoundError:
            last_problem = "marker present but body missing"
            time.sleep(TORN_READ_BACKOFF_S)
            continue
        except OSError as exc:
            last_problem = f"body unreadable: {exc}"
            time.sleep(TORN_READ_BACKOFF_S)
            continue

        if len(raw) != expected:
            last_problem = f"length mismatch: marker says {expected}, body is {len(raw)} bytes"
            time.sleep(TORN_READ_BACKOFF_S)
            continue

        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            last_problem = f"body is not valid JSON: {exc}"
            time.sleep(TORN_READ_BACKOFF_S)
            continue

        if not isinstance(parsed, dict):
            raise ProtocolError(
                f"Response for id {rid} is a {type(parsed).__name__}, not a JSON object.",
                request_id=rid,
            )
        return parsed

    raise ProtocolError(
        f"Response for id {rid} never settled after {TORN_READ_RETRIES} reads "
        f"({last_problem}). The plugin's two-file commit looks broken: the .done marker "
        f"must be written last, in its own iron_file_save_bytes call, and must contain "
        f"string_length(body).",
        request_id=rid,
        detail=last_problem,
    )


def _as_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("true", "1", "yes"):
            return True
        if low in ("false", "0", "no", ""):
            return False
    return None


def _interpret(envelope: dict[str, Any], op: str, rid: str) -> dict[str, Any]:
    got_id = envelope.get("id")
    if got_id is not None and str(got_id) != rid:
        raise ProtocolError(
            f"Response in {rid}.json carries id {got_id!r}. Ids are never reused, so the "
            f"plugin echoed the wrong one.",
            request_id=rid,
            response_id=str(got_id),
        )

    version = envelope.get("v")
    if version is not None and str(version) != ENVELOPE_VERSION:
        raise ProtocolError(
            f"Response envelope version {version!r} != {ENVELOPE_VERSION}.",
            request_id=rid,
        )

    ok = _as_bool(envelope.get("ok"))
    if ok is None:
        raise ProtocolError(
            f"Response for '{op}' has no usable 'ok' field.", request_id=rid
        )

    if not ok:
        err = envelope.get("error")
        if isinstance(err, dict):
            code = str(err.get("code") or "internal")
            message = str(err.get("message") or "")
        else:
            code = "internal"
            message = str(err or "")
        extra = {}
        if code == "unsupported":
            extra["hint"] = (
                "The bridge reports this operation is not implementable against "
                "ArmorPaint's 529 plugin bindings. Nothing in the request will make it work."
            )
        elif code == "no_project":
            extra["hint"] = "Open or create a project first (ap_project_new / ap_project_open)."
        raise OpFailed(op, code, message, **extra)

    result = envelope.get("result")
    if result is None:
        result = {}
    if not isinstance(result, dict):
        result = {"value": result}
    if "elapsed_ms" in envelope and "elapsed_ms" not in result:
        result = dict(result)
        result["elapsed_ms"] = envelope["elapsed_ms"]
    return result


def _await_response(
    spool: Path,
    rid: str,
    op: str,
    timeout_s: float,
) -> dict[str, Any]:
    req_path = spool / REQ_DIR / f"{rid}.json"
    res_dir = spool / RES_DIR
    started = time.monotonic()
    deadline = started + timeout_s

    hb_snapshot_t: float | None = None
    frozen_since: float | None = None
    next_liveness_at = started + LIVENESS_FIRST_CHECK_S
    consumed = False
    wakes: list[dict[str, Any]] = []
    next_wake_at = started + WAKE_RETRY_S
    next_ring_at = started + RERING_S

    while True:
        envelope = _read_committed_response(res_dir, rid)
        if envelope is not None:
            _unlink(res_dir / f"{rid}.json")
            _unlink(res_dir / f"{rid}.done")
            return envelope

        now = time.monotonic()
        if now >= deadline:
            break

        if not consumed and now >= next_ring_at:
            next_ring_at = now + RERING_S
            if req_path.exists():
                ring_doorbell(spool, rid)
            else:
                consumed = True

        if now >= next_liveness_at:
            if not consumed:
                consumed = not req_path.exists()
            hb = read_heartbeat(spool)
            if hb is None:
                _unlink(req_path)
                raise BridgeNotRunning(
                    f"The bridge stopped: {HEARTBEAT_FILE} disappeared from {spool} while "
                    f"'{op}' was in flight. ArmorPaint probably exited.",
                    spool=str(spool),
                    op=op,
                )
            t = _hb_t(hb)
            if consumed:
                frozen_since = None
            elif hb_snapshot_t is not None and t is not None and t == hb_snapshot_t:
                frozen_since = frozen_since or now
            else:
                frozen_since = None
            hb_snapshot_t = t
            if not consumed and _is_dozing(hb) and now >= next_wake_at and len(wakes) < WAKE_MAX_TRIES:
                # The plugin dozed (possibly between our heartbeat read and the write):
                # nudge the app awake so it can see the request.
                wakes.append(wake_armorpaint(hb))
                next_wake_at = now + WAKE_RETRY_S
            if frozen_since is not None and now - frozen_since >= STALL_LIMIT_S:
                # Heartbeat frozen for a long time AND the request was never picked up:
                # the plugin is not polling. Fail now instead of burning the whole timeout.
                _unlink(req_path)
                dozing = _is_dozing(hb)
                raise BridgeNotResponding(
                    f"'{op}' was never picked up: req/{rid}.json is still on disk and the "
                    f"heartbeat has not advanced in {now - frozen_since:.1f}s. "
                    + (
                        "The bridge was dozing and waking ArmorPaint did not work (see "
                        "'wake'): move the pointer over its window, or call "
                        "ap_bridge_set_idle(linger=-1) so it never dozes."
                        if dozing
                        else "ArmorPaint is blocked on a modal dialog, hung, or stuck in "
                        "a very long frame. Try ap_bridge_status."
                    ),
                    spool=str(spool),
                    op=op,
                    heartbeat=hb,
                    wake=wakes or None,
                )
            next_liveness_at = now + (WAKE_RETRY_S if (not consumed and _is_dozing(hb)) else LIVENESS_WINDOW_S)

        time.sleep(min(_poll_interval(now - started), max(0.0, deadline - now)))

    # Timed out. Say which of the two failure modes it was — that is the whole diagnosis.
    request_consumed = not req_path.exists()
    body_present = (res_dir / f"{rid}.json").exists()
    _unlink(req_path)
    _unlink(spool / REQ_DIR / f"{rid}.json.tmp")

    if request_consumed and body_present:
        detail = (
            "The plugin wrote a response body but never committed the .done marker — it "
            "crashed or errored between the two writes. Check the ArmorPaint console."
        )
    elif request_consumed:
        detail = (
            "The plugin consumed the request (it deletes it before executing, so a crash "
            "cannot replay it) but never replied. The handler is either still running "
            "inline on the render thread or it faulted — check the ArmorPaint console for "
            "a 'plugin.c:<line>: error:' line."
        )
    else:
        detail = (
            "The request was never picked up. The bridge is not polling: check that the "
            "plugin is enabled in the Plugins tab. If ArmorPaint's window has gone grey and "
            "unresponsive, see ap_bridge_status (an old bridge on Linux/macOS runs out of "
            "file descriptors)."
        )
    raise RequestTimeout(
        f"'{op}' did not answer within {timeout_s:.1f}s. {detail}",
        op=op,
        request_id=rid,
        spool=str(spool),
        timeout_s=timeout_s,
        request_consumed=request_consumed,
    )


def send_to_armorpaint(
    op: str,
    args: dict[str, Any] | None = None,
    timeout_s: float | None = None,
    follow_pending: bool = True,
) -> dict[str, Any]:
    """Run one operation on the bridge and return its ``result`` object.

    Blocking, thread-safe, and always bounded by ``timeout_s``. Raises a
    :class:`BridgeError` subclass on every failure path; ``BridgeError.to_dict()`` is the
    payload an MCP tool should hand back.
    """
    timeout = DEFAULT_TIMEOUT_S if timeout_s is None else float(timeout_s)
    timeout = max(MIN_TIMEOUT_S, min(MAX_TIMEOUT_S, timeout))
    rid = mint_id()
    payload = build_request(op, args, rid, timeout)
    result = _send_payload(op, rid, payload, timeout)
    if follow_pending:
        result = _follow_pending(result, op, _last_spool or spool_dir(), timeout)
    return result


def send_batch(
    items: list[tuple[str, dict[str, Any]]],
    timeout_s: float | None = None,
    stop_on_error: bool = False,
) -> dict[str, Any]:
    """Run an ordered list of ``(op, wire_args)`` as ONE request.

    Returns ``{"count", "executed", "errors", "frames", "stopped_early", "results": [...]}``
    where each result is ``{"i", "op", "ok", "result"|"error"}``. A failed step does not
    raise; set ``stop_on_error`` to skip the steps after it.
    """
    timeout = DEFAULT_TIMEOUT_S if timeout_s is None else float(timeout_s)
    timeout = max(MIN_TIMEOUT_S, min(MAX_TIMEOUT_S, timeout))
    rid = mint_id()
    payload = build_batch_request(items, rid, timeout, stop_on_error)
    return _send_payload("batch", rid, payload, timeout)


_last_spool: Path | None = None


def _send_payload(op: str, rid: str, payload: bytes, timeout: float) -> dict[str, Any]:
    global _swept, _last_spool

    res = spool_resolution()
    spool = res.path

    # Pre-flight: never write into a mailbox nobody is reading.
    hb = read_heartbeat(spool)
    if hb is None:
        if res.is_fallback:
            tail = (
                " This spool path is only a last-resort default, so it is probably not "
                "where the plugin is writing. " + _hint_where_to_point()
            )
        else:
            tail = f" (Spool resolved from {res.source}.)"
            if sys.platform != "win32":
                tail += " " + _hint_where_to_point()
        raise BridgeNotRunning(
            f"No {HEARTBEAT_FILE} in {spool}, so nothing is reading that mailbox. Start "
            f"ArmorPaint and enable the MCP bridge plugin (Plugins tab)." + tail,
            spool=str(spool),
            resolved_from=res.source,
            resolution_trace=res.trace if res.is_fallback else None,
        )
    _check_bridge_version(hb, spool)
    if _is_dozing(hb):
        # Asleep between requests (see the module docstring). Wake it first so the
        # request is seen on the next frame rather than after a retry.
        wake_armorpaint(hb)

    _ensure_dirs(spool)
    if not _swept:
        _swept = True
        sweep_orphans(spool)

    _last_spool = spool
    req_dir = spool / REQ_DIR
    tmp_path = req_dir / f"{rid}.json.tmp"
    final_path = req_dir / f"{rid}.json"
    try:
        tmp_path.write_bytes(payload)
        os.replace(tmp_path, final_path)  # atomic on this side; the plugin cannot do this
    except OSError as exc:
        _unlink(tmp_path)
        raise SpoolNotFound(
            f"Could not write the request into {req_dir}: {exc}", spool=str(spool)
        ) from exc
    ring_doorbell(spool, rid)  # after the request, so a ring never names a missing file

    envelope = _await_response(spool, rid, op, timeout)
    return _interpret(envelope, op, rid)


def _follow_pending(
    result: dict[str, Any], op: str, spool: Path, timeout_s: float
) -> dict[str, Any]:
    """Poll a long-running op that answered ``{"status":"pending","token":...}``.

    PROTOCOL.md §Concurrency specifies this shape for bakes/exports. If the bridge does not
    implement the status op, the pending result is returned as-is with an explanation
    rather than being turned into an error.
    """
    if str(result.get("status")) != "pending":
        return result
    token = result.get("token")
    if token is None:
        return result

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        time.sleep(PENDING_POLL_S)
        try:
            status = send_to_armorpaint(
                PENDING_STATUS_OP,
                {"token": str(token)},
                timeout_s=min(10.0, max(MIN_TIMEOUT_S, deadline - time.monotonic())),
                follow_pending=False,
            )
        except OpFailed as exc:
            if exc.code == "unsupported":
                out = dict(result)
                out["note"] = (
                    f"'{op}' is running asynchronously (token {token}) but this bridge does "
                    f"not implement the '{PENDING_STATUS_OP}' op, so completion cannot be "
                    f"confirmed from here."
                )
                return out
            raise
        state = str(status.get("status", "done"))
        if state in ("done", "ok", "complete", "completed"):
            merged = dict(result)
            merged.update(status)
            merged["status"] = "done"
            return merged
        if state in ("error", "failed"):
            raise OpFailed(
                op,
                str(status.get("code") or "internal"),
                str(status.get("message") or f"'{op}' failed while running asynchronously."),
                token=str(token),
            )

    raise RequestTimeout(
        f"'{op}' was accepted as an async job (token {token}) but did not finish within "
        f"{timeout_s:.1f}s. It may still be running inside ArmorPaint.",
        op=op,
        token=str(token),
        spool=str(spool),
    )


# ---------------------------------------------------------------------------
# Diagnostics — deliberately never raises, so it works when nothing else does
# ---------------------------------------------------------------------------


def bridge_diagnostics(probe: bool = True) -> dict[str, Any]:
    """Everything a caller needs to fix a broken connection, without a round trip."""
    res = spool_resolution(refresh=True)
    spool = res.path
    out: dict[str, Any] = res.to_dict()
    out["spool_exists"] = spool.is_dir()
    out["req_dir_exists"] = (spool / REQ_DIR).is_dir()
    out["res_dir_exists"] = (spool / RES_DIR).is_dir()
    out["bridge_lock_present"] = (spool / LOCK_FILE).is_file()
    out["server_wire_version"] = ENVELOPE_VERSION
    out["server_supports_bridge_major"] = list(SUPPORTED_BRIDGE_MAJORS)

    try:
        out["pending_requests"] = len(list((spool / REQ_DIR).glob("*.json")))
        out["uncollected_responses"] = len(list((spool / RES_DIR).glob("*.done")))
    except OSError:
        pass

    hb = read_heartbeat(spool)
    out["heartbeat"] = hb
    try:
        try:
            from .desktop_input import supported as _input_backend
        except ImportError:
            from desktop_input import supported as _input_backend  # type: ignore[no-redef]

        out["wake_backend"] = _input_backend()
    except Exception:
        out["wake_backend"] = None
    if hb is not None:
        out["dozing"] = _is_dozing(hb)
    if hb is None:
        out["alive"] = False
        out["diagnosis"] = (
            f"No {HEARTBEAT_FILE} in {spool}. ArmorPaint is not running, the bridge plugin "
            f"is not enabled, or the spool path is wrong."
        )
        out["next_step"] = _hint_where_to_point()
        return out

    if not probe:
        out["alive"] = None
        out["diagnosis"] = "Heartbeat present; liveness not probed."
        return out

    try:
        out["liveness"] = probe_liveness(spool)
        out["alive"] = True
        out["diagnosis"] = "Bridge is alive: heartbeat 't' advanced between two reads."
        if out["liveness"].get("dozing_before_probe"):
            out["diagnosis"] += (
                " It was dozing (letting ArmorPaint sleep between requests) and was woken "
                "for the probe; that is normal."
            )
        if out["liveness"].get("restarted"):
            out["diagnosis"] += " (t decreased — ArmorPaint restarted.)"
        if str(hb.get("bridge_version", "")).startswith("1.") and sys.platform != "win32":
            out["warning"] = (
                f"Bridge {hb.get('bridge_version')} polls by listing req/, and on Linux/macOS "
                "each listing leaks a file descriptor inside ArmorPaint; launched from the "
                "desktop (1024-fd limit) it hangs after about a minute of activity. Update "
                "armorpaint_mcp_bridge.c in ArmorPaint's plugins folder to bridge 2."
            )
    except BridgeError as exc:
        out["alive"] = False
        out["diagnosis"] = exc.message
        out["code"] = exc.code
    return out
