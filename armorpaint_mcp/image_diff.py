"""Before/after comparison of two captures: what changed, where, and a picture of it.

Pure Python over packed 8-bit RGB. Rows are compared as whole byte strings first, and
only differing rows are scanned pixel by pixel, so a stroke that touched a small region
of a large window costs little.
"""

from __future__ import annotations

from typing import Any

from .window_capture import _png

DEFAULT_THRESHOLD = 8  # per-channel difference below which a pixel counts as unchanged

# Changes smaller than this share of the image are reported, but do not count as a visible
# change. Measured live: a stroke that paints nothing still moves the brush cursor ring,
# 18-74 changed pixels of 412 800 (0.018 %).
NOISE_FRACTION = 0.0003


def noise_floor(w: int, h: int) -> int:
    return int(w * h * NOISE_FRACTION)


def _first_diff(a: bytes, b: bytes) -> int:
    """Index of the first differing byte (a != b, same length), by bisection on slices."""
    lo, hi = 0, len(a)
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if a[lo:mid] != b[lo:mid]:
            hi = mid
        else:
            lo = mid
    return lo


def _last_diff(a: bytes, b: bytes) -> int:
    lo, hi = 0, len(a)
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if a[mid:hi] != b[mid:hi]:
            lo = mid
        else:
            hi = mid
    return lo


def diff(
    w: int, h: int, a: bytes, b: bytes, *, threshold: int = DEFAULT_THRESHOLD,
    w2: int | None = None, h2: int | None = None,
) -> dict[str, Any]:
    """Compare two same-sized RGB images. A pixel has changed when any channel moved by
    more than ``threshold``."""
    if (w2 is not None and w2 != w) or (h2 is not None and h2 != h) or len(a) != len(b) or len(a) != w * h * 3:
        raise ValueError(f"image size differs ({w}x{h} vs {w2 or w}x{h2 or h}); capture both with the "
                         f"same crop and downscale")
    stride = w * 3
    count = 0
    x0, y0, x1, y1 = w, h, -1, -1
    for y in range(h):
        ra, rb = a[y * stride : (y + 1) * stride], b[y * stride : (y + 1) * stride]
        if ra == rb:
            continue
        start = _first_diff(ra, rb) // 3
        end = _last_diff(ra, rb) // 3
        for x in range(start, end + 1):
            i = x * 3
            if (abs(ra[i] - rb[i]) > threshold or abs(ra[i + 1] - rb[i + 1]) > threshold
                    or abs(ra[i + 2] - rb[i + 2]) > threshold):
                count += 1
                x0, x1 = min(x0, x), max(x1, x)
                y0, y1 = min(y0, y), max(y1, y)
    bbox = [x0, y0, x1 - x0 + 1, y1 - y0 + 1] if count else None
    floor = noise_floor(w, h)
    return {
        "changed_pixels": count,
        "changed_fraction": count / (w * h) if w * h else 0.0,
        "bbox": bbox,
        "no_visible_change": count <= floor,
        "noise_floor": floor,
        "size": [w, h],
    }


def highlight(
    w: int, h: int, a: bytes, b: bytes, bbox: list[int], *, pad: int = 16,
    threshold: int = DEFAULT_THRESHOLD,
) -> bytes:
    """A PNG of the 'after' image around ``bbox``: changed pixels tinted magenta, the rest
    dimmed, so the change is what the eye lands on."""
    bx, by, bw, bh = bbox
    cx0, cy0 = max(0, bx - pad), max(0, by - pad)
    cx1, cy1 = min(w, bx + bw + pad), min(h, by + bh + pad)
    rows = []
    for y in range(cy0, cy1):
        row = bytearray(b"\x00")
        for x in range(cx0, cx1):
            i = (y * w + x) * 3
            r, g, bl = b[i], b[i + 1], b[i + 2]
            if (abs(a[i] - r) > threshold or abs(a[i + 1] - g) > threshold or abs(a[i + 2] - bl) > threshold):
                row += bytes(((r + 255) // 2, g // 2, (bl + 255) // 2))
            else:
                row += bytes((r // 3, g // 3, bl // 3))
        rows.append(bytes(row))
    return _png(cx1 - cx0, cy1 - cy0, b"".join(rows))


def contact_sheet(frames: list[tuple[int, int, bytes]], *, columns: int = 4, gap: int = 4) -> bytes:
    """Tile same-sized RGB frames into one PNG, left to right, top to bottom."""
    if not frames:
        raise ValueError("no frames")
    w, h = frames[0][0], frames[0][1]
    columns = max(1, min(columns, len(frames)))
    rows_n = (len(frames) + columns - 1) // columns
    sheet_w = columns * w + (columns - 1) * gap
    sheet_h = rows_n * h + (rows_n - 1) * gap
    canvas = bytearray(sheet_w * sheet_h * 3)
    for k, (fw, fh, rgb) in enumerate(frames):
        if (fw, fh) != (w, h):
            raise ValueError("frames differ in size")
        ox, oy = (k % columns) * (w + gap), (k // columns) * (h + gap)
        for y in range(h):
            dst = ((oy + y) * sheet_w + ox) * 3
            canvas[dst : dst + w * 3] = rgb[y * w * 3 : (y + 1) * w * 3]
    rows = b"".join(b"\x00" + bytes(canvas[y * sheet_w * 3 : (y + 1) * sheet_w * 3]) for y in range(sheet_h))
    return _png(sheet_w, sheet_h, rows)
