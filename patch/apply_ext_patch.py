#!/usr/bin/env python3
"""Add the armorpaint-mcp native extension to an ArmorPaint source checkout.

WHY THIS EXISTS
---------------
ArmorPaint's plugin API (minic) has no binding for layer management, undo/redo, export
format / bit depth / preset, bake runs and bake parameters, render settings (tone, LUT,
post-processing), the texture-set resolution, the live project lists or camera views.
Those are not gaps a plugin can work around: the functions exist inside ArmorPaint, but
nothing exposes them. This patch exposes them through ONE new binding,

    char *mcp_ext_call(char *op, any_map_t *args, char *prefix)

implemented in ``patch/mcp_ext.c``. The bridge plugin detects the binding at run time, so the
same plugin file works on a stock build (those tools answer ``unsupported``) and on a patched
one (they work). It also provides a file-based viewport capture, so a patched build does not
need the separate viewport patch.

What it changes, all marked ``armorpaint-mcp``:

  * copies ``mcp_ext.c`` to ``paint/sources/mcp_ext.c``
  * ``paint/sources/main.c``           -- one ``#include "mcp_ext.c"`` after the unity-build list
  * ``paint/sources/functions.h``      -- one declaration
  * ``paint/sources/minic_api_list.h`` -- one binding line

Idempotent (re-running updates mcp_ext.c in place), and reversible with --revert.

  python apply_ext_patch.py <path-to-armorpaint-checkout>
  python apply_ext_patch.py <path-to-armorpaint-checkout> --revert

Then rebuild (Linux: ``cd paint && ../base/make --compile``; Windows: ``..\\base\\make`` and
build ``build\\ArmorPaint.sln``; macOS: ``../base/make`` and build the Xcode project).

Upstream is zlib-licensed, which permits modification. This script edits YOUR OWN checkout in
place and publishes no fork; see docs/UPSTREAM_CHANGES.md.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

MARKER = "armorpaint-mcp"
HERE = Path(__file__).resolve().parent
EXT_SOURCE = HERE / "mcp_ext.c"
EXT_DEST = Path("paint/sources/mcp_ext.c")

DECLARATION = f"char *mcp_ext_call(char *op, any_map_t *args, char *prefix); // {MARKER}"
BINDING = (
    'X3(mcp_ext_call, "p:char(p:char op,p:any_map_t args,p:char prefix)", p, p, p, p) '
    f"// {MARKER}"
)
BINDING_ANCHOR = 'X0(iron_delay_idle_sleep, "v()", v)'
INCLUDE = f'#include "mcp_ext.c" // {MARKER}'


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="")
    os.replace(tmp, path)


def _eol(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def _insert_after(text: str, anchor_pred, line: str, what: str) -> str:
    lines = text.splitlines(keepends=True)
    idx = None
    for i, existing in enumerate(lines):
        if anchor_pred(existing.strip()):
            idx = i
    if idx is None:
        raise RuntimeError(f"anchor for {what} not found -- upstream changed; patch by hand")
    lines.insert(idx + 1, line + _eol(text))
    return "".join(lines)


def apply(root: Path) -> int:
    src = root / "paint" / "sources"
    for name in ("main.c", "functions.h", "minic_api_list.h"):
        if not (src / name).is_file():
            print(f"  MISSING  paint/sources/{name} -- is this an ArmorPaint checkout?", file=sys.stderr)
            return 2

    dest = root / EXT_DEST
    if dest.is_file() and dest.read_bytes() == EXT_SOURCE.read_bytes():
        print(f"  skip     {EXT_DEST} (up to date)")
    else:
        shutil.copyfile(EXT_SOURCE, dest)
        print(f"  copied   {EXT_DEST}")

    try:
        main_c = src / "main.c"
        text = _read(main_c)
        if INCLUDE in text:
            print("  skip     paint/sources/main.c (already patched)")
        else:
            # After the LAST .c include of the unity build, so every function and file-static
            # global mcp_ext.c calls is already defined above it.
            text = _insert_after(
                text,
                lambda s: s.startswith('#include "') and s.endswith('.c"'),
                INCLUDE,
                "main.c's include list",
            )
            _write_atomic(main_c, text)
            print("  patched  paint/sources/main.c")

        functions_h = src / "functions.h"
        text = _read(functions_h)
        if DECLARATION in text:
            print("  skip     paint/sources/functions.h (already patched)")
        else:
            if not text.endswith(("\n", "\r\n")):
                text += _eol(text)
            _write_atomic(functions_h, text + DECLARATION + _eol(text))
            print("  patched  paint/sources/functions.h")

        api = src / "minic_api_list.h"
        text = _read(api)
        if BINDING in text:
            print("  skip     paint/sources/minic_api_list.h (already patched)")
        else:
            text = _insert_after(text, lambda s: s == BINDING_ANCHOR, BINDING, "minic_api_list.h")
            _write_atomic(api, text)
            print("  patched  paint/sources/minic_api_list.h")
    except RuntimeError as exc:
        print(f"  FAILED   {exc}", file=sys.stderr)
        return 2

    print("\nDone. Rebuild ArmorPaint (Linux: cd paint && ../base/make --compile).")
    return 0


def revert(root: Path) -> int:
    src = root / "paint" / "sources"
    for name in ("main.c", "functions.h", "minic_api_list.h"):
        path = src / name
        if not path.is_file():
            continue
        text = _read(path)
        kept = [l for l in text.splitlines(keepends=True) if not (MARKER in l and ("mcp_ext" in l))]
        new = "".join(kept)
        if new != text:
            _write_atomic(path, new)
            print(f"  reverted paint/sources/{name}")
    dest = root / EXT_DEST
    if dest.is_file():
        dest.unlink()
        print(f"  removed  {EXT_DEST}")
    print("\nReverted. Rebuild ArmorPaint.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("checkout", type=Path, help="Path to the ArmorPaint source checkout")
    ap.add_argument("--revert", action="store_true", help="Remove the extension instead of adding it")
    args = ap.parse_args()

    root = args.checkout.expanduser().resolve()
    if not (root / "paint" / "sources").is_dir():
        print(f"Not an ArmorPaint checkout (no paint/sources): {root}", file=sys.stderr)
        return 2
    print(f"{'Reverting' if args.revert else 'Applying'} {MARKER} native extension in {root}\n")
    return revert(root) if args.revert else apply(root)


if __name__ == "__main__":
    raise SystemExit(main())
