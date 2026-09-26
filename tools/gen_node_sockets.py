"""Regenerate armorpaint_mcp/data/node_sockets.json from an ArmorPaint checkout.

    python tools/gen_node_sockets.py /path/to/armorpaint

Reads paint/sources/nodes_material/*.c and nodes_neural/*.c -- the material node set.
The brush nodes (nodes_brush/) are left out: they reuse names like VALUE and MATH with
different sockets. Records the checkout's commit.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from armorpaint_mcp import node_catalogue  # noqa: E402


# Types that node_add cannot create in a material canvas everywhere. From the registration
# in paint/sources/nodes_material.c (nodes_material_init) at the pinned commit:
#   GROUP_INPUT / GROUP_OUTPUT  defined in group_node.c, but only GROUP is pushed to a
#                               creatable list; the other two live inside a group's canvas
#   NEURAL_IMAGE_TO_3D_MESH     image_to_3d_mesh_node_init() is under #ifdef IRON_WINDOWS
#   NEURAL_TEXTURE_MESH         texture_mesh_node_init() only if g_config->experimental
# tests/test_live_graph.py::test_catalogue_matches_every_live_node_type checks this list.
AVAILABILITY = {
    "GROUP_INPUT": "group_canvas_only",
    "GROUP_OUTPUT": "group_canvas_only",
    "NEURAL_IMAGE_TO_3D_MESH": "windows_only",
    "NEURAL_TEXTURE_MESH": "experimental_only",
}


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    checkout = Path(sys.argv[1])
    sources = checkout / "paint" / "sources"
    files = {
        f"{p.parent.name}/{p.name}": p.read_text(encoding="utf-8")
        for folder in ("nodes_material", "nodes_neural")
        for p in sorted((sources / folder).glob("*.c"))
    }
    if not files:
        print(f"no node sources under {sources}", file=sys.stderr)
        return 1
    nodes = node_catalogue.parse_sources(files)
    for node_type, availability in AVAILABILITY.items():
        if node_type in nodes:
            nodes[node_type]["availability"] = availability
    try:
        commit = subprocess.run(
            ["git", "-C", str(checkout), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        commit = "unknown"
    out = {
        "_meta": {
            "generator": "tools/gen_node_sockets.py",
            "armorpaint_commit": commit,
            "source": "paint/sources/nodes_material/*.c, nodes_neural/*.c",
            "node_count": len(nodes),
        },
        "nodes": dict(sorted(nodes.items())),
    }
    node_catalogue.CATALOGUE_PATH.write_text(json.dumps(out, indent=1) + "\n", encoding="utf-8")
    print(f"{len(nodes)} node types -> {node_catalogue.CATALOGUE_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
