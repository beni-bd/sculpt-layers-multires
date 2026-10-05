# bl_info kept for legacy Install from File compatibility; primary metadata is in blender_manifest.toml
bl_info = {
    "name": "Sculpt Layers for Multires",
    "author": "beni",
    "version": (1, 13, 27),
    "blender": (4, 2, 0),
    "location": "View3D > Sidebar > Sculpt Layers",
    "description": "Sculpt Layers for Multires — per-mesh multi-layer Multires sculpting",
    "category": "Sculpt",
}

import bpy
from bpy.props import PointerProperty

from .properties import SculptLayerItem, SculptLayersSettings
from .operators import (
    SCULPTLAYERS_OT_setup_base,
    SCULPTLAYERS_OT_subdivide_base,
    SCULPTLAYERS_OT_unsubdivide_base,
    SCULPTLAYERS_OT_add_layer,
    SCULPTLAYERS_OT_remove_layer,
    SCULPTLAYERS_OT_move_layer_up,
    SCULPTLAYERS_OT_move_layer_down,
    SCULPTLAYERS_OT_switch_to_layer,
    SCULPTLAYERS_OT_apply_layer,
    SCULPTLAYERS_OT_apply_all,
    SCULPTLAYERS_OT_apply_selected,
    SCULPTLAYERS_OT_select_all_layers,
    SCULPTLAYERS_OT_deselect_all_layers,
    SCULPTLAYERS_OT_clear_list,
    SCULPTLAYERS_OT_update_list,
    SCULPTLAYERS_OT_toggle_hide,
    SCULPTLAYERS_OT_hide_all_layers,
    SCULPTLAYERS_OT_show_all_layers,
)
from .ui import SCULPTLAYERS_UL_layers, SCULPTLAYERS_PT_panel
from .core import (
    sculptlayers_depsgraph_handler,
    _register_msgbus,
    _unregister_msgbus,
)

classes = (
    SculptLayerItem,
    SculptLayersSettings,
    SCULPTLAYERS_OT_setup_base,
    SCULPTLAYERS_OT_subdivide_base,
    SCULPTLAYERS_OT_unsubdivide_base,
    SCULPTLAYERS_OT_add_layer,
    SCULPTLAYERS_OT_remove_layer,
    SCULPTLAYERS_OT_move_layer_up,
    SCULPTLAYERS_OT_move_layer_down,
    SCULPTLAYERS_OT_switch_to_layer,
    SCULPTLAYERS_OT_apply_layer,
    SCULPTLAYERS_OT_apply_all,
    SCULPTLAYERS_OT_apply_selected,
    SCULPTLAYERS_OT_select_all_layers,
    SCULPTLAYERS_OT_deselect_all_layers,
    SCULPTLAYERS_OT_clear_list,
    SCULPTLAYERS_OT_update_list,
    SCULPTLAYERS_OT_toggle_hide,
    SCULPTLAYERS_OT_hide_all_layers,
    SCULPTLAYERS_OT_show_all_layers,
    SCULPTLAYERS_UL_layers,
    SCULPTLAYERS_PT_panel,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Object.sculpt_layers = PointerProperty(type=SculptLayersSettings)
    if sculptlayers_depsgraph_handler not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(sculptlayers_depsgraph_handler)
    _register_msgbus()


def unregister():
    _unregister_msgbus()
    if sculptlayers_depsgraph_handler in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(sculptlayers_depsgraph_handler)
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
    if hasattr(bpy.types.Object, "sculpt_layers"):
        del bpy.types.Object.sculpt_layers


if __name__ == "__main__":
    register()
