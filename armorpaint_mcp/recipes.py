"""Parameterised material recipes: node-graph specs (see node_graph.apply) with named
parameters, stored as JSON in data/recipes/.

A parameter is referenced in the spec as the string ``"${name}"``, which is replaced by
the value itself (a number stays a number, a colour stays a list).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .node_graph import SpecError

RECIPE_DIR = Path(__file__).resolve().parent / "data" / "recipes"
_PARAM_RE = re.compile(r"^\$\{(\w+)\}$")


def _all() -> dict[str, dict[str, Any]]:
    out = {}
    for path in sorted(RECIPE_DIR.glob("*.json")):
        recipe = json.loads(path.read_text(encoding="utf-8"))
        out[recipe["name"]] = recipe
    return out


def list_recipes() -> list[dict[str, Any]]:
    return [{k: r[k] for k in ("name", "description", "params")} for r in _all().values()]


def get(name: str) -> dict[str, Any]:
    recipes = _all()
    if name not in recipes:
        raise SpecError([f"unknown recipe {name!r}; available: {', '.join(sorted(recipes))}"])
    return recipes[name]


def _check(name: str, decl: dict[str, Any], value: Any) -> Any:
    numeric = isinstance(value, (int, float)) and not isinstance(value, bool)
    if decl.get("type") == "color":
        if not (isinstance(value, list) and len(value) in (3, 4)
                and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in value)):
            raise SpecError([f"parameter {name!r} must be a colour: 3 or 4 numbers in 0..1"])
    elif not numeric:
        raise SpecError([f"parameter {name!r} must be a number, got {value!r}"])
    return value


def render(name: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """The recipe's spec with parameters filled in (defaults for any not given)."""
    recipe = get(name)
    declared = recipe.get("params", {})
    params = dict(params or {})
    unknown = sorted(set(params) - set(declared))
    if unknown:
        raise SpecError([f"unknown parameter(s) {', '.join(unknown)} for recipe {name!r}; "
                         f"it takes: {', '.join(sorted(declared)) or 'none'}"])
    values = {k: _check(k, d, params.get(k, d["default"])) for k, d in declared.items()}

    def fill(obj: Any) -> Any:
        if isinstance(obj, dict):
            return {k: fill(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [fill(v) for v in obj]
        if isinstance(obj, str):
            m = _PARAM_RE.match(obj)
            if m:
                return values[m.group(1)]
        return obj

    return fill(recipe["spec"])
