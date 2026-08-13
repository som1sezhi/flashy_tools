import bpy

from .blender import FLASHY_OP_import_svg, FLASHY_PT_import_svg

classes = [
    FLASHY_OP_import_svg,
    FLASHY_PT_import_svg,
]


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
