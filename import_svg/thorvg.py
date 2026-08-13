import ctypes
from typing import TypeVar, overload

import numpy as np
import thorvg_python as tvg


class ThorVGException(Exception):
    def __init__(self, result: tvg.Result):
        self.result = result

    def __str__(self):
        return str(self.result.name)


T = TypeVar("T")
T2 = TypeVar("T2")
T3 = TypeVar("T3")
T4 = TypeVar("T4")


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


class PaintNode:
    def __init__(self, paint: tvg.Paint, name: str = ""):
        self.addr = _addr(paint._paint)

        self.name = name
        self.transform = _matrix_to_numpy(_check(paint.get_transform()))
        self.visible = bool(paint.get_visible())
        self.mask: PaintNode | None = None
        self.mask_method: tvg.MaskMethod = tvg.MaskMethod.NONE
        self.opacity = _check(paint.get_opacity()) / 255

    def set_mask(self, mask: "PaintNode", mask_method: tvg.MaskMethod):
        self.mask = mask
        self.mask_method = mask_method

    def __str__(self):
        return f'<{type(self).__name__} {self.addr:x} "{self.name}" {self.visible}>'


Float4 = tuple[float, float, float, float]
StrokeColor = Float4 | None
FillColor = Float4


class ShapeNode(PaintNode):
    def __init__(self, paint: tvg.Shape, name: str = ""):
        super().__init__(paint, name)

        path_cmds, path_pts = _check(paint.get_path())
        self.path_cmds = list(path_cmds)
        self.path_pts = [(pt.x, pt.y) for pt in path_pts]

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
        col = _check(paint.get_fill_color())
        self.fill_color: FillColor = (
            col[0] / 255,
            col[1] / 255,
            col[2] / 255,
            col[3] / 255,
        )


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


def debug_print(node: PaintNode, depth=0):
    indent = "    " * depth
    print(indent + str(node))
    print(
        indent + "  Transform:",
        node.transform[0][0],
        node.transform[0][1],
        node.transform[0][2],
        node.transform[1][0],
        node.transform[1][1],
        node.transform[1][2],
    )
    print(indent + "  Mask: " + node.mask_method.name)
    if node.mask:
        debug_print(node.mask, depth + 1)
    if isinstance(node, GroupNode):
        print(indent + "  Children:")
        for c in node.children:
            debug_print(c, depth + 1)


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
        id_to_names[paint_id] = accessor.accessor_get_name(paint_id)
        return True

    pic.set_accessible(True)
    _check(accessor.set(pic, _visit_gather_names, b""))

    # ======== pass 2: gather paint nodes and relationships ========

    visit_later: list[tvg.Paint] = [pic]
    cur_paints: dict[int, tuple[tvg.Paint, str]] = {}
    cur_children: dict[int, list[int]] = {}
    masks: dict[int, tuple[tvg.Paint, tvg.MaskMethod]] = {}

    def _visit(ptr: tvg.paint.PaintPointer, data: bytes) -> bool:
        paint = _ptr_to_paint_obj(engine, ptr)
        addr = _addr(paint._paint)
        name = id_to_names.get(paint.get_id(), "")
        cur_paints[addr] = (paint, name)

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
            if addr in cur_paints:
                parent = paint_nodes[addr]
                assert isinstance(parent, GroupNode)
                parent.set_children(
                    [paint_nodes[child_addr] for child_addr in child_addrs]
                )

        cur_paints.clear()
        cur_children.clear()

    for addr, (mask, mask_method) in masks.items():
        node = paint_nodes[addr]
        mask_node = paint_nodes[_addr(mask._paint)]
        node.set_mask(mask_node, mask_method)

    _check(accessor._del())

    return paint_nodes[_addr(pic._paint)]


def open_svg(path: str) -> PaintNode:
    with tvg.Engine(threads=0) as engine:
        pic = tvg.Picture(engine)
        pic.set_accessible(True)
        _check(pic.load(path))
        node = _extract_data(pic)
        pic._rel()
        return node
