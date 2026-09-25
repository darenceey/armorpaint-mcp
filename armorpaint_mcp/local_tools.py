"""
Tools answered by this server from the filesystem: resource search and project metadata.
========================================================================================

ArmorPaint's plugin API has no shelf or resource-database binding, and a project has no
metadata store. Neither needs one to be useful to an agent:

* **Resource search** walks the places resources actually live -- ArmorPaint's own ``data``
  directory (bundled meshes, export presets, envmaps, fonts), the open project's folder, and
  any library folders the user names -- and returns files by kind and name. Anything it
  finds is imported with ``ap_import_asset`` / ``ap_import_envmap``.
* **Project metadata** is a JSON sidecar next to the ``.arm`` (``<project>.arm.mcp.json``):
  notes, a material brief, texel-density targets -- whatever an agent should remember about
  a project between sessions. The ``.arm`` itself is never touched.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import sys
from pathlib import Path
from typing import Any

LIBRARY_ENV = "ARMORPAINT_LIBRARY"  # os.pathsep-separated extra resource folders

KINDS: dict[str, tuple[str, ...]] = {
    "texture": (".png", ".jpg", ".jpeg", ".tga", ".bmp", ".psd", ".tif", ".tiff", ".hdr", ".exr", ".k"),
    "envmap": (".hdr", ".exr"),
    "mesh": (".obj", ".fbx", ".gltf", ".glb", ".blend", ".stl", ".ply"),
    "material": (".arm",),
    "font": (".ttf", ".otf"),
    "lut": (".cube",),
    "export_preset": (".json",),
}

MAX_FILES_SCANNED = 60_000
MAX_DEPTH = 7
SKIP_DIRS = {".git", "__pycache__", "node_modules", "mcp_spool", ".cache"}


def _armorpaint_data_dirs() -> list[tuple[Path, str]]:
    """Where ArmorPaint's own data directory is, as far as this machine can tell."""
    out: list[tuple[Path, str]] = []
    env_dir = os.environ.get("ARMORPAINT_DIR", "").strip()
    if env_dir:
        out.append((Path(env_dir).expanduser() / "data", "$ARMORPAINT_DIR"))
    env_exe = os.environ.get("ARMORPAINT_EXE", "").strip()
    if env_exe:
        out.append((Path(env_exe).expanduser().parent / "data", "$ARMORPAINT_EXE"))
    if sys.platform.startswith("linux"):
        # The running process's executable is the most reliable pointer on Linux.
        proc = Path("/proc")
        try:
            for pid_dir in proc.iterdir():
                if not pid_dir.name.isdigit():
                    continue
                try:
                    exe = Path(os.readlink(pid_dir / "exe"))
                except OSError:
                    continue
                if exe.name.lower().startswith("armorpaint"):
                    out.append((exe.parent / "data", f"running process {pid_dir.name}"))
        except OSError:
            pass
        out.append((Path("/usr/lib/armorpaint/data"), "distro package default"))
    elif sys.platform == "win32":
        try:
            try:
                from .transport import _install_root_candidates
            except ImportError:
                from transport import _install_root_candidates  # type: ignore[no-redef]

            for root in _install_root_candidates():
                out.append((root / "data", "install candidate"))
        except Exception:
            pass
    elif sys.platform == "darwin":
        out.append((Path("/Applications/ArmorPaint.app/Contents/Resources/data"), "default app bundle"))
    seen: set[str] = set()
    uniq = []
    for path, why in out:
        key = str(path)
        if key not in seen and path.is_dir():
            seen.add(key)
            uniq.append((path, why))
    return uniq


def resource_roots(extra: list[str] | None, project_path: str | None) -> list[dict[str, str]]:
    roots: list[dict[str, str]] = []
    for r in extra or []:
        roots.append({"path": str(Path(r).expanduser()), "source": "argument"})
    for r in os.environ.get(LIBRARY_ENV, "").split(os.pathsep):
        if r.strip():
            roots.append({"path": str(Path(r.strip()).expanduser()), "source": f"${LIBRARY_ENV}"})
    if project_path:
        parent = Path(project_path).expanduser().parent
        if parent.is_dir():
            roots.append({"path": str(parent), "source": "open project folder"})
    for path, why in _armorpaint_data_dirs():
        roots.append({"path": str(path), "source": f"ArmorPaint data ({why})"})
    seen: set[str] = set()
    out = []
    for r in roots:
        if r["path"] not in seen:
            seen.add(r["path"])
            out.append(r)
    return out


def _kind_of(path: Path, wanted: set[str]) -> str | None:
    ext = path.suffix.lower()
    for kind in ("envmap", "export_preset", "material", "mesh", "font", "lut", "texture"):
        if kind not in wanted:
            continue
        if ext not in KINDS[kind]:
            continue
        if kind == "export_preset" and path.parent.name != "export_presets":
            continue
        return kind
    return None


def search_resources(
    query: str,
    kinds: list[str] | None = None,
    roots: list[str] | None = None,
    project_path: str | None = None,
    max_results: int = 50,
) -> dict[str, Any]:
    wanted = set(kinds or KINDS.keys())
    unknown = wanted - set(KINDS)
    if unknown:
        raise ValueError(f"unknown kind(s) {sorted(unknown)}; valid: {sorted(KINDS)}")
    terms = [t for t in (query or "").lower().split() if t]
    root_list = resource_roots(roots, project_path)
    results: list[dict[str, Any]] = []
    scanned = 0
    truncated = False
    for root in root_list:
        base = Path(root["path"])
        if not base.is_dir():
            root["missing"] = "true"
            continue
        base_depth = len(base.parts)
        for dirpath, dirnames, filenames in os.walk(base):
            depth = len(Path(dirpath).parts) - base_depth
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
            if depth >= MAX_DEPTH:
                dirnames[:] = []
            for fn in filenames:
                scanned += 1
                if scanned > MAX_FILES_SCANNED:
                    truncated = True
                    break
                path = Path(dirpath) / fn
                kind = _kind_of(path, wanted)
                if kind is None:
                    continue
                rel = str(path.relative_to(base)).replace("\\", "/")
                hay = rel.lower()
                if all(t in hay for t in terms):
                    try:
                        size = path.stat().st_size
                    except OSError:
                        size = None
                    results.append(
                        {
                            "path": str(path).replace("\\", "/"),
                            "kind": kind,
                            "name": path.stem,
                            "root": root["path"],
                            "bytes": size,
                        }
                    )
            if truncated:
                break
        if truncated:
            break
    # Name matches before path-only matches, then shorter paths.
    results.sort(key=lambda r: (not all(t in r["name"].lower() for t in terms), len(r["path"])))
    return {
        "query": query,
        "kinds": sorted(wanted),
        "roots": root_list,
        "count": len(results),
        "returned": min(len(results), max_results),
        "results": results[:max_results],
        "scan_truncated": truncated,
        "import_with": {
            "texture": "ap_import_asset",
            "mesh": "ap_import_asset (or ap_append_mesh to add to the scene)",
            "material": "ap_import_asset",
            "font": "ap_import_asset",
            "envmap": "ap_import_envmap",
            "lut": "ap_render_settings(lut_path=...)",
            "export_preset": "ap_export_textures(preset=<name>)",
        },
    }


def sidecar_path(project_path: str) -> Path:
    return Path(project_path + ".mcp.json")


def project_metadata(
    project_path: str,
    set_values: dict[str, Any] | None = None,
    remove: list[str] | None = None,
) -> dict[str, Any]:
    if not project_path or not project_path.lower().endswith(".arm"):
        raise ValueError(
            "The project has no .arm path yet. Save it first (ap_project_save_as), or pass "
            "project_path explicitly."
        )
    side = sidecar_path(project_path)
    data: dict[str, Any] = {}
    if side.is_file():
        try:
            loaded = json.loads(side.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"{side} exists but is not valid JSON: {exc}") from exc
    meta = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    changed = False
    for key, value in (set_values or {}).items():
        if not isinstance(key, str) or not key:
            raise ValueError("metadata keys must be non-empty strings")
        meta[key] = value
        changed = True
    for key in remove or []:
        if key in meta:
            del meta[key]
            changed = True
    if changed:
        data = {
            "format": "armorpaint-mcp project metadata v1",
            "project": Path(project_path).name,
            "updated": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
            "metadata": meta,
        }
        tmp = side.with_suffix(side.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, side)
    return {
        "project": project_path,
        "sidecar": str(side),
        "exists": side.is_file(),
        "changed": changed,
        "updated": data.get("updated"),
        "metadata": meta,
    }
