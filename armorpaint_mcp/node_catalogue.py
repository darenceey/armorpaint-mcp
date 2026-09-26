"""The material-node socket catalogue: every node type's inputs, outputs and buttons.

ArmorPaint builds its node definitions in C (``paint/sources/nodes_material/*.c``) and a
plugin can only see a node's sockets after creating one. The catalogue is those
definitions read out of the source ahead of time, so a graph spec can be checked -- node
types, socket names, value shapes -- before anything touches the user's material.

``parse_sources`` is the parser; ``tools/gen_node_sockets.py`` runs it over a checkout and
writes ``data/node_sockets.json``, which ``load`` reads. Socket tables that ``node_add``
returns at run time can be folded in with ``merge_runtime`` (a build newer than the
catalogue, or a plugin-defined node type).

Names here are the English source strings. A localised ArmorPaint translates them at run
time (``_tr``), which is why graph specs resolve socket names against the catalogue and
then address sockets by index on the wire.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

CATALOGUE_PATH = Path(__file__).resolve().parent / "data" / "node_sockets.json"

_STRING_RE = r'"((?:[^"\\]|\\.)*)"'
_NAME_RE = re.compile(r"(?:^|[\s,{.])name\s*[:=]\s*(?:_tr\(\s*)?" + _STRING_RE)
_TYPE_RE = re.compile(r"(?:^|[\s,{.])type\s*[:=]\s*" + _STRING_RE)
_DEFAULT_RE = re.compile(r"default_value\s*[:=]\s*(NULL|null|(f32_array_create\w*)\s*\()")
_NUMBER_RE = re.compile(r"-?\d+(?:\.\d*)?(?:[eE][-+]?\d+)?")
_DATA_RE = re.compile(r"(?:^|[\s,{.])data\s*[:=]\s*u8_array_create_from_string\(\s*([^)]*?)\s*\)")
_SECTION_RE = re.compile(r"(?:^|[\s,{.])(inputs|outputs|buttons)\s*[:=]\s*")
_NODE_START_RE = re.compile(r"ALLOC_INIT\(\s*ui_node_t\s*,|\bui_node_t\s*=\s*(?=\{)")
_ITEM_RE = re.compile(r"ALLOC_INIT\(\s*ui_node_(?:socket|button)_t\s*,")
_TR_RE = re.compile(r"_tr\(\s*" + _STRING_RE + r"\s*\)")


def _uncomment(text: str) -> str:
    """material_output_node.c keeps its definition as a ``//`` comment only."""
    return "\n".join(re.sub(r"^\s*//\s?", "", line) for line in text.splitlines())


def _match(text: str, start: int) -> int:
    """Index just past the bracket that closes the one at ``start`` (strings skipped)."""
    pairs = {"(": ")", "[": "]", "{": "}"}
    stack = [pairs[text[start]]]
    i = start + 1
    while i < len(text) and stack:
        ch = text[i]
        if ch == '"':
            i += 1
            while i < len(text) and text[i] != '"':
                i += 2 if text[i] == "\\" else 1
        elif ch in pairs:
            stack.append(pairs[ch])
        elif ch == stack[-1]:
            stack.pop()
        i += 1
    return i


def _default(block: str) -> list[float] | None:
    m = _DEFAULT_RE.search(block)
    if not m or m.group(1).lower() == "null":
        return None
    args = block[m.end() : _match(block, m.end() - 1) - 1]
    if m.group(2) == "f32_array_create":  # a zeroed array of n: f32_array_create(128 + 4)
        return [0.0] * sum(int(term) for term in args.split("+"))
    if "{" in args:  # f32_array_create_from_raw((f32[]){...}, n)
        args = args[args.index("{") + 1 : args.rindex("}")]
    return [float(v) for v in _NUMBER_RE.findall(args)]


def _enum_options(block: str, source: str) -> list[str] | None:
    m = _DATA_RE.search(block)
    if not m:
        return None
    arg = m.group(1)
    if arg.startswith('"'):
        return [s for s in arg.strip('"').split("\\n") if s]
    decl = re.search(r"\b" + re.escape(arg) + r"\s*=\s*string_tmp\s*\(", source)
    if not decl:
        return None
    call = source[decl.end() - 1 : _match(source, decl.end() - 1)]
    return _TR_RE.findall(call) or None


def _items(section: str) -> list[str]:
    """The socket/button blocks inside an ``inputs``/``outputs``/``buttons`` value."""
    blocks = []
    if _ITEM_RE.search(section):
        for m in _ITEM_RE.finditer(section):
            brace = section.index("{", m.end())
            blocks.append(section[brace : _match(section, brace)])
        return blocks
    # JavaScript-style: [ {...}, {...} ]
    if not section.startswith("["):
        return blocks
    i = 1
    while i < len(section) - 1:
        if section[i] == "{":
            end = _match(section, i)
            blocks.append(section[i:end])
            i = end
        else:
            i += 1
    return blocks


def _parse_node(body: str, source: str) -> tuple[str, dict[str, Any]] | None:
    sections: dict[str, str] = {}
    first_section = len(body)
    for m in _SECTION_RE.finditer(body):
        start = m.end()
        if start >= len(body):
            continue
        first_section = min(first_section, m.start())
        if body[start] in "([{":
            sections.setdefault(m.group(1), body[start : _match(body, start)])
        else:
            call = re.match(r"\w+\s*\(", body[start:])
            if call:
                paren = start + call.end() - 1
                sections.setdefault(m.group(1), body[start : _match(body, paren)])
    head = body[:first_section]
    type_m = _TYPE_RE.search(head)
    if not type_m:
        return None
    name_m = _NAME_RE.search(head)
    node: dict[str, Any] = {"name": name_m.group(1) if name_m else type_m.group(1)}
    for key in ("inputs", "outputs", "buttons"):
        entries = []
        for block in _items(sections.get(key, "")):
            n = _NAME_RE.search(block)
            t = _TYPE_RE.search(block)
            entry: dict[str, Any] = {
                "name": n.group(1) if n else "",
                "type": t.group(1) if t else "",
                "default": _default(block),
            }
            if key == "buttons":
                options = _enum_options(block, source)
                if options:
                    entry["options"] = options
            entries.append(entry)
        node[key] = entries
    return type_m.group(1), node


def parse_sources(files: dict[str, str]) -> dict[str, dict[str, Any]]:
    """Parse ``{filename: C source}`` into ``{node type: definition}``."""
    catalogue: dict[str, dict[str, Any]] = {}
    for filename, text in sorted(files.items()):
        source = text if _NODE_START_RE.search(text) else _uncomment(text)
        for m in _NODE_START_RE.finditer(source):
            brace = source.index("{", m.end() - 1 if source[m.end() - 1] == "{" else m.end())
            parsed = _parse_node(source[brace : _match(source, brace)], source)
            if parsed is None:
                continue
            node_type, node = parsed
            node["file"] = filename
            catalogue.setdefault(node_type, node)
    return catalogue


def parse_socket_table(table: str | None) -> list[dict[str, Any]]:
    """Decode the bridge's ``index:name:type=v0,v1;`` table (see ``socket_table`` in the
    plugin). ArmorPaint's catalogue uses no ``:``, ``;`` or ``=`` in names."""
    out: list[dict[str, Any]] = []
    for entry in (table or "").split(";"):
        if not entry:
            continue
        head, _, values = entry.partition("=")
        index, _, rest = head.partition(":")
        name, _, typ = rest.rpartition(":")
        out.append(
            {
                "index": int(index),
                "name": name,
                "type": typ,
                "value": [float(v) for v in values.split(",") if v != ""],
            }
        )
    return out


def merge_runtime(
    catalogue: dict[str, dict[str, Any]], node_type: str, reply: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    """A copy of ``catalogue`` with ``node_type`` described by a ``node_add``/``node_get``
    reply -- the ground truth for this build."""
    def entries(key: str) -> list[dict[str, Any]]:
        return [
            {"name": s["name"], "type": s["type"], "default": s["value"] or None}
            for s in parse_socket_table(reply.get(key))
        ]

    merged = dict(catalogue)
    merged[node_type] = {
        "name": reply.get("name") or node_type,
        "inputs": entries("input_sockets"),
        "outputs": entries("output_sockets"),
        "buttons": entries("buttons"),
        "source": "runtime",
    }
    return merged


@lru_cache(maxsize=1)
def _load_file(path: str) -> dict[str, dict[str, Any]]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return data["nodes"]


def load(path: Path | None = None) -> dict[str, dict[str, Any]]:
    """The shipped catalogue (``data/node_sockets.json``)."""
    return dict(_load_file(str(path or CATALOGUE_PATH)))


def creatable(catalogue: dict[str, dict[str, Any]], platform: str) -> set[str]:
    """Types ``node_add`` can create in a material canvas on ``platform`` (sys.platform).
    Experimental-only types are included: they work once the user enables ArmorPaint's
    experimental option, and the bridge answers 'unknown node type' otherwise."""
    out = set()
    for node_type, node in catalogue.items():
        availability = node.get("availability")
        if availability == "group_canvas_only":
            continue
        if availability == "windows_only" and platform != "win32":
            continue
        out.add(node_type)
    return out


def meta(path: Path | None = None) -> dict[str, Any]:
    data = json.loads(Path(path or CATALOGUE_PATH).read_text(encoding="utf-8"))
    return data.get("_meta", {})
