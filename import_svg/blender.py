import os
import time
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Literal, cast

import bpy
import numpy as np
from bpy_extras.io_utils import ImportHelper
from thorvg_python import PathCommand

from ..utils import select_only
from .thorvg import (
    FillColor,
    GroupNode,
    PaintNode,
    ShapeNode,
    StrokeColor,
    # debug_print,
    open_svg,
)


def _simplify_nodes(node: PaintNode):
    if not isinstance(node, GroupNode):
        return

    def _has_conflicting_masks_or_clips(child: GroupNode):
        return (child.mask is not None and any(c.mask for c in child.children)) or (
            child.clip is not None and any(c.clip for c in child.children)
        )

    def _prepare_compaction(child: GroupNode):
        if child.mask:
            for c in child.children:
                assert not c.mask
                c.mask = child.mask
                c.mask_method = child.mask_method
        if child.clip:
            for c in child.children:
                assert not c.clip
                c.clip = child.clip
        # pass along name if this group is the only child
        if not node.name and child.name and len(node.children) == 1:
            node.name = child.name

    # recursively simplify the node hierarchy
    for child in node.children:
        _simplify_nodes(child)

    if node.mask:
        _simplify_nodes(node.mask)
        if isinstance(node.mask, GroupNode):
            if len(node.mask.children) == 0:
                # TODO: is this correct? (is no mask equivalent to an empty mask?)
                node.mask = None
            elif len(node.mask.children) == 1 and not _has_conflicting_masks_or_clips(
                node.mask
            ):
                _prepare_compaction(node.mask)
                node.mask = node.mask.children[0]
    if node.clip:
        _simplify_nodes(node.clip)
        if isinstance(node.clip, GroupNode):
            if len(node.clip.children) == 0:
                # TODO: is this correct? (is no clip equivalent to an empty clip?)
                node.clip = None
            elif len(node.clip.children) == 1 and not _has_conflicting_masks_or_clips(
                node.clip
            ):
                _prepare_compaction(node.clip)
                node.clip = node.clip.children[0]

    i = 0
    while i < len(node.children):
        child = node.children[i]
        if (
            isinstance(child, GroupNode)
            and not any(isinstance(c, GroupNode) for c in child.children)
            and not _has_conflicting_masks_or_clips(child)
        ):
            _prepare_compaction(child)
            node.children[i : i + 1] = child.children
            i += len(child.children)
        else:
            i += 1


def _create_layers(
    gp: bpy.types.GreasePencil,
    node: PaintNode,
    parent_group: bpy.types.GreasePencilLayerGroup | None,
    mask_layers: list[bpy.types.GreasePencilLayer],
) -> dict[PaintNode, bpy.types.GreasePencilLayer]:
    if not isinstance(node, (GroupNode, ShapeNode)):
        return {}  # we don't support other nodes yet

    nodes_to_layers: dict[PaintNode, bpy.types.GreasePencilLayer] = {}

    # create layers for masks and clips
    if node.mask:
        mask_nodes_to_layers = _create_mask_layers(gp, node.mask)
        nodes_to_layers.update(mask_nodes_to_layers)
        # TODO: is this the correct way to combine masks?
        mask_layers = mask_layers + list(mask_nodes_to_layers.values())
    if node.clip:
        clip_nodes_to_layers = _create_mask_layers(gp, node.clip)
        nodes_to_layers.update(clip_nodes_to_layers)
        # TODO: is this the correct way to combine clips?
        mask_layers = mask_layers + list(clip_nodes_to_layers.values())

    if isinstance(node, GroupNode):
        n2l = _create_layers_from_group_node(gp, node, parent_group, mask_layers)
        nodes_to_layers.update(n2l)
    else:  # isinstance(node, ShapeNode)
        layer = gp.layers.new(node.name or "Layer", layer_group=parent_group)
        layer.frames.new(1)
        nodes_to_layers[node] = layer

        # set up masks in the grease pencil layer
        if mask_layers:
            layer.use_masks = True
            for mask_layer in mask_layers:
                layer.mask_layers.add(mask_layer)

    return nodes_to_layers


def _create_mask_layers(gp: bpy.types.GreasePencil, mask_node: PaintNode):
    mask_nodes_to_layers = _create_layers(gp, mask_node, None, [])
    for mask_layer in mask_nodes_to_layers.values():
        mask_layer.opacity = 0.0
    return mask_nodes_to_layers


def _create_layers_from_group_node(
    gp: bpy.types.GreasePencil,
    node: GroupNode,
    parent_group: bpy.types.GreasePencilLayerGroup | None,
    mask_layers: list[bpy.types.GreasePencilLayer],
) -> dict[PaintNode, bpy.types.GreasePencilLayer]:
    name = node.name or "Group"
    gp_group = gp.layer_groups.new(name, parent_group=parent_group)

    nodes_to_layers: dict[PaintNode, bpy.types.GreasePencilLayer] = {}

    shape_nodes: list[PaintNode] = []
    n_shape_layers = 0

    def _add_shape_layer():
        nonlocal n_shape_layers
        n_shape_layers += 1
        n2l = _create_layers(gp, shape_nodes[-1], gp_group, mask_layers)
        shape_layer = n2l[shape_nodes[-1]]
        shape_layer.name = f"{gp_group.name}_Shapes_{n_shape_layers}"

        nodes_to_layers.update(n2l)
        for shape in shape_nodes[:-1]:
            nodes_to_layers[shape] = shape_layer
        shape_nodes.clear()

    for child in node.children:
        if isinstance(child, GroupNode):
            # emit a layer for the shape nodes between the
            # last group and this one
            if shape_nodes:
                _add_shape_layer()
            child_nodes_to_layers = _create_layers(gp, child, gp_group, mask_layers)
            nodes_to_layers.update(child_nodes_to_layers)
        elif isinstance(child, ShapeNode):
            if shape_nodes:
                prev_shape = shape_nodes[-1]
                # we may only collect shapes into one layer if they have the same
                # mask/clip settings. if we detect a change in these settings,
                # emit a layer to flush these shapes before proceeding
                if prev_shape.mask != child.mask or prev_shape.clip != child.clip:
                    _add_shape_layer()
            shape_nodes.append(child)
    # emit layer for remaining shape nodes
    if shape_nodes:
        _add_shape_layer()

    return nodes_to_layers


@dataclass
class StrokeData:
    position: np.ndarray  # shape (N, 3)
    handle_left: np.ndarray  # shape (N, 3)
    handle_right: np.ndarray  # shape (N, 3)
    cyclic: bool


def _path_to_stroke_data(
    shape: ShapeNode,
) -> list[StrokeData]:
    points = np.insert(shape.path_pts, 1, 0.0, axis=1)
    if len(points) == 0:
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


@dataclass
class BuildContext:
    # inputs
    gp: bpy.types.GreasePencil
    nodes_to_layers: Mapping[PaintNode, bpy.types.GreasePencilLayer]
    scale: float
    scale_vec: np.ndarray
    is_mask: bool
    is_clip: bool

    # state
    material_idxs: dict[tuple[StrokeColor, FillColor] | Literal["mask"], int]
    cur_fill_id: int
    visited: set[int]

    # outputs
    layer_to_builder: dict[str, LayerBuilder]


def _get_material(
    ctx: BuildContext, stroke_color: StrokeColor, fill_color: FillColor
) -> int:
    key = (stroke_color, fill_color)
    if key in ctx.material_idxs:
        return ctx.material_idxs[key]

    material = bpy.data.materials.new(ctx.gp.name + "_Material")
    bpy.data.materials.create_gpencil_data(material)
    ctx.gp.materials.append(material)
    idx = len(ctx.gp.materials) - 1
    ctx.material_idxs[key] = idx

    assert material.grease_pencil
    material.grease_pencil.fill_color = _srgb_to_linear(fill_color)  # type: ignore
    if stroke_color:
        material.grease_pencil.color = _srgb_to_linear(stroke_color)  # type: ignore
    else:
        material.grease_pencil.color = (0, 0, 0, 0)  # type: ignore

    return idx


def _get_mask_material(ctx: BuildContext) -> int:
    if "mask" in ctx.material_idxs:
        return ctx.material_idxs["mask"]

    material = bpy.data.materials.new(ctx.gp.name + "_MaskMaterial")
    bpy.data.materials.create_gpencil_data(material)
    ctx.gp.materials.append(material)
    idx = len(ctx.gp.materials) - 1
    ctx.material_idxs["mask"] = idx

    assert material.grease_pencil
    material.grease_pencil.fill_color = (1, 1, 1, 1)  # type: ignore
    material.grease_pencil.color = (1, 1, 1, 1)  # type: ignore

    return idx


def _gather_geometry_and_materials_data(ctx: BuildContext, node: PaintNode):
    # we may visit a node multiple times if e.g. the simplification process
    # assigns the same mask node to multiple nodes
    if node.addr in ctx.visited:
        return
    ctx.visited.add(node.addr)

    if not isinstance(node, (GroupNode, ShapeNode)):
        return

    if node.mask:
        # TODO: do we need to reset the transform stack when building the mask?
        _gather_geometry_and_materials_data(replace(ctx, is_mask=True), node.mask)
    if node.clip:
        # TODO: do we need to reset the transform stack when building the clip?
        _gather_geometry_and_materials_data(replace(ctx, is_clip=True), node.clip)

    if isinstance(node, GroupNode):
        for child in node.children:
            _gather_geometry_and_materials_data(ctx, child)
    elif isinstance(node, ShapeNode):
        strokes = _path_to_stroke_data(node)
        if not strokes:
            return  # for safety (otherwise add_strokes will crash)

        layer = ctx.nodes_to_layers[node]
        if layer.name not in ctx.layer_to_builder:
            ctx.layer_to_builder[layer.name] = LayerBuilder(layer)
        builder = ctx.layer_to_builder[layer.name]

        builder.add_stroke_lengths([len(s.position) for s in strokes])

        if ctx.is_mask or ctx.is_clip:
            mat_idx = _get_mask_material(ctx)
        else:
            mat_idx = _get_material(ctx, node.stroke_color, node.fill_color)
        # note: clip paths do not take stroke width into account
        no_stroke = node.stroke_color is None or ctx.is_clip

        # curve-domain attributes
        # curve_type = np.full((len(strokes),), 2, dtype=np.int8)  # 2 => bezier
        cyclic = np.array([s.cyclic for s in strokes], dtype=np.bool)
        fill_id = np.full((len(strokes),), ctx.cur_fill_id, dtype=np.int32)
        material_index = np.full((len(strokes),), mat_idx, dtype=np.int32)
        hide_stroke = np.full((len(strokes),), no_stroke, dtype=np.bool)
        ctx.cur_fill_id += 1
        # builder.append_to_attr("curve_type", "INT8", "CURVE", curve_type)
        builder.append_to_attr("cyclic", "BOOLEAN", "CURVE", cyclic)
        builder.append_to_attr("fill_id", "INT", "CURVE", fill_id)
        builder.append_to_attr("material_index", "INT", "CURVE", material_index)
        builder.append_to_attr("hide_stroke", "BOOLEAN", "CURVE", hide_stroke)

        # point-domain attributes
        position = ctx.scale_vec * np.vstack([s.position for s in strokes])
        handle_left = ctx.scale_vec * np.vstack([s.handle_left for s in strokes])
        handle_right = ctx.scale_vec * np.vstack([s.handle_right for s in strokes])
        handle_type = np.full((len(position),), 0, dtype=np.int8)  # 0 => free
        radius = np.full(
            (len(position),), node.stroke_width * ctx.scale * 0.5, dtype=np.float32
        )
        builder.append_to_attr("position", "FLOAT_VECTOR", "POINT", position)
        builder.append_to_attr("handle_left", "FLOAT_VECTOR", "POINT", handle_left)
        builder.append_to_attr("handle_right", "FLOAT_VECTOR", "POINT", handle_right)
        builder.append_to_attr("handle_type_left", "INT8", "POINT", handle_type)
        builder.append_to_attr("handle_type_right", "INT8", "POINT", handle_type)
        builder.append_to_attr("radius", "FLOAT", "POINT", radius)


def _create_geometry_and_materials(
    gp: bpy.types.GreasePencil,
    root_node: PaintNode,
    nodes_to_layers: Mapping[PaintNode, bpy.types.GreasePencilLayer],
    scale: float,
):
    # negate Z to flip Z-down convention to Z-up
    scale_vec = np.array([scale, scale, -scale])

    ctx = BuildContext(
        gp=gp,
        nodes_to_layers=nodes_to_layers,
        scale=scale,
        scale_vec=scale_vec,
        is_mask=False,
        is_clip=False,
        material_idxs={},
        cur_fill_id=1,
        visited=set(),
        layer_to_builder={},
    )
    _gather_geometry_and_materials_data(ctx, root_node)

    for builder in ctx.layer_to_builder.values():
        builder.build()


def _paint_to_gp(node: PaintNode, name: str, scale: float) -> bpy.types.GreasePencil:
    gp = bpy.data.grease_pencils.new(name)
    nodes_to_layers = _create_layers(gp, node, None, [])
    _create_geometry_and_materials(gp, node, nodes_to_layers, scale)
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
    center_geometry: bpy.props.BoolProperty(
        name="Center Geometry",
        description="Center the geometry's bounding box on the origin",
        default=True,
    )

    def execute(self, context: bpy.types.Context):
        print(self.filepath)
        path = cast(str, self.filepath)

        start = time.time()
        node = open_svg(path)
        # debug_print(node)
        parse_end = time.time()
        print("parse", parse_end - start)

        # print("================")
        _simplify_nodes(node)
        # debug_print(node)
        obj_name = os.path.basename(path)
        gp = _paint_to_gp(node, obj_name, cast(float, self.scale))
        gp_end = time.time()
        print("gp", gp_end - parse_end)

        obj = bpy.data.objects.new(obj_name, gp)
        context.collection.objects.link(obj)

        scene: bpy.types.Scene = context.scene
        obj.location = scene.cursor.location
        select_only(obj, context.view_layer)
        if self.center_geometry:
            old_pivot = scene.tool_settings.transform_pivot_point
            scene.tool_settings.transform_pivot_point = "BOUNDING_BOX_CENTER"
            bpy.ops.object.origin_set(type="GEOMETRY_ORIGIN")
            scene.tool_settings.transform_pivot_point = old_pivot

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
