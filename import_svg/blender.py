import math
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Literal, TypeVar, cast

import bpy
import numpy as np
from bpy_extras.io_utils import ImportHelper
from thorvg_python import PathCommand, StrokeCap, StrokeFill, StrokeJoin

from ..utils import (
    normalize,
    normalize_and_get_length,
    select_only,
    vec_length,
    vec_length_sq,
)
from .thorvg import (
    FillColor,
    Float4,
    Gradient,
    GroupNode,
    LinearGradAttrs,
    PaintNode,
    RadialGradAttrs,
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


# ============== functions for creating layers ==============


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
class BuildOptions:
    scale: float
    use_vertex_colors: bool
    stroke_grad_strat: Literal["AVERAGE", "VERTEX"]
    fill_grad_strat: Literal["AVERAGE", "GRADIENT", "TEXTURE"]


T = TypeVar("T")


@dataclass
class VisitShapesContext[T]:
    is_mask: bool
    is_clip: bool
    visited: set[int]
    data: T


def _visit_shapes[T](
    ctx: VisitShapesContext[T],
    node: PaintNode,
    callback: Callable[[VisitShapesContext[T], ShapeNode], None],
):
    if node.addr in ctx.visited:
        return
    ctx.visited.add(node.addr)

    # ignore TextNode
    if not isinstance(node, (GroupNode, ShapeNode)):
        return

    if node.mask:
        _visit_shapes(replace(ctx, is_mask=True), node.mask, callback)
    if node.clip:
        _visit_shapes(replace(ctx, is_clip=True), node.clip, callback)

    if isinstance(node, GroupNode):
        for child in node.children:
            _visit_shapes(ctx, child, callback)
    elif isinstance(node, ShapeNode):
        callback(ctx, node)


# ============== functions for creating materials ==============


ColorDesc = tuple[StrokeColor, FillColor | None] | Literal["mask"]
"""A description of how a shape should be colored in blender,
minus info like gradient transforms.
"""


@dataclass
class GatherColorDescsData:
    opts: BuildOptions  # input
    nodes_to_material_keys: dict[ShapeNode, ColorDesc]  # output


def _get_color_desc(opts: BuildOptions, node: ShapeNode) -> ColorDesc:
    stroke = node.stroke_color
    fill = node.fill_color

    # indicate we don't care about the color since it'll get hidden anyway
    if stroke == (0, 0, 0, 0):
        stroke = None
    if fill == (0, 0, 0, 0):
        fill = None

    if isinstance(stroke, Gradient):
        if opts.stroke_grad_strat == "AVERAGE":
            stroke = stroke.avg_color()
        elif len(stroke.stops) == 1:
            stroke = stroke.stops[0][1]
        elif len(stroke.stops) == 0:
            stroke = None

    if isinstance(fill, Gradient):
        if opts.fill_grad_strat == "AVERAGE":
            fill = fill.avg_color()
        elif len(fill.stops) == 1:
            fill = fill.stops[0][1]
        elif len(fill.stops) == 0:
            fill = None

    return (stroke, fill)


def _gather_color_descs_callback(
    ctx: VisitShapesContext[GatherColorDescsData], node: ShapeNode
):
    if ctx.is_mask or ctx.is_clip:
        ctx.data.nodes_to_material_keys[node] = "mask"
    else:
        ctx.data.nodes_to_material_keys[node] = _get_color_desc(ctx.data.opts, node)


def _gather_color_descs(
    opts: BuildOptions, root_node: PaintNode
) -> dict[ShapeNode, ColorDesc]:
    ctx = VisitShapesContext(
        is_mask=False,
        is_clip=False,
        visited=set(),
        data=GatherColorDescsData(opts=opts, nodes_to_material_keys={}),
    )
    _visit_shapes(ctx, root_node, _gather_color_descs_callback)
    return ctx.data.nodes_to_material_keys


def _srgb_transfer_func(r: float) -> float:
    return r / 12.92 if r <= 0.04045 else pow((r + 0.055) / 1.055, 2.4)


def _srgb_to_linear(col: tuple[float, float, float, float]):
    return (
        _srgb_transfer_func(col[0]),
        _srgb_transfer_func(col[1]),
        _srgb_transfer_func(col[2]),
        col[3],
    )


def _create_gradient_image(grad: Gradient):
    if isinstance(grad.attrs, LinearGradAttrs):
        # NOTE: blender's current behavior for texture clamping is to clamp
        # right at the edge of the texture, which means it partially wraps to
        # the other side of the texture and so the color ends up being a mix
        # of the pixels on opposite sides of the texture. this is undesired
        # for padded linear gradients, of course.
        # to partially work around this, we split the texture into thirds
        # and fill the first and last thirds with padding manually, to at
        # least give some buffer space
        IMG_W = 512 if grad.spread == StrokeFill.PAD else 128
        img = bpy.data.images.new("gradient", IMG_W, 1, alpha=True)
        for i in range(IMG_W):
            if grad.spread == StrokeFill.PAD:
                t = max(0, min(1, 3 * i / IMG_W - 1))
            else:
                t = i / (IMG_W - 1)
            col = grad.eval_stops(t)
            img.pixels[i * 4 : (i + 1) * 4] = col  # type: ignore
        img.update()
        return img
    else:
        raise NotImplementedError


def _should_use_gradient_fill(opts: BuildOptions, grad: Gradient):
    return (
        opts.fill_grad_strat == "GRADIENT"
        and len(grad.stops) == 2
        and grad.spread == StrokeFill.PAD
        # cannot represent radial gradients when the first stop is not at center
        and not (isinstance(grad.attrs, RadialGradAttrs) and grad.stops[0][0] > 0)
    )


def _setup_material_fill_gradient(
    opts: BuildOptions, gp_style: bpy.types.MaterialGPencilStyle, grad: Gradient
):
    if _should_use_gradient_fill(opts, grad):
        gp_style.fill_style = "GRADIENT"
        gp_style.gradient_type = (
            "LINEAR" if isinstance(grad.attrs, LinearGradAttrs) else "RADIAL"
        )
        gp_style.fill_color = _srgb_to_linear(grad.stops[0][1])  # type: ignore
        gp_style.mix_color = _srgb_to_linear(grad.stops[1][1])  # type: ignore
    else:
        gp_style.fill_style = "TEXTURE"
        gp_style.fill_image = _create_gradient_image(grad)
        gp_style.texture_clamp = grad.spread == StrokeFill.PAD
    gp_style.mix_factor = 0
    gp_style.texture_offset = (-0.5, -0.5)
    gp_style.texture_angle = 0
    gp_style.texture_scale = (1, 1)


def _create_material(
    gp: bpy.types.GreasePencil,
    stroke_color: Float4,
    fill_color: Float4 | Gradient,
    opts: BuildOptions,
    suffix: str = "_Material",
) -> int:
    material = bpy.data.materials.new(gp.name + suffix)
    bpy.data.materials.create_gpencil_data(material)
    gp.materials.append(material)
    idx = len(gp.materials) - 1

    assert material.grease_pencil

    material.grease_pencil.color = _srgb_to_linear(stroke_color)  # type: ignore

    if isinstance(fill_color, Gradient):
        _setup_material_fill_gradient(opts, material.grease_pencil, fill_color)
    else:
        material.grease_pencil.fill_color = _srgb_to_linear(fill_color)  # type: ignore

    return idx


@dataclass
class ShapeMaterialInfo:
    mat_idx: int
    stroke_vert_color: Float4 | Gradient | None
    fill_vert_color: Float4 | None
    # these are separated from MaterialInfo to allow different shapes
    # to be e.g. stroke-only or fill-only while sharing the underlying
    # material, which i hope to take advantage of later
    hide_stroke: bool
    hide_fill: bool


MaterialKey = tuple[tuple | None, tuple | None]


def _build_materials(
    gp: bpy.types.GreasePencil,
    color_descs: dict[ShapeNode, ColorDesc],
    opts: BuildOptions,
) -> dict[ShapeNode, ShapeMaterialInfo]:
    # will populate and return at the end
    node_to_mat_info: dict[ShapeNode, ShapeMaterialInfo] = {}
    # allows for reusing materials if multiple keys turn out to need the same
    # material specification
    mat_spec_to_idx: dict[MaterialKey | Literal["mask"], int] = {}

    for shape, desc in color_descs.items():
        stroke: StrokeColor = None
        fill: FillColor | None = None
        stroke_vert_color: Float4 | Gradient | None = None
        fill_vert_color: Float4 | None = None

        if desc == "mask":
            spec = "mask"
            hide_stroke = shape.stroke_color is None or shape.stroke_color == (
                0,
                0,
                0,
                0,
            )
            hide_fill = shape.fill_color == (0, 0, 0, 0)
        else:
            # if using vertex colors:
            #   - stroke is always opaque black (rely on vert cols/opacity)
            #   - fill is opaque black unless it's a gradient
            # if not using vertex colors:
            #   - stroke is the stroke color, or opaque black for gradients
            #   - fill is the fill color/gradient
            stroke, fill = desc
            if opts.use_vertex_colors:
                if stroke is not None:
                    stroke_vert_color = stroke
                    stroke = (0, 0, 0, 1)
                if fill is not None and not isinstance(fill, Gradient):
                    fill_vert_color = fill
                    fill = (0, 0, 0, 1)
            else:
                if stroke is not None and isinstance(stroke, Gradient):
                    stroke_vert_color = stroke
                    stroke = (0, 0, 0, 1)
            stroke_key = stroke.key() if isinstance(stroke, Gradient) else stroke
            fill_key = fill.key() if isinstance(fill, Gradient) else fill
            spec = (stroke_key, fill_key)

            hide_stroke = stroke is None
            hide_fill = fill is None

        # create new material if not already existing
        if spec not in mat_spec_to_idx:
            if spec == "mask":
                idx = _create_material(
                    gp, (1, 1, 1, 1), (1, 1, 1, 1), opts, "_MaskMaterial"
                )
                mat_spec_to_idx[spec] = idx
            else:
                # if the user unhides the stroke or fill of a shape,
                # opaque black will be more clearly visible and give more of an
                # indication about what the user has done
                if stroke is None:
                    stroke = (0, 0, 0, 1)
                if fill is None:
                    fill = (0, 0, 0, 1)
                idx = _create_material(gp, stroke, fill, opts, "_Material")
                mat_spec_to_idx[spec] = idx

        node_to_mat_info[shape] = ShapeMaterialInfo(
            mat_spec_to_idx[spec],
            stroke_vert_color,
            fill_vert_color,
            hide_stroke,
            hide_fill,
        )

    return node_to_mat_info


# ============== functions for building geometry ==============


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


DomainType = Literal["CURVE", "POINT"]
VECTOR_TYPES = {"FLOAT2": 2, "FLOAT_VECTOR": 3, "FLOAT_COLOR": 4}


class LayerBuilder:
    def __init__(self, layer: bpy.types.GreasePencilLayer):
        self.layer = layer
        self.stroke_lengths: list[int] = []
        # maps name to (type, domain)
        self.attrs: dict[str, tuple[str, DomainType]] = {}
        # maps name to extension fill value
        self.default_vals: dict[str, float] = {}
        # maps name to data
        self.data: dict[str, np.ndarray] = {}

    def add_stroke_lengths(self, lengths: list[int]):
        self.stroke_lengths.extend(lengths)

    def _extend_attr_data(self, name: str, incoming_data_len: int):
        data_type, domain = self.attrs[name]
        if domain == "CURVE":
            wanted_len = len(self.stroke_lengths)
        else:  # domain == "POINT"
            wanted_len = sum(self.stroke_lengths)
        pad_len = wanted_len - incoming_data_len - len(self.data[name])
        if pad_len > 0:
            pad = [(0, pad_len)]
            if data_type in VECTOR_TYPES:
                pad.append((0, 0))
            pad_val = self.default_vals.get(name, 0)
            self.data[name] = np.pad(self.data[name], pad, constant_values=pad_val)

    def append_to_attr(
        self,
        name: str,
        data_type: str,
        domain: DomainType,
        data: np.ndarray,
        default_val: float | None = None,
    ):
        if name not in self.attrs:
            self.attrs[name] = (data_type, domain)
            if data_type in VECTOR_TYPES:
                size = VECTOR_TYPES[data_type]
                self.data[name] = np.array([], dtype=data.dtype).reshape((0, size))
            else:
                self.data[name] = np.array([], dtype=data.dtype)
            if default_val is not None:
                self.default_vals[name] = default_val
        self._extend_attr_data(name, len(data))
        self.data[name] = np.concat([self.data[name], data], axis=0)

    def build(self):
        for name in self.attrs:
            self._extend_attr_data(name, 0)

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
            if data_type == "FLOAT_COLOR":
                attribute.data.foreach_set("color_srgb", np.ravel(data))  # type: ignore
            elif data_type in VECTOR_TYPES:
                attribute.data.foreach_set("vector", np.ravel(data))  # type: ignore
            else:
                attribute.data.foreach_set("value", data)  # type: ignore

        drawing.tag_positions_changed()


def _get_curve_normal(positions: np.ndarray) -> np.ndarray:
    # needed to accurately compute the stroke's local coordinate system.
    # ported from blender:
    # https://projects.blender.org/blender/blender/src/commit/fea4184c17f0af51c5a5bff3c41457f3a278ab55/source/blender/blenkernel/intern/grease_pencil.cc#L679
    if len(positions) < 2:
        return np.array((1.0, 0.0, 0.0))
    # newell's method for calculating normals
    normal = np.array((0.0, 0.0, 0.0))
    prev_pt = positions[-1]
    for cur_pt in positions:
        normal[0] += (prev_pt[1] - cur_pt[1]) * (prev_pt[2] + cur_pt[2])
        normal[1] += (prev_pt[2] - cur_pt[2]) * (prev_pt[0] + cur_pt[0])
        normal[2] += (prev_pt[0] - cur_pt[0]) * (prev_pt[1] + cur_pt[1])
        prev_pt = cur_pt
    # handle degenerate case where all points are colinear
    normal, length = normalize_and_get_length(normal)
    if length < np.finfo(float).eps * len(positions):
        for i in range(len(positions) - 1):
            segment_vec = positions[i] - positions[i + 1]
            if np.dot(segment_vec, segment_vec) != 0:
                normal = normalize(np.array((segment_vec[1], -segment_vec[0], 0.0)))
                break
    return normal


@dataclass
class StrokeUVTransforms:
    translation: tuple[float, float]
    rotation: float
    scale: tuple[float, float]


def _get_uv_transforms(
    positions: np.ndarray,
    grad: Gradient,
    scale: float,
    shape_transform: np.ndarray,
    account_for_trim: bool,
    has_padding: bool,
) -> StrokeUVTransforms:
    if len(positions) < 2:
        return StrokeUVTransforms((0, 0), 0, (1, -1))
    pos0, pos1 = positions[0], positions[1]
    xaxis = normalize(pos1 - pos0)
    yaxis = np.cross(_get_curve_normal(positions), xaxis)
    if vec_length_sq(xaxis) == 0 or vec_length_sq(yaxis) == 0:
        return StrokeUVTransforms((0, 0), 0, (1, -1))
    layer_to_stroke = np.array(
        [
            [xaxis[0], xaxis[1], xaxis[2], -np.dot(pos0, xaxis)],
            [yaxis[0], yaxis[1], yaxis[2], -np.dot(pos0, yaxis)],
        ]
    )
    if isinstance(grad.attrs, LinearGradAttrs):
        # linear gradient is bound by two lines.
        # we want the stroke xaxis to be rotated to be perpendicular to these
        # lines (in world space).
        # we can't just transform (x1, y1) and (x2, y2) by the gradient transform
        # to get the perpendicular, since skewing might be involved, so we
        # instead transform (x1, y1) and the line through (x2, y2), and then
        # project (x1, y1) onto the line to get the new endpoint
        # TODO: since this doesn't depend on positions, we can probably
        # factor this out and avoid repeating this work for each stroke
        p1_orig = np.array([grad.attrs.x1, grad.attrs.y1])
        p2_orig = np.array([grad.attrs.x2, grad.attrs.y2])
        offset = p2_orig - p1_orig
        l2_dir = np.array([-offset[1], offset[0]])
        if account_for_trim:
            # if we're using gradient fills, we need to account for the
            # start and end stops possibly not being at 0/1
            p1 = p1_orig + grad.stops[0][0] * offset
            p2 = p1_orig + grad.stops[-1][0] * offset
        else:
            # if we're using texture fills, those stops are already positioned
            # correctly in the texture
            p1, p2 = p1_orig, p2_orig
        transform = shape_transform @ grad.transform
        p1_w = (transform @ np.append(p1, 1))[:2]
        p2_w = (transform @ np.append(p2, 1))[:2]
        l2_dir_w = (transform @ np.append(l2_dir, 0))[:2]
        offset_w = p1_w - p2_w
        p1_proj = p2_w + (
            np.dot(offset_w, l2_dir_w) / np.dot(l2_dir_w, l2_dir_w) * l2_dir_w
        )
        # transform from SVG space to layer space
        start = np.array([scale * p1_w[0], 0, -scale * p1_w[1], 1])
        end = np.array([scale * p1_proj[0], 0, -scale * p1_proj[1], 1])
        # transform to stroke's coord space
        start = layer_to_stroke @ start
        end = layer_to_stroke @ end
        u = end - start
        if has_padding:
            start -= u
            u *= 3
        v = np.array([u[1], -u[0]])
        tex_to_stroke = np.vstack([np.array([u, v, start]).T, [0.0, 0.0, 1.0]])
        stroke_to_tex = np.linalg.pinv(tex_to_stroke)
        # decompose into translation/rotation/scale. see:
        # https://projects.blender.org/blender/blender/src/commit/fea4184c17f0af51c5a5bff3c41457f3a278ab55/source/blender/blenkernel/intern/grease_pencil.cc#L914
        translation = tuple(stroke_to_tex[0:2, 2])
        rotation = math.atan2(stroke_to_tex[1][0], stroke_to_tex[0][0])
        xscale = 1 / vec_length(stroke_to_tex[0:2, 0])
        yscale = 1 / vec_length(stroke_to_tex[0:2, 1])
        if np.linalg.det(stroke_to_tex[0:2, 0:2]) < 0:
            yscale = -yscale
        return StrokeUVTransforms(translation, rotation, (xscale, yscale))
    else:
        raise NotImplementedError


def _get_gradient_vertex_color_attrs(
    local_positions: np.ndarray, grad: Gradient
) -> tuple[np.ndarray, np.ndarray]:
    """Given positions in the gradient's local space, return appropriate
    vertex colors (shape (N, 4)) and opacities (shape (N,)) that approximate
    the given gradient.
    """
    colors = np.array([grad.eval_pos(pos) for pos in local_positions])
    opacity = colors[..., 3].copy()
    colors[..., 3] = 1
    return colors, opacity


def _get_solid_vertex_color_attrs(
    n: int, color: Float4
) -> tuple[np.ndarray, np.ndarray]:
    """Return vertex color (shape (N, 4)) and opacity (shape (N,)) attribute
    data with the given length.
    """
    colors = np.repeat([color], n, axis=0)
    opacity = colors[..., 3].copy()
    colors[..., 3] = 1
    return colors, opacity


@dataclass
class GatherGeometryData:
    # inputs
    gp: bpy.types.GreasePencil
    nodes_to_layers: dict[PaintNode, bpy.types.GreasePencilLayer]
    nodes_to_mat_info: dict[ShapeNode, ShapeMaterialInfo]
    opts: BuildOptions
    scale_vec: np.ndarray
    # state
    cur_fill_id: int
    # outputs
    layer_to_builder: dict[str, LayerBuilder]


def _gather_geometry_callback(
    ctx: VisitShapesContext[GatherGeometryData], node: ShapeNode
):
    strokes = _path_to_stroke_data(node)
    if not strokes:
        return  # for safety (otherwise add_strokes will crash)

    ctxd = ctx.data
    layer = ctxd.nodes_to_layers[node]
    if layer.name not in ctxd.layer_to_builder:
        ctxd.layer_to_builder[layer.name] = LayerBuilder(layer)
    builder = ctxd.layer_to_builder[layer.name]

    stroke_lengths = [len(s.position) for s in strokes]
    builder.add_stroke_lengths(stroke_lengths)

    shape_mat_info = ctxd.nodes_to_mat_info[node]
    mat_idx = shape_mat_info.mat_idx
    # note: clip paths do not take stroke width into account
    no_stroke = shape_mat_info.hide_stroke or ctx.is_clip
    no_fill = shape_mat_info.hide_fill and not ctx.is_clip

    stroke_vert_color = shape_mat_info.stroke_vert_color
    fill_vert_color = shape_mat_info.fill_vert_color
    if isinstance(stroke_vert_color, Gradient):
        # transform positions back to node's local svg coordinates
        ps = np.vstack([s.position for s in strokes])
        ps = np.delete(ps, 1, axis=-1)
        ps = np.pad(ps, [(0, 0), (0, 1)], constant_values=1)
        ps = ps[..., np.newaxis]
        inverse_transform = np.linalg.inv(node.world_transform)
        ps = (inverse_transform @ ps)[..., :2, 0]
        vertex_color, opacity = _get_gradient_vertex_color_attrs(ps, stroke_vert_color)
    elif isinstance(stroke_vert_color, tuple):
        vertex_color, opacity = _get_solid_vertex_color_attrs(
            sum(stroke_lengths), stroke_vert_color
        )
    else:
        vertex_color, opacity = None, None

    # point-domain attributes
    transformed_positions = [ctxd.scale_vec * s.position for s in strokes]
    position = np.vstack(transformed_positions)
    handle_left = ctxd.scale_vec * np.vstack([s.handle_left for s in strokes])
    handle_right = ctxd.scale_vec * np.vstack([s.handle_right for s in strokes])
    handle_type = np.full((len(position),), 0, dtype=np.int8)  # 0 => free
    radius = np.full(
        (len(position),), node.stroke_width * ctxd.opts.scale * 0.5, dtype=np.float32
    )
    builder.append_to_attr("position", "FLOAT_VECTOR", "POINT", position)
    builder.append_to_attr("handle_left", "FLOAT_VECTOR", "POINT", handle_left)
    builder.append_to_attr("handle_right", "FLOAT_VECTOR", "POINT", handle_right)
    builder.append_to_attr("handle_type_left", "INT8", "POINT", handle_type)
    builder.append_to_attr("handle_type_right", "INT8", "POINT", handle_type)
    builder.append_to_attr("radius", "FLOAT", "POINT", radius)
    if node.stroke_join != StrokeJoin.ROUND:
        if node.stroke_join == StrokeJoin.MITER:
            # thanks mdn
            angle = 2 * math.asin(1 / node.stroke_miterlimit)
            # NOTE: currently, blender rounds miter angles to nearest pi/62
            # during the packing process:
            # https://projects.blender.org/blender/blender/src/commit/4d6a448ec8e203a080b276c34ae73fb91078d088/source/blender/draw/intern/draw_cache_impl_grease_pencil.cc#L264
            # this can cause some points close to the miter angle to have
            # incorrect miter cutoff status.
            # err on the side of keeping the miter, so the user can fix it
            # themselves if needed by setting corner type to Flat
            angle = math.floor(angle / math.pi * 62) / 62 * math.pi
        else:  # node.stroke_join == StrokeJoin.BEVEL
            angle = 3.142
        miter_angle = np.full((len(position),), angle, dtype=np.float32)
        builder.append_to_attr("miter_angle", "FLOAT", "POINT", miter_angle)
    if vertex_color is not None and opacity is not None:
        builder.append_to_attr("vertex_color", "FLOAT_COLOR", "POINT", vertex_color)
        builder.append_to_attr("opacity", "FLOAT", "POINT", opacity, 1)

    # curve-domain attributes
    if no_fill:
        fill_id_val = 0
    else:
        fill_id_val = ctxd.cur_fill_id
        ctxd.cur_fill_id += 1
    cyclic = np.array([s.cyclic for s in strokes], dtype=np.bool)
    fill_id = np.full((len(strokes),), fill_id_val, dtype=np.int32)
    material_index = np.full((len(strokes),), mat_idx, dtype=np.int32)
    builder.append_to_attr("cyclic", "BOOLEAN", "CURVE", cyclic)
    builder.append_to_attr("fill_id", "INT", "CURVE", fill_id)
    builder.append_to_attr("material_index", "INT", "CURVE", material_index)
    if no_stroke:
        hide_stroke = np.full((len(strokes),), no_stroke, dtype=np.bool)
        builder.append_to_attr("hide_stroke", "BOOLEAN", "CURVE", hide_stroke)
    if node.stroke_cap != StrokeCap.ROUND:
        # TODO: proper square cap support? (maybe by adding new points at the ends)
        cap = np.full((len(strokes),), 1, dtype=np.int8)  # 0 => flat
        builder.append_to_attr("start_cap", "INT8", "CURVE", cap)
        builder.append_to_attr("end_cap", "INT8", "CURVE", cap)
    if fill_vert_color is not None:
        fill_color, fill_opacity = _get_solid_vertex_color_attrs(
            len(strokes), fill_vert_color
        )
        builder.append_to_attr("fill_color", "FLOAT_COLOR", "CURVE", fill_color)
        builder.append_to_attr("fill_opacity", "FLOAT", "CURVE", fill_opacity, 1)
    if ctxd.gp.materials[mat_idx].grease_pencil.fill_style != "SOLID":  # type: ignore
        grad = node.fill_color
        assert isinstance(grad, Gradient)
        is_using_gradient_fill = _should_use_gradient_fill(ctxd.opts, grad)
        account_for_trim = is_using_gradient_fill
        has_padding = (
            not is_using_gradient_fill
            and isinstance(grad.attrs, LinearGradAttrs)
            and grad.spread == StrokeFill.PAD
        )
        uvs = [
            _get_uv_transforms(
                positions,
                grad,
                ctxd.opts.scale,
                node.world_transform,
                account_for_trim,
                has_padding,
            )
            for positions in transformed_positions
        ]
        uv_rotation = np.array([uv.rotation for uv in uvs], dtype=np.float32)
        uv_translation = np.array([uv.translation for uv in uvs], dtype=np.float32)
        uv_scale = np.array([uv.scale for uv in uvs], dtype=np.float32)
        builder.append_to_attr("uv_rotation", "FLOAT", "CURVE", uv_rotation)
        builder.append_to_attr("uv_translation", "FLOAT2", "CURVE", uv_translation)
        builder.append_to_attr("uv_scale", "FLOAT2", "CURVE", uv_scale)


def _gather_geometry(
    gp: bpy.types.GreasePencil,
    root_node: PaintNode,
    nodes_to_layers: dict[PaintNode, bpy.types.GreasePencilLayer],
    nodes_to_mat_info: dict[ShapeNode, ShapeMaterialInfo],
    opts: BuildOptions,
):
    # negate Z to flip Z-down convention to Z-up
    scale_vec = np.array([opts.scale, opts.scale, -opts.scale])
    ctx = VisitShapesContext(
        is_mask=False,
        is_clip=False,
        visited=set(),
        data=GatherGeometryData(
            gp=gp,
            nodes_to_layers=nodes_to_layers,
            nodes_to_mat_info=nodes_to_mat_info,
            opts=opts,
            scale_vec=scale_vec,
            cur_fill_id=1,
            layer_to_builder={},
        ),
    )
    _visit_shapes(ctx, root_node, _gather_geometry_callback)
    return ctx.data.layer_to_builder


def _create_geometry_and_materials(
    gp: bpy.types.GreasePencil,
    root_node: PaintNode,
    nodes_to_layers: dict[PaintNode, bpy.types.GreasePencilLayer],
    opts: BuildOptions,
):
    color_descs = _gather_color_descs(opts, root_node)
    nodes_to_mat_info = _build_materials(gp, color_descs, opts)
    layer_to_builder = _gather_geometry(
        gp, root_node, nodes_to_layers, nodes_to_mat_info, opts
    )
    for builder in layer_to_builder.values():
        builder.build()


def _paint_to_gp(
    node: PaintNode, name: str, opts: BuildOptions
) -> bpy.types.GreasePencil:
    gp = bpy.data.grease_pencils.new(name)
    nodes_to_layers = _create_layers(gp, node, None, [])
    _create_geometry_and_materials(gp, node, nodes_to_layers, opts)
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
    use_vertex_colors: bpy.props.BoolProperty(
        name="Use Vertex Colors",
        description="Use vertex colors instead of materials for coloring strokes, only falling back on materials for fill gradients",
        default=True,
    )
    stroke_grad_strat: bpy.props.EnumProperty(
        items=[
            (
                "AVERAGE",
                "Average Color",
                "Uniformly color the stroke with the average color of the gradient",
            ),
            (
                "VERTEX",
                "Vertex Coloring",
                "Use vertex colors to approximate the stroke gradient",
            ),
        ],
        name="Stroke Gradient Conversion",
        description="Strategy for converting stroke gradients to Grease Pencil",
        default="VERTEX",
    )
    fill_grad_strat: bpy.props.EnumProperty(
        items=[
            (
                "AVERAGE",
                "Average Color",
                "Uniformly color the fill with the average color of the gradient",
            ),
            (
                "GRADIENT",
                "Gradient Fill",
                "Use gradient fills for 2-color gradients, falling back on texture fills when needed (will match SVG appearance less w.r.t color blending, but colors will be easier to adjust)",
            ),
            (
                "TEXTURE",
                "Texture Fill",
                "Create gradient textures and use them for material fills (will match SVG appearance more w.r.t color blending, but colors will be harder to adjust)",
            ),
        ],
        name="Fill Gradient Conversion",
        description="Strategy for converting fill gradients to Grease Pencil",
        default="GRADIENT",
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
        options = BuildOptions(
            scale=self.scale,
            use_vertex_colors=self.use_vertex_colors,
            stroke_grad_strat=self.stroke_grad_strat,
            fill_grad_strat=self.fill_grad_strat,
        )
        gp = _paint_to_gp(node, obj_name, options)
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

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return context.mode == "OBJECT"

    def draw(self, context: bpy.types.Context):
        layout = self.layout
        assert layout

        layout.operator("flashy.import_svg")
