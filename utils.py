import math

import bpy
import numpy as np


def lerp(a: float, b: float, t: float):
    return (1 - t) * a + t * b


def invlerp(a: float, b: float, v: float):
    return (v - a) / (b - a)


def vec_length_sq(vec: np.ndarray) -> float:
    return np.dot(vec, vec)


def vec_length(vec: np.ndarray) -> float:
    return math.sqrt(np.dot(vec, vec))


def normalize_and_get_length(vec: np.ndarray) -> tuple[np.ndarray, float]:
    length_sq = np.dot(vec, vec)
    if length_sq > 1e-35:
        length = math.sqrt(length_sq)
        return vec / length, length
    return np.zeros_like(vec), 0.0


def normalize(vec: np.ndarray) -> np.ndarray:
    length_sq = np.dot(vec, vec)
    if length_sq > 1e-35:
        length = math.sqrt(length_sq)
        return vec / length
    return np.zeros_like(vec)


def select_only(obj: bpy.types.Object, vl: bpy.types.ViewLayer):
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True, view_layer=vl)
    vl.objects.active = obj
