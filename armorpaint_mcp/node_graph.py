"""Whole material graphs: read, apply declaratively, lint, snapshot and restore.

The bridge exposes ArmorPaint's node API one operation at a time (``node_add``,
``node_connect``, ``node_set_value`` ...), addressed by node id and socket *index*. That
is exact but brittle for an agent: ids are only known after creation, socket indices must
be looked up, and a half-finished edit leaves a broken material. This module works a
level up:

* ``read_graph`` -- the whole graph in one description: nodes, sockets by name with their
  values, links with both ends named. One ``node_list`` plus one batch of ``node_get``.
* ``apply`` -- a spec that names nodes with local keys and sockets by name
  (``"noise.Color -> mix.Color 2"``) is validated against the socket catalogue *before*
  anything is sent, then run as batches (adds, then values and links), recompiled and
  applied with a fill. If any step fails, the graph is restored to what it was.
* ``lint`` -- cycles, nodes that feed nothing, links into disabled paint channels, and
  socket type conversions.
* ``snapshot`` / ``restore`` -- ArmorPaint's undo history does not record node edits
  (``script_material_*`` push no history step; only ``script_material_create`` does), so
  rolling a graph back has to happen here: restore diffs the live graph against a
  snapshot and sends the smallest set of operations that brings it back.

Everything talks to ArmorPaint through a two-method ``Bridge`` so tests can substitute an
in-memory canvas (tests/fake_armorpaint.py).
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Protocol

from . import node_catalogue
from .transport import OpFailed

BATCH_MAX = 64  # transport.build_batch_request's limit
OUTPUT_TYPE = "OUTPUT_MATERIAL_PBR"

# Output-node input order -> material_get_active's channel flag names.
CHANNEL_BY_OUTPUT_SOCKET = (
    "base", "opacity", "occlusion", "roughness", "metallic", "normal", "emission", "height", "subsurface",
)

# Canvas geometry for auto layout (ArmorPaint's default node width is 140).
NODE_W = 140.0
NODE_HEADER_H = 32.0
NODE_ROW_H = 22.0
COL_W = 220.0
ROW_GAP = 30.0

VALUE_TOLERANCE = 1e-4


class Bridge(Protocol):
    def call(self, op: str, wire: dict[str, Any]) -> dict[str, Any]: ...

    def batch(self, items: list[tuple[str, dict[str, Any]]], stop_on_error: bool = False) -> dict[str, Any]: ...


class SpecError(ValueError):
    """A graph spec that cannot be applied. ``problems`` lists every reason found."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__("; ".join(problems))


class _Failed(Exception):
    def __init__(self, step: dict[str, Any], error: Any) -> None:
        super().__init__(str(error))
        self.step = step
        self.error = error


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def parse_node_list(result: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, int]], bool]:
    """Decode ``node_list``: ``id,type,name,x,y;`` nodes and ``from_id,from_socket,to_id,
    to_socket;`` links. A name may contain commas; the id, type and coordinates cannot."""
    nodes = []
    for entry in (result.get("nodes") or "").split(";"):
        if not entry:
            continue
        parts = entry.split(",")
        nodes.append(
            {
                "id": int(parts[0]),
                "type": parts[1],
                "name": ",".join(parts[2:-2]),
                "x": float(parts[-2]),
                "y": float(parts[-1]),
            }
        )
    links = []
    for entry in (result.get("links") or "").split(";"):
        if entry:
            a, b, c, d = (int(v) for v in entry.split(","))
            links.append({"from_id": a, "from_socket": b, "to_id": c, "to_socket": d})
    truncated = int(result.get("returned", len(nodes))) < int(result.get("count", len(nodes))) or int(
        result.get("links_returned", len(links))
    ) < int(result.get("link_count", len(links)))
    return nodes, links, truncated


def _run_batch(bridge: Bridge, items: list[tuple[str, dict[str, Any]]], stop_on_error: bool = True) -> list[dict[str, Any]]:
    """Send items in batches of BATCH_MAX; results in order. Stops at the first failure
    when ``stop_on_error`` (the failing result is the last one returned)."""
    results: list[dict[str, Any]] = []
    for start in range(0, len(items), BATCH_MAX):
        chunk = items[start : start + BATCH_MAX]
        reply = bridge.batch(chunk, stop_on_error)
        for r in reply.get("results") or []:
            results.append({**r, "i": start + int(r.get("i", 0))})
            if stop_on_error and not r.get("ok"):
                return results
    return results


def _entries(table: str | None, with_link: bool = False) -> list[dict[str, Any]]:
    out = []
    for s in node_catalogue.parse_socket_table(table):
        entry = {"index": s["index"], "name": s["name"], "type": s["type"], "value": s["value"]}
        if with_link:
            entry["link"] = None
        out.append(entry)
    return out


def read_graph(bridge: Bridge) -> dict[str, Any]:
    """The active material's whole graph as one description."""
    mat = bridge.call("material_get_active", {})
    nodes, links, truncated = parse_node_list(bridge.call("node_list", {}))
    details = _run_batch(bridge, [("node_get", {"id": n["id"]}) for n in nodes], stop_on_error=False)
    by_id: dict[int, dict[str, Any]] = {}
    for n, d in zip(nodes, details):
        info = d.get("result") or {} if d.get("ok") else {}
        n["inputs"] = _entries(info.get("input_sockets"), with_link=True)
        n["outputs"] = _entries(info.get("output_sockets"))
        n["buttons"] = _entries(info.get("buttons"))
        by_id[n["id"]] = n
    named_links = []
    for lk in links:
        src, dst = by_id.get(lk["from_id"]), by_id.get(lk["to_id"])
        from_name = _name_at(src, "outputs", lk["from_socket"])
        to_name = _name_at(dst, "inputs", lk["to_socket"])
        named_links.append({**lk, "from_name": from_name, "to_name": to_name})
        if dst is not None and lk["to_socket"] < len(dst["inputs"]):
            dst["inputs"][lk["to_socket"]]["link"] = {
                "from_id": lk["from_id"], "from_socket": lk["from_socket"], "from_name": from_name,
            }
    named_links = [
        {k: lk[k] for k in ("from_id", "from_socket", "from_name", "to_id", "to_socket", "to_name")}
        for lk in named_links
    ]
    return {
        "material": mat.get("name"),
        "material_id": mat.get("id"),
        "channels": {c: bool(mat.get(c)) for c in CHANNEL_BY_OUTPUT_SOCKET if c in mat},
        "nodes": nodes,
        "links": named_links,
        "truncated": truncated,
    }


def _name_at(node: dict[str, Any] | None, side: str, index: int) -> str | None:
    if node is None or index >= len(node[side]):
        return None
    return node[side][index]["name"]


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


class _Ref:
    """A node id that is only known once the node exists (a spec key)."""

    def __init__(self, key: str) -> None:
        self.key = key

    def __repr__(self) -> str:
        return f"<{self.key}>"


_SOCKET_REF_RE = re.compile(r"^(?P<name>.*?)\[(?P<k>\d+)\]$")


def _socket_index(entries: list[dict[str, Any]], alt: list[dict[str, Any]] | None, ref: Any, what: str, key: str) -> int:
    """Resolve a socket reference: an int or '#i' (index), 'Name' (unique), 'Name[k]'
    (k-th socket of that name). ``alt`` is a second name list (the catalogue's English
    names for a live node whose names may be translated)."""
    if isinstance(ref, int) and not isinstance(ref, bool):
        idx = ref
    elif isinstance(ref, str) and ref.startswith("#") and ref[1:].isdigit():
        idx = int(ref[1:])
    elif isinstance(ref, str):
        m = _SOCKET_REF_RE.match(ref)
        name, occurrence = (m.group("name"), int(m.group("k"))) if m else (ref, None)
        for table in (entries, alt or []):
            hits = [i for i, e in enumerate(table) if e["name"] == name]
            if not hits:
                continue
            if occurrence is None:
                if len(hits) > 1:
                    options = ", ".join(f"'{name}[{k}]'" for k in range(len(hits)))
                    raise SpecError([f"{key}: {what} '{name}' is ambiguous ({len(hits)} sockets share it); use {options}"])
                return hits[0]
            if occurrence >= len(hits):
                raise SpecError([f"{key}: there are only {len(hits)} {what}s named '{name}'"])
            return hits[occurrence]
        names = ", ".join(repr(e["name"]) for e in entries) or "none"
        raise SpecError([f"{key}: no {what} '{name}' (has: {names})"])
    else:
        raise SpecError([f"{key}: bad {what} reference {ref!r}"])
    if not 0 <= idx < len(entries):
        raise SpecError([f"{key}: {what} index {idx} out of range (0..{len(entries) - 1})"])
    return idx


def _numbers(value: Any) -> list[float] | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return [float(value)]
    if isinstance(value, (list, tuple)) and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in value):
        return [float(v) for v in value]
    return None


def _value_wire(node_id: Any, index: int, is_input: bool, sock: dict[str, Any], value: Any, key: str) -> dict[str, Any]:
    nums = _numbers(value)
    typ = sock["type"]
    label = f"{key}: {'input' if is_input else 'output'} '{sock['name']}' ({typ})"
    if typ == "RGBA":
        if nums is None or len(nums) not in (3, 4):
            raise SpecError([f"{label} expects 3 or 4 numbers (r, g, b[, a]), got {value!r}"])
        if len(nums) == 3:
            nums.append(1.0)
        return {"id": node_id, "kind": "color", "socket": index, "is_input": is_input,
                "r": nums[0], "g": nums[1], "b": nums[2], "alpha": nums[3]}
    if typ == "VECTOR":
        if nums is None or len(nums) != 3:
            raise SpecError([f"{label} expects 3 numbers (x, y, z), got {value!r}"])
        return {"id": node_id, "kind": "vector", "socket": index, "is_input": is_input,
                "x": nums[0], "y": nums[1], "z": nums[2]}
    if nums is None or len(nums) != 1:
        raise SpecError([f"{label} expects 1 number, got {value!r}"])
    return {"id": node_id, "kind": "float", "socket": index, "is_input": is_input, "value": nums[0]}


def _button_wire(node_id: Any, index: int, button: dict[str, Any], value: Any, key: str) -> dict[str, Any]:
    label = f"{key}: button '{button['name']}' ({button['type']})"
    default = button.get("default")
    if default is not None and len(default) > 1:
        raise SpecError([f"{label} holds {len(default)} values (curve or ramp data); node_set_value "
                         f"writes only the first, so it cannot be set from a spec"])
    options = button.get("options")
    if isinstance(value, str):
        if not options:
            raise SpecError([f"{label} takes a number, not {value!r}"])
        if value not in options:
            raise SpecError([f"{label}: {value!r} is not an option; options: {', '.join(options)}"])
        number = float(options.index(value))
    elif isinstance(value, bool):
        number = 1.0 if value else 0.0
    elif isinstance(value, (int, float)):
        number = float(value)
    else:
        raise SpecError([f"{label}: bad value {value!r}"])
    return {"id": node_id, "kind": "button", "button": index, "value": number}


def _parse_link(link: Any) -> tuple[str, Any, str, Any]:
    if isinstance(link, dict):
        try:
            return str(link["from"]), link.get("from_socket", 0), str(link["to"]), link.get("to_socket", 0)
        except KeyError as exc:
            raise SpecError([f"link {link!r} needs 'from' and 'to'"]) from exc
    if not isinstance(link, str) or "->" not in link:
        raise SpecError([f"link {link!r} must be 'node.Socket -> node.Socket' (with '->') or an object"])
    left, right = (s.strip() for s in link.split("->", 1))
    if "." not in left or "." not in right:
        raise SpecError([f"link {link!r}: each end must be 'node.Socket'"])
    a, sa = left.split(".", 1)
    b, sb = right.split(".", 1)
    return a.strip(), sa.strip(), b.strip(), sb.strip()


def node_box(node: dict[str, Any]) -> tuple[float, float, float, float]:
    """The canvas rectangle a node roughly occupies: (x0, y0, x1, y1)."""
    rows = len(node.get("inputs") or []) + len(node.get("outputs") or []) + len(node.get("buttons") or [])
    h = NODE_HEADER_H + NODE_ROW_H * max(rows, 1)
    return (node["x"], node["y"], node["x"] + NODE_W, node["y"] + h)


def _find_cycle(edges: dict[Any, set[Any]]) -> list[Any] | None:
    state: dict[Any, int] = {}
    path: list[Any] = []

    def visit(n: Any) -> list[Any] | None:
        state[n] = 1
        path.append(n)
        for m in sorted(edges.get(n, ()), key=str):
            if state.get(m) == 1:
                return path[path.index(m) :] + [m]
            if state.get(m) is None:
                found = visit(m)
                if found:
                    return found
        path.pop()
        state[n] = 2
        return None

    for n in sorted(edges, key=str):
        if state.get(n) is None:
            found = visit(n)
            if found:
                return found
    return None


def plan_apply(
    spec: dict[str, Any], graph: dict[str, Any], catalogue: dict[str, Any], mode: str = "merge"
) -> dict[str, Any]:
    """Validate ``spec`` against the live ``graph`` and the catalogue and turn it into
    remove / add / edit steps. Raises SpecError listing every problem found."""
    if mode not in ("merge", "replace"):
        raise SpecError([f"mode must be 'merge' or 'replace', not {mode!r}"])
    if not isinstance(spec, dict) or not isinstance(spec.get("nodes", {}), dict):
        raise SpecError(["spec must be an object with a 'nodes' object"])
    problems: list[str] = []
    live = {n["id"]: n for n in graph["nodes"]}
    new: dict[str, dict[str, Any]] = {}  # key -> catalogue entry + spec entry
    existing: dict[str, dict[str, Any]] = {}  # key -> live node

    for key, entry in spec.get("nodes", {}).items():
        if not isinstance(entry, dict):
            problems.append(f"{key}: must be an object")
            continue
        if "." in key or "->" in key:
            problems.append(f"{key}: node keys cannot contain '.' or '->'")
        if "existing" in entry:
            hits = [n for n in graph["nodes"] if n["type"] == str(entry["existing"]).upper()]
            if not hits:
                problems.append(f"{key}: the material has no {entry['existing']} node")
            else:
                existing[key] = hits[0]
        elif "id" in entry:
            if entry["id"] not in live:
                problems.append(f"{key}: no node with id {entry['id']} in the material")
            else:
                existing[key] = live[entry["id"]]
        else:
            t = str(entry.get("type", "")).upper()
            if t not in catalogue:
                problems.append(f"{key}: unknown node type {entry.get('type')!r}")
            elif catalogue[t].get("availability") == "group_canvas_only":
                problems.append(f"{key}: {t} exists only inside a node group (its own canvas); "
                                f"the bridge edits the material's top-level canvas")
            elif t == OUTPUT_TYPE:
                problems.append(f"{key}: a material has exactly one {OUTPUT_TYPE}; refer to it with "
                                f"{{\"existing\": \"{OUTPUT_TYPE}\"}}")
            else:
                new[key] = {"type": t, "cat": catalogue[t], "spec": entry}
    if problems:
        raise SpecError(problems)

    def sockets(key: str, side: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]] | None]:
        if key in new:
            return new[key]["cat"][side], None
        node = existing[key]
        cat = catalogue.get(node["type"])
        return node[side], (cat[side] if cat and len(cat[side]) == len(node[side]) else None)

    def node_id(key: str) -> Any:
        return _Ref(key) if key in new else existing[key]["id"]

    def catalogue_sock(key: str, side: str, idx: int) -> dict[str, Any]:
        entries, alt = sockets(key, side)
        sock = dict(entries[idx])
        if alt is not None and key in existing and "options" in alt[idx]:
            sock.setdefault("options", alt[idx]["options"])
        return sock

    # --- values and buttons ------------------------------------------------------------
    sets: list[dict[str, Any]] = []
    for key, entry in list(spec["nodes"].items()):
        for side, is_input in (("inputs", True), ("outputs", False)):
            for ref, value in (entry.get(side) or {}).items():
                try:
                    entries, alt = sockets(key, side)
                    idx = _socket_index(entries, alt, ref, "input socket" if is_input else "output socket", key)
                    wire = _value_wire(node_id(key), idx, is_input, catalogue_sock(key, side, idx), value, key)
                    sets.append({"op": "node_set_value", "wire": wire, "describe": f"{key}.{ref} = {value!r}"})
                except SpecError as exc:
                    problems.extend(exc.problems)
        for ref, value in (entry.get("buttons") or {}).items():
            try:
                entries, alt = sockets(key, "buttons")
                idx = _socket_index(entries, alt, ref, "button", key)
                wire = _button_wire(node_id(key), idx, catalogue_sock(key, "buttons", idx), value, key)
                sets.append({"op": "node_set_value", "wire": wire, "describe": f"{key} button {ref} = {value!r}"})
            except SpecError as exc:
                problems.extend(exc.problems)

    # --- links ------------------------------------------------------------------------
    connects: list[dict[str, Any]] = []
    spec_edges: list[tuple[Any, Any, Any, int]] = []  # (from key/id, to key/id, from socket, to socket)
    for link in spec.get("links") or []:
        try:
            a, sa, b, sb = _parse_link(link)
            for k in (a, b):
                if k not in new and k not in existing:
                    raise SpecError([f"link {link!r}: unknown node '{k}'"])
            fi = _socket_index(*sockets(a, "outputs"), sa, "output socket", a)
            ti = _socket_index(*sockets(b, "inputs"), sb, "input socket", b)
            text = link if isinstance(link, str) else f"{a}.{sa} -> {b}.{sb}"
            connects.append({"op": "node_connect", "wire": {"from_id": node_id(a), "from_socket": fi,
                                                            "to_id": node_id(b), "to_socket": ti},
                             "describe": text})
            spec_edges.append((a, b, fi, ti))
        except SpecError as exc:
            problems.extend(exc.problems)
    if problems:
        raise SpecError(problems)

    # --- removals (replace mode) --------------------------------------------------------
    keep = {n["id"] for n in existing.values()}
    removes = []
    if mode == "replace":
        for n in graph["nodes"]:
            if n["type"] != OUTPUT_TYPE and n["id"] not in keep:
                removes.append({"op": "node_remove", "wire": {"id": n["id"]},
                                "describe": f"remove {n['type']} #{n['id']}"})
    removed = {s["wire"]["id"] for s in removes}

    # --- cycle check over the graph as it will be ---------------------------------------
    def ident(k: str) -> Any:
        return f"<{k}>" if k in new else existing[k]["id"]

    replaced = {(ident(b), ti) for a, b, fi, ti in spec_edges}
    edges: dict[Any, set[Any]] = {}
    for lk in graph["links"]:
        if lk["from_id"] in removed or lk["to_id"] in removed or (lk["to_id"], lk["to_socket"]) in replaced:
            continue
        edges.setdefault(lk["from_id"], set()).add(lk["to_id"])
    for a, b, _, _ in spec_edges:
        edges.setdefault(ident(a), set()).add(ident(b))
    cycle = _find_cycle(edges)
    if cycle:
        names = {ident(k): k for k in list(new) + list(existing)}
        raise SpecError(["the links form a cycle: " + " -> ".join(str(names.get(c, f"#{c}")) for c in cycle)])

    # --- adds, with auto layout for nodes that give no position ------------------------
    positions = _layout(new, existing, spec_edges, graph, removed)
    adds = []
    for key, info in new.items():
        x, y = positions[key]
        adds.append({"op": "node_add", "key": key, "wire": {"type": info["type"], "x": x, "y": y},
                     "describe": f"add {key} ({info['type']}) at {x:.0f},{y:.0f}"})
    return {"remove": removes, "add": adds, "edit": sets + connects, "existing": {k: n["id"] for k, n in existing.items()}}


def _layout(
    new: dict[str, dict[str, Any]],
    existing: dict[str, dict[str, Any]],
    spec_edges: list[tuple[Any, Any, Any, int]],
    graph: dict[str, Any],
    removed: set[int],
) -> dict[str, tuple[float, float]]:
    """Columns right-to-left from the consumers: a node sits one column left of the
    leftmost thing it feeds. Stacked top-down per column, clear of existing nodes."""
    out: dict[str, tuple[float, float]] = {}
    consumers: dict[str, set[str]] = {}
    for a, b, _, _ in spec_edges:
        consumers.setdefault(a, set()).add(b)
    live_nodes = [n for n in graph["nodes"] if n["id"] not in removed]
    output = next((n for n in live_nodes if n["type"] == OUTPUT_TYPE), None)
    base_x = output["x"] if output else 0.0
    base_y = output["y"] if output else 0.0

    memo: dict[str, float] = {}

    def x_of(key: str, depth: int = 0) -> float:
        if key in existing:
            return existing[key]["x"]
        spec_entry = new[key]["spec"]
        if "x" in spec_entry:
            return float(spec_entry["x"])
        if key in memo:
            return memo[key]
        if depth > len(new):  # defensive; cycles are rejected before layout
            return base_x - COL_W
        feeds = [x_of(c, depth + 1) for c in consumers.get(key, ())]
        memo[key] = (min(feeds) if feeds else base_x) - COL_W
        return memo[key]

    occupied = [node_box(n) for n in live_nodes]
    for key in new:
        spec_entry = new[key]["spec"]
        x = x_of(key)
        if "x" in spec_entry and "y" in spec_entry:
            out[key] = (x, float(spec_entry["y"]))
            continue
        box_template = {"x": x, "y": base_y, **{k: new[key]["cat"][k] for k in ("inputs", "outputs", "buttons")}}
        x0, _, x1, h_end = node_box(box_template)
        height = h_end - base_y
        y = float(spec_entry.get("y", base_y))
        moved = True
        while moved:
            moved = False
            for bx0, by0, bx1, by1 in occupied:
                if x0 < bx1 and bx0 < x1 and y < by1 and by0 < y + height:
                    y = by1 + ROW_GAP
                    moved = True
        out[key] = (x, y)
        occupied.append((x0, y, x1, y + height))
    return out


# ---------------------------------------------------------------------------
# Applying
# ---------------------------------------------------------------------------


def _render(step: dict[str, Any]) -> dict[str, Any]:
    wire = {k: (repr(v) if isinstance(v, _Ref) else v) for k, v in step["wire"].items()}
    return {"op": step["op"], "wire": wire, "describe": step["describe"]}


def _resolve(wire: dict[str, Any], ids: dict[str, int]) -> dict[str, Any]:
    return {k: (ids[v.key] if isinstance(v, _Ref) else v) for k, v in wire.items()}


def _send_steps(bridge: Bridge, steps: list[dict[str, Any]], ids: dict[str, int]) -> list[dict[str, Any]]:
    if not steps:
        return []
    results = _run_batch(bridge, [(s["op"], _resolve(s["wire"], ids)) for s in steps])
    for r in results:
        if not r.get("ok"):
            raise _Failed(steps[r["i"]], r.get("error"))
    return results


def apply(
    bridge: Bridge,
    spec: dict[str, Any],
    *,
    mode: str = "merge",
    dry_run: bool = False,
    fill: bool = True,
    catalogue: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build ``spec`` into the active material, recompile it and (by default) fill the
    selected layer with it. On any failure the graph is restored and the report says so.
    The report's ``snapshot`` is the graph as it was before."""
    catalogue = catalogue if catalogue is not None else node_catalogue.load()
    before = read_graph(bridge)
    plan = plan_apply(spec, before, catalogue, mode)
    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "plan": {k: [_render(s) for s in plan[k]] for k in ("remove", "add", "edit")},
            "existing": plan["existing"],
        }
    ids: dict[str, int] = {}
    try:
        _send_steps(bridge, plan["remove"], ids)
        for step, result in zip(plan["add"], _send_steps(bridge, plan["add"], ids)):
            reply = result.get("result") or {}
            ids[step["key"]] = int(reply["id"])
            cat = catalogue[step["wire"]["type"]]
            got = [len(node_catalogue.parse_socket_table(reply.get(k))) for k in ("input_sockets", "output_sockets")]
            if got != [len(cat["inputs"]), len(cat["outputs"])]:
                raise _Failed(step, f"{step['wire']['type']} has {got[0]} inputs / {got[1]} outputs on this "
                                    f"build, the catalogue says {len(cat['inputs'])} / {len(cat['outputs'])}: "
                                    f"regenerate data/node_sockets.json (tools/gen_node_sockets.py)")
        _send_steps(bridge, plan["edit"], ids)
        for op in ("material_update",) + (("fill_layer",) if fill else ()):
            step = {"op": op, "wire": {}, "describe": op}
            try:
                bridge.call(op, {})
            except OpFailed as exc:
                raise _Failed(step, {"code": exc.code, "message": exc.message}) from exc
    except _Failed as failure:
        restored = restore(bridge, before, force=True)
        return {
            "ok": False,
            "code": "apply_failed",
            "failed_step": {**_render({**failure.step, "wire": _resolve(failure.step["wire"], ids)}),
                            "error": failure.error},
            "rolled_back": bool(restored.get("ok")),
            "restore": restored,
        }
    return {
        "ok": True,
        "created": ids,
        "removed": [s["wire"]["id"] for s in plan["remove"]],
        "steps": len(plan["remove"]) + len(plan["add"]) + len(plan["edit"]),
        "material_updated": True,
        "filled": fill,
        "snapshot": before,
    }


# ---------------------------------------------------------------------------
# Lint
# ---------------------------------------------------------------------------


def lint(graph: dict[str, Any], channels: dict[str, bool] | None = None) -> list[dict[str, Any]]:
    """Problems an agent would otherwise find by looking at a wrong render."""
    issues: list[dict[str, Any]] = []
    channels = channels if channels is not None else graph.get("channels") or {}
    by_id = {n["id"]: n for n in graph["nodes"]}
    if graph.get("truncated"):
        issues.append({"level": "warning", "code": "truncated",
                       "message": "the bridge lists at most 64 nodes and 64 links; this graph is larger, "
                                  "so the checks below see only part of it"})
    edges: dict[int, set[int]] = {}
    for lk in graph["links"]:
        edges.setdefault(lk["from_id"], set()).add(lk["to_id"])
    cycle = _find_cycle(edges)
    if cycle:
        issues.append({"level": "error", "code": "cycle", "nodes": cycle,
                       "message": "the links form a cycle: " + " -> ".join(f"#{c}" for c in cycle)})

    outputs = {n["id"] for n in graph["nodes"] if n["type"] == OUTPUT_TYPE}
    reaches = set(outputs)
    changed = True
    while changed:
        changed = False
        for src, dsts in edges.items():
            if src not in reaches and dsts & reaches:
                reaches.add(src)
                changed = True
    for n in graph["nodes"]:
        if n["id"] not in reaches:
            issues.append({"level": "warning", "code": "does_not_reach_output", "node": n["id"],
                           "message": f"#{n['id']} {n['type']} ({n['name']}) feeds nothing that reaches the "
                                      f"material output, so it has no effect"})

    for lk in graph["links"]:
        dst, src = by_id.get(lk["to_id"]), by_id.get(lk["from_id"])
        if dst is None or src is None:
            continue
        if dst["type"] == OUTPUT_TYPE and lk["to_socket"] < len(CHANNEL_BY_OUTPUT_SOCKET):
            channel = CHANNEL_BY_OUTPUT_SOCKET[lk["to_socket"]]
            if channels.get(channel) is False:
                issues.append({"level": "warning", "code": "disabled_channel_linked", "node": dst["id"],
                               "message": f"{lk['to_name']} is linked, but the material's '{channel}' paint "
                                          f"channel is off, so it paints nothing (ap_material_set_channels)"})
        if lk["from_socket"] < len(src["outputs"]) and lk["to_socket"] < len(dst["inputs"]):
            a = src["outputs"][lk["from_socket"]]["type"]
            b = dst["inputs"][lk["to_socket"]]["type"]
            if a != b:
                how = {
                    ("RGBA", "VALUE"): "the colour is converted to grayscale",
                    ("VECTOR", "VALUE"): "the vector is reduced to one value",
                    ("VALUE", "RGBA"): "the value becomes a gray colour",
                    ("VALUE", "VECTOR"): "the value is copied to x, y and z",
                }.get((a, b), "ArmorPaint converts between the two types")
                issues.append({"level": "info", "code": "type_conversion", "node": dst["id"],
                               "message": f"#{src['id']}.{lk['from_name']} ({a}) -> #{dst['id']}.{lk['to_name']} "
                                          f"({b}): {how}"})
    return issues


# ---------------------------------------------------------------------------
# Snapshots
# ---------------------------------------------------------------------------


def snapshot(bridge: Bridge) -> dict[str, Any]:
    graph = read_graph(bridge)
    graph["taken_at"] = time.time()
    return graph


def _differs(a: list[float], b: list[float]) -> bool:
    return len(a) != len(b) or any(abs(x - y) > VALUE_TOLERANCE * max(1.0, abs(x)) for x, y in zip(a, b))


def restore(bridge: Bridge, snap: dict[str, Any], *, force: bool = False) -> dict[str, Any]:
    """Bring the active material's graph back to ``snap`` with the fewest operations.

    Nodes that were removed are re-created (with new ids -- ArmorPaint never reuses one;
    ``id_map`` says which is which), extra nodes are removed, socket and button values are
    reset and links are put back. Node positions of surviving nodes and custom node names
    cannot be written by any binding and are left as they are."""
    current = read_graph(bridge)
    if not force and current["material"] != snap.get("material"):
        return {"ok": False, "code": "material_mismatch",
                "error": f"the snapshot is of material {snap.get('material')!r} but {current['material']!r} "
                         f"is active; select it first (ap_material_select) or pass force"}
    snap_nodes = {n["id"]: n for n in snap["nodes"]}
    cur_nodes = {n["id"]: n for n in current["nodes"]}
    notes: list[str] = []
    unrestorable: list[str] = []

    removes = [i for i, n in cur_nodes.items() if (i not in snap_nodes or snap_nodes[i]["type"] != n["type"])
               and n["type"] != OUTPUT_TYPE]
    re_add = [i for i, n in snap_nodes.items() if (i not in cur_nodes or cur_nodes[i]["type"] != n["type"])
              and n["type"] != OUTPUT_TYPE]
    ops = 0
    id_map: dict[int, int] = {}
    try:
        _send_steps(bridge, [{"op": "node_remove", "wire": {"id": i}, "describe": f"remove #{i}"} for i in removes], {})
        ops += len(removes)
        add_steps = [{"op": "node_add", "wire": {"type": snap_nodes[i]["type"], "x": snap_nodes[i]["x"],
                                                "y": snap_nodes[i]["y"]}, "describe": f"re-add #{i}"} for i in re_add]
        fresh: dict[int, dict[str, Any]] = {}
        for old, result in zip(re_add, _send_steps(bridge, add_steps, {})):
            reply = result.get("result") or {}
            id_map[old] = int(reply["id"])
            fresh[old] = {"inputs": _entries(reply.get("input_sockets")),
                          "outputs": _entries(reply.get("output_sockets")),
                          "buttons": _entries(reply.get("buttons"))}
        ops += len(re_add)

        def live_id(old: int) -> int:
            return id_map.get(old, old)

        edits: list[dict[str, Any]] = []
        for old, want in snap_nodes.items():
            have = fresh.get(old) or cur_nodes.get(old)
            if have is None:
                continue
            if old in cur_nodes and (cur_nodes[old]["x"], cur_nodes[old]["y"]) != (want["x"], want["y"]):
                notes.append(f"#{old} was moved on the canvas; node positions cannot be set by the bridge")
            for side, is_input in (("inputs", True), ("outputs", False)):
                for w, h in zip(want[side], have[side]):
                    if not _differs(w["value"], h["value"]):
                        continue
                    try:
                        wire = _value_wire(live_id(old), w["index"], is_input, w, w["value"], f"#{old}")
                    except SpecError:
                        unrestorable.append(f"#{old} {side[:-1]} '{w['name']}'")
                        continue
                    edits.append({"op": "node_set_value", "wire": wire, "describe": f"reset #{old}.{w['name']}"})
            for w, h in zip(want["buttons"], have["buttons"]):
                if not _differs(w["value"], h["value"]):
                    continue
                if len(w["value"]) != 1:
                    unrestorable.append(f"#{old} button '{w['name']}' ({len(w['value'])} values)")
                    continue
                edits.append({"op": "node_set_value", "wire": {"id": live_id(old), "kind": "button",
                                                               "button": w["index"], "value": w["value"][0]},
                              "describe": f"reset #{old} button {w['name']}"})

        # Links, per target input socket: what feeds it now vs. in the snapshot.
        gone = set(removes)
        now = {(lk["to_id"], lk["to_socket"]): (lk["from_id"], lk["from_socket"])
               for lk in current["links"] if lk["from_id"] not in gone and lk["to_id"] not in gone}
        want_links = {(live_id(lk["to_id"]), lk["to_socket"]): (live_id(lk["from_id"]), lk["from_socket"])
                      for lk in snap["links"]}
        for (to_id, to_socket), src in sorted(now.items()):
            if (to_id, to_socket) not in want_links:
                edits.append({"op": "node_disconnect", "wire": {"to_id": to_id, "to_socket": to_socket},
                              "describe": f"unlink #{to_id}:{to_socket}"})
        for (to_id, to_socket), src in sorted(want_links.items()):
            if now.get((to_id, to_socket)) != src:
                edits.append({"op": "node_connect", "wire": {"from_id": src[0], "from_socket": src[1],
                                                            "to_id": to_id, "to_socket": to_socket},
                              "describe": f"relink #{src[0]}:{src[1]} -> #{to_id}:{to_socket}"})
        _send_steps(bridge, edits, {})
        ops += len(edits)
        if ops:
            bridge.call("material_update", {})
    except (_Failed, OpFailed) as exc:
        step = exc.step["describe"] if isinstance(exc, _Failed) else "material_update"
        return {"ok": False, "code": "restore_failed", "error": f"{step}: {exc}", "ops": ops, "id_map": id_map}
    return {"ok": True, "ops": ops, "id_map": id_map, "unrestorable": unrestorable, "notes": notes}


class SnapshotStore:
    """Graph snapshots on disk, one JSON file each, so they outlive the server process."""

    def __init__(self, directory: Path) -> None:
        self.dir = Path(directory)

    def put(self, snap: dict[str, Any], label: str | None = None) -> str:
        self.dir.mkdir(parents=True, exist_ok=True)
        created = time.time()
        sid = f"g{int(created * 1000)}"
        n = 0
        while (self.dir / f"{sid}.json").exists():
            n += 1
            sid = f"g{int(created * 1000)}-{n}"
        entry = {"id": sid, "label": label, "created": created, "material": snap.get("material"),
                 "nodes": len(snap.get("nodes", [])), "snapshot": snap}
        tmp = self.dir / f"{sid}.json.tmp"
        tmp.write_text(json.dumps(entry), encoding="utf-8")
        tmp.replace(self.dir / f"{sid}.json")
        return sid

    def get(self, sid: str) -> dict[str, Any]:
        if not re.fullmatch(r"g\d+(?:-\d+)?", sid or ""):
            raise KeyError(sid)
        path = self.dir / f"{sid}.json"
        if not path.exists():
            raise KeyError(sid)
        return json.loads(path.read_text(encoding="utf-8"))

    def list(self) -> list[dict[str, Any]]:
        entries = []
        for path in sorted(self.dir.glob("g*.json")):
            try:
                e = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            entries.append({k: e.get(k) for k in ("id", "label", "created", "material", "nodes")})
        return sorted(entries, key=lambda e: e["created"] or 0)
