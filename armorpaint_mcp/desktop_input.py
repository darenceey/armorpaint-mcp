"""
Synthetic input for ArmorPaint's window: wake it up, click, type, drag and scroll.
==================================================================================

Two limitations of ArmorPaint's plugin API are answered from OUTSIDE the app, by this
server, rather than by the plugin:

* **Waking a dozing ArmorPaint.** The bridge lets the app fall asleep between requests (it
  used to hold it awake -- rendering at full rate -- for as long as it was enabled). Iron's
  idle gate resets its counter on any input event (``iron.h`` ``_mouse_move`` and friends),
  so a synthetic 1-pixel pointer wiggle that ends where the real pointer already is wakes the
  app for about 120 frames, which is all the bridge needs to pick up a request and hold
  itself awake again. Measured on Linux/Xvfb: 120 frames asleep -> wiggle -> frames resume.
  Two moves are sent, not one: Iron drops a move whose delta is zero, and it has no delta
  for the first move it ever sees.

* **UI automation.** ArmorPaint's ``ui_*`` bindings draw a plugin's OWN widgets; nothing in
  the API clicks ArmorPaint's. Synthetic input does, the same way a person would, so an agent
  can pair ``ap_capture_window`` (look) with ``ap_ui_click`` / ``ap_ui_key`` (act) for the
  parts of the app no binding reaches. Keyboard shortcuts give undo/redo on a stock build.

Delivery is to the window itself, never through the global input queue, so none of this
moves the user's real pointer or steals focus:

* **X11 / XWayland (Linux)** -- ``XSendEvent`` to ArmorPaint's window with an empty event
  mask, which delivers to the client that created it. Iron's X11 loop does not reject
  ``send_event`` events. Verified on Linux (Xvfb + openbox).
* **Windows** -- ``PostMessageW`` of ``WM_MOUSEMOVE`` / button / ``WM_KEYDOWN`` / wheel
  messages to the ``IronWindow`` class window; Iron's ``WndProc`` handles posted messages
  exactly like hardware ones. Implemented against ``windows_system.c``; not yet run on
  Windows hardware.
* **macOS** -- ``CGEventPostToPid``. Implemented, untested, and macOS may require granting the
  server's host app Accessibility permission. The bridge therefore does not doze on macOS by
  default.

Coordinates are WINDOW pixels, the same space ``ap_capture_window`` images use (before any
``downscale``).
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import sys
import threading
import time
from typing import Any, Callable

try:
    from .window_capture import APP_MARKER, CaptureError, _find_windows, _load_xlib
except ImportError:  # running as a loose script next to window_capture.py
    from window_capture import APP_MARKER, CaptureError, _find_windows, _load_xlib  # type: ignore[no-redef]

_lock = threading.Lock()

# A "frame fence": a callable that round-trips a bridge ping (a reply is written from inside
# a frame's update, after that frame's input was processed). It makes shortcuts and clicks
# deterministic however slow the app's frames are -- a fixed delay is not: right after a
# project opens, a software-rendered frame took well over the 250 ms hold and a ctrl+z was
# lost. Without a fence, KEY_HOLD_S is slept instead.
#
# _wait runs it TWICE. One ping can be answered by the very frame whose event pump ran just
# before our event arrived, leaving that event for the next frame -- together with the key
# sent after the fence, which then lands in the same frame as its modifier and does nothing.
# MEASURED (KDE Plasma 6 / XWayland, one ping): ctrl+z undid 6 of 10 times. The second ping
# is written after the first reply, so the frame that answers it pumped events after ours.
Fence = Callable[[], None] | None


def _wait(fence: Fence) -> None:
    if fence is not None:
        try:
            fence()
            fence()
            return
        except Exception:
            pass  # fall back to the fixed hold
    time.sleep(KEY_HOLD_S)

# Seconds between the press and release halves of a click or shortcut. Iron clears
# "started" at the end of every frame and a shortcut needs its modifier still DOWN in the
# frame that sees the key start (keymap.c keymap_shortcut), so press and release must land
# in different frames. Measured: 70 ms was not enough on a software-rendered ArmorPaint
# (~100-200 ms frames); 250 ms is, and is still quicker than a person.
KEY_HOLD_S = 0.25


class InputError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def supported() -> str | None:
    """Name of the input backend for this platform, or None."""
    if sys.platform.startswith("linux"):
        return "x11"
    if sys.platform == "win32":
        return "win32"
    if sys.platform == "darwin":
        return "quartz"
    return None


# ---------------------------------------------------------------------------
# X11
# ---------------------------------------------------------------------------

_c_ulong = ctypes.c_ulong

KEY_PRESS, KEY_RELEASE, BUTTON_PRESS, BUTTON_RELEASE, MOTION_NOTIFY = 2, 3, 4, 5, 6
SHIFT_MASK, CONTROL_MASK, MOD1_MASK = 1, 4, 8
BUTTON1_MASK = 1 << 8


class _XKeyEvent(ctypes.Structure):
    # XKeyEvent, XButtonEvent and XMotionEvent share this layout up to `state`; the field
    # after it is keycode / button / is_hint.
    _fields_ = [
        ("type", ctypes.c_int),
        ("serial", _c_ulong),
        ("send_event", ctypes.c_int),
        ("display", ctypes.c_void_p),
        ("window", _c_ulong),
        ("root", _c_ulong),
        ("subwindow", _c_ulong),
        ("time", _c_ulong),
        ("x", ctypes.c_int),
        ("y", ctypes.c_int),
        ("x_root", ctypes.c_int),
        ("y_root", ctypes.c_int),
        ("state", ctypes.c_uint),
        ("detail", ctypes.c_uint),  # keycode (key) / button (button); is_hint is a char here
        ("same_screen", ctypes.c_int),
    ]


class _XEvent(ctypes.Union):
    _fields_ = [("xkey", _XKeyEvent), ("pad", ctypes.c_long * 24)]


_X11_KEYSYMS = {
    # names this module accepts -> X keysym names
    "ctrl": "Control_L",
    "control": "Control_L",
    "shift": "Shift_L",
    "alt": "Alt_L",
    "enter": "Return",
    "return": "Return",
    "escape": "Escape",
    "esc": "Escape",
    "tab": "Tab",
    "space": "space",
    "backspace": "BackSpace",
    "delete": "Delete",
    "up": "Up",
    "down": "Down",
    "left": "Left",
    "right": "Right",
    "home": "Home",
    "end": "End",
    "pageup": "Prior",
    "pagedown": "Next",
    **{f"f{i}": f"F{i}" for i in range(1, 13)},
}


def _x11_prepare(x: Any) -> None:
    if getattr(x, "_input_ready", False):
        return
    x.XSendEvent.restype = ctypes.c_int
    x.XSendEvent.argtypes = [ctypes.c_void_p, _c_ulong, ctypes.c_int, ctypes.c_long, ctypes.POINTER(_XEvent)]
    x.XFlush.argtypes = [ctypes.c_void_p]
    x.XStringToKeysym.restype = _c_ulong
    x.XStringToKeysym.argtypes = [ctypes.c_char_p]
    x.XKeysymToKeycode.restype = ctypes.c_ubyte
    x.XKeysymToKeycode.argtypes = [ctypes.c_void_p, _c_ulong]
    x.XQueryPointer.restype = ctypes.c_int
    x.XQueryPointer.argtypes = [
        ctypes.c_void_p, _c_ulong, ctypes.POINTER(_c_ulong), ctypes.POINTER(_c_ulong),
        ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int),
        ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_uint),
    ]
    x._input_ready = True


class _X11Session:
    """One display connection, one located ArmorPaint window."""

    def __init__(self, title_hint: str | None) -> None:
        try:
            self.x = _load_xlib()
        except CaptureError as exc:
            raise InputError(exc.code, exc.message) from exc
        _x11_prepare(self.x)
        display = os.environ.get("DISPLAY") or ":0"
        self.dpy = self.x.XOpenDisplay(display.encode())
        if not self.dpy:
            raise InputError(
                "unsupported",
                f"Could not open X display {display!r}; this server needs DISPLAY to reach the "
                f"display ArmorPaint runs on.",
            )
        try:
            windows = _find_windows(self.x, self.dpy)
        except CaptureError as exc:
            self.close()
            raise InputError(exc.code, exc.message) from exc
        if not windows:
            self.close()
            raise InputError("window_not_found", f"No {APP_MARKER} window on display {display}.")
        self.wid, self.title = windows[0]
        if title_hint:
            for cand, cand_title in windows:
                if cand_title == title_hint:
                    self.wid, self.title = cand, cand_title
        self.root = self.x.XDefaultRootWindow(self.dpy)

    def close(self) -> None:
        if self.dpy:
            self.x.XCloseDisplay(self.dpy)
            self.dpy = None

    def pointer(self) -> tuple[int, int]:
        root, child = _c_ulong(), _c_ulong()
        rx, ry, wx, wy = ctypes.c_int(), ctypes.c_int(), ctypes.c_int(), ctypes.c_int()
        mask = ctypes.c_uint()
        self.x.XQueryPointer(
            self.dpy, self.wid, ctypes.byref(root), ctypes.byref(child), ctypes.byref(rx),
            ctypes.byref(ry), ctypes.byref(wx), ctypes.byref(wy), ctypes.byref(mask),
        )
        return wx.value, wy.value

    def send(self, etype: int, x: int, y: int, state: int = 0, detail: int = 0) -> None:
        ev = _XEvent()
        k = ev.xkey
        k.type = etype
        k.send_event = 1
        k.display = self.dpy
        k.window = self.wid
        k.root = self.root
        k.x, k.y = x, y
        k.x_root, k.y_root = x, y
        k.state = state
        k.detail = detail
        k.same_screen = 1
        if etype == MOTION_NOTIFY:
            k.detail = 0
        # Empty event mask: delivered to the client that created the window (ArmorPaint),
        # whatever it selected.
        if not self.x.XSendEvent(self.dpy, self.wid, 0, 0, ctypes.byref(ev)):
            raise InputError("input_failed", "XSendEvent was refused.")
        # XSync, not XFlush: return only once the X server has queued the event for
        # ArmorPaint, so a frame fence that follows really comes after it.
        self.x.XSync(self.dpy, 0)

    def keycode(self, name: str) -> int:
        sym_name = _X11_KEYSYMS.get(name.lower(), name if len(name) > 1 else name.lower())
        sym = self.x.XStringToKeysym(sym_name.encode())
        code = self.x.XKeysymToKeycode(self.dpy, sym) if sym else 0
        if not code:
            raise InputError("bad_args", f"Unknown key {name!r}.")
        return int(code)


def _x11_modstate(mods: list[str]) -> int:
    state = 0
    for m in mods:
        state |= {"ctrl": CONTROL_MASK, "control": CONTROL_MASK, "shift": SHIFT_MASK, "alt": MOD1_MASK}[m]
    return state


# ---------------------------------------------------------------------------
# Win32
# ---------------------------------------------------------------------------

WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN, WM_LBUTTONUP = 0x0201, 0x0202
WM_RBUTTONDOWN, WM_RBUTTONUP = 0x0204, 0x0205
WM_MBUTTONDOWN, WM_MBUTTONUP = 0x0207, 0x0208
WM_MOUSEWHEEL = 0x020A
WM_KEYDOWN, WM_KEYUP = 0x0100, 0x0101
MK_LBUTTON, MK_RBUTTON, MK_SHIFT, MK_CONTROL, MK_MBUTTON = 0x1, 0x2, 0x4, 0x8, 0x10

_VK = {
    "ctrl": 0x11, "control": 0x11, "shift": 0x10, "alt": 0x12, "enter": 0x0D, "return": 0x0D,
    "escape": 0x1B, "esc": 0x1B, "tab": 0x09, "space": 0x20, "backspace": 0x08,
    "delete": 0x2E, "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28, "home": 0x24,
    "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
    **{f"f{i}": 0x6F + i for i in range(1, 13)},
}


def _win_vk(name: str) -> int:
    low = name.lower()
    if low in _VK:
        return _VK[low]
    if len(name) == 1 and name.isalnum():
        return ord(name.upper())
    raise InputError("bad_args", f"Unknown key {name!r}.")


class _Win32Session:
    def __init__(self, title_hint: str | None) -> None:
        from ctypes import wintypes

        self.user32 = ctypes.WinDLL("user32", use_last_error=True)  # type: ignore[attr-defined]
        u = self.user32
        u.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        u.PostMessageW.restype = wintypes.BOOL
        u.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        u.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        u.IsWindowVisible.argtypes = [wintypes.HWND]
        u.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
        u.ScreenToClient.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
        self.wintypes = wintypes
        self.hwnd, self.title = _win32_find_window(u, title_hint)

    def close(self) -> None:
        pass

    def pointer(self) -> tuple[int, int]:
        pt = self.wintypes.POINT()
        self.user32.GetCursorPos(ctypes.byref(pt))
        self.user32.ScreenToClient(self.hwnd, ctypes.byref(pt))
        return pt.x, pt.y

    def post(self, msg: int, wparam: int, lparam: int) -> None:
        if not self.user32.PostMessageW(self.hwnd, msg, wparam, lparam):
            raise InputError("input_failed", f"PostMessageW failed (error {ctypes.get_last_error()}).")  # type: ignore[attr-defined]


def _lparam_xy(x: int, y: int) -> int:
    return ((y & 0xFFFF) << 16) | (x & 0xFFFF)


def _win32_find_window(u: Any, title_hint: str | None) -> tuple[int, str]:
    from ctypes import wintypes

    found: list[tuple[int, str]] = []
    enum_proc_t = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)  # type: ignore[attr-defined]

    def cb(hwnd: int, _lp: int) -> bool:
        cls = ctypes.create_unicode_buffer(256)
        u.GetClassNameW(hwnd, cls, 256)
        if cls.value != "IronWindow" or not u.IsWindowVisible(hwnd):
            return True
        title = ctypes.create_unicode_buffer(512)
        u.GetWindowTextW(hwnd, title, 512)
        found.append((hwnd, title.value))
        return True

    u.EnumWindows(enum_proc_t(cb), 0)
    if not found:
        raise InputError("window_not_found", "No ArmorPaint (IronWindow) window is open.")
    for hwnd, title in found:
        if title_hint and title == title_hint:
            return hwnd, title
    for hwnd, title in found:
        if APP_MARKER in title:
            return hwnd, title
    return found[0]


# ---------------------------------------------------------------------------
# macOS (Quartz)
# ---------------------------------------------------------------------------

_MAC_KEYCODES = {
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8, "v": 9, "b": 11,
    "q": 12, "w": 13, "e": 14, "r": 15, "y": 16, "t": 17, "1": 18, "2": 19, "3": 20, "4": 21,
    "6": 22, "5": 23, "9": 25, "7": 26, "8": 28, "0": 29, "o": 31, "u": 32, "i": 34, "p": 35,
    "l": 37, "j": 38, "k": 40, "n": 45, "m": 46, "enter": 36, "return": 36, "tab": 48,
    "space": 49, "backspace": 51, "delete": 117, "escape": 53, "esc": 53, "ctrl": 59,
    "control": 59, "shift": 56, "alt": 58, "left": 123, "right": 124, "down": 125, "up": 126,
    "home": 115, "end": 119, "pageup": 116, "pagedown": 121,
}
_MAC_FLAGS = {"shift": 0x20000, "ctrl": 0x40000, "control": 0x40000, "alt": 0x80000}


class _CGPoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


class _QuartzSession:
    def __init__(self, title_hint: str | None) -> None:
        try:
            from .window_capture import _mac_find_window
        except ImportError:
            from window_capture import _mac_find_window  # type: ignore[no-redef]

        info = _mac_find_window(title_hint)
        self.pid = info["pid"]
        self.bounds = info["bounds"]  # (x, y, w, h) in global points
        self.title = info["title"]
        self.scale = info.get("scale", 1.0)
        path = ctypes.util.find_library("ApplicationServices") or (
            "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"
        )
        cg = ctypes.CDLL(path)
        cg.CGEventCreateMouseEvent.restype = ctypes.c_void_p
        cg.CGEventCreateMouseEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint32, _CGPoint, ctypes.c_uint32]
        cg.CGEventCreateKeyboardEvent.restype = ctypes.c_void_p
        cg.CGEventCreateKeyboardEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint16, ctypes.c_bool]
        cg.CGEventCreateScrollWheelEvent.restype = ctypes.c_void_p
        cg.CGEventCreateScrollWheelEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_int32]
        cg.CGEventSetFlags.argtypes = [ctypes.c_void_p, ctypes.c_uint64]
        cg.CGEventPostToPid.argtypes = [ctypes.c_int, ctypes.c_void_p]
        cg.CGEventCreate.restype = ctypes.c_void_p
        cg.CGEventCreate.argtypes = [ctypes.c_void_p]
        cg.CGEventGetLocation.restype = _CGPoint
        cg.CGEventGetLocation.argtypes = [ctypes.c_void_p]
        cf = ctypes.CDLL(ctypes.util.find_library("CoreFoundation"))
        cf.CFRelease.argtypes = [ctypes.c_void_p]
        self.cg, self.cf = cg, cf

    def close(self) -> None:
        pass

    def to_global(self, x: int, y: int) -> _CGPoint:
        bx, by, _, _ = self.bounds
        return _CGPoint(bx + x / self.scale, by + y / self.scale)

    def pointer(self) -> tuple[int, int]:
        ev = self.cg.CGEventCreate(None)
        loc = self.cg.CGEventGetLocation(ev)
        self.cf.CFRelease(ev)
        bx, by, _, _ = self.bounds
        return int((loc.x - bx) * self.scale), int((loc.y - by) * self.scale)

    def post(self, ev: int, flags: int = 0) -> None:
        if not ev:
            raise InputError("input_failed", "Quartz refused to create the event.")
        if flags:
            self.cg.CGEventSetFlags(ev, flags)
        self.cg.CGEventPostToPid(self.pid, ev)
        self.cf.CFRelease(ev)

    def mouse(self, etype: int, x: int, y: int, button: int = 0) -> None:
        self.post(self.cg.CGEventCreateMouseEvent(None, etype, self.to_global(x, y), button))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

_BUTTONS = ("left", "right", "middle")
_MODS = ("ctrl", "control", "shift", "alt")


def _session(title_hint: str | None) -> Any:
    backend = supported()
    if backend == "x11":
        return _X11Session(title_hint)
    if backend == "win32":
        return _Win32Session(title_hint)
    if backend == "quartz":
        try:
            return _QuartzSession(title_hint)
        except CaptureError as exc:
            raise InputError(exc.code, exc.message) from exc
    raise InputError("unsupported", f"No synthetic-input backend for {sys.platform}.")


def _check_mods(mods: list[str] | None) -> list[str]:
    out = [m.lower() for m in (mods or [])]
    for m in out:
        if m not in _MODS:
            raise InputError("bad_args", f"Unknown modifier {m!r}; use ctrl, shift or alt.")
    return out


def wake(title_hint: str | None = None) -> dict[str, Any]:
    """Wake a dozing ArmorPaint with a 1-pixel pointer wiggle that ends where it started."""
    with _lock:
        s = _session(title_hint)
        try:
            px, py = s.pointer()
            backend = supported()
            for x, y in ((px + 1, py), (px, py)):
                if backend == "x11":
                    s.send(MOTION_NOTIFY, x, y)
                elif backend == "win32":
                    s.post(WM_MOUSEMOVE, 0, _lparam_xy(x, y))
                else:
                    s.mouse(5, x, y)  # kCGEventMouseMoved
            return {"woke": True, "method": f"{backend} pointer wiggle", "window": s.title}
        finally:
            s.close()


def click(
    x: int, y: int, button: str = "left", double: bool = False, modifiers: list[str] | None = None,
    title_hint: str | None = None, fence: Fence = None,
) -> dict[str, Any]:
    if button not in _BUTTONS:
        raise InputError("bad_args", f"button must be one of {', '.join(_BUTTONS)}.")
    mods = _check_mods(modifiers)
    with _lock:
        s = _session(title_hint)
        try:
            backend = supported()
            for _ in range(2 if double else 1):
                if backend == "x11":
                    state = _x11_modstate(mods)
                    b = {"left": 1, "middle": 2, "right": 3}[button]
                    s.send(MOTION_NOTIFY, x + 1, y, state)
                    s.send(MOTION_NOTIFY, x, y, state)
                    s.send(BUTTON_PRESS, x, y, state, b)
                    _wait(fence)
                    s.send(BUTTON_RELEASE, x, y, state | (BUTTON1_MASK << (b - 1)), b)
                elif backend == "win32":
                    mk = (MK_CONTROL if "ctrl" in mods or "control" in mods else 0) | (MK_SHIFT if "shift" in mods else 0)
                    down, up, mkb = {
                        "left": (WM_LBUTTONDOWN, WM_LBUTTONUP, MK_LBUTTON),
                        "right": (WM_RBUTTONDOWN, WM_RBUTTONUP, MK_RBUTTON),
                        "middle": (WM_MBUTTONDOWN, WM_MBUTTONUP, MK_MBUTTON),
                    }[button]
                    s.post(WM_MOUSEMOVE, mk, _lparam_xy(x + 1, y))
                    s.post(WM_MOUSEMOVE, mk, _lparam_xy(x, y))
                    s.post(down, mk | mkb, _lparam_xy(x, y))
                    _wait(fence)
                    s.post(up, mk, _lparam_xy(x, y))
                else:
                    down, up, cgb = {"left": (1, 2, 0), "right": (3, 4, 1), "middle": (25, 26, 2)}[button]
                    s.mouse(5, x, y)
                    s.mouse(down, x, y, cgb)
                    _wait(fence)
                    s.mouse(up, x, y, cgb)
                _wait(fence)
            return {"clicked": [x, y], "button": button, "double": double, "modifiers": mods, "method": backend}
        finally:
            s.close()


def drag(
    points: list[tuple[int, int]], button: str = "left", modifiers: list[str] | None = None,
    step_s: float = 0.03, title_hint: str | None = None,
) -> dict[str, Any]:
    if len(points) < 2:
        raise InputError("bad_args", "a drag needs at least two points.")
    if button not in _BUTTONS:
        raise InputError("bad_args", f"button must be one of {', '.join(_BUTTONS)}.")
    mods = _check_mods(modifiers)
    with _lock:
        s = _session(title_hint)
        try:
            backend = supported()
            x0, y0 = points[0]
            if backend == "x11":
                state = _x11_modstate(mods)
                b = {"left": 1, "middle": 2, "right": 3}[button]
                held = state | (BUTTON1_MASK << (b - 1))
                s.send(MOTION_NOTIFY, x0 + 1, y0, state)
                s.send(MOTION_NOTIFY, x0, y0, state)
                s.send(BUTTON_PRESS, x0, y0, state, b)
                for x, y in points[1:]:
                    time.sleep(step_s)
                    s.send(MOTION_NOTIFY, x, y, held)
                time.sleep(KEY_HOLD_S)
                xe, ye = points[-1]
                s.send(BUTTON_RELEASE, xe, ye, held, b)
            elif backend == "win32":
                mk = (MK_CONTROL if "ctrl" in mods or "control" in mods else 0) | (MK_SHIFT if "shift" in mods else 0)
                down, up, mkb = {
                    "left": (WM_LBUTTONDOWN, WM_LBUTTONUP, MK_LBUTTON),
                    "right": (WM_RBUTTONDOWN, WM_RBUTTONUP, MK_RBUTTON),
                    "middle": (WM_MBUTTONDOWN, WM_MBUTTONUP, MK_MBUTTON),
                }[button]
                s.post(WM_MOUSEMOVE, mk, _lparam_xy(x0 + 1, y0))
                s.post(WM_MOUSEMOVE, mk, _lparam_xy(x0, y0))
                s.post(down, mk | mkb, _lparam_xy(x0, y0))
                for x, y in points[1:]:
                    time.sleep(step_s)
                    s.post(WM_MOUSEMOVE, mk | mkb, _lparam_xy(x, y))
                time.sleep(KEY_HOLD_S)
                xe, ye = points[-1]
                s.post(up, mk, _lparam_xy(xe, ye))
            else:
                down, up, drag_t, cgb = {
                    "left": (1, 2, 6, 0), "right": (3, 4, 7, 1), "middle": (25, 26, 27, 2),
                }[button]
                s.mouse(5, x0, y0)
                s.mouse(down, x0, y0, cgb)
                for x, y in points[1:]:
                    time.sleep(step_s)
                    s.mouse(drag_t, x, y, cgb)
                time.sleep(KEY_HOLD_S)
                xe, ye = points[-1]
                s.mouse(up, xe, ye, cgb)
            return {"dragged": len(points), "button": button, "modifiers": mods, "method": backend}
        finally:
            s.close()


def scroll(x: int, y: int, clicks: int, title_hint: str | None = None) -> dict[str, Any]:
    """Scroll at (x, y). Positive clicks scroll DOWN / zoom out, as Iron's own sign."""
    if clicks == 0 or abs(clicks) > 50:
        raise InputError("bad_args", "clicks must be -50..50 and not 0.")
    with _lock:
        s = _session(title_hint)
        try:
            backend = supported()
            if backend == "x11":
                s.send(MOTION_NOTIFY, x + 1, y)
                s.send(MOTION_NOTIFY, x, y)
                b = 5 if clicks > 0 else 4
                for _ in range(abs(clicks)):
                    # Iron reads the wheel from ButtonRelease 4/5.
                    s.send(BUTTON_PRESS, x, y, 0, b)
                    s.send(BUTTON_RELEASE, x, y, 0, b)
                    time.sleep(0.01)
            elif backend == "win32":
                s.post(WM_MOUSEMOVE, 0, _lparam_xy(x + 1, y))
                s.post(WM_MOUSEMOVE, 0, _lparam_xy(x, y))
                for _ in range(abs(clicks)):
                    # WM_MOUSEWHEEL carries SCREEN coordinates; Iron only reads the delta.
                    delta = (-120 if clicks > 0 else 120) & 0xFFFF
                    s.post(WM_MOUSEWHEEL, delta << 16, _lparam_xy(x, y))
            else:
                s.mouse(5, x, y)
                for _ in range(abs(clicks)):
                    s.post(s.cg.CGEventCreateScrollWheelEvent(None, 0, 1, -1 if clicks > 0 else 1))
            return {"scrolled": clicks, "at": [x, y], "method": backend}
        finally:
            s.close()


def key(
    name: str, modifiers: list[str] | None = None, title_hint: str | None = None, fence: Fence = None
) -> dict[str, Any]:
    """Press and release one key, with modifiers held around it (a shortcut)."""
    if not isinstance(name, str) or not name:
        raise InputError("bad_args", "key must be a non-empty string.")
    mods = _check_mods(modifiers)
    with _lock:
        s = _session(title_hint)
        try:
            backend = supported()
            if backend == "x11":
                px, py = s.pointer()
                code = s.keycode(name)
                mod_codes = [s.keycode(m) for m in mods]
                # Wake the app first: the frame that wakes a sleeping ArmorPaint does not
                # reliably run its shortcut handlers.
                s.send(MOTION_NOTIFY, px + 1, py)
                s.send(MOTION_NOTIFY, px, py)
                _wait(fence)
                state = 0
                for m, mc in zip(mods, mod_codes):
                    s.send(KEY_PRESS, px, py, state, mc)
                    state |= _x11_modstate([m])
                if mods:
                    # MEASURED: modifier and key pressed in the same frame -> no shortcut;
                    # modifier a frame earlier -> works (as it does for a person).
                    _wait(fence)
                s.send(KEY_PRESS, px, py, state, code)
                _wait(fence)
                s.send(KEY_RELEASE, px, py, state, code)
                for m, mc in reversed(list(zip(mods, mod_codes))):
                    state &= ~_x11_modstate([m])
                    s.send(KEY_RELEASE, px, py, state | _x11_modstate([m]), mc)
            elif backend == "win32":
                vk = _win_vk(name)
                mod_vks = [_win_vk(m) for m in mods]
                px, py = s.pointer()
                s.post(WM_MOUSEMOVE, 0, _lparam_xy(px + 1, py))
                s.post(WM_MOUSEMOVE, 0, _lparam_xy(px, py))
                _wait(fence)
                for mv in mod_vks:
                    s.post(WM_KEYDOWN, mv, 1)
                if mod_vks:
                    _wait(fence)
                s.post(WM_KEYDOWN, vk, 1)
                _wait(fence)
                s.post(WM_KEYUP, vk, 0xC0000001)
                for mv in reversed(mod_vks):
                    s.post(WM_KEYUP, mv, 0xC0000001)
            else:
                low = name.lower()
                if low not in _MAC_KEYCODES:
                    raise InputError("bad_args", f"Unknown key {name!r}.")
                flags = 0
                for m in mods:
                    flags |= _MAC_FLAGS[m]
                px, py = s.pointer()
                s.mouse(5, px, py)
                _wait(fence)
                for m in mods:
                    s.post(s.cg.CGEventCreateKeyboardEvent(None, _MAC_KEYCODES[m], True), flags)
                if mods:
                    _wait(fence)
                s.post(s.cg.CGEventCreateKeyboardEvent(None, _MAC_KEYCODES[low], True), flags)
                _wait(fence)
                s.post(s.cg.CGEventCreateKeyboardEvent(None, _MAC_KEYCODES[low], False), flags)
                for m in reversed(mods):
                    s.post(s.cg.CGEventCreateKeyboardEvent(None, _MAC_KEYCODES[m], False), 0)
            return {"key": name, "modifiers": mods, "method": backend}
        finally:
            s.close()
