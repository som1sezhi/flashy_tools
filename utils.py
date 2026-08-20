import bpy


def lerp(a: float, b: float, t: float):
    return (1 - t) * a + t * b


def invlerp(a: float, b: float, v: float):
    return (v - a) / (b - a)


def select_only(obj: bpy.types.Object, vl: bpy.types.ViewLayer):
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True, view_layer=vl)
    vl.objects.active = obj
