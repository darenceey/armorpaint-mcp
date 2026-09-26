"""
Capture ArmorPaint's rendered window from OUTSIDE the app.
==========================================================

Why this exists: a stock ArmorPaint 1.0 plugin cannot write the viewport to a file.
``viewport_save_texture`` encodes into the project's in-memory asset list, and
``iron_encode_png`` / ``gpu_get_texture_pixels`` are not bound (see the README's
Limitations). Upstream added ``viewport_save_texture_to_file`` on 2026-09-09
(commit 1e14e27e), but binaries built before that still lack it. So this server takes the
picture itself: it reads the ArmorPaint window's pixels from the display server.

One backend per platform, all reading the window's OWN pixels (so a covered window still
captures and focus is never stolen); a minimised window has no pixels on any of them:

* **Linux** -- ``XGetImage`` on the window. Iron's Linux backend is X11-only
  (``base/sources/backends/linux_system.c`` has no Wayland path), so on a Wayland desktop
  ArmorPaint runs under XWayland and is still an X window. Verified.
* **Windows** -- ``PrintWindow(..., PW_RENDERFULLCONTENT)``, which asks DWM for the
  window's composed content including its Direct3D 12 swap chain (Windows 8.1+), then
  ``GetDIBits``. Implemented against the Win32 documentation; not yet run on hardware.
* **macOS** -- the window id from ``CGWindowListCopyWindowInfo``, then the system
  ``screencapture -l <id>``. Needs the Screen Recording permission for whatever app runs
  this server. Implemented, untested.

No dependencies beyond the OS libraries via ctypes: no ImageMagick, no Pillow. The PNG
encoder and the small decoder used to crop macOS captures are at the bottom.
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
    rgb: bytes | None = None  # packed 8-bit RGB, filled lazily by pixels()

    def pixels(self) -> bytes:
        """The image as packed RGB rows (for diffing), decoded from the PNG once."""
        if self.rgb is None:
            _, _, self.rgb = _decode_png_rgb(self.png)
        return self.rgb

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


def _crop_box(crop: tuple[int, int, int, int] | None, ww: int, wh: int) -> tuple[int, int, int, int]:
    if crop is None:
        return (0, 0, ww, wh)
    cx, cy, cw, ch = crop
    cx, cy = max(0, cx), max(0, cy)
    cw, ch = min(cw, ww - cx), min(ch, wh - cy)
    if cw <= 0 or ch <= 0:
        raise CaptureError("bad_args", f"crop {list(crop)} lies outside the {ww}x{wh} window.")
    return (cx, cy, cw, ch)


# ---------------------------------------------------------------------------
# Windows: PrintWindow + GetDIBits
# ---------------------------------------------------------------------------

PW_RENDERFULLCONTENT = 0x2


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_int32), ("biHeight", ctypes.c_int32),
        ("biPlanes", ctypes.c_uint16), ("biBitCount", ctypes.c_uint16),
        ("biCompression", ctypes.c_uint32), ("biSizeImage", ctypes.c_uint32),
        ("biXPelsPerMeter", ctypes.c_int32), ("biYPelsPerMeter", ctypes.c_int32),
        ("biClrUsed", ctypes.c_uint32), ("biClrImportant", ctypes.c_uint32),
    ]


def _capture_win32(
    title_hint: str | None, crop: tuple[int, int, int, int] | None, downscale: int
) -> Capture:
    from ctypes import wintypes

    try:
        from .desktop_input import InputError, _win32_find_window
    except ImportError:
        from desktop_input import InputError, _win32_find_window  # type: ignore[no-redef]

    user32 = ctypes.WinDLL("user32", use_last_error=True)  # type: ignore[attr-defined]
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)  # type: ignore[attr-defined]
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetDC.argtypes = [wintypes.HWND]
    user32.GetDC.restype = wintypes.HDC
    user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
    gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    gdi32.CreateCompatibleDC.restype = wintypes.HDC
    gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
    gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
    gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    gdi32.SelectObject.restype = wintypes.HGDIOBJ
    gdi32.GetDIBits.argtypes = [
        wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT, ctypes.c_void_p,
        ctypes.c_void_p, wintypes.UINT,
    ]
    gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    gdi32.DeleteDC.argtypes = [wintypes.HDC]

    try:
        hwnd, title = _win32_find_window(user32, title_hint)
    except InputError as exc:
        raise CaptureError(exc.code, exc.message) from exc
    if user32.IsIconic(hwnd):
        raise CaptureError(
            "window_hidden",
            "ArmorPaint's window is minimised, so it has no pixels to read. Restore it (it "
            "can stay behind other windows).",
        )
    rect = wintypes.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(rect))
    ww, wh = rect.right - rect.left, rect.bottom - rect.top
    if ww <= 0 or wh <= 0:
        raise CaptureError("capture_failed", "ArmorPaint's window has an empty client area.")
    box = _crop_box(crop, ww, wh)

    hdc_win = user32.GetDC(hwnd)
    hdc_mem = gdi32.CreateCompatibleDC(hdc_win)
    bmp = gdi32.CreateCompatibleBitmap(hdc_win, ww, wh)
    old = gdi32.SelectObject(hdc_mem, bmp)
    try:
        # Client area only, including DirectX content composed by DWM.
        if not user32.PrintWindow(hwnd, hdc_mem, 0x1 | PW_RENDERFULLCONTENT):
            raise CaptureError("capture_failed", "PrintWindow failed on ArmorPaint's window.")
        bih = _BITMAPINFOHEADER()
        bih.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        bih.biWidth, bih.biHeight = ww, -wh  # negative: top-down rows
        bih.biPlanes, bih.biBitCount, bih.biCompression = 1, 32, 0  # BI_RGB
        buf = ctypes.create_string_buffer(ww * wh * 4)
        if gdi32.GetDIBits(hdc_mem, bmp, 0, wh, buf, ctypes.byref(bih), 0) != wh:
            raise CaptureError("capture_failed", "GetDIBits returned fewer rows than requested.")
        scan, out_w, out_h = _bgrx_to_rgb_rows(buf.raw, ww * 4, box, downscale)
    finally:
        gdi32.SelectObject(hdc_mem, old)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(hdc_mem)
        user32.ReleaseDC(hwnd, hdc_win)

    return Capture(
        png=_png(out_w, out_h, scan), width=out_w, height=out_h, window_width=ww,
        window_height=wh, window_id=int(hwnd or 0), window_title=title,
        method="win32 PrintWindow(PW_RENDERFULLCONTENT)",
    )


# ---------------------------------------------------------------------------
# macOS: CGWindowListCopyWindowInfo + screencapture -l
# ---------------------------------------------------------------------------


def _mac_find_window(title_hint: str | None) -> dict[str, Any]:
    """Locate ArmorPaint's on-screen window: id, owner pid, bounds (points), title."""
    cf = ctypes.CDLL(ctypes.util.find_library("CoreFoundation"))
    cg = ctypes.CDLL(
        ctypes.util.find_library("CoreGraphics")
        or "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics"
    )
    vp = ctypes.c_void_p
    cg.CGWindowListCopyWindowInfo.restype = vp
    cg.CGWindowListCopyWindowInfo.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
    cf.CFArrayGetCount.restype = ctypes.c_long
    cf.CFArrayGetCount.argtypes = [vp]
    cf.CFArrayGetValueAtIndex.restype = vp
    cf.CFArrayGetValueAtIndex.argtypes = [vp, ctypes.c_long]
    cf.CFDictionaryGetValue.restype = vp
    cf.CFDictionaryGetValue.argtypes = [vp, vp]
    cf.CFStringCreateWithCString.restype = vp
    cf.CFStringCreateWithCString.argtypes = [vp, ctypes.c_char_p, ctypes.c_uint32]
    cf.CFStringGetCString.restype = ctypes.c_bool
    cf.CFStringGetCString.argtypes = [vp, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
    cf.CFNumberGetValue.restype = ctypes.c_bool
    cf.CFNumberGetValue.argtypes = [vp, ctypes.c_int, vp]
    cf.CFRelease.argtypes = [vp]
    utf8 = 0x08000100

    def key(name: str) -> int:
        return cf.CFStringCreateWithCString(None, name.encode(), utf8)

    def text(ref: int | None) -> str:
        if not ref:
            return ""
        out = ctypes.create_string_buffer(1024)
        return out.value.decode("utf-8", "replace") if cf.CFStringGetCString(ref, out, 1024, utf8) else ""

    def num(ref: int | None) -> float:
        if not ref:
            return 0.0
        val = ctypes.c_double()
        cf.CFNumberGetValue(ref, 13, ctypes.byref(val))  # kCFNumberDoubleType
        return val.value

    k_owner, k_name, k_num, k_pid = key("kCGWindowOwnerName"), key("kCGWindowName"), key("kCGWindowNumber"), key("kCGWindowOwnerPID")
    k_layer, k_bounds = key("kCGWindowLayer"), key("kCGWindowBounds")
    k_x, k_y, k_w, k_h = key("X"), key("Y"), key("Width"), key("Height")
    # kCGWindowListOptionOnScreenOnly | kCGWindowListExcludeDesktopElements
    arr = cg.CGWindowListCopyWindowInfo(1 | 16, 0)
    if not arr:
        raise CaptureError("unsupported", "CGWindowListCopyWindowInfo returned nothing.")
    found: list[dict[str, Any]] = []
    try:
        for i in range(cf.CFArrayGetCount(arr)):
            d = cf.CFArrayGetValueAtIndex(arr, i)
            owner = text(cf.CFDictionaryGetValue(d, k_owner))
            if APP_MARKER not in owner or num(cf.CFDictionaryGetValue(d, k_layer)) != 0:
                continue
            b = cf.CFDictionaryGetValue(d, k_bounds)
            bounds = tuple(num(cf.CFDictionaryGetValue(b, k)) for k in (k_x, k_y, k_w, k_h)) if b else (0, 0, 0, 0)
            found.append(
                {
                    "id": int(num(cf.CFDictionaryGetValue(d, k_num))),
                    "pid": int(num(cf.CFDictionaryGetValue(d, k_pid))),
                    "title": text(cf.CFDictionaryGetValue(d, k_name)) or owner,
                    "bounds": bounds,
                }
            )
    finally:
        cf.CFRelease(arr)
        for k in (k_owner, k_name, k_num, k_pid, k_layer, k_bounds, k_x, k_y, k_w, k_h):
            cf.CFRelease(k)
    if not found:
        raise CaptureError(
            "window_not_found",
            "No on-screen ArmorPaint window. (A minimised window is not on screen; macOS "
            "also hides other apps' window titles until Screen Recording is granted.)",
        )
    for w in found:
        if title_hint and w["title"] == title_hint:
            return w
    return max(found, key=lambda w: w["bounds"][2] * w["bounds"][3])


def _capture_macos(
    title_hint: str | None, crop: tuple[int, int, int, int] | None, downscale: int
) -> Capture:
    import subprocess
    import tempfile

    win = _mac_find_window(title_hint)
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "armorpaint.png")
        # -x: no sound, -o: no window shadow, -l: this window only (even if covered).
        proc = subprocess.run(
            ["screencapture", "-x", "-o", "-l", str(win["id"]), out], capture_output=True, timeout=20
        )
        if proc.returncode != 0 or not os.path.isfile(out) or os.path.getsize(out) == 0:
            raise CaptureError(
                "capture_failed",
                "screencapture could not read ArmorPaint's window. Grant Screen Recording to "
                "the app that runs this MCP server (System Settings > Privacy & Security).",
            )
        data = open(out, "rb").read()
    ww, wh, rgb = _decode_png_rgb(data)
    box = _crop_box(crop, ww, wh)
    x0, y0, w, h = box
    rows = []
    for y in range(y0, y0 + h, downscale):
        start = (y * ww + x0) * 3
        row = rgb[start : start + w * 3]
        if downscale > 1:
            px = [row[i : i + 3] for i in range(0, len(row), 3 * downscale)]
            row = b"".join(px)
        rows.append(b"\x00" + bytes(row))
    out_w = len(range(0, w, downscale))
    return Capture(
        png=_png(out_w, len(rows), b"".join(rows)), width=out_w, height=len(rows),
        window_width=ww, window_height=wh, window_id=win["id"], window_title=win["title"],
        method="macos screencapture -l",
    )


def _decode_png_rgb(data: bytes) -> tuple[int, int, bytes]:
    """Minimal decoder for 8-bit, non-interlaced RGB/RGBA PNG (what screencapture writes)."""
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise CaptureError("capture_failed", "screencapture did not produce a PNG.")
    pos, idat = 8, []
    width = height = color = 0
    while pos < len(data):
        (length,) = struct.unpack(">I", data[pos : pos + 4])
        tag = data[pos + 4 : pos + 8]
        body = data[pos + 8 : pos + 8 + length]
        if tag == b"IHDR":
            width, height, depth, color, _, _, interlace = struct.unpack(">IIBBBBB", body)
            if depth != 8 or color not in (2, 6) or interlace:
                raise CaptureError("unsupported", f"PNG format depth={depth} color={color} is not handled.")
        elif tag == b"IDAT":
            idat.append(body)
        pos += 12 + length
    bpp = 4 if color == 6 else 3
    raw = zlib.decompress(b"".join(idat))
    stride = width * bpp
    out = bytearray(width * height * 3)
    prev = bytearray(stride)
    for y in range(height):
        f = raw[y * (stride + 1)]
        line = bytearray(raw[y * (stride + 1) + 1 : (y + 1) * (stride + 1)])
        for i in range(stride if f else 0):  # filter 0 (what the capture backends write): as is
            a = line[i - bpp] if i >= bpp else 0
            b = prev[i]
            c = prev[i - bpp] if i >= bpp else 0
            if f == 1:
                line[i] = (line[i] + a) & 0xFF
            elif f == 2:
                line[i] = (line[i] + b) & 0xFF
            elif f == 3:
                line[i] = (line[i] + ((a + b) >> 1)) & 0xFF
            elif f == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                line[i] = (line[i] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 0xFF
        if bpp == 3:
            out[y * width * 3 : (y + 1) * width * 3] = line
        else:
            o = out[y * width * 3 : (y + 1) * width * 3]
            o[0::3], o[1::3], o[2::3] = line[0::4], line[1::4], line[2::4]
            out[y * width * 3 : (y + 1) * width * 3] = o
        prev = line
    return width, height, bytes(out)


def capture_window(
    title_hint: str | None = None,
    crop: tuple[int, int, int, int] | None = None,
    downscale: int = 1,
) -> Capture:
    """Grab ArmorPaint's window as a PNG. Raises CaptureError with an actionable message."""
    if not 1 <= downscale <= MAX_DOWNSCALE:
        raise CaptureError("bad_args", f"downscale must be 1..{MAX_DOWNSCALE}.")
    with _lock:
        if sys.platform.startswith("linux"):
            return _capture_x11(title_hint, crop, downscale)
        if sys.platform == "win32":
            return _capture_win32(title_hint, crop, downscale)
        if sys.platform == "darwin":
            return _capture_macos(title_hint, crop, downscale)
    raise CaptureError(
        "unsupported",
        f"Window capture has no backend for {sys.platform}. Use ap_export_textures + "
        f"ap_read_image_file to see results.",
    )
