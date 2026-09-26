"""The documentation agrees with the code (plan phase 5)."""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from armorpaint_mcp import server, strokes  # noqa: E402

README = (ROOT / "README.md").read_text(encoding="utf-8")
ARCH = (ROOT / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")
MINIC = (ROOT / "docs" / "MINIC_DIALECT_AND_API.md").read_text(encoding="utf-8")
UPSTREAM = (ROOT / "docs" / "UPSTREAM_CHANGES.md").read_text(encoding="utf-8")
SKILL = (ROOT / "skill" / "SKILL.md").read_text(encoding="utf-8")


def test_readme_tool_count_and_index():
    names = {t.name for t in server.TOOLS}
    counts = {int(n) for n in re.findall(r"\b(\d+) tools\b", README)}
    assert counts == {len(names)}, f"README says {counts}, the server has {len(names)}"
    missing = sorted(n for n in names if f"`{n}`" not in README)
    assert missing == [], missing


def test_readme_has_the_review_response_with_hard_limits():
    section = README[README.index("## Review response"):]
    section = section[: section.index("\n## ", 5)]
    for topic in ("stroke", "latency", "node", "UV", "undo"):
        assert topic.lower() in section.lower(), topic
    assert "Cannot be done" in section or "cannot be done" in section


def test_no_stale_claims():
    assert "At most 48 points" not in README and "the whole stroke runs inside one" not in README
    assert "64 globals" not in ARCH  # script globals are capped at 128 (MINIC_MAX_VARS)
    assert "node edits, layer" not in README  # node edits are not in ArmorPaint's history
    assert "physically untestable" in README.lower() or "untestable" in README.lower()


def test_minic_caps_table_lists_script_globals():
    caps = MINIC[MINIC.index("## 1.13 Hard caps"):]
    caps = caps[: caps.index("\n## ", 5)]
    assert re.search(r"script globals \| 128", caps)


def test_upstream_changes_documents_extension_v2():
    assert "mesh_op" in UPSTREAM and "project_snapshot" in UPSTREAM and "version 2" in UPSTREAM.lower()


def test_skill_teaches_the_new_workflow():
    for tool in ("ap_mesh_inspect", "ap_node_graph_apply", "ap_node_recipe", "ap_checkpoint", "ap_paint_stroke_uv"):
        assert tool in SKILL, tool
    assert str(strokes.MAX_POINTS) in README  # the per-request stroke limit is stated
