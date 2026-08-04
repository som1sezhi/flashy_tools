# flashy-tools

A Blender addon mainly for my personal use, but hopefully it can be useful to others as well. It implements features inspired by working in Adobe Flash/Animate, hence the name.

## Features

### Set Ease

<img width="499" height="180" alt="image" src="https://github.com/user-attachments/assets/761923c3-2719-498e-b754-c36c922ae026" />

A Dope Sheet sidebar panel allowing you to quickly view and change the easing interpolation/mode of selected keyframes.

### Skew Controls

<img width="266" height="115" alt="image" src="https://github.com/user-attachments/assets/71ae1897-4842-4ba4-b633-aefc8e38fa59" />

<img width="548" height="249" alt="image" src="https://github.com/user-attachments/assets/f6cdef57-c822-4137-8501-8dca0e607ead" />

A set of operators (and a 3D Viewport side panel) that adds or removes skew controls from selected objects/bones. These custom properties allow you to skew the object/bone in the XY plane, which can be useful for 2D cutout animation. This is implemented via a combination of drivers, Geometry Nodes, and Geometry Attribute constraints.

Some notes/caveats:

- You must be in Object Mode or Pose Mode for these operators to appear.
- When adding skew controls, a new mesh object is created as a sibling of the target object/armature. This object is named "_shearcalc" (or some variation therof), and its purpose is to compute the transformations required to create the skew effect. You will want to include this object in any rigs you distribute.
    - Objects that are children of the same parent will share the same _shearcalc object.
    - Parentless objects will share the same _shearcalc object, but only if they are in the same collection. Different collections will use different _shearcalc objects.
    - If an object in multiple collections has skew controls added, the _shearcalc will be added to an arbitrary collection.
    - Removing skew controls from an object/bone will also remove the _shearcalc object if it is not needed anymore. However, deleting the object/bone itself will not automatically remove the _shearcalc; you'll need to do so manually.
- Duplicating objects/bones with the skew controls will also duplicate the constraints, leading to glitchy behavior where the original controls affect both objects. To resolve this, select the duplicate object/bone and then remove and re-add skew controls.
