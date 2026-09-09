#!/usr/bin/env python3
"""Add file-based viewport capture to an ArmorPaint source checkout.

WHY THIS EXISTS
---------------
ArmorPaint's plugin API (minic) can already *capture* the viewport into a GPU texture
(`viewport_capture_screenshot_to`), but it cannot get those pixels onto disk:

  * `viewport_save_texture()` encodes the PNG into `g_project->packed_assets` — an in-memory
    list persisted only inside the .arm on save. An external tool cannot read it.
  * `iron_encode_png` and `gpu_get_texture_pixels` are not in the minic binding table.

So an MCP agent driving ArmorPaint through a plugin is blind to the 3D viewport. It can still see
its work via `export_texture_run` (which does write real files), but it cannot see the shaded
result in context.

This patch closes that gap with the smallest possible change: one convenience function that wraps
the already-existing `iron_write_png`, plus the one binding line that exposes it to plugins.
It adds no socket, no thread, and no new dependency.

Total diff: 3 hunks, ~10 added lines. Idempotent, and reversible with --revert.

  python apply_viewport_patch.py <path-to-armorpaint-checkout>
  python apply_viewport_patch.py <path-to-armorpaint-checkout> --revert

After patching, rebuild:
  cd paint && ../base/make
  MSBuild build/ArmorPaint.vcxproj -p:Configuration=Release -p:Platform=x64 \
      -p:LLVMInstallDir="C:\\Program Files\\LLVM"

Upstream is zlib-licensed, which permits modification. This script edits YOUR OWN checkout in
place and publishes no fork; see docs/UPSTREAM_CHANGES.md for the exact diff.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

MARKER = "armorpaint-mcp"

# Each hunk: (relative path, anchor line to insert AFTER, text to insert).
# Anchors are matched on stripped equality so trailing-whitespace churn upstream does not break us.
HUNKS = [
    (
        Path("paint/sources/viewport.c"),
        "}",  # resolved specially: the closing brace of viewport_save_texture
        """
// --- {marker}: begin -------------------------------------------------------
// Write a captured viewport texture straight to a PNG on disk.
// Upstream's viewport_save_texture() encodes into the project's in-memory packed_assets, which an
// external process cannot read. This variant uses iron_write_png so an MCP agent can see its work.
void viewport_save_texture_to_file(gpu_texture_t *screenshot, char *path) {{
\tiron_write_png(path, gpu_get_texture_pixels(screenshot), screenshot->width, screenshot->height, 0);
}}
// --- {marker}: end ---------------------------------------------------------
""".format(marker=MARKER),
    ),
    (
        Path("paint/sources/functions.h"),
        "void                      viewport_save_texture(gpu_texture_t *screenshot);",
        "void                      viewport_save_texture_to_file(gpu_texture_t *screenshot, char *path); "
        f"// {MARKER}",
    ),
    (
        Path("paint/sources/minic_api_list.h"),
        'X1(viewport_save_texture, "v(p:gpu_texture_t screenshot)", v, p)',
        'X2(viewport_save_texture_to_file, "v(p:gpu_texture_t screenshot,p:char path)", v, p, p) '
        f"// {MARKER}",
    ),
]


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _write_atomic(path: Path, text: str) -> None:
    """Write via temp + os.replace. A truncating open() would destroy the file if we crash midway."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="")
    os.replace(tmp, path)


def _find_viewport_anchor(text: str) -> int:
    """Return the index just past the closing brace of viewport_save_texture().

    We cannot anchor on a bare '}' — the file is full of them. Instead find the function, then walk
    braces to its end. If the function ever disappears upstream, this raises rather than guessing.
    """
    start = text.find("void viewport_save_texture(gpu_texture_t *screenshot) {")
    if start == -1:
        raise RuntimeError(
            "viewport_save_texture() not found in viewport.c — upstream changed. "
            "Re-derive the patch by hand; do not force it."
        )
    depth = 0
    i = text.index("{", start)
    while i < len(text):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    raise RuntimeError("Unbalanced braces after viewport_save_texture() — refusing to patch.")


def apply(root: Path) -> int:
    changed = 0
    for rel, anchor, insert in HUNKS:
        path = root / rel
        if not path.is_file():
            print(f"  MISSING  {rel} — is this an ArmorPaint checkout?", file=sys.stderr)
            return 2
        text = _read(path)

        if MARKER in text:
            print(f"  skip     {rel} (already patched)")
            continue

        if rel.name == "viewport.c":
            pos = _find_viewport_anchor(text)
            new = text[:pos] + "\n" + insert + text[pos:]
        else:
            lines = text.splitlines(keepends=True)
            for idx, line in enumerate(lines):
                if line.strip() == anchor.strip():
                    eol = "\n" if not line.endswith("\r\n") else "\r\n"
                    lines.insert(idx + 1, insert.rstrip() + eol)
                    break
            else:
                print(f"  FAILED   {rel}: anchor not found:\n           {anchor}", file=sys.stderr)
                return 2
            new = "".join(lines)

        _write_atomic(path, new)
        print(f"  patched  {rel}")
        changed += 1

    if changed == 0:
        print("\nAlready fully patched — nothing to do.")
    else:
        print(f"\nPatched {changed} file(s). Now regenerate and rebuild:")
        print("  cd paint && ../base/make")
        print('  MSBuild build/ArmorPaint.vcxproj -p:Configuration=Release -p:Platform=x64 '
              '-p:LLVMInstallDir="C:\\Program Files\\LLVM"')
    return 0


def revert(root: Path) -> int:
    changed = 0
    for rel, _anchor, _insert in HUNKS:
        path = root / rel
        if not path.is_file():
            continue
        text = _read(path)
        if MARKER not in text:
            continue

        if rel.name == "viewport.c":
            begin = text.find(f"// --- {MARKER}: begin")
            end_tag = f"// --- {MARKER}: end"
            end = text.find(end_tag)
            if begin == -1 or end == -1:
                print(f"  FAILED   {rel}: block markers not found; revert by hand", file=sys.stderr)
                return 2
            end = text.index("\n", end) + 1
            # also swallow the blank line we inserted before the block
            while begin > 0 and text[begin - 1] == "\n" and text[begin - 2:begin - 1] == "\n":
                begin -= 1
            new = text[:begin] + text[end:]
        else:
            new = "".join(l for l in text.splitlines(keepends=True) if MARKER not in l)

        _write_atomic(path, new)
        print(f"  reverted {rel}")
        changed += 1

    print(f"\nReverted {changed} file(s)." if changed else "\nNot patched — nothing to revert.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("checkout", type=Path, help="Path to the ArmorPaint source checkout")
    ap.add_argument("--revert", action="store_true", help="Remove the patch instead of applying it")
    args = ap.parse_args()

    root = args.checkout.expanduser().resolve()
    if not (root / "paint" / "sources").is_dir():
        print(f"Not an ArmorPaint checkout (no paint/sources): {root}", file=sys.stderr)
        return 2

    print(f"{'Reverting' if args.revert else 'Applying'} {MARKER} viewport patch in {root}\n")
    return revert(root) if args.revert else apply(root)


if __name__ == "__main__":
    raise SystemExit(main())
