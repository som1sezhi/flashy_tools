import abc
import ctypes
from typing import NamedTuple, TypeVar, overload

import numpy as np
import thorvg_python as tvg

from ..utils import invlerp, lerp, vec_length, vec_length_sq


class ThorVGException(Exception):
    def __init__(self, result: tvg.Result):
        self.result = result

    def __str__(self):
        return str(self.result.name)


T = TypeVar("T")
T2 = TypeVar("T2")
T3 = TypeVar("T3")
T4 = TypeVar("T4")
T5 = TypeVar("T5")
T6 = TypeVar("T6")


@overload
def _check(ret: tvg.Result) -> None: ...
@overload
def _check(ret: tuple[tvg.Result, T]) -> T: ...
@overload
def _check(ret: tuple[tvg.Result, T, T2]) -> tuple[T, T2]: ...
@overload
def _check(ret: tuple[tvg.Result, T, T2, T3]) -> tuple[T, T2, T3]: ...
@overload
def _check(ret: tuple[tvg.Result, T, T2, T3, T4]) -> tuple[T, T2, T3, T4]: ...
@overload
def _check(
    ret: tuple[tvg.Result, T, T2, T3, T4, T5, T6],
) -> tuple[T, T2, T3, T4, T5, T6]: ...
def _check(ret):
    if isinstance(ret, tuple):
        if ret[0] != tvg.Result.SUCCESS:
            raise ThorVGException(ret[0])
        if len(ret) == 2:
            return ret[1]
        else:
            return ret[1:]
    if ret != tvg.Result.SUCCESS:
        raise ThorVGException(ret)


def _ptr_to_paint_obj(engine: tvg.Engine, ptr: tvg.paint.PaintPointer) -> tvg.Paint:
    """Convert a PaintPointer to a Paint object with the correct subtype."""
    paint = tvg.Paint(engine, ptr)
    paint_type = _check(paint.get_type())
    paint_types_to_classes = {
        tvg.TvgType.SHAPE: tvg.Shape,
        tvg.TvgType.SCENE: tvg.Scene,
        tvg.TvgType.PICTURE: tvg.Picture,
        tvg.TvgType.TEXT: tvg.Text,
    }
    if paint_type not in paint_types_to_classes:
        raise ValueError(f"Unknown paint type {paint_type}")
    cls = paint_types_to_classes[paint_type]
    return cls(engine, ptr)


def _get_parent_ptr(paint: tvg.Paint) -> tvg.paint.PaintPointer:
    """Return a pointer to the parent of the given Paint.
    As of 1.1.3, tvg.Paint.get_parent() is broken; this is a fixed version."""
    paint.thorvg_lib.tvg_paint_get_parent.argtypes = [tvg.paint.PaintPointer]
    paint.thorvg_lib.tvg_paint_get_parent.restype = tvg.paint.PaintPointer
    parent: tvg.paint.PaintPointer = paint.thorvg_lib.tvg_paint_get_parent(paint._paint)
    return parent


def _get_mask(paint: tvg.Paint) -> tuple[tvg.Result, tvg.Paint | None, tvg.MaskMethod]:
    """Get the masking target object and the masking method."""
    target = tvg.paint.PaintPointer(0)
    method = ctypes.c_uint8()
    paint.thorvg_lib.tvg_paint_get_mask_method.argtypes = [
        tvg.paint.PaintPointer,
        ctypes.POINTER(tvg.paint.PaintPointer),
        ctypes.POINTER(ctypes.c_uint8),
    ]
    paint.thorvg_lib.tvg_paint_get_mask_method.restype = tvg.Result
    result = paint.thorvg_lib.tvg_paint_get_mask_method(
        paint._paint,
        ctypes.pointer(target),
        ctypes.pointer(method),
    )
    target_paint = _ptr_to_paint_obj(paint.engine, target) if target else None
    return (
        result,
        target_paint,
        tvg.MaskMethod(method.value),
    )


def _get_clip(paint: tvg.Paint) -> tvg.Paint | None:
    paint.thorvg_lib.tvg_paint_get_clip.argtypes = [
        tvg.paint.PaintPointer,
    ]
    paint.thorvg_lib.tvg_paint_get_clip.restype = tvg.paint.PaintPointer
    clipper = paint.thorvg_lib.tvg_paint_get_clip(paint._paint)
    if clipper:
        return _ptr_to_paint_obj(paint.engine, clipper)
    else:
        return None


def get_aabb(paint: tvg.Paint) -> tuple[tvg.Result, float, float, float, float]:
    """Get the bounding box of the given Paint.
    As of 1.1.3, tvg.Paint.get_aabb() does not give an actual Result; this is a fixed version.
    """
    x = ctypes.c_float()
    y = ctypes.c_float()
    w = ctypes.c_float()
    h = ctypes.c_float()
    paint.thorvg_lib.tvg_paint_get_aabb.argtypes = [
        tvg.paint.PaintPointer,
        ctypes.POINTER(ctypes.c_float),
        ctypes.POINTER(ctypes.c_float),
        ctypes.POINTER(ctypes.c_float),
        ctypes.POINTER(ctypes.c_float),
    ]
    paint.thorvg_lib.tvg_paint_get_aabb.restype = tvg.Result
    result = paint.thorvg_lib.tvg_paint_get_aabb(
        paint._paint,
        ctypes.pointer(x),
        ctypes.pointer(y),
        ctypes.pointer(w),
        ctypes.pointer(h),
    )
    return result, x.value, y.value, w.value, h.value


def _get_stroke_gradient(self: tvg.Shape) -> tuple[tvg.Result, tvg.Gradient | None]:
    """Get the stroke gradient of the given shape.
    As of 1.1.3, tvg.Shape.get_stroke_gradient() does not handle the case of no gradient
    correctly, so this is a fixed version.
    """
    grad = tvg.gradient.GradientPointer()
    self.thorvg_lib.tvg_shape_get_stroke_gradient.argtypes = [
        tvg.paint.PaintPointer,
        ctypes.POINTER(tvg.gradient.GradientPointer),
    ]
    self.thorvg_lib.tvg_shape_get_stroke_gradient.restype = tvg.Result
    result = self.thorvg_lib.tvg_shape_get_stroke_gradient(
        self._paint,
        ctypes.pointer(grad),
    )
    if not grad:
        return result, None
    result, grad_type = tvg.Gradient(self.engine, grad).get_type()
    if grad_type == tvg.TvgType.LINEAR_GRAD:
        return result, tvg.LinearGradient(self.engine, grad)
    elif grad_type == tvg.TvgType.RADIAL_GRAD:
        return result, tvg.RadialGradient(self.engine, grad)
    elif result != tvg.Result.SUCCESS:
        return result, None
    else:
        raise RuntimeError(f"Invalid gradient type {grad_type}")


def _get_gradient(
    self: tvg.Shape,
) -> tuple[tvg.Result, tvg.LinearGradient | tvg.RadialGradient | None]:
    """Get the fill gradient of the given shape.
    As of 1.1.3, tvg.Shape.get_gradient() does not handle the case of no gradient correctly,
    so this is a fixed version.
    """
    grad = tvg.gradient.GradientPointer()
    self.thorvg_lib.tvg_shape_get_gradient.argtypes = [
        tvg.paint.PaintPointer,
        ctypes.POINTER(tvg.gradient.GradientPointer),
    ]
    self.thorvg_lib.tvg_shape_get_gradient.restype = tvg.Result
    result = self.thorvg_lib.tvg_shape_get_gradient(
        self._paint,
        ctypes.pointer(grad),
    )
    if not grad:
        return result, None
    result, tvg_type = tvg.Gradient(self.engine, grad).get_type()
    if tvg_type == tvg.TvgType.LINEAR_GRAD:
        return result, tvg.LinearGradient(self.engine, grad)
    elif tvg_type == tvg.TvgType.RADIAL_GRAD:
        return result, tvg.RadialGradient(self.engine, grad)
    else:
        raise RuntimeError(f"Invalid gradient TvgType: {tvg_type}")


def _addr(ptr: tvg.paint.PaintPointer) -> int:
    assert ptr.value is not None
    return ptr.value


def _matrix_to_numpy(m: tvg.Matrix) -> np.ndarray:
    return np.array(
        [
            [m.e11, m.e12, m.e13],
            [m.e21, m.e22, m.e23],
            [m.e31, m.e32, m.e33],
        ]
    )


Float4 = tuple[float, float, float, float]
ColorStop = tuple[float, Float4]


class LinearGradAttrs(NamedTuple):
    x1: float
    y1: float
    x2: float
    y2: float


class RadialGradAttrs(NamedTuple):
    cx: float
    cy: float
    r: float
    fx: float
    fy: float
    fr: float


class Gradient(abc.ABC):
    def __init__(self, grad: tvg.Gradient):
        color_stops = _check(grad.get_color_stops())
        self.stops: tuple[ColorStop, ...] = tuple(
            (s.offset, (s.r / 255, s.g / 255, s.b / 255, s.a / 255))
            for s in color_stops
        )
        self.spread = _check(grad.get_spread())
        self.transform = _matrix_to_numpy(_check(grad.get_transform()))
        self.type = _check(grad.get_type())
        if isinstance(grad, tvg.LinearGradient):
            self.attrs = LinearGradAttrs(*_check(grad.get()))
        else:
            assert isinstance(grad, tvg.RadialGradient)
            self.attrs = RadialGradAttrs(*_check(grad.get()))

    def key(self):
        return (
            self.type,
            self.stops,
            self.spread,
            # TODO: add focus point info for radial gradient
        )

    def eval_stops(self, t: float) -> Float4:
        assert len(self.stops) >= 2
        i = 1
        while i < len(self.stops) - 1 and self.stops[i][0] < t:
            i += 1
        fac = invlerp(self.stops[i - 1][0], self.stops[i][0], t)
        fac = max(0, min(1, fac))
        a = self.stops[i - 1][1]
        b = self.stops[i][1]
        return tuple(lerp(a[j], b[j], fac) for j in range(4))  # type: ignore

    def eval_pos(self, pos: np.typing.ArrayLike) -> Float4:
        if isinstance(self.attrs, LinearGradAttrs):
            p1 = np.array([self.attrs.x1, self.attrs.y1])
            p2 = np.array([self.attrs.x2, self.attrs.y2])
            b = p2 - p1
            a = pos - p1
            b_len_sq = vec_length_sq(b)
            t = np.dot(a, b) / b_len_sq if b_len_sq > 0 else 1
        else:  # radial gradient
            # NOTE: fx/fy/fr is not supported yet; we assume they have their
            # default values (fx=cx, fy=cy, fr=0)
            if self.attrs.r > 0:
                c = np.array([self.attrs.cx, self.attrs.cy])
                t = vec_length(pos - c) / self.attrs.r
            else:
                t = 1
        if self.spread == tvg.StrokeFill.PAD:
            t = max(0, min(1, t))
        elif self.spread == tvg.StrokeFill.REPEAT:
            t = t % 1
        else:  # REFLECT
            t = abs((t + 1) % 2 - 1)
        return self.eval_stops(t)

    def avg_color(self) -> Float4:
        splits = [
            (self.stops[i][0] + self.stops[i + 1][0]) / 2
            for i in range(len(self.stops) - 1)
        ]
        splits.insert(0, 0)
        splits.append(1)
        accum = [0.0, 0.0, 0.0, 0.0]
        for i in range(len(splits) - 1):
            weight = splits[i + 1] - splits[i]
            for j in range(4):
                accum[j] += weight * self.stops[i][1][j]
        return tuple(accum)  # type: ignore

    def __str__(self):
        return f"<{type(self).__name__}>"


StrokeColor = Float4 | Gradient | None
FillColor = Float4 | Gradient


class PaintNode:
    def __init__(self, paint: tvg.Paint, name: str = ""):
        self.addr = _addr(paint._paint)

        self.name = name
        self.transform = _matrix_to_numpy(_check(paint.get_transform()))
        self.visible = bool(paint.get_visible())
        self.opacity = _check(paint.get_opacity()) / 255

        # to help with removing extraneous clip paths later
        self.aabb = _check(get_aabb(paint))

        parent = _get_parent_ptr(paint)
        self.parent_addr = _addr(parent) if parent else None

        # to be set later
        self.mask: PaintNode | None = None
        self.mask_method: tvg.MaskMethod = tvg.MaskMethod.NONE
        self.clip: PaintNode | None = None
        self.world_transform = self.transform

    def __str__(self):
        return f'<{type(self).__name__} {self.addr} "{self.name}">'

    __repr__ = __str__

    def __hash__(self):
        return hash(self.addr)

    def __eq__(self, value: object) -> bool:
        return isinstance(value, PaintNode) and value.addr == self.addr


class ShapeNode(PaintNode):
    def __init__(self, paint: tvg.Shape, name: str = ""):
        super().__init__(paint, name)

        path_cmds, path_pts = _check(paint.get_path())
        self.path_cmds = list(path_cmds)
        self.path_pts = np.array([[pt.x, pt.y] for pt in path_pts])  # (N, 2)

        grad = _check(_get_stroke_gradient(paint))
        if grad:
            self.stroke_color: StrokeColor = Gradient(grad)
        else:
            try:
                col = _check(paint.get_stroke_color())
                self.stroke_color: StrokeColor = (
                    col[0] / 255,
                    col[1] / 255,
                    col[2] / 255,
                    col[3] / 255,
                )
            except ThorVGException as e:
                if e.result == tvg.Result.INSUFFICIENT_CONDITION:
                    self.stroke_color = None
                else:
                    raise
        self.stroke_width = _check(paint.get_stroke_width())
        self.stroke_cap = _check(paint.get_stroke_cap())
        self.stroke_join = _check(paint.get_stroke_join())
        self.stroke_miterlimit = _check(paint.get_stroke_miterlimit())
        grad = _check(_get_gradient(paint))
        if grad:
            self.fill_color: FillColor = Gradient(grad)
        else:
            col = _check(paint.get_fill_color())
            self.fill_color: FillColor = (
                col[0] / 255,
                col[1] / 255,
                col[2] / 255,
                col[3] / 255,
            )

    def transform_pts(self, transform: np.ndarray):
        x = self.path_pts
        x = np.pad(x, [(0, 0), (0, 1)], constant_values=1)[..., np.newaxis]
        self.path_pts = (transform @ x)[..., :2, 0]

        det = transform[0][0] * transform[1][1] - transform[0][1] * transform[1][0]
        self.stroke_width *= np.sqrt(abs(det))


class GroupNode(PaintNode):
    def __init__(self, paint: tvg.Paint, name: str = ""):
        super().__init__(paint, name)
        self.children: list[PaintNode] = []

    def set_children(self, children: list[PaintNode]):
        self.children = children


class TextNode(PaintNode):
    def __init__(self, paint: tvg.Paint, name: str = ""):
        super().__init__(paint, name)
        # currently unsupported, so no need to record data here


def _debug_print_matrix(prefix: str, mat: np.ndarray):
    print(
        prefix,
        mat[0][0],
        mat[0][1],
        mat[0][2],
        mat[1][0],
        mat[1][1],
        mat[1][2],
    )


def debug_print(node: PaintNode, depth=0):
    indent = "    " * depth
    print(indent + str(node))
    print(indent + "  Parent:", node.parent_addr)
    _debug_print_matrix(indent + "  Transform:", node.transform)
    if isinstance(node, ShapeNode):
        # print(
        #     indent
        #     + f"  cmds {[cmd.value for cmd in node.path_cmds]}, pts {[(round(p[0]), round(p[1])) for p in node.path_pts]}"
        # )
        print(indent + f"  {len(node.path_cmds)} cmds, {len(node.path_pts)} pts")
        print(indent + f"  Stroke: {node.stroke_color}")
        if isinstance(node.stroke_color, Gradient):
            _debug_print_matrix(indent + "  ", node.stroke_color.transform)
            print(indent + "  " + str(node.stroke_color.attrs))
        print(indent + f"  Fill: {node.fill_color}")
        if isinstance(node.fill_color, Gradient):
            _debug_print_matrix(indent + "  ", node.fill_color.transform)
            print(indent + "  " + str(node.fill_color.attrs))
    print(indent + "  Mask: " + node.mask_method.name)
    if node.mask:
        debug_print(node.mask, depth + 1)
    if node.clip:
        print(indent + "  Clip")
        debug_print(node.clip, depth + 1)
    if isinstance(node, GroupNode):
        print(indent + "  Children:")
        for c in node.children:
            debug_print(c, depth + 1)


def _is_rect(pts: np.ndarray):
    return len(pts) == 4 and (
        (
            pts[0][0] == pts[1][0]
            and pts[1][1] == pts[2][1]
            and pts[2][0] == pts[3][0]
            and pts[3][1] == pts[0][1]
        )
        or (
            pts[0][1] == pts[1][1]
            and pts[1][0] == pts[2][0]
            and pts[2][1] == pts[3][1]
            and pts[3][0] == pts[0][0]
        )
    )


def _is_extraneous_rect_clip(node: PaintNode, clip: ShapeNode) -> bool:
    if clip.path_cmds != [
        tvg.PathCommand.MOVE_TO,
        tvg.PathCommand.LINE_TO,
        tvg.PathCommand.LINE_TO,
        tvg.PathCommand.LINE_TO,
        tvg.PathCommand.CLOSE,
    ] or not _is_rect(clip.path_pts):
        return False

    return np.allclose(node.aabb, clip.aabb)


def _extract_data(pic: tvg.Picture) -> PaintNode:
    engine = pic.engine
    accessor = tvg.Accessor(engine, None)

    # set_accessible(True) gives us SVG id name data, but doesn't traverse
    # nodes that don't have ids.
    # set_accessible(False) visits all nodes (besides masks, etc.) but doesn't
    # give id names.
    # so we proceed in 2 passes to gather both sets of information

    # ======== pass 1: gather the SVG ids of paints ========

    id_to_names: dict[int, str] = {}

    def _visit_gather_names(ptr: tvg.paint.PaintPointer, data: bytes) -> bool:
        # when iterating with set_accessible(True), we only get paints
        # with SVG ids associated with them, so we shouldn't need to check if
        # get_id() gives 0
        paint = _ptr_to_paint_obj(engine, ptr)
        paint_id = paint.get_id()
        if paint_id:
            id_to_names[paint_id] = accessor.accessor_get_name(paint_id)
        return True

    pic.set_accessible(True)
    _check(accessor.set(pic, _visit_gather_names, b""))

    # ======== pass 2: gather paint nodes and relationships ========

    visit_later: list[tvg.Paint] = [pic]
    cur_paints: dict[int, tuple[tvg.Paint, str]] = {}
    cur_paints_in_hierarchy: set[int] = set()
    cur_children: dict[int, list[int]] = {}
    masks: dict[int, tuple[tvg.Paint, tvg.MaskMethod]] = {}
    clips: dict[int, tvg.Paint] = {}

    def _visit(ptr: tvg.paint.PaintPointer, data: bytes) -> bool:
        paint = _ptr_to_paint_obj(engine, ptr)
        addr = _addr(paint._paint)
        name = id_to_names.get(paint.get_id(), "")
        cur_paints[addr] = (paint, name)
        cur_paints_in_hierarchy.add(addr)

        parent_ptr = _get_parent_ptr(paint)
        if parent_ptr:
            parent_addr = _addr(parent_ptr)
            if parent_addr in cur_children:
                cur_children[parent_addr].append(addr)
            else:
                cur_children[parent_addr] = [addr]

        mask, mask_method = _check(_get_mask(paint))
        if mask:
            masks[addr] = (mask, mask_method)
            # mask shapes seem to reside in a separate hierarchy,
            # we'll iterate over them separately later
            if isinstance(mask, tvg.Scene):
                visit_later.append(mask)
            else:
                mask_name = id_to_names.get(mask.get_id(), "")
                cur_paints[_addr(mask._paint)] = (mask, mask_name)

        clip = _get_clip(paint)
        if clip:
            clips[addr] = clip
            # i think clips are always shape paints actually,
            # but just in case
            if isinstance(clip, tvg.Scene):
                visit_later.append(clip)
            else:
                clip_name = id_to_names.get(clip.get_id(), "")
                cur_paints[_addr(clip._paint)] = (clip, clip_name)

        return True

    paint_nodes: dict[int, PaintNode] = {}

    pic.set_accessible(False)
    while visit_later:
        p = visit_later.pop()
        _check(accessor.set(p, _visit, b""))

        # record data into our own structures
        for addr, (paint, name) in cur_paints.items():
            if isinstance(paint, tvg.Shape):
                node = ShapeNode(paint, name)
            elif isinstance(paint, tvg.Text):
                node = TextNode(paint, name)
            else:
                node = GroupNode(paint, name)
            paint_nodes[addr] = node

        for addr, child_addrs in cur_children.items():
            if addr in cur_paints_in_hierarchy:
                parent = paint_nodes[addr]
                assert isinstance(parent, GroupNode)
                parent.set_children(
                    [paint_nodes[child_addr] for child_addr in child_addrs]
                )

        cur_paints.clear()
        cur_paints_in_hierarchy.clear()
        cur_children.clear()

    # transform path points into world space
    world_transforms: dict[PaintNode, np.ndarray] = {}

    def _get_world_transform(node: PaintNode) -> np.ndarray:
        if node in world_transforms:
            return world_transforms[node]

        transform = node.transform
        if node.parent_addr:
            parent = paint_nodes[node.parent_addr]
            transform = _get_world_transform(parent) @ transform
        world_transforms[node] = transform
        return transform

    for node in paint_nodes.values():
        world_transform = _get_world_transform(node)
        node.world_transform = world_transform
        if isinstance(node, ShapeNode):
            node.transform_pts(world_transform)

    # assign masks and clips
    pic_node = paint_nodes[_addr(pic._paint)]
    for addr, (mask, mask_method) in masks.items():
        node = paint_nodes[addr]
        mask_node = paint_nodes[_addr(mask._paint)]
        node.mask = mask_node
        node.mask_method = mask_method
    for addr, clip in clips.items():
        node = paint_nodes[addr]
        clip_node = paint_nodes[_addr(clip._paint)]
        # thorvg will add rectangular clips around the svg viewbox.
        # we don't want these, so detect if a clip is a rectangle matching
        # the svg's bounding box and do not add the clip if so
        if not (
            isinstance(clip_node, ShapeNode)
            and _is_extraneous_rect_clip(pic_node, clip_node)
        ):
            node.clip = clip_node

    _check(accessor._del())

    return pic_node


def open_svg(path: str) -> PaintNode:
    with tvg.Engine(threads=0) as engine:
        pic = tvg.Picture(engine)
        pic.set_accessible(True)
        _check(pic.load(path))
        node = _extract_data(pic)
        pic._rel()
        return node
