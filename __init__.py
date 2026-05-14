import bpy


bl_info = {
    "name": "flashy-tools",
    "blender": (5, 0, 0),
    "category": "Animation",
}


def get_selected_keyframes(context: bpy.types.Context):
    for fcurve in context.editable_fcurves:
        for kf in fcurve.keyframe_points:
            if kf.select_control_point:
                yield kf


class SetEasePanel(bpy.types.Panel):
    """Dope Sheet panel allowing easy viewing/modification of ease settings
    for multiple keyframes at once."""

    bl_category = "Set Ease"
    bl_label = "Set Ease"
    bl_idname = "DOPESHEET_PT_set_ease"
    bl_space_type = "DOPESHEET_EDITOR"
    bl_region_type = "UI"
    
    @staticmethod
    def _get_icon(enum: str):
        if enum[0] == "<":
            return "NONE"
        elif enum == "AUTO":
            return "IPO_EASE_IN_OUT"
        return "IPO_" + enum
    
    @staticmethod
    def _get_text(enum: str):
        if enum[0] == "<":
            return enum
        return enum.replace("_", " ").title()

    def draw(self, context):
        layout = self.layout
        
        # figure out whether all selected keyframes share an
        # interpolation/easing type
        common_ipo = None
        common_easing = None
        for kf in get_selected_keyframes(context):
            if common_ipo is None:
                # this is the first keyframe, use it to
                # populate the vars to compare against
                common_ipo = kf.interpolation
                common_easing = kf.easing
            else:
                # compare against first keyframe
                if common_ipo != kf.interpolation:
                    common_ipo = "<multiple interpolations>"
                if common_easing != kf.easing:
                    common_easing = "<multiple ease types>"
                if common_ipo[0] == "<" and common_easing[0] == "<":
                    break
        
        # draw the panel
        if common_easing is not None:
            layout.operator_menu_enum(
                "action.interpolation_type", "type",
                text=self._get_text(common_ipo),
                icon=self._get_icon(common_ipo)
            )
            layout.operator_menu_enum(
                "action.easing_type", "type",
                text=self._get_text(common_easing),
                icon=self._get_icon(common_easing)
            )
        else:
            layout.label(text="No keyframes selected")


def register():
    bpy.utils.register_class(SetEasePanel)


def unregister():
    bpy.utils.unregister_class(SetEasePanel)


if __name__ == "__main__":
    register()
