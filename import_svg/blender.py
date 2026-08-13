import os
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast

import bpy
import numpy as np
from bpy_extras.io_utils import ImportHelper
from thorvg_python import PathCommand

from .thorvg import (
    FillColor,
    GroupNode,
    PaintNode,
    ShapeNode,
    StrokeColor,
    debug_print,
    open_svg,
)


def create_layers(
    gp: bpy.types.GreasePencil,
    node: GroupNode,
    parent_group: bpy.types.GreasePencilLayerGroup | None,
) -> dict[int, bpy.types.GreasePencilLayer]:
    name = node.name or "Group"
    gp_group = gp.layer_groups.new(name, parent_group=parent_group)
    nodes_to_layers: dict[int, bpy.types.GreasePencilLayer] = {}

    shape_nodes: list[PaintNode] = []
    n_shape_layers = 0

    def _add_shape_layer():
        nonlocal n_shape_layers
        n_shape_layers += 1
        shape_layer = gp.layers.new(
            f"{gp_group.name}_Shapes_{n_shape_layers}", layer_group=gp_group
        )
        shape_layer.frames.new(1)
        for shape in shape_nodes:
            nodes_to_layers[shape.addr] = shape_layer
        shape_nodes.clear()

    for child in node.children:
        if isinstance(child, GroupNode):
            # emit a layer for the shape nodes between the
            # last group and this one
            if shape_nodes:
                _add_shape_layer()
            nodes_to_layers.update(create_layers(gp, child, gp_group))
        else:
            shape_nodes.append(child)

    if shape_nodes:
        _add_shape_layer()

    return nodes_to_layers


@dataclass
class StrokeData:
    position: np.ndarray  # shape (N, 3)
    handle_left: np.ndarray  # shape (N, 3)
    handle_right: np.ndarray  # shape (N, 3)
    cyclic: bool


def path_to_stroke_data(
    shape: ShapeNode,
) -> list[StrokeData]:
    points = [np.array([p[0], 0.0, p[1]], dtype=np.float32) for p in shape.path_pts]
    if not points:
        return []

    strokes: list[StrokeData] = []
    cur_start_pt = points[0]
    # list of (handle1, handle2, end_pt) bezier segments
    cur_segs: list[tuple[np.ndarray, np.ndarray, np.ndarray]] = []

    def end_stroke(cyclic: bool):
        position = [cur_start_pt]
        handle_left = [cur_start_pt]
        handle_right = []
        for rh, lh, pos in cur_segs:
            handle_right.append(rh)
            handle_left.append(lh)
            position.append(pos)
        # at this point:
        # len(position) == len(handle_left) == N, len(handle_right) == N-1

        # does a cyclic stroke has the same start and end point?
        if (
            cyclic
            and len(position) > 1
            # TODO: make tolerance configurable?
            and np.allclose(position[0], position[-1], atol=1e-5)
        ):
            # merge the points and their handles together
            handle_left[0] = handle_left[-1]
            handle_left.pop()
            position.pop()
        else:
            handle_right.append(position[-1])

        strokes.append(
            StrokeData(
                np.vstack(position),
                np.vstack(handle_left),
                np.vstack(handle_right),
                cyclic,
            )
        )
        cur_segs.clear()

    i = 0
    for cmd in shape.path_cmds:
        if cmd == PathCommand.MOVE_TO:
            if cur_segs:
                end_stroke(False)
            cur_start_pt = points[i]
            i += 1
        elif cmd == PathCommand.LINE_TO:
            start_pt = cur_segs[-1][2] if cur_segs else cur_start_pt
            cur_segs.append((start_pt, points[i], points[i]))
            i += 1
        elif cmd == PathCommand.CUBIC_TO:
            cur_segs.append((points[i], points[i + 1], points[i + 2]))
            i += 3
        else:  # cmd == PathCommand.CLOSE
            end_stroke(True)
    if cur_segs:
        end_stroke(False)

    return strokes


def _srgb_transfer_func(r: float) -> float:
    return r / 12.92 if r <= 0.04045 else pow((r + 0.055) / 1.055, 2.4)


def _srgb_to_linear(col: tuple[float, float, float, float]):
    return (
        _srgb_transfer_func(col[0]),
        _srgb_transfer_func(col[1]),
        _srgb_transfer_func(col[2]),
        col[3],
    )


def _expand_to_4x4(m: np.ndarray) -> np.ndarray:
    return np.array(
        [
            [m[0][0], 0, m[0][1], m[0][2]],
            [0, 1, 0, 0],
            [m[1][0], 0, m[1][1], m[1][2]],
            [m[2][0], 0, m[2][1], m[2][2]],
        ],
        dtype=np.float32,
    )


class LayerBuilder:
    def __init__(self, layer: bpy.types.GreasePencilLayer):
        self.layer = layer
        self.stroke_lengths: list[int] = []
        # maps name to (type, domain)
        self.attrs: dict[str, tuple[str, str]] = {}
        # maps name to data
        self.data: dict[str, np.ndarray] = {}

    def add_stroke_lengths(self, lengths: list[int]):
        self.stroke_lengths.extend(lengths)

    def append_to_attr(self, name: str, data_type: str, domain: str, data: np.ndarray):
        if name in self.attrs:
            self.data[name] = np.concat([self.data[name], data], axis=0)
        else:
            self.attrs[name] = (data_type, domain)
            self.data[name] = data

    def build(self):
        assert len(self.layer.frames) > 0 and self.layer.frames[0].drawing
        drawing = self.layer.frames[0].drawing
        attributes = drawing.attributes
        drawing.add_strokes(self.stroke_lengths)
        drawing.set_types(type="BEZIER")

        for name, (data_type, domain) in self.attrs.items():
            data = self.data[name]
            if name not in attributes:
                attribute = attributes.new(name, data_type, domain)  # type: ignore
            else:
                attribute = attributes[name]
            if data_type == "FLOAT_VECTOR":
                attribute.data.foreach_set("vector", np.ravel(data))  # type: ignore
            else:
                attribute.data.foreach_set("value", data)  # type: ignore

        drawing.tag_positions_changed()


def create_geometry_and_materials(
    gp: bpy.types.GreasePencil,
    root_node: PaintNode,
    nodes_to_layers: Mapping[int, bpy.types.GreasePencilLayer],
    scale: float,
):
    cur_fill_id = 1
    # negate Z to flip Z-down convention to Z-up
    scale_mat = np.diag([scale, scale, -scale, 1.0])

    material_idxs: dict[tuple[StrokeColor, FillColor], int] = {}
    transform_stack = [np.identity(4, dtype=np.float32)]
    layer_to_builder: dict[str, LayerBuilder] = {}

    def _get_material(stroke_color: StrokeColor, fill_color: FillColor) -> int:
        key = (stroke_color, fill_color)
        if key in material_idxs:
            return material_idxs[key]

        material = bpy.data.materials.new(gp.name + "_Material")
        bpy.data.materials.create_gpencil_data(material)
        gp.materials.append(material)
        idx = len(gp.materials) - 1
        material_idxs[key] = idx

        assert material.grease_pencil
        material.grease_pencil.fill_color = _srgb_to_linear(fill_color)  # type: ignore
        if stroke_color:
            material.grease_pencil.color = _srgb_to_linear(stroke_color)  # type: ignore
        else:
            material.grease_pencil.color = (0, 0, 0, 0)  # type: ignore

        return idx

    def _apply_transform(transform: np.ndarray, x: np.ndarray) -> np.ndarray:
        x = np.pad(x, [(0, 0), (0, 1)], constant_values=1)[..., np.newaxis]
        return (transform @ x)[..., :3, 0]

    def _recurse(node: PaintNode):
        transform = _expand_to_4x4(node.transform)
        transform = transform @ transform_stack[-1]
        transform_stack.append(transform)
        transform = scale_mat @ transform

        if isinstance(node, GroupNode):
            for child in node.children:
                _recurse(child)
        elif isinstance(node, ShapeNode):
            strokes = path_to_stroke_data(node)
            if not strokes:
                transform_stack.pop()
                return  # for safety (otherwise add_strokes will crash)
            layer = nodes_to_layers[node.addr]
            if layer.name not in layer_to_builder:
                layer_to_builder[layer.name] = LayerBuilder(layer)
            builder = layer_to_builder[layer.name]

            builder.add_stroke_lengths([len(s.position) for s in strokes])

            mat_idx = _get_material(node.stroke_color, node.fill_color)

            # curve-domain attributes
            nonlocal cur_fill_id
            # curve_type = np.full((len(strokes),), 2, dtype=np.int8)  # 2 => bezier
            cyclic = np.array([s.cyclic for s in strokes], dtype=np.bool)
            fill_id = np.full((len(strokes),), cur_fill_id, dtype=np.int32)
            material_index = np.full((len(strokes),), mat_idx, dtype=np.int32)
            cur_fill_id += 1
            # builder.append_to_attr("curve_type", "INT8", "CURVE", curve_type)
            builder.append_to_attr("cyclic", "BOOLEAN", "CURVE", cyclic)
            builder.append_to_attr("fill_id", "INT", "CURVE", fill_id)
            builder.append_to_attr("material_index", "INT", "CURVE", material_index)

            # point-domain attributes
            position = _apply_transform(
                transform, np.vstack([s.position for s in strokes])
            )
            handle_left = _apply_transform(
                transform, np.vstack([s.handle_left for s in strokes])
            )
            handle_right = _apply_transform(
                transform, np.vstack([s.handle_right for s in strokes])
            )
            handle_type = np.full((len(position),), 0, dtype=np.int8)  # 0 => free
            radius = np.full(
                (len(position),), node.stroke_width * scale * 0.5, dtype=np.float32
            )
            builder.append_to_attr("position", "FLOAT_VECTOR", "POINT", position)
            builder.append_to_attr("handle_left", "FLOAT_VECTOR", "POINT", handle_left)
            builder.append_to_attr(
                "handle_right", "FLOAT_VECTOR", "POINT", handle_right
            )
            builder.append_to_attr("handle_type_left", "INT8", "POINT", handle_type)
            builder.append_to_attr("handle_type_right", "INT8", "POINT", handle_type)
            builder.append_to_attr("radius", "FLOAT", "POINT", radius)

        transform_stack.pop()

    _recurse(root_node)

    for builder in layer_to_builder.values():
        builder.build()


def paint_to_gp(node: PaintNode, name: str, scale: float) -> bpy.types.GreasePencil:
    gp = bpy.data.grease_pencils.new(name)
    if isinstance(node, GroupNode):
        nodes_to_layers = create_layers(gp, node, None)
    else:
        layer = gp.layers.new(node.name or "Layer")
        nodes_to_layers = {node.addr: layer}
    create_geometry_and_materials(gp, node, nodes_to_layers, scale)
    return gp


class FLASHY_OP_import_svg(bpy.types.Operator, ImportHelper):
    """Import SVG as Grease Pencil (with slightly better support for SVG features)"""

    bl_idname = "flashy.import_svg"
    bl_label = "SVG to Grease Pencil (Improved)"
    bl_options = {"REGISTER", "UNDO"}  # noqa: RUF012

    # will be filled out by ImportHelper
    filepath = bpy.props.StringProperty(name="File Path", maxlen=1024, default="")

    filter_glob: bpy.props.StringProperty(default="*.svg", options={"HIDDEN"})

    scale: bpy.props.FloatProperty(
        name="Scale",
        description="Scale factor, in units per pixel",
        default=0.005,
        min=0.0,
        precision=3,
    )

    def execute(self, context: bpy.types.Context):
        print(self.filepath)
        path = cast(str, self.filepath)

        start = time.time()
        node = open_svg(path)
        # debug_print(node)
        parse_end = time.time()
        print("parse", parse_end - start)

        obj_name = os.path.basename(path)
        gp = paint_to_gp(node, obj_name, cast(float, self.scale))
        gp_end = time.time()
        print("gp", gp_end - parse_end)

        obj = bpy.data.objects.new(obj_name, gp)
        context.scene.collection.objects.link(obj)
        link_end = time.time()
        print("link", link_end - gp_end)
        return {"FINISHED"}


class FLASHY_PT_import_svg(bpy.types.Panel):
    """Dope Sheet panel allowing easy viewing/modification of ease settings
    for multiple keyframes at once."""

    bl_category = "Flashy"
    bl_label = "Import SVG"
    bl_idname = "FLASHY_PT_import_svg"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"

    def draw(self, context: bpy.types.Context):
        layout = self.layout
        assert layout

        layout.operator("flashy.import_svg")
