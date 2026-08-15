import re
from collections.abc import Iterable
from typing import cast

import bpy

from .utils import select_only

FP_PROP_NAME = "Frame"


def get_active_grease_pencil_tree_node(
    context: bpy.types.Context,
) -> bpy.types.GreasePencilLayer | bpy.types.GreasePencilLayerGroup | None:
    """Get the active Grease Pencil layer or layer group. Note that a non-None
    return value implies that the active object is a Grease Pencil object."""
    obj = context.active_object
    if obj and obj.type == "GREASEPENCIL":
        gp = cast(bpy.types.GreasePencil, obj.data)
        if gp.layer_groups.active:
            return gp.layer_groups.active
        elif gp.layers.active:
            return gp.layers.active
    return None


def context_mode_to_object_mode(mode: str):
    """Convert a mode given by `context.mode` into a mode accepted by
    `bpy.ops.object.mode_set().`"""
    if mode == "EDIT_GREASE_PENCIL":
        return "EDIT"
    # OBJECT, (PAINT|SCULPT|WEIGHT|VERTEX)_GREASE_PENCIL
    return mode


def asset_name_prefix(base_name: str) -> str:
    return "~fp_" + base_name


def get_modifier_if_exists(
    obj: bpy.types.Object, tree_node: bpy.types.GreasePencilTreeNode
):
    for mod in obj.modifiers:
        if mod.type == "GREASE_PENCIL_TIME":
            mod = cast(bpy.types.GreasePencilTimeModifier, mod)
            if (
                mod.mode == "FIX"
                and mod.tree_node_filter == tree_node.name
                and mod.use_layer_group_filter
                == isinstance(tree_node, bpy.types.GreasePencilLayerGroup)
                and not mod.invert_layer_filter
            ):
                return mod
    return None


def frame_picker_data_path(bone_name: str):
    return f'pose.bones["{bone_name}"]["{FP_PROP_NAME}"]'


escape_table = str.maketrans({"\\": r"\\", '"': r"\""})


def get_driver_if_exists(
    obj: bpy.types.Object, modifier_name: str
) -> bpy.types.FCurve | None:
    driver_data_path = f'modifiers["{modifier_name.translate(escape_table)}"].offset'
    if obj.animation_data:
        for fcurve in obj.animation_data.drivers:
            if fcurve.data_path == driver_data_path:
                return fcurve
    return None


def should_keep_driver(
    driver: bpy.types.FCurve, armature_obj: bpy.types.Object, prop_data_path: str
) -> bool:
    assert driver.driver is not None
    for var in driver.driver.variables:
        if var.targets is None:
            continue
        if any(
            target.id == armature_obj and target.data_path == prop_data_path
            for target in var.targets
        ):
            return True
    return False


def get_bone_from_driver(driver: bpy.types.FCurve):
    assert driver.driver is not None
    for var in driver.driver.variables:
        if var.targets is None:
            continue
        for target in var.targets:
            if (
                isinstance(target.id, bpy.types.Object)
                and target.id.type == "ARMATURE"
                and target.id.pose
            ):
                m = re.fullmatch(
                    rf'pose.bones\["(.*)"\]\["{FP_PROP_NAME}"\]', target.data_path
                )
                if m and m[1] in target.id.pose.bones:
                    bone = target.id.pose.bones[m[1]]
                    if FP_PROP_NAME in bone and type(bone[FP_PROP_NAME]) is int:
                        return target.id, bone
    return None, None


def save_object_selection(vl: bpy.types.ViewLayer):
    return (
        vl.objects.active,
        {obj.name: obj.select_get(view_layer=vl) for obj in vl.objects},
    )


def restore_object_selection(vl: bpy.types.ViewLayer, selection_state):
    original_active_obj, original_selection = selection_state
    for obj in vl.objects:
        obj.select_set(original_selection[obj.name], view_layer=vl)
    vl.objects.active = original_active_obj


def save_bone_selection(armature_obj: bpy.types.Object):
    assert isinstance(armature_obj.data, bpy.types.Armature)
    bones: Iterable[bpy.types.PoseBone] = (
        armature_obj.pose.bones if armature_obj.pose else []
    )
    return (
        cast(bpy.types.Armature, armature_obj.data).bones.active,
        {b.name: b.select for b in bones},
    )


def restore_bone_selection(armature_obj: bpy.types.Object, selection_state):
    assert isinstance(armature_obj.data, bpy.types.Armature)
    original_active_bone, original_bone_selection = selection_state
    if armature_obj.pose:
        for b in armature_obj.pose.bones:
            b.select = original_bone_selection[b.name]
    armature_obj.data.bones.active = original_active_bone


def hide_all_gp_layers_except(
    gp: bpy.types.GreasePencil, tree_node: bpy.types.GreasePencilTreeNode
) -> dict[str, bool]:
    dont_touch = set()

    def _mark_dont_touch(node: bpy.types.GreasePencilTreeNode):
        if isinstance(node, bpy.types.GreasePencilLayer):
            dont_touch.add(node.name)
        else:
            assert isinstance(node, bpy.types.GreasePencilLayerGroup)
            for child in node.children:
                _mark_dont_touch(child)

    _mark_dont_touch(tree_node)

    original_hide = {}
    for layer in gp.layers:
        if layer.name not in dont_touch:
            original_hide[layer.name] = layer.hide
            layer.hide = True

    return original_hide


def restore_gp_layers(gp: bpy.types.GreasePencil, original_hide: dict[str, bool]):
    for layer in gp.layers:
        if layer.name in original_hide:
            layer.hide = original_hide[layer.name]


def gather_frames(tree_node: bpy.types.GreasePencilTreeNode) -> set[int]:
    """Return the frame numbers of all the frames in the given layer or group."""
    frames = set()
    if isinstance(tree_node, bpy.types.GreasePencilLayer):
        for frame in tree_node.frames:
            frames.add(frame.frame_number)
    else:
        assert isinstance(tree_node, bpy.types.GreasePencilLayerGroup)
        for child in tree_node.children:
            frames.update(gather_frames(child))
    return frames


def generate_pose_assets(
    context: bpy.types.Context,
    gp_obj: bpy.types.Object,
    tree_node: bpy.types.GreasePencilTreeNode,
    armature: bpy.types.Object,
    bone: bpy.types.PoseBone,
    base_name: str,
):
    gp_data = cast(bpy.types.GreasePencil, gp_obj.data)
    armature_data = cast(bpy.types.Armature, armature.data)

    # add keyframe to frame property so that poselib picks it up
    # when creating a new pose asset
    should_insert_keyframe = True
    if armature.animation_data and armature.animation_data.action:
        prop_data_path = frame_picker_data_path(bone.name)
        should_insert_keyframe = not any(
            fcurve.data_path == prop_data_path and len(fcurve.keyframe_points) > 0
            for layer in armature.animation_data.action.layers
            for strip in layer.strips
            if isinstance(strip, bpy.types.ActionKeyframeStrip)
            for channelbag in strip.channelbags
            for fcurve in channelbag.fcurves
        )
    if should_insert_keyframe:
        bone.keyframe_insert(f'["{FP_PROP_NAME}"]', frame=1)

    # gather temp_context variables
    window: bpy.types.Window = bpy.context.window
    screen = window.screen
    area = next(a for a in screen.areas if a.type == "VIEW_3D")
    region = next(r for r in area.regions if r.type == "WINDOW")
    space = area.spaces.active
    assert isinstance(space, bpy.types.SpaceView3D) and space.region_3d
    original_view_persp = space.region_3d.view_perspective

    # set up pose asset preview camera
    cam = bpy.data.cameras.new("PreviewCamera")
    cam.type = "ORTHO"
    cam_obj = bpy.data.objects.new("PreviewCamera", cam)
    context.scene.collection.objects.link(cam_obj)

    # set our camera as active
    old_active_camera = context.scene.camera
    context.scene.camera = cam_obj

    # hide all gp layers except the ones we're focusing on
    original_hide_state = hide_all_gp_layers_except(gp_data, tree_node)
    # save selections and mode to restore later
    vl: bpy.types.ViewLayer = context.view_layer
    original_mode = context.mode
    if original_mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")
    obj_selection_state = save_object_selection(vl)
    select_only(armature, vl)
    bpy.ops.object.mode_set(mode="POSE")
    bone_selection_state = save_bone_selection(armature)
    # select only the chosen bone
    bpy.ops.pose.select_all(action="DESELECT")
    bone.select = True
    armature_data.bones.active = bone.bone

    # delete existing pose assets
    prefix = asset_name_prefix(base_name)
    actions_to_delete = [
        a for a in bpy.data.actions if a.name.rsplit("_", 1)[0] == prefix
    ]
    for a in actions_to_delete:
        bpy.data.actions.remove(a, do_unlink=True)
    # create pose assets for each frame
    # TODO: ensure modifier is enabled beforehand so the previews are correct
    frame_nums = gather_frames(tree_node)
    zfill_len = max(len(str(n)) for n in frame_nums)
    for frame_num in frame_nums:
        bone[FP_PROP_NAME] = frame_num
        armature.update_tag()
        context.view_layer.update()

        # position preview camera
        bpy.ops.object.mode_set(mode="OBJECT")
        select_only(gp_obj, vl)
        with context.temp_override(
            window=window, area=area, region=region, screen=screen
        ):  # type: ignore
            # view perspective cannot be camera or camera_to_view poll will fail
            space.region_3d.view_perspective = "ORTHO"
            bpy.ops.view3d.camera_to_view()
            bpy.ops.view3d.camera_to_view_selected()
        cam.ortho_scale *= 1.25
        select_only(armature, vl)
        bpy.ops.object.mode_set(mode="POSE")

        bpy.ops.poselib.create_pose_asset(
            pose_name=f"{prefix}_{str(frame_num).zfill(zfill_len)}",
            asset_library_reference="LOCAL",
            catalog_path="Frame Picker",
        )
    # TODO: for each pose action, clear all channels other than our prop

    # restore selections, mode, and layer hide state
    restore_bone_selection(armature, bone_selection_state)
    bpy.ops.object.mode_set(mode="OBJECT")
    restore_object_selection(vl, obj_selection_state)
    if original_mode != "OBJECT":
        bpy.ops.object.mode_set(mode=context_mode_to_object_mode(original_mode))  # type: ignore
    restore_gp_layers(gp_data, original_hide_state)

    # restore previous active camera and remove our preview camera
    context.scene.camera = old_active_camera
    bpy.data.objects.remove(cam_obj, do_unlink=True)
    space.region_3d.view_perspective = original_view_persp

    # remove added keyframe
    if should_insert_keyframe:
        bone.keyframe_delete(f'["{FP_PROP_NAME}"]', frame=1)

    # default to first frame of the layer/group
    bone[FP_PROP_NAME] = min(frame_nums)


def multiline_label(layout: bpy.types.UILayout, text: list[str], icon="STATUS_WARNING"):
    layout.label(text=text[0], icon=icon)  # type: ignore
    for line in text[1:]:
        layout.label(text=line, icon="BLANK1")


class FLASHY_OP_setup_frame_picker(bpy.types.Operator):
    """Create a custom property on the chosen bone hooked up to a
    Time Offset modifier on the chosen Grease Pencil layer, and create pose
    assets for each frame."""

    bl_idname = "flashy.setup_frame_picker"
    bl_label = "Set Up Frame Picker"
    bl_options = {"REGISTER", "UNDO", "INTERNAL"}  # noqa: RUF012

    armature: bpy.props.StringProperty(
        name="Armature",
        description="Armature containing the bone that will hold the frame picker control.",
    )
    bone: bpy.props.StringProperty(
        name="Bone", description="Bone that will hold the frame picker control."
    )
    should_create_assets: bpy.props.BoolProperty(
        name="Generate Pose Assets",
        description="Whether pose assets should be created for each Grease Pencil frame.",
        default=True,
    )
    base_name: bpy.props.StringProperty(
        name="Asset Base Name",
        description='Pose assets will be named like "~fp_[base name]_[frame #]".',
    )

    @classmethod
    def poll(cls, context: bpy.types.Context):
        return (
            "GREASE_PENCIL" in context.mode or context.mode == "OBJECT"
        ) and get_active_grease_pencil_tree_node(context) is not None

    def draw(self, context: bpy.types.Context):
        layout = self.layout
        assert layout

        armature: bpy.types.Object | None = None
        if self.armature and self.armature in bpy.data.objects:
            armature = bpy.data.objects[self.armature]
        armature_is_valid = armature is not None and armature.type == "ARMATURE"

        tree_node = get_active_grease_pencil_tree_node(context)
        gp_obj = context.active_object
        assert tree_node is not None and gp_obj is not None
        mod = get_modifier_if_exists(gp_obj, tree_node)

        if mod is not None:
            layout.label(
                text="Time Offset modifier already exists, will be reused", icon="INFO"
            )
            driver = get_driver_if_exists(gp_obj, mod.name)
            if driver is not None and armature_is_valid and self.bone:
                armature = cast(bpy.types.Object, armature)
                prop_data_path = frame_picker_data_path(self.bone)
                if should_keep_driver(driver, armature, prop_data_path):
                    layout.label(
                        text="Driver already exists on modifier, will be kept",
                        icon="INFO",
                    )
                else:
                    layout.label(
                        text="Driver already exists on modifier, will be replaced",
                        icon="INFO",
                    )
            layout.separator(type="LINE")

        layout.prop_search(self, "armature", bpy.data, "objects")
        if not self.armature:  # empty, not chosen yet
            pass
        elif armature is None:
            layout.label(text="Object does not exist", icon="ERROR")
        elif not armature_is_valid:
            layout.label(text="Object is not an armature", icon="ERROR")
        else:
            layout.prop_search(self, "bone", armature.pose, "bones")

            if self.bone and armature.pose and self.bone in armature.pose.bones:
                bone: bpy.types.PoseBone = armature.pose.bones[self.bone]
                if FP_PROP_NAME in bone:
                    if type(bone[FP_PROP_NAME]) is int:
                        multiline_label(
                            layout,
                            [
                                'Bone already has a property named "Frame".',
                                "It will be used as the frame picker control.",
                            ],
                            "INFO",
                        )
                    else:
                        multiline_label(
                            layout,
                            [
                                'Bone already has a property named "Frame",',
                                "but it is not of integer type.",
                                "It will be replaced with a property of integer type.",
                            ],
                            "INFO",
                        )

        layout.separator(type="LINE")
        layout.prop(self, "should_create_assets")

        if self.should_create_assets:
            layout.prop(self, "base_name")

            if self.base_name:
                prefix = asset_name_prefix(self.base_name)
                layout.label(text=f'Pose asset names will have prefix "{prefix}"')

                if bpy.data.actions and any(
                    a.name.rsplit("_", 1)[0] == prefix for a in bpy.data.actions
                ):
                    multiline_label(
                        layout,
                        [
                            "Poses already exist with this name prefix.",
                            "They will be deleted and regenerated.",
                        ],
                        "INFO",
                    )
            else:
                layout.label(
                    text="Base name must be specified", icon="STATUS_ERROR_FILLED"
                )

    def execute(self, context: bpy.types.Context):
        if not self.armature or not self.bone:
            self.report({"ERROR"}, "Please select both an armature and a bone")
            return {"CANCELLED"}
        if self.should_create_assets and not self.base_name:
            self.report({"ERROR"}, "Please specify an asset base name")
            return {"CANCELLED"}

        try:
            armature: bpy.types.Object = bpy.data.objects[self.armature]
            assert armature.pose
            bone: bpy.types.PoseBone = armature.pose.bones[self.bone]
        except (KeyError, AssertionError):
            self.report({"ERROR"}, "Armature/bone could not be found")
            return {"CANCELLED"}

        tree_node = get_active_grease_pencil_tree_node(context)
        assert tree_node and context.active_object
        gp_obj = context.active_object

        # add custom prop to chosen bone, if not already present.
        # save old value to restore at the end
        try:
            old_prop_val = int(bone[FP_PROP_NAME])
        except (KeyError, ValueError):
            old_prop_val = None
        bone[FP_PROP_NAME] = 1  # set prop as integer
        bone.property_overridable_library_set(f'["{FP_PROP_NAME}"]', True)

        # add time offset modifier, if not already present
        mod = get_modifier_if_exists(gp_obj, tree_node)
        if mod is None:
            # modifier name will be used to suggest a base_name value
            # if the user runs this operator on an existing setup
            if self.should_create_assets and self.base_name:
                mod_name_suffix = self.base_name
            else:
                mod_name_suffix = tree_node.name
            mod = cast(
                bpy.types.GreasePencilTimeModifier,
                context.active_object.modifiers.new(
                    "TimeOffset_" + mod_name_suffix, "GREASE_PENCIL_TIME"
                ),
            )
            mod.mode = "FIX"
            mod.use_layer_group_filter = isinstance(
                tree_node, bpy.types.GreasePencilLayerGroup
            )
            mod.tree_node_filter = tree_node.name

        # create/replace the driver, if needed
        prop_data_path = frame_picker_data_path(bone.name)
        fcurve = get_driver_if_exists(gp_obj, mod.name)
        if fcurve is not None:
            if should_keep_driver(fcurve, armature, prop_data_path):
                should_create_driver = False
            else:
                mod.driver_remove("offset")
                should_create_driver = True
        else:
            should_create_driver = True
        if should_create_driver:
            driver = cast(bpy.types.FCurve, mod.driver_add("offset")).driver
            assert driver
            driver.type = "SCRIPTED"
            var = driver.variables.new()
            var.name = "fr"
            var.type = "SINGLE_PROP"
            var.targets[0].id_type = "OBJECT"
            var.targets[0].id = armature
            var.targets[0].data_path = prop_data_path
            driver.expression = var.name
            # if this isn't added, the data path may report as broken in the driver,
            # leading to broken preview images
            armature.update_tag()
            context.view_layer.update()

        if self.should_create_assets:
            generate_pose_assets(
                context, gp_obj, tree_node, armature, bone, self.base_name
            )

        # restore previous value of Frame, if any
        if old_prop_val is not None:
            bone[FP_PROP_NAME] = old_prop_val

        # if this isn't added, the UI can still be stuck on the last pose asset frame
        armature.update_tag()
        context.view_layer.update()

        self.report({"INFO"}, f"Successfully set up frame picker for {tree_node.name}")
        return {"FINISHED"}

    def invoke(self, context: bpy.types.Context, event):
        return context.window_manager.invoke_props_dialog(self)


class FLASHY_PT_frame_picker(bpy.types.Panel):
    """Panel for generating frame picker-like poses"""

    bl_category = "Flashy"
    bl_label = "Frame Picker"
    bl_idname = "FLASHY_PT_frame_picker"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"

    @classmethod
    def poll(cls, context: bpy.types.Context):
        return "GREASE_PENCIL" in context.mode or context.mode == "OBJECT"

    def draw(self, context: bpy.types.Context):
        layout = self.layout
        assert layout

        tree_node = get_active_grease_pencil_tree_node(context)

        if tree_node is None:
            layout.label(text="No Grease Pencil object selected")
            return
        assert context.active_object

        gp_obj = context.active_object
        mod = get_modifier_if_exists(gp_obj, tree_node)
        driver = get_driver_if_exists(gp_obj, mod.name) if mod else None
        armature_obj, bone = get_bone_from_driver(driver) if driver else (None, None)

        is_layer = isinstance(tree_node, bpy.types.GreasePencilLayer)

        layout.label(text=gp_obj.name, icon="OUTLINER_OB_GREASEPENCIL")
        layout.label(
            text=tree_node.name,
            icon="OUTLINER_DATA_GP_LAYER" if is_layer else "GREASEPENCIL_LAYER_GROUP",
        )

        if bone is None:
            # a frame picker has not been set up yet.
            # show setup operators
            op = layout.operator(
                "flashy.setup_frame_picker",
                text=f'Set Up FP for "{tree_node.name}"',
            )
            op.base_name = tree_node.name
        else:
            assert armature_obj and isinstance(mod, bpy.types.GreasePencilTimeModifier)
            layout.separator(type="LINE")
            layout.label(text="Frame Picker Location:")
            layout.label(text=armature_obj.name, icon="OUTLINER_OB_ARMATURE")
            layout.label(text=bone.name, icon="BONE_DATA")
            layout.prop(bone, f'["{FP_PROP_NAME}"]', text=FP_PROP_NAME)
            if bone[FP_PROP_NAME] != mod.offset:
                layout.label(text=f"Actual Frame: {mod.offset}")

            layout.separator(type="LINE")

            enabled_txt = "Enabled" if mod.show_viewport else "Disabled"
            layout.prop(mod, "show_viewport", text=f"{enabled_txt} in Viewport")

            op = layout.operator(
                "flashy.setup_frame_picker",
                text=f'Redo Setup for "{tree_node.name}"',
            )
            op.armature = armature_obj.name
            op.bone = bone.name
            op.base_name = mod.name.split("_", 1)[-1]

            # TODO: operators to copy frames between the current timeline frame
            # and the currently selected frame in the picker


classes = [
    FLASHY_OP_setup_frame_picker,
    FLASHY_PT_frame_picker,
]


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
