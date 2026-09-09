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
4. **The plugin runs inline on the render thread**, one request per frame, with no threads
   and no exceptions. So: small payloads, bulk data by path, and every argument validated
   *here* before it can reach an unchecked binding over there.

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
* heartbeat frozen AND our request still on disk -> the plugin is not polling: disabled,
  or its ``on_update`` is gated (fail immediately, with the reason)
* heartbeat frozen BUT our request was consumed -> a long handler is blocking the render
  thread inline, which also freezes the heartbeat. This is expected; keep waiting.

Spool directory discovery (first hit wins)
------------------------------------------
1. ``$ARMORPAINT_SPOOL`` — an absolute path to the spool directory itself.
2. A config file: ``$ARMORPAINT_MCP_CONFIG``, else ``%APPDATA%/armorpaint-mcp/config.json``,
   ``~/.config/armorpaint-mcp/config.json``, ``~/.armorpaint-mcp.json``. Recognised keys:
   ``{"spool": "<abs path>"}`` or ``{"armorpaint_dir": "<install root>"}``.
3. ``$ARMORPAINT_DIR`` / ``$ARMORPAINT_EXE``, then a short list of common install roots,
   each accepted only if it really contains ``data/plugins``. The spool is
   ``<install>/data/mcp_spool`` — the plugin's own default, because ``data_path()``
   (``engine.c:1782``) is the only stable directory a plugin can name.
4. Last resort: a per-user directory (``%LOCALAPPDATA%/armorpaint-mcp/spool``). This only
   works if the plugin is pointed at the same path, so errors say so out loud.

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
SUPPORTED_BRIDGE_MAJOR = 1
ARG_PREFIX = "a_"
RESERVED_ENVELOPE_KEYS = frozenset({"v", "id", "op", "deadline_ms"})

SPOOL_LEAF = "mcp_spool"
REQ_DIR = "req"
RES_DIR = "res"
HEARTBEAT_FILE = "heartbeat.json"
LOCK_FILE = "bridge.lock"

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
    out: list[Path] = []
    env_dir = os.environ.get(INSTALL_DIR_ENV, "").strip()
    if env_dir:
        out.append(Path(env_dir).expanduser())
    env_exe = os.environ.get(INSTALL_EXE_ENV, "").strip()
    if env_exe:
        out.append(Path(env_exe).expanduser().parent)
    if sys.platform == "win32":
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
    elif sys.platform == "darwin":
        out.append(Path("/Applications/ArmorPaint.app/Contents/Resources"))
        out.append(Path.home() / "Applications" / "ArmorPaint.app" / "Contents" / "Resources")
        out.append(Path.home() / "armorpaint")
    else:
        out.append(Path("/opt/armorpaint"))
        out.append(Path("/usr/share/armorpaint"))
        out.append(Path.home() / "armorpaint")
    return out


def _last_resort_spool() -> Path:
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
        if isinstance(root, str) and root.strip():
            p = Path(root).expanduser() / "data" / SPOOL_LEAF
            trace.append(f"config {cfg} key 'armorpaint_dir' -> {p}")
            return SpoolResolution(p, f"config file {cfg}", trace)
        trace.append(f"config {cfg}: no 'spool' or 'armorpaint_dir' key")

    for root in _install_root_candidates():
        if (root / "data" / "plugins").is_dir():
            p = root / "data" / SPOOL_LEAF
            trace.append(f"install root {root} (has data/plugins) -> {p}")
            return SpoolResolution(p, f"ArmorPaint install at {root}", trace)
        trace.append(f"install root {root}: no data/plugins")

    p = _last_resort_spool()
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
    if major != SUPPORTED_BRIDGE_MAJOR:
        raise BridgeVersionMismatch(
            f"The ArmorPaint bridge reports version {ver}, but this server speaks wire "
            f"protocol major {SUPPORTED_BRIDGE_MAJOR}. Update whichever half is older; "
            f"they will not interoperate.",
            bridge_version=ver,
            server_supports_major=SUPPORTED_BRIDGE_MAJOR,
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
                "spool": str(root),
            }

    raise BridgeNotResponding(
        f"{HEARTBEAT_FILE} exists in {root} but its 't' has not advanced in "
        f"{timeout_s:.1f}s, so the plugin's on_update is not running. Likely causes: the "
        f"bridge toggle is off (call ap_bridge_set_enabled, or flip it in the Plugins tab), "
        f"ArmorPaint is showing a modal dialog, or the app is hung. A stale heartbeat also "
        f"looks like this after a crash.",
        spool=str(root),
        heartbeat=first,
    )


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

    # Compact separators are mandatory: the plugin detects a key by testing for ':'
    # immediately after the closing quote (iron_json.c:91).
    text = json.dumps(body, separators=(",", ":"), ensure_ascii=True)
    return text.encode("utf-8")


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
    next_liveness_at = started + LIVENESS_FIRST_CHECK_S
    consumed = False

    while True:
        envelope = _read_committed_response(res_dir, rid)
        if envelope is not None:
            _unlink(res_dir / f"{rid}.json")
            _unlink(res_dir / f"{rid}.done")
            return envelope

        now = time.monotonic()
        if now >= deadline:
            break

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
            if hb_snapshot_t is None or t is None:
                hb_snapshot_t = t
            elif t == hb_snapshot_t and not consumed:
                # Heartbeat frozen AND the request was never picked up: the plugin is not
                # polling. Fail now instead of burning the whole timeout.
                _unlink(req_path)
                raise BridgeNotResponding(
                    f"'{op}' was never picked up: req/{rid}.json is still on disk and the "
                    f"heartbeat has not advanced in {LIVENESS_WINDOW_S:.1f}s. The bridge is "
                    f"enabled but idle, disabled, or ArmorPaint is blocked on a modal "
                    f"dialog. Try ap_bridge_status, then ap_bridge_set_enabled(true).",
                    spool=str(spool),
                    op=op,
                    heartbeat=hb,
                )
            else:
                hb_snapshot_t = t
            next_liveness_at = now + LIVENESS_WINDOW_S

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
            "The request was never picked up. The bridge is not polling req/: check that "
            "the plugin is enabled in the Plugins tab."
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
    global _swept

    timeout = DEFAULT_TIMEOUT_S if timeout_s is None else float(timeout_s)
    timeout = max(MIN_TIMEOUT_S, min(MAX_TIMEOUT_S, timeout))

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
        raise BridgeNotRunning(
            f"No {HEARTBEAT_FILE} in {spool}, so nothing is reading that mailbox. Start "
            f"ArmorPaint and enable the MCP bridge plugin (Plugins tab)." + tail,
            spool=str(spool),
            resolved_from=res.source,
            resolution_trace=res.trace if res.is_fallback else None,
        )
    _check_bridge_version(hb, spool)

    _ensure_dirs(spool)
    if not _swept:
        _swept = True
        sweep_orphans(spool)

    rid = mint_id()
    payload = build_request(op, args, rid, timeout)

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

    envelope = _await_response(spool, rid, op, timeout)
    result = _interpret(envelope, op, rid)

    if follow_pending:
        result = _follow_pending(result, op, spool, timeout)
    return result


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
    out["server_supports_bridge_major"] = SUPPORTED_BRIDGE_MAJOR

    try:
        out["pending_requests"] = len(list((spool / REQ_DIR).glob("*.json")))
        out["uncollected_responses"] = len(list((spool / RES_DIR).glob("*.done")))
    except OSError:
        pass

    hb = read_heartbeat(spool)
    out["heartbeat"] = hb
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
        if out["liveness"].get("restarted"):
            out["diagnosis"] += " (t decreased — ArmorPaint restarted.)"
    except BridgeError as exc:
        out["alive"] = False
        out["diagnosis"] = exc.message
        out["code"] = exc.code
    return out
