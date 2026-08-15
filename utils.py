import bpy


def select_only(obj: bpy.types.Object, vl: bpy.types.ViewLayer):
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True, view_layer=vl)
    vl.objects.active = obj
