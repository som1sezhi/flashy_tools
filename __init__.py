from . import set_ease


bl_info = {
    "name": "flashy-tools",
    "blender": (5, 0, 0),
    "category": "Animation",
}


def register():
    set_ease.register()


def unregister():
    set_ease.unregister()


if __name__ == "__main__":
    register()
