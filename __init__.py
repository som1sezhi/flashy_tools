from . import frame_picker, set_ease, skew_controls

bl_info = {
    "name": "flashy-tools",
    "blender": (5, 2, 0),
    "category": "Animation",
}

modules = [set_ease, skew_controls, frame_picker]


def register():
    for module in modules:
        module.register()


def unregister():
    for module in reversed(modules):
        module.unregister()


if __name__ == "__main__":
    register()
