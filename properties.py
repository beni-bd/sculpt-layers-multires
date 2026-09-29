"""PropertyGroup definitions for per-object sculpt layer data."""

import bpy
from bpy.props import (
    StringProperty,
    IntProperty,
    FloatProperty,
    BoolProperty,
    CollectionProperty,
    PointerProperty,
)
from bpy.types import PropertyGroup


def _on_active_index_update(self, context):
    """List row click — defer to core."""
    from . import core
    core._on_active_index_update(self, context)


class SculptLayerItem(PropertyGroup):
    name: StringProperty(name="Name", default="Layer")
    object: PointerProperty(name="Object", type=bpy.types.Object)
    is_base: BoolProperty(name="Is Base", default=False)
    is_shell_layer: BoolProperty(name="Is Shell Layer", default=False)
    is_layer00: BoolProperty(
        name="Is Shell Layer",
        description="Hidden clean Multires template; new layers are duplicated from this",
        default=False,
    )
    is_selected: BoolProperty(
        name="Selected",
        description="Include this layer when using Apply Selected Layers",
        default=False,
    )
    strength: FloatProperty(
        name="Strength",
        description="Shape-key influence on Layer_00 (-1 to 1). "
                    "Change this value, then click Apply Layer / Apply Selected "
                    "to replace the shape key and update the Base.",
        default=1.0,
        min=-1.0,
        max=1.0,
        soft_min=-1.0,
        soft_max=1.0,
    )


class SculptLayersSettings(PropertyGroup):
    layers: CollectionProperty(type=SculptLayerItem)
    active_index: IntProperty(
        name="Active Layer",
        default=0,
        update=_on_active_index_update,
    )
    auto_sculpt_mode: BoolProperty(
        name="Enter Sculpt mode automatically",
        description="Automatically enter Sculpt mode upon every new layer creation",
        default=False,
    )
    offset_distance: FloatProperty(
        name="Offset Distance",
        default=4.0,
        min=0.0,
        soft_max=100.0,
        description="X offset from the previous layer (default: Base X size × 1.5)",
    )
    base_vertex_count: IntProperty(default=0)
    base_multires_level: IntProperty(
        default=0,
        description="Highest Multires level of Base when Layer_00 was created",
    )


