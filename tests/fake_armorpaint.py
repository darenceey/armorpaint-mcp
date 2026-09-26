"""An in-memory stand-in for the bridge's material-node ops, for offline tests.

It answers ``call(op, wire)`` and ``batch(items, stop_on_error)`` with the same reply
shapes the plugin produces (recorded from a live ArmorPaint 1.0, see test_node_graph.py),
and follows upstream's semantics from ``minic_impl.c``:

* ``node_connect`` replaces whatever is linked into the target input socket;
* ``node_remove`` drops the node's links and refuses the PBR output node;
* node ids come from a counter that never goes back (a fresh project's next id was 7);
* nothing here is recorded in ArmorPaint's undo history -- node edits never are.

Node templates come from the shipped socket catalogue.
"""

from __future__ import annotations

from typing import Any

from armorpaint_mcp import node_catalogue
from armorpaint_mcp.transport import OpFailed

MAX_LIST_ITEMS = 64


def _fmt(v: float) -> str:
    """f32_to_string: 0.8 -> '0.8', 1.0 -> '1'."""
    text = f"{v:.6f}".rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


def _table(entries: list[dict[str, Any]]) -> str:
    return "".join(
        f"{i}:{e['name']}:{e['type']}={','.join(_fmt(v) for v in e['value'])};" for i, e in enumerate(entries)
    )


class FakeArmorPaint:
    def __init__(self, catalogue: dict[str, Any] | None = None) -> None:
        self.catalogue = catalogue or node_catalogue.load()
        self.nodes: dict[int, dict[str, Any]] = {}
        self.order: list[int] = []
        self.links: list[dict[str, int]] = []
        self.next_id = 0
        self.log: list[tuple[str, dict[str, Any]]] = []
        self.fail_on: set[tuple[str, int]] = set()  # (op, nth call) -> fail it
        self._counts: dict[str, int] = {}
        self.material = {"id": 0, "name": "Material 1"}
        self.channels = {c: True for c in ("base", "opacity", "occlusion", "roughness", "metallic",
                                           "normal", "height", "emission", "subsurface")}
        self.updates = 0
        self.fills = 0
        # A new project's default material: Color (RGB) -> Material Output.Base Color.
        out = self._make("OUTPUT_MATERIAL_PBR", 386, 100)
        out["inputs"][3]["value"] = [0.3]  # the default material's roughness (live), not the node default 0.1
        rgb = self._make("RGB", 122, 100)
        rgb["outputs"][0]["value"] = [0.8, 0.8, 0.8, 1.0]  # likewise (live), not the node default 0.5
        rgb["buttons"][0]["name"] = "default_value"  # loaded from an older .arm: not today's "RGBA"
        self.order = [rgb["id"], out["id"]]
        self.links.append({"from_id": rgb["id"], "from_socket": 0, "to_id": out["id"], "to_socket": 0})
        self.next_id = 7

    # ---- helpers -------------------------------------------------------------------
    def _make(self, node_type: str, x: float, y: float) -> dict[str, Any]:
        tpl = self.catalogue[node_type]

        def entries(key: str) -> list[dict[str, Any]]:
            return [
                {"name": e["name"], "type": e["type"], "value": list(e["default"] or [0.0] if key == "buttons" else e["default"] or [])}
                for e in tpl[key]
            ]

        node = {
            "id": self.next_id,
            "type": node_type,
            "name": tpl["name"],
            "x": float(x),
            "y": float(y),
            "inputs": entries("inputs"),
            "outputs": entries("outputs"),
            "buttons": entries("buttons"),
        }
        self.nodes[node["id"]] = node
        self.order.append(node["id"])
        self.next_id += 1
        return node

    def _node(self, wire: dict[str, Any], key: str = "id") -> dict[str, Any]:
        nid = int(wire.get(key, -1))
        if nid < 0:
            raise OpFailed("x", "bad_args", f"missing or negative '{key}'")
        if nid not in self.nodes:
            raise OpFailed("x", "not_found", f"no node with id {nid}")
        return self.nodes[nid]

    def _describe(self, n: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": n["id"], "type": n["type"], "name": n["name"], "x": n["x"], "y": n["y"],
            "input_sockets": _table(n["inputs"]),
            "output_sockets": _table(n["outputs"]),
            "buttons": _table(n["buttons"]),
            "socket_format": "index:name:type=default_values;",
        }

    # ---- the bridge surface ---------------------------------------------------------
    def call(self, op: str, wire: dict[str, Any]) -> dict[str, Any]:
        self._counts[op] = self._counts.get(op, 0) + 1
        self.log.append((op, dict(wire)))
        if (op, self._counts[op]) in self.fail_on:
            raise OpFailed(op, "internal", f"injected failure of {op} #{self._counts[op]}")
        handler = getattr(self, "op_" + op, None)
        if handler is None:  # as a stock build answers an extension op
            raise OpFailed(op, "unsupported", f"'{op}' needs the native extension")
        return handler(wire)

    def batch(self, items: list[tuple[str, dict[str, Any]]], stop_on_error: bool = False) -> dict[str, Any]:
        results, errors, executed, stopped = [], 0, 0, False
        for i, (op, wire) in enumerate(items):
            if stopped:
                break
            executed += 1
            try:
                results.append({"i": i, "op": op, "ok": True, "result": self.call(op, wire)})
            except OpFailed as exc:
                errors += 1
                results.append({"i": i, "op": op, "ok": False, "error": {"code": exc.code, "message": exc.message}})
                stopped = stop_on_error
        return {"count": len(items), "executed": executed, "errors": errors, "frames": 1,
                "stopped_early": stopped and executed < len(items), "results": results}

    def op_node_list(self, wire: dict[str, Any]) -> dict[str, Any]:
        ids = self.order[:MAX_LIST_ITEMS]
        nodes = "".join(
            f"{n['id']},{n['type']},{n['name']},{n['x']:.6f},{n['y']:.6f};" for n in (self.nodes[i] for i in ids)
        )
        links = "".join(
            f"{lk['from_id']},{lk['from_socket']},{lk['to_id']},{lk['to_socket']};" for lk in self.links[:MAX_LIST_ITEMS]
        )
        return {"count": len(self.order), "returned": len(ids), "nodes": nodes,
                "node_format": "id,type,name,x,y;", "link_count": len(self.links),
                "links_returned": min(len(self.links), MAX_LIST_ITEMS), "links": links,
                "link_format": "from_id,from_socket,to_id,to_socket;"}

    def op_node_get(self, wire: dict[str, Any]) -> dict[str, Any]:
        return self._describe(self._node(wire))

    def op_node_add(self, wire: dict[str, Any]) -> dict[str, Any]:
        t = wire.get("type")
        if t not in self.catalogue:
            raise OpFailed("node_add", "bad_args", f"unknown node type: {t}")
        n = self._make(t, float(wire.get("x", 0)), float(wire.get("y", 0)))
        reply = self._describe(n)
        reply["inputs"], reply["outputs"] = len(n["inputs"]), len(n["outputs"])
        return reply

    def op_node_remove(self, wire: dict[str, Any]) -> dict[str, Any]:
        n = self._node(wire)
        if n["type"] == "OUTPUT_MATERIAL_PBR":
            raise OpFailed("node_remove", "unsupported", "the PBR output node cannot be removed")
        del self.nodes[n["id"]]
        self.order.remove(n["id"])
        self.links = [lk for lk in self.links if n["id"] not in (lk["from_id"], lk["to_id"])]
        return {}

    def op_node_connect(self, wire: dict[str, Any]) -> dict[str, Any]:
        src, dst = self._node(wire, "from_id"), self._node(wire, "to_id")
        fs, ts = int(wire.get("from_socket", -1)), int(wire.get("to_socket", -1))
        if fs < 0 or fs >= len(src["outputs"]):
            raise OpFailed("node_connect", "bad_args", f"from_socket {fs} out of range")
        if ts < 0 or ts >= len(dst["inputs"]):
            raise OpFailed("node_connect", "bad_args", f"to_socket {ts} out of range")
        self.links = [lk for lk in self.links if not (lk["to_id"] == dst["id"] and lk["to_socket"] == ts)]
        self.links.append({"from_id": src["id"], "from_socket": fs, "to_id": dst["id"], "to_socket": ts})
        return {}

    def op_node_disconnect(self, wire: dict[str, Any]) -> dict[str, Any]:
        dst = self._node(wire, "to_id")
        ts = int(wire["to_socket"])
        self.links = [lk for lk in self.links if not (lk["to_id"] == dst["id"] and lk["to_socket"] == ts)]
        return {}

    def op_node_set_value(self, wire: dict[str, Any]) -> dict[str, Any]:
        n = self._node(wire)
        kind = wire["kind"]
        if kind == "button":
            b = int(wire["button"])
            if b >= len(n["buttons"]):
                raise OpFailed("node_set_value", "bad_args", f"button {b} out of range")
            if n["buttons"][b]["value"]:
                n["buttons"][b]["value"][0] = float(wire.get("value", 0))
            return {}
        side = n["inputs"] if str(wire.get("is_input", True)).lower() in ("true", "1") else n["outputs"]
        s = int(wire["socket"])
        if s >= len(side):
            raise OpFailed("node_set_value", "bad_args", f"socket {s} out of range")
        val = side[s]["value"]
        if kind == "float" and val:
            val[0] = float(wire.get("value", 0))
        elif kind == "color" and len(val) >= 3:
            val[0:3] = [float(wire.get(k, 0)) for k in ("r", "g", "b")]
            if len(val) >= 4:
                val[3] = float(wire.get("alpha", 1))
        elif kind == "vector" and len(val) >= 3:
            val[0:3] = [float(wire.get(k, 0)) for k in ("x", "y", "z")]
        return {}

    def op_material_update(self, wire: dict[str, Any]) -> dict[str, Any]:
        self.updates += 1
        return {}

    def op_fill_layer(self, wire: dict[str, Any]) -> dict[str, Any]:
        self.fills += 1
        return {"refill_next_frame": self.fills == 1}

    def op_material_get_active(self, wire: dict[str, Any]) -> dict[str, Any]:
        return {**self.material, "node_count": len(self.nodes), "link_count": len(self.links), **self.channels}

    # ---- test conveniences ------------------------------------------------------------
    def state(self) -> dict[str, Any]:
        """Everything a restore must bring back, with node ids replaced by a stable key
        (type plus position) so a re-created node compares equal."""
        def key(nid: int) -> tuple[Any, ...]:
            n = self.nodes[nid]
            return (n["type"], n["x"], n["y"])

        return {
            "nodes": sorted(
                # Values and types only: a re-created node carries today's socket names
                # (the default material's RGB button is "default_value", a new one "RGBA").
                (key(i), [[(e["type"], e["value"]) for e in self.nodes[i][k]] for k in ("inputs", "outputs", "buttons")])
                for i in self.nodes
            ),
            "links": sorted((key(lk["from_id"]), lk["from_socket"], key(lk["to_id"]), lk["to_socket"]) for lk in self.links),
        }


class FakeExtArmorPaint(FakeArmorPaint):
    """Adds the native extension's history, as history.c keeps it: steps with an identity,
    undo/redo moving a cursor, a new step discarding the redo branch, and the oldest step
    falling off once there are more than undo_steps. Fills push a step; node edits do not
    (upstream's script_material_* record none). Plus project snapshots and paths."""

    def __init__(self, undo_steps: int = 8) -> None:
        super().__init__()
        self.undo_steps = undo_steps
        self.steps: list[dict[str, Any]] = []
        self.redos = 0
        self._next_step = 1
        self.filepath = ""
        self.files: dict[str, dict[str, Any]] = {}
        self.opened: list[str] = []

    def push(self, name: str) -> None:
        if self.redos:
            del self.steps[len(self.steps) - self.redos :]
            self.redos = 0
        self.steps.append({"id": f"0x{self._next_step:x}", "name": name})
        self._next_step += 1
        if len(self.steps) > self.undo_steps:
            self.steps.pop(0)

    def _history(self) -> dict[str, Any]:
        active = len(self.steps) - 1 - self.redos
        return {
            "undos_available": len(self.steps) - self.redos, "redos_available": self.redos,
            "undo_steps_config": self.undo_steps, "active_index": active,
            "steps": [{"index": i, "id": st["id"], "name": st["name"], "undone": i > active}
                      for i, st in enumerate(self.steps)],
        }

    def op_history(self, wire: dict[str, Any]) -> dict[str, Any]:
        return self._history()

    def op_undo(self, wire: dict[str, Any]) -> dict[str, Any]:
        done = 0
        for _ in range(int(wire.get("steps", 1))):
            if len(self.steps) - self.redos <= 0:
                break
            self.redos += 1
            done += 1
        return {"undone": done, **self._history()}

    def op_redo(self, wire: dict[str, Any]) -> dict[str, Any]:
        done = 0
        for _ in range(int(wire.get("steps", 1))):
            if self.redos <= 0:
                break
            self.redos -= 1
            done += 1
        return {"redone": done, **self._history()}

    def op_fill_layer(self, wire: dict[str, Any]) -> dict[str, Any]:
        self.push("Fill Layer")
        return super().op_fill_layer(wire)

    def op_project_snapshot(self, wire: dict[str, Any]) -> dict[str, Any]:
        self.files[wire["path"]] = self.state()
        return {"path": wire["path"], "exists": True, "project_filepath": self.filepath}

    def op_project_open(self, wire: dict[str, Any]) -> dict[str, Any]:
        self.opened.append(wire["path"])
        self.filepath = wire["path"]
        return {}

    def op_project_set_path(self, wire: dict[str, Any]) -> dict[str, Any]:
        self.filepath = wire["path"]
        return {"path": wire["path"]}

    def op_project_get_info(self, wire: dict[str, Any]) -> dict[str, Any]:
        return {"filepath": self.filepath}
