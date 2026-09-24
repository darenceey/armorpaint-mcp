"""
Capture ArmorPaint's rendered window from OUTSIDE the app.
==========================================================

Why this exists: a stock ArmorPaint 1.0 plugin cannot write the viewport to a file.
``viewport_save_texture`` encodes into the project's in-memory asset list, and
``iron_encode_png`` / ``gpu_get_texture_pixels`` are not bound (see the README's
Limitations). Upstream added ``viewport_save_texture_to_file`` on 2026-09-09
(commit 1e14e27e), but binaries built before that still lack it. So this server takes the
picture itself: it reads the ArmorPaint window's pixels from the display server.

Linux only, and that is enough there: Iron's Linux backend is X11-only
(``base/sources/backends/linux_system.c`` has no Wayland path), so on a Wayland desktop
ArmorPaint runs under XWayland and is still an X window. ``XGetImage`` on a window returns
the window's own contents, so the capture works while ArmorPaint is covered by other
windows and never steals focus. A minimised (unmapped) window has no pixels to read.

No dependencies beyond libX11 via ctypes: no ImageMagick, no Pillow. The PNG encoder is
the ~20 lines at the bottom.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import struct
import sys
import threading
import zlib
from dataclasses import dataclass
from typing import Any

APP_MARKER = "ArmorPaint"  # present in WM_CLASS ("<title>", "<title>_IronApplication")
MAX_DOWNSCALE = 4


class CaptureError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class Capture:
    png: bytes
    width: int
    height: int
    window_width: int
    window_height: int
    window_id: int
    window_title: str
    method: str

    def describe(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "window_id": hex(self.window_id),
            "window_title": self.window_title,
            "window_size": [self.window_width, self.window_height],
            "image_size": [self.width, self.height],
            "png_bytes": len(self.png),
        }


# ---------------------------------------------------------------------------
# Xlib via ctypes
# ---------------------------------------------------------------------------

_c_ulong = ctypes.c_ulong
_Window = _c_ulong
_Atom = _c_ulong

XA_WINDOW = 33
ZPIXMAP = 2
IS_VIEWABLE = 2
ALL_PLANES = 0xFFFFFFFFFFFFFFFF if ctypes.sizeof(_c_ulong) == 8 else 0xFFFFFFFF


class _XImage(ctypes.Structure):
    _fields_ = [
        ("width", ctypes.c_int),
        ("height", ctypes.c_int),
        ("xoffset", ctypes.c_int),
        ("format", ctypes.c_int),
        ("data", ctypes.c_void_p),
        ("byte_order", ctypes.c_int),
        ("bitmap_unit", ctypes.c_int),
        ("bitmap_bit_order", ctypes.c_int),
        ("bitmap_pad", ctypes.c_int),
        ("depth", ctypes.c_int),
        ("bytes_per_line", ctypes.c_int),
        ("bits_per_pixel", ctypes.c_int),
        ("red_mask", _c_ulong),
        ("green_mask", _c_ulong),
        ("blue_mask", _c_ulong),
        ("obdata", ctypes.c_void_p),
        ("funcs", ctypes.c_void_p * 6),
    ]


class _XWindowAttributes(ctypes.Structure):
    _fields_ = [
        ("x", ctypes.c_int),
        ("y", ctypes.c_int),
        ("width", ctypes.c_int),
        ("height", ctypes.c_int),
        ("border_width", ctypes.c_int),
        ("depth", ctypes.c_int),
        ("visual", ctypes.c_void_p),
        ("root", _Window),
        ("c_class", ctypes.c_int),
        ("bit_gravity", ctypes.c_int),
        ("win_gravity", ctypes.c_int),
        ("backing_store", ctypes.c_int),
        ("backing_planes", _c_ulong),
        ("backing_pixel", _c_ulong),
        ("save_under", ctypes.c_int),
        ("colormap", _c_ulong),
        ("map_installed", ctypes.c_int),
        ("map_state", ctypes.c_int),
        ("all_event_masks", ctypes.c_long),
        ("your_event_mask", ctypes.c_long),
        ("do_not_propagate_mask", ctypes.c_long),
        ("override_redirect", ctypes.c_int),
        ("screen", ctypes.c_void_p),
    ]


class _XClassHint(ctypes.Structure):
    # c_void_p, not c_char_p: reading a c_char_p field yields a Python COPY, and passing
    # that to XFree frees memory Xlib never allocated ("free(): invalid size").
    _fields_ = [("res_name", ctypes.c_void_p), ("res_class", ctypes.c_void_p)]


_ERROR_HANDLER_T = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)

_lock = threading.Lock()
_xlib: Any = None
_last_x_error: list[int] = []


def _on_x_error(_display: int, event: int) -> int:
    # XErrorEvent: int type; Display *; XID resourceid; unsigned long serial;
    # unsigned char error_code; ... Only "an error happened" matters here. The default
    # handler would call exit() and take the whole MCP server down with it.
    _last_x_error.append(1)
    return 0


_error_handler = _ERROR_HANDLER_T(_on_x_error)  # keep a reference: ctypes callbacks are GC'd


def _load_xlib() -> Any:
    global _xlib
    if _xlib is not None:
        return _xlib
    name = ctypes.util.find_library("X11") or "libX11.so.6"
    try:
        x = ctypes.CDLL(name)
    except OSError as exc:
        raise CaptureError(
            "unsupported", f"libX11 could not be loaded ({exc}); window capture needs X11."
        ) from exc
    x.XOpenDisplay.restype = ctypes.c_void_p
    x.XOpenDisplay.argtypes = [ctypes.c_char_p]
    x.XCloseDisplay.argtypes = [ctypes.c_void_p]
    x.XDefaultRootWindow.restype = _Window
    x.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
    x.XInternAtom.restype = _Atom
    x.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
    x.XGetWindowProperty.restype = ctypes.c_int
    x.XGetWindowProperty.argtypes = [
        ctypes.c_void_p, _Window, _Atom, ctypes.c_long, ctypes.c_long, ctypes.c_int, _Atom,
        ctypes.POINTER(_Atom), ctypes.POINTER(ctypes.c_int), ctypes.POINTER(_c_ulong),
        ctypes.POINTER(_c_ulong), ctypes.POINTER(ctypes.c_void_p),
    ]
    x.XGetClassHint.restype = ctypes.c_int
    x.XGetClassHint.argtypes = [ctypes.c_void_p, _Window, ctypes.POINTER(_XClassHint)]
    x.XGetWindowAttributes.restype = ctypes.c_int
    x.XGetWindowAttributes.argtypes = [
        ctypes.c_void_p, _Window, ctypes.POINTER(_XWindowAttributes)
    ]
    x.XGetImage.restype = ctypes.POINTER(_XImage)
    x.XGetImage.argtypes = [
        ctypes.c_void_p, _Window, ctypes.c_int, ctypes.c_int, ctypes.c_uint, ctypes.c_uint,
        _c_ulong, ctypes.c_int,
    ]
    x.XDestroyImage.argtypes = [ctypes.POINTER(_XImage)]
    x.XFree.argtypes = [ctypes.c_void_p]
    x.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
    x.XSetErrorHandler.restype = ctypes.c_void_p
    x.XSetErrorHandler.argtypes = [_ERROR_HANDLER_T]
    x.XSetErrorHandler(_error_handler)
    _xlib = x
    return x


def _get_property(x: Any, dpy: int, win: int, atom: int, req_type: int) -> tuple[int, bytes]:
    """Return (format, raw bytes) of a window property, or (0, b"") if absent."""
    actual_type = _Atom()
    actual_format = ctypes.c_int()
    nitems = _c_ulong()
    after = _c_ulong()
    prop = ctypes.c_void_p()
    status = x.XGetWindowProperty(
        dpy, win, atom, 0, 1 << 20, 0, req_type, ctypes.byref(actual_type),
        ctypes.byref(actual_format), ctypes.byref(nitems), ctypes.byref(after),
        ctypes.byref(prop),
    )
    if status != 0 or not prop.value:
        return 0, b""
    try:
        fmt = actual_format.value
        # Xlib hands back format-32 data as C longs, not 32-bit ints.
        unit = {8: 1, 16: 2, 32: ctypes.sizeof(ctypes.c_long)}.get(fmt, 1)
        return fmt, ctypes.string_at(prop.value, nitems.value * unit)
    finally:
        x.XFree(prop)


def _find_windows(x: Any, dpy: int) -> list[tuple[int, str]]:
    root = x.XDefaultRootWindow(dpy)
    client_list = x.XInternAtom(dpy, b"_NET_CLIENT_LIST", 0)
    net_wm_name = x.XInternAtom(dpy, b"_NET_WM_NAME", 0)
    utf8 = x.XInternAtom(dpy, b"UTF8_STRING", 0)
    fmt, raw = _get_property(x, dpy, root, client_list, XA_WINDOW)
    if fmt != 32:
        raise CaptureError(
            "window_not_found",
            "The window manager publishes no _NET_CLIENT_LIST, so ArmorPaint's window "
            "cannot be located.",
        )
    size = ctypes.sizeof(ctypes.c_ulong)
    ids = [int.from_bytes(raw[i : i + size], sys.byteorder) for i in range(0, len(raw), size)]
    found: list[tuple[int, str]] = []
    for wid in ids:
        hint = _XClassHint()
        if not x.XGetClassHint(dpy, wid, ctypes.byref(hint)):
            continue
        try:
            names = " ".join(
                ctypes.string_at(v).decode("utf-8", "replace")
                for v in (hint.res_name, hint.res_class)
                if v
            )
        finally:
            if hint.res_name:
                x.XFree(hint.res_name)
            if hint.res_class:
                x.XFree(hint.res_class)
        if APP_MARKER not in names:
            continue
        _, title = _get_property(x, dpy, wid, net_wm_name, utf8)
        found.append((wid, title.decode("utf-8", "replace")))
    return found


def _bgrx_to_rgb_rows(
    data: bytes, bpl: int, box: tuple[int, int, int, int], step: int
) -> tuple[bytes, int, int]:
    """Crop a 32-bit LSB-first BGRX image and turn it into PNG scanlines (filter 0)."""
    x0, y0, w, h = box
    out_w = len(range(0, w, step))
    rows = []
    for y in range(y0, y0 + h, step):
        start = y * bpl + x0 * 4
        src = data[start : start + w * 4]
        rgb = bytearray(out_w * 3)
        rgb[0::3] = src[2 :: 4 * step]
        rgb[1::3] = src[1 :: 4 * step]
        rgb[2::3] = src[0 :: 4 * step]
        rows.append(b"\x00" + bytes(rgb))
    return b"".join(rows), out_w, len(rows)


def _png(width: int, height: int, scanlines: bytes) -> bytes:
    def chunk(tag: bytes, body: bytes) -> bytes:
        return (
            struct.pack(">I", len(body))
            + tag
            + body
            + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)  # 8-bit RGB
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(scanlines, 6))
        + chunk(b"IEND", b"")
    )


def _capture_x11(
    title_hint: str | None, crop: tuple[int, int, int, int] | None, downscale: int
) -> Capture:
    x = _load_xlib()
    display = os.environ.get("DISPLAY") or ":0"
    dpy = x.XOpenDisplay(display.encode())
    if not dpy:
        raise CaptureError(
            "unsupported",
            f"Could not open X display {display!r}. ArmorPaint on Linux is an X11 client "
            f"(XWayland on Wayland desktops); this server needs DISPLAY to reach the same "
            f"display.",
        )
    del _last_x_error[:]
    try:
        windows = _find_windows(x, dpy)
        if not windows:
            raise CaptureError(
                "window_not_found",
                f"No ArmorPaint window on display {display}. Is ArmorPaint running on this "
                f"machine and display?",
            )
        wid, title = windows[0]
        if title_hint:
            for cand, cand_title in windows:
                if cand_title == title_hint:
                    wid, title = cand, cand_title
                    break

        attrs = _XWindowAttributes()
        if not x.XGetWindowAttributes(dpy, wid, ctypes.byref(attrs)):
            raise CaptureError("capture_failed", f"Could not read window {wid:#x}'s attributes.")
        if attrs.map_state != IS_VIEWABLE:
            raise CaptureError(
                "window_hidden",
                "ArmorPaint's window is minimised or unmapped, so it has no pixels to read. "
                "Restore it (it can stay behind other windows).",
            )
        ww, wh = attrs.width, attrs.height

        if crop is None:
            box = (0, 0, ww, wh)
        else:
            cx, cy, cw, ch = crop
            cx, cy = max(0, cx), max(0, cy)
            cw, ch = min(cw, ww - cx), min(ch, wh - cy)
            if cw <= 0 or ch <= 0:
                raise CaptureError(
                    "bad_args", f"crop {list(crop)} lies outside the {ww}x{wh} window."
                )
            box = (cx, cy, cw, ch)

        img = x.XGetImage(dpy, wid, 0, 0, ww, wh, ALL_PLANES, ZPIXMAP)
        x.XSync(dpy, 0)
        if not img or _last_x_error:
            raise CaptureError(
                "capture_failed",
                "XGetImage failed on ArmorPaint's window (it may have been resized or "
                "unmapped mid-capture). Try again.",
            )
        try:
            im = img.contents
            if im.bits_per_pixel != 32 or im.byte_order != 0 or im.red_mask != 0xFF0000:
                raise CaptureError(
                    "unsupported",
                    f"Unexpected pixel format (bpp={im.bits_per_pixel}, "
                    f"byte_order={im.byte_order}, red_mask={im.red_mask:#x}); only 32-bit "
                    f"little-endian BGRX is handled.",
                )
            raw = ctypes.string_at(im.data, im.bytes_per_line * im.height)
            scan, out_w, out_h = _bgrx_to_rgb_rows(raw, im.bytes_per_line, box, downscale)
        finally:
            x.XDestroyImage(img)
    finally:
        x.XCloseDisplay(dpy)

    return Capture(
        png=_png(out_w, out_h, scan),
        width=out_w,
        height=out_h,
        window_width=ww,
        window_height=wh,
        window_id=wid,
        window_title=title,
        method="x11 XGetImage",
    )


def capture_window(
    title_hint: str | None = None,
    crop: tuple[int, int, int, int] | None = None,
    downscale: int = 1,
) -> Capture:
    """Grab ArmorPaint's window as a PNG. Raises CaptureError with an actionable message."""
    if not 1 <= downscale <= MAX_DOWNSCALE:
        raise CaptureError("bad_args", f"downscale must be 1..{MAX_DOWNSCALE}.")
    if not sys.platform.startswith("linux"):
        raise CaptureError(
            "unsupported",
            f"Window capture is implemented for Linux (X11/XWayland) only; this is "
            f"{sys.platform}. Use ap_export_textures + ap_read_image_file to see results.",
        )
    with _lock:
        return _capture_x11(title_hint, crop, downscale)
