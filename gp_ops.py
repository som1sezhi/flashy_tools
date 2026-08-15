import bpy


class FLASHY_OP_enclose_in_layer_group(bpy.types.Operator):
    """Move all GP layers and groups under a new layer group at the root of the hierarchy"""

    bl_idname = "flashy.enclose_in_layer_group"
    bl_label = "Enclose in Layer Group"
    bl_options = {"REGISTER", "UNDO"}  # noqa: RUF012

    def execute(self, context: bpy.types.Context):
        obj_count = 0
        if context.selected_objects:
            for obj in context.selected_objects:
                if obj.type == "GREASEPENCIL":
                    obj_count += 1
                    assert isinstance(obj.data, bpy.types.GreasePencil)
                    nodes = list(obj.data.root_nodes)
                    group = obj.data.layer_groups.new(obj.name)
                    for node in nodes:
                        if isinstance(node, bpy.types.GreasePencilLayer):
                            obj.data.layers.move_to_layer_group(node, group)
                        else:
                            assert isinstance(node, bpy.types.GreasePencilLayerGroup)
                            obj.data.layer_groups.move_to_layer_group(node, group)
        self.report({"INFO"}, f"{obj_count} objects affected")
        return {"FINISHED"}


class FLASHY_PT_gp_ops(bpy.types.Panel):
    """Miscellaneous Grease Pencil operations"""

    bl_category = "Flashy"
    bl_label = "Grease Pencil Operations"
    bl_idname = "FLASHY_PT_gp_ops"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"

    def draw(self, context: bpy.types.Context):
        layout = self.layout
        assert layout

        layout.operator("flashy.enclose_in_layer_group")


classes = [
    FLASHY_OP_enclose_in_layer_group,
    FLASHY_PT_gp_ops,
]


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
