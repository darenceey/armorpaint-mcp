"""Socket catalogue (plan 3.3): generated from ArmorPaint's nodes_material/*.c."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from armorpaint_mcp import node_catalogue  # noqa: E402
from armorpaint_mcp.server import NODE_TYPES  # noqa: E402

# Two definitions in the exact shape of upstream nodes_material/*.c at the pinned commit:
# designated initialisers, _tr() names, f32_array_create_* defaults, and an ENUM button
# whose options come from a string_tmp() format built out of _tr() arguments.
MATH_SRC = r'''
void math2_node_init() {

	char *math_operation_data = string_tmp(
	    "%s\n%s\n%s",
	    _tr("Add"), _tr("Subtract"), _tr("Multiply"));
	ui_node_t *math2_node_def =
	    ALLOC_INIT(ui_node_t, {.id     = 0,
	                           .name   = _tr("Math"),
	                           .type   = "MATH",
	                           .x      = 0,
	                           .y      = 0,
	                           .color  = 0xff4982a0,
	                           .inputs = any_array_create_from_raw(
	                               (void *[]){
	                                   ALLOC_INIT(ui_node_socket_t, {.id            = 0,
	                                                                 .node_id       = 0,
	                                                                 .name          = _tr("Value"),
	                                                                 .type          = "VALUE",
	                                                                 .color         = 0xffa1a1a1,
	                                                                 .default_value = f32_array_create_x(0.5),
	                                                                 .min           = 0.0,
	                                                                 .max           = 1.0,
	                                                                 .precision     = 100,
	                                                                 .display       = 0}),
	                                   ALLOC_INIT(ui_node_socket_t, {.id            = 0,
	                                                                 .node_id       = 0,
	                                                                 .name          = _tr("Value"),
	                                                                 .type          = "VALUE",
	                                                                 .color         = 0xffa1a1a1,
	                                                                 .default_value = f32_array_create_x(-2.5),
	                                                                 .min           = 0.0,
	                                                                 .max           = 1.0,
	                                                                 .precision     = 100,
	                                                                 .display       = 0}),
	                               },
	                               2),
	                           .outputs = any_array_create_from_raw(
	                               (void *[]){
	                                   ALLOC_INIT(ui_node_socket_t, {.id            = 0,
	                                                                 .node_id       = 0,
	                                                                 .name          = _tr("Value"),
	                                                                 .type          = "VALUE",
	                                                                 .color         = 0xffa1a1a1,
	                                                                 .default_value = f32_array_create_x(0.0),
	                                                                 .min           = 0.0,
	                                                                 .max           = 1.0,
	                                                                 .precision     = 100,
	                                                                 .display       = 0}),
	                               },
	                               1),
	                           .buttons = any_array_create_from_raw(
	                               (void *[]){
	                                   ALLOC_INIT(ui_node_button_t, {.name          = _tr("operation"),
	                                                                 .type          = "ENUM",
	                                                                 .output        = 0,
	                                                                 .default_value = f32_array_create_x(0),
	                                                                 .data          = u8_array_create_from_string(math_operation_data),
	                                                                 .height        = 0}),
	                                   ALLOC_INIT(ui_node_button_t, {.name          = _tr("Clamp"),
	                                                                 .type          = "BOOL",
	                                                                 .output        = 0,
	                                                                 .default_value = f32_array_create_x(0),
	                                                                 .data          = NULL,
	                                                                 .height        = 0}),
	                               },
	                               2),
	                           .width = 0,
	                           .flags = 0});

	any_array_push(nodes_material_utilities, math2_node_def);
}
'''

RGB_SRC = r'''
void rgb_node_init() {
	ui_node_t *rgb_node_def = ALLOC_INIT(ui_node_t, {.id      = 0,
	                                                 .name    = _tr("RGB"),
	                                                 .type    = "RGB",
	                                                 .inputs  = any_array_create_from_raw((void *[]){}, 0),
	                                                 .outputs = any_array_create_from_raw(
	                                                     (void *[]){
	                                                         ALLOC_INIT(ui_node_socket_t, {.id            = 0,
	                                                                                       .node_id       = 0,
	                                                                                       .name          = _tr("Color"),
	                                                                                       .type          = "RGBA",
	                                                                                       .default_value = f32_array_create_xyzw(0.8, 0.8, 0.8, 1.0),
	                                                                                       .display       = 0}),
	                                                     },
	                                                     1),
	                                                 .buttons = any_array_create_from_raw(
	                                                     (void *[]){
	                                                         ALLOC_INIT(ui_node_button_t, {.name          = "default_value",
	                                                                                       .type          = "RGBA",
	                                                                                       .output        = 0,
	                                                                                       .default_value = NULL,
	                                                                                       .data          = NULL,
	                                                                                       .height        = 0}),
	                                                     },
	                                                     1),
	                                                 .width   = 0,
	                                                 .flags   = 0});
}
'''

# material_output_node.c keeps its definition only as a JavaScript-style comment.
OUTPUT_SRC = r'''
// let material_output_node_def: ui_node_t = {
//     id: 0,
//     name: _tr("Material Output"),
//     type: "OUTPUT_MATERIAL_PBR",
//     inputs: [
//         {
//             name: _tr("Base Color"),
//             type: "RGBA",
//             default_value: f32_array_create_xyzw(0.8, 0.8, 0.8, 1.0),
//         },
//         {
//             name: _tr("Opacity"),
//             type: "VALUE",
//             default_value: f32_array_create_x(1.0),
//         }
//     ],
//     outputs: [],
//     buttons: []
// };
'''


def test_parse_designated_initialiser_node():
    cat = node_catalogue.parse_sources({"math2_node.c": MATH_SRC})
    math = cat["MATH"]
    assert math["name"] == "Math"
    assert [(s["name"], s["type"], s["default"]) for s in math["inputs"]] == [
        ("Value", "VALUE", [0.5]),
        ("Value", "VALUE", [-2.5]),
    ]
    assert [(s["name"], s["type"]) for s in math["outputs"]] == [("Value", "VALUE")]
    assert math["buttons"][0] == {
        "name": "operation",
        "type": "ENUM",
        "default": [0.0],
        "options": ["Add", "Subtract", "Multiply"],
    }
    assert math["buttons"][1]["name"] == "Clamp" and math["buttons"][1]["type"] == "BOOL"
    assert "options" not in math["buttons"][1]


def test_parse_empty_inputs_and_null_button_default():
    cat = node_catalogue.parse_sources({"rgb_node.c": RGB_SRC})
    rgb = cat["RGB"]
    assert rgb["inputs"] == []
    assert rgb["outputs"][0]["default"] == [0.8, 0.8, 0.8, 1.0]
    assert rgb["buttons"][0]["name"] == "default_value" and rgb["buttons"][0]["default"] is None


def test_parse_commented_output_node():
    cat = node_catalogue.parse_sources({"material_output_node.c": OUTPUT_SRC})
    out = cat["OUTPUT_MATERIAL_PBR"]
    assert [s["name"] for s in out["inputs"]] == ["Base Color", "Opacity"]
    assert out["inputs"][0]["type"] == "RGBA"
    assert out["outputs"] == [] and out["buttons"] == []


def test_runtime_socket_tables_merge_into_the_catalogue():
    cat = node_catalogue.parse_sources({"math2_node.c": MATH_SRC})
    merged = node_catalogue.merge_runtime(
        cat,
        "TEX_FAKE",
        {"input_sockets": "0:Scale:VALUE=5;", "output_sockets": "0:Fac:VALUE=0;", "buttons": ""},
    )
    assert merged["TEX_FAKE"]["inputs"][0] == {"name": "Scale", "type": "VALUE", "default": [5.0]}
    assert merged["TEX_FAKE"]["source"] == "runtime"
    assert "MATH" in merged  # the rest is untouched


def test_shipped_catalogue_is_the_validated_node_type_list():
    cat = node_catalogue.load()
    # The server validates node types against exactly the catalogue. The hand-written list
    # it replaced also held BOOL, ENUM, RGBA, STRING, VECTOR and CUSTOM -- socket and
    # button types, which ArmorPaint rejects as "unknown node type" (checked live).
    assert set(NODE_TYPES) == node_catalogue.creatable(cat, sys.platform)
    assert not {"BOOL", "ENUM", "RGBA", "STRING", "VECTOR", "CUSTOM"} & set(NODE_TYPES)
    assert {"NEURAL_REPEAT", "NEURAL_UPSCALE_IMAGE", "GROUP", "RGB"} <= set(NODE_TYPES)
    pbr = cat["OUTPUT_MATERIAL_PBR"]["inputs"]
    assert [s["name"] for s in pbr] == [
        "Base Color", "Opacity", "Occlusion", "Roughness", "Metallic",
        "Normal Map", "Emission", "Height", "Subsurface",
    ]
    assert cat["TEX_NOISE"]["inputs"][1]["name"] == "Scale"
    assert cat["MIX_RGB"]["buttons"][0]["options"][2] == "Multiply"


def test_default_value_constructor_forms():
    """Upstream uses _x/_xyz/_xyzw, a raw literal array, and a zeroed array of n (curves
    and ramps keep their control points there)."""
    src = r'''
    ALLOC_INIT(ui_node_t, {.name = _tr("Curve"), .type = "CURVE_FAKE",
        .inputs = any_array_create_from_raw((void *[]){
            ALLOC_INIT(ui_node_socket_t, {.name = _tr("A"), .type = "VALUE",
                .default_value = f32_array_create_from_raw((f32[]){1.0, 2.5, -3.0}, 3)}),
            ALLOC_INIT(ui_node_socket_t, {.name = _tr("B"), .type = "VALUE",
                .default_value = f32_array_create(128 + 4)}),
            ALLOC_INIT(ui_node_socket_t, {.name = _tr("C"), .type = "VALUE",
                .default_value = f32_array_create(0)}),
        }, 3),
        .outputs = any_array_create_from_raw((void *[]){}, 0),
        .buttons = any_array_create_from_raw((void *[]){}, 0)});
    '''
    node = node_catalogue.parse_sources({"curve.c": src})["CURVE_FAKE"]
    assert node["inputs"][0]["default"] == [1.0, 2.5, -3.0]
    assert node["inputs"][1]["default"] == [0.0] * 132
    assert node["inputs"][2]["default"] == []


def test_availability_of_types_node_add_cannot_always_create():
    """Found by the live catalogue test: four catalogue types are not creatable in a
    material canvas everywhere (paint/sources/nodes_material.c registration)."""
    cat = node_catalogue.load()
    assert cat["GROUP_INPUT"]["availability"] == "group_canvas_only"
    assert cat["GROUP_OUTPUT"]["availability"] == "group_canvas_only"
    assert cat["NEURAL_IMAGE_TO_3D_MESH"]["availability"] == "windows_only"
    assert cat["NEURAL_TEXTURE_MESH"]["availability"] == "experimental_only"
    assert "availability" not in cat["TEX_NOISE"]
    assert node_catalogue.creatable(cat, "linux") == {t for t in cat} - {
        "GROUP_INPUT", "GROUP_OUTPUT", "NEURAL_IMAGE_TO_3D_MESH"}
    assert "NEURAL_IMAGE_TO_3D_MESH" in node_catalogue.creatable(cat, "win32")
