"""Checkpoints: a named point to roll the project back to.

A checkpoint is made of up to three parts, because ArmorPaint keeps state in places no
single mechanism covers:

* history -- ArmorPaint's own undo history (paint, fills, layers, material create/delete),
  through the native extension. The checkpoint remembers the identity of the step that
  was current; rolling back undoes (or redoes) until that step is current again. It is
  validated against the live history every time: if the step fell off the end (more than
  undo_steps since) or was discarded by a branch (undo past it, then a new action), the
  rollback is refused and says why, instead of undoing to the wrong place.
* graph -- the active material's node graph (node_graph.snapshot), because node edits are
  not in ArmorPaint's history at all.
* project -- the whole project written to a snapshot .arm (extension op project_snapshot),
  for operations nothing else records: mesh edits, texture resolution changes, opening or
  replacing the project. Rolling back opens the snapshot and points the project back at
  its own file.

Every part talks to ArmorPaint through node_graph's two-method Bridge.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from . import node_graph
from .transport import OpFailed

KINDS = ("auto", "history", "graph", "project")
MAX_STEPS_PER_CALL = 64  # the extension's undo/redo limit per request


class CheckpointStore:
    def __init__(self, directory: Path) -> None:
        self.dir = Path(directory)

    def put(self, entry: dict[str, Any]) -> str:
        self.dir.mkdir(parents=True, exist_ok=True)
        created = time.time()
        cid = f"cp{int(created * 1000)}"
        n = 0
        while (self.dir / f"{cid}.json").exists():
            n += 1
            cid = f"cp{int(created * 1000)}-{n}"
        entry = {**entry, "id": cid, "created": created}
        tmp = self.dir / f"{cid}.json.tmp"
        tmp.write_text(json.dumps(entry), encoding="utf-8")
        tmp.replace(self.dir / f"{cid}.json")
        return cid

    def get(self, cid: str) -> dict[str, Any]:
        if not re.fullmatch(r"cp\d+(?:-\d+)?", cid or ""):
            raise KeyError(cid)
        path = self.dir / f"{cid}.json"
        if not path.exists():
            raise KeyError(cid)
        return json.loads(path.read_text(encoding="utf-8"))

    def list(self) -> list[dict[str, Any]]:
        out = []
        for path in self.dir.glob("cp*.json"):
            try:
                out.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
        return sorted(out, key=lambda e: e.get("created", 0))


def _history_mark(h: dict[str, Any]) -> dict[str, Any]:
    steps = h.get("steps") or []
    if steps and "id" not in steps[0]:
        raise OpFailed(
            "history", "ext_outdated",
            "this build's native extension lists history steps without an identity (extension "
            "version 1); history checkpoints need version 2 or later -- re-run "
            "patch/apply_ext_patch.py on the ArmorPaint checkout and rebuild",
        )
    active = int(h.get("active_index", len(steps) - 1 - int(h.get("redos_available", 0))))
    return {
        "active_index": active,
        "active_id": steps[active]["id"] if 0 <= active < len(steps) else None,
        "active_name": steps[active]["name"] if 0 <= active < len(steps) else None,
        "length": len(steps),
        "undo_steps": h.get("undo_steps_config"),
    }


def take(
    bridge: node_graph.Bridge, *, kind: str = "auto", label: str | None = None,
    store: CheckpointStore, graph_store: node_graph.SnapshotStore, snapshot_dir: Path,
) -> dict[str, Any]:
    """Record a checkpoint. 'auto' is history (if the extension is there) plus the graph.
    Raises OpFailed('unsupported') when an explicitly requested part needs the extension."""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}")
    entry: dict[str, Any] = {"label": label, "kinds": []}
    if kind == "project":
        snapshot_dir = Path(snapshot_dir)
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        path = (snapshot_dir / f"snapshot_{int(time.time() * 1000)}.arm").as_posix()
        info = bridge.call("project_get_info", {})
        reply = bridge.call("project_snapshot", {"path": path})
        entry["project"] = {"path": reply.get("path", path), "original_path": info.get("filepath") or ""}
        entry["kinds"] = ["project"]
    else:
        if kind in ("auto", "history"):
            try:
                entry["history"] = _history_mark(bridge.call("history", {}))
                entry["kinds"].append("history")
            except OpFailed as exc:
                if kind == "history" or exc.code not in ("unsupported", "ext_outdated"):
                    raise
        if kind in ("auto", "graph"):
            snap = node_graph.snapshot(bridge)
            entry["graph_snapshot_id"] = graph_store.put(snap, label=f"checkpoint {label or ''}".strip())
            entry["material"] = snap.get("material")
            entry["kinds"].append("graph")
    entry["id"] = store.put(entry)
    return store.get(entry["id"])


def headroom(bridge: node_graph.Bridge, entry: dict[str, Any]) -> int | None:
    """How many more history steps before this checkpoint's step falls off the end
    (None: no history part, or the history cannot be read)."""
    mark = entry.get("history")
    if not mark:
        return None
    try:
        h = bridge.call("history", {})
    except OpFailed:
        return None
    ids = [s["id"] for s in h.get("steps") or []]
    limit = int(h.get("undo_steps_config") or len(ids))
    if mark["active_id"] is None:
        return max(0, limit - len(ids))
    if mark["active_id"] not in ids:
        return 0
    return max(0, ids.index(mark["active_id"]) + limit - len(ids))


def _rollback_history(bridge: node_graph.Bridge, mark: dict[str, Any]) -> dict[str, Any]:
    h = bridge.call("history", {})
    steps = h.get("steps") or []
    ids = [s["id"] for s in steps]
    limit = int(h.get("undo_steps_config") or 0)
    active = int(h.get("active_index", len(steps) - 1 - int(h.get("redos_available", 0))))
    if mark["active_id"] is None:
        if limit and len(steps) >= limit:
            return {"ok": False, "code": "history_truncated",
                    "error": f"the checkpoint predates every step in the history, which is full ({limit} "
                             f"steps, undo_steps): older steps have fallen off, so undoing everything would "
                             f"not reach it. Raise undo_steps (ap_set_config) before long edits."}
        target = -1
    elif mark["active_id"] in ids:
        target = ids.index(mark["active_id"])
    elif limit and len(steps) >= limit:
        return {"ok": False, "code": "history_truncated",
                "error": f"the checkpoint's step ({mark.get('active_name')!r}) has fallen off the end of the "
                         f"history: more than undo_steps ({limit}) steps were made since. Raise undo_steps "
                         f"(ap_set_config) before long edits, or use a 'project' checkpoint."}
    else:
        return {"ok": False, "code": "history_branched",
                "error": f"the checkpoint's step ({mark.get('active_name')!r}) is gone: it was undone and a new "
                         f"action discarded it (a branch). The state it marked no longer exists in the history."}
    undone = redone = 0
    while active > target:
        n = min(MAX_STEPS_PER_CALL, active - target)
        undone += int(bridge.call("undo", {"steps": n}).get("undone", 0))
        active -= n
    while active < target:
        n = min(MAX_STEPS_PER_CALL, target - active)
        redone += int(bridge.call("redo", {"steps": n}).get("redone", 0))
        active += n
    after = _history_mark(bridge.call("history", {}))
    if after["active_id"] != mark["active_id"]:
        return {"ok": False, "code": "history_mismatch", "undone": undone, "redone": redone,
                "error": "after undoing, the current step is not the checkpoint's; the history changed underneath"}
    return {"ok": True, "undone": undone, "redone": redone}


def rollback(
    bridge: node_graph.Bridge, entry: dict[str, Any], *, graph_store: node_graph.SnapshotStore
) -> dict[str, Any]:
    """Roll back every part of a checkpoint: project, or history then graph (history first,
    since it can re-create or delete materials the graph part belongs to)."""
    report: dict[str, Any] = {"ok": True, "parts": []}
    if "project" in entry.get("kinds", []):
        proj = entry["project"]
        try:
            bridge.call("project_open", {"path": proj["path"]})
            if proj.get("original_path"):
                bridge.call("project_set_path", {"path": proj["original_path"]})
                report["project"] = {"ok": True, "opened": proj["path"], "path_restored": proj["original_path"]}
            else:
                report["project"] = {"ok": True, "opened": proj["path"],
                                     "note": "the project had never been saved, so it now points at the "
                                             "snapshot file; use ap_project_save_as to give it a home"}
        except OpFailed as exc:
            report["project"] = {"ok": False, "code": exc.code, "error": exc.message}
        report["parts"].append("project")
        report["ok"] = report["project"]["ok"]
        return _lift(report)
    if entry.get("history"):
        try:
            report["history"] = _rollback_history(bridge, entry["history"])
        except OpFailed as exc:
            report["history"] = {"ok": False, "code": exc.code, "error": exc.message}
        report["parts"].append("history")
        report["ok"] = report["ok"] and report["history"]["ok"]
    if entry.get("graph_snapshot_id"):
        try:
            snap = graph_store.get(entry["graph_snapshot_id"])["snapshot"]
            report["graph"] = node_graph.restore(bridge, snap)
        except KeyError:
            report["graph"] = {"ok": False, "code": "not_found", "error": "the graph snapshot file is gone"}
        report["parts"].append("graph")
        report["ok"] = report["ok"] and report["graph"]["ok"]
    return _lift(report)


def _lift(report: dict[str, Any]) -> dict[str, Any]:
    """Put the first failing part's code and error at the top of the report."""
    if not report["ok"]:
        for part in report["parts"]:
            if not report[part].get("ok"):
                report["code"] = report[part].get("code", "rollback_failed")
                report["error"] = f"{part}: {report[part].get('error', 'failed')}"
                break
    return report
