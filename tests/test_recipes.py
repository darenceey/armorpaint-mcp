"""Parameterised material recipes (plan 3.5)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from armorpaint_mcp import node_graph, recipes  # noqa: E402
from armorpaint_mcp.node_graph import SpecError  # noqa: E402
from fake_armorpaint import FakeArmorPaint  # noqa: E402

EXPECTED = {"worn_painted_metal", "painted_wood", "stone", "edge_wear_grunge"}


def test_the_recipes_are_listed_with_their_parameters():
    listed = {r["name"]: r for r in recipes.list_recipes()}
    assert EXPECTED <= set(listed)
    for r in listed.values():
        assert r["description"]
        for p in r["params"].values():
            assert "default" in p and p["description"]


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_each_recipe_validates_against_the_catalogue(name):
    ap = FakeArmorPaint()
    report = node_graph.apply(ap, recipes.render(name), dry_run=True)
    assert report["ok"], report
    assert report["plan"]["add"] and report["plan"]["edit"]


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_each_recipe_builds_a_lint_clean_graph(name):
    ap = FakeArmorPaint()
    assert node_graph.apply(ap, recipes.render(name), mode="replace")["ok"]
    bad = [i for i in node_graph.lint(node_graph.read_graph(ap)) if i["level"] != "info"]
    assert bad == []


def test_parameters_are_substituted_with_their_types():
    spec = recipes.render("edge_wear_grunge", {"grunge_scale": 12, "paint_color": [0.1, 0.2, 0.3, 1]})
    assert spec["nodes"]["grunge"]["inputs"]["Scale"] == 12
    assert spec["nodes"]["paint"]["outputs"]["Color"] == [0.1, 0.2, 0.3, 1]
    default = recipes.render("edge_wear_grunge")
    assert default["nodes"]["grunge"]["inputs"]["Scale"] == recipes.get("edge_wear_grunge")["params"]["grunge_scale"]["default"]


def test_unknown_recipe_and_parameter_are_errors():
    with pytest.raises(SpecError, match="unknown recipe"):
        recipes.render("nope")
    with pytest.raises(SpecError, match="unknown parameter"):
        recipes.render("stone", {"wobble": 1})
    with pytest.raises(SpecError, match="number"):
        recipes.render("stone", {"cell_scale": "big"})
