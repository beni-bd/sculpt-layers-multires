"""UIList and sidebar panel."""

import bpy
from bpy.types import UIList, Panel

from .constants import (
    COLLECTION_NAME,
    SHELL_LAYER_NAME,
    LAYER_00_NAME,
    MULTIRES_VIEWPORT_MAX,
    BASE_FLAG,
)
from . import core
from .core import *  # noqa: F401,F403

# private helpers
from .core import (
    _multires_highest_level,
)

class SCULPTLAYERS_UL_layers(UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            row = layout.row(align=True)

            # Multi-select checkbox (sculpt layers only)
            if not item.is_base and not item.is_shell_layer and not getattr(item, "is_layer00", False):
                row.prop(item, "is_selected", text="")
            else:
                row.label(text="", icon='BLANK1')

            # Clickable eye — toggles hide without changing list selection
            obj = item.object
            if obj:
                # Prefer hide_viewport (cheap); only call hide_get if needed
                is_hidden = bool(obj.hide_viewport)
                if not is_hidden:
                    try:
                        is_hidden = bool(obj.hide_get())
                    except Exception:
                        pass
                eye = row.row(align=True)
                eye.ui_units_x = 1.0
                op = eye.operator(
                    "sculptlayers.toggle_hide",
                    text="",
                    icon='HIDE_ON' if is_hidden else 'HIDE_OFF',
                    emboss=False,
                    depress=False,
                )
                op.index = index
            else:
                row.label(text="", icon='ERROR')

            if item.is_base:
                row.label(text="Base", icon='MESH_UVSPHERE')
                row.label(text="", icon='PINNED')
            elif item.is_shell_layer:
                row.label(text=SHELL_LAYER_NAME, icon='SHAPEKEY_DATA')
                row.label(text="", icon='PINNED')
            elif getattr(item, "is_layer00", False):
                row.label(text=LAYER_00_NAME, icon='MOD_MULTIRES')
                row.label(text="", icon='PINNED')
            else:
                row.prop(item, "name", text="", emboss=False, icon='MESH_DATA')
                sub = row.row(align=True)
                sub.scale_x = 0.6
                sub.prop(item, "strength", text="")

        elif self.layout_type == 'GRID':
            layout.alignment = 'CENTER'
            layout.label(text="", icon='MESH_DATA')


class SCULPTLAYERS_PT_panel(Panel):
    bl_label = "Sculpt Layers for Multires"
    bl_idname = "SCULPTLAYERS_PT_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Sculpt Layers"

    def draw(self, context):
        layout = self.layout
        active = get_active_mesh(context)
        base_obj, settings = get_settings_for_context(context)

        # Direct fallback: if active mesh itself is a registered base, use it
        # (avoids any resolution edge-case after Add Layer)
        active_is_base = active is not None and is_registered_base(active)
        if active_is_base:
            base_obj = active
            settings = get_object_settings(active)

        # Has usable layer data? (reuse active_is_base when possible)
        has_base = (
            base_obj is not None
            and settings is not None
            and (active_is_base if base_obj is active else is_registered_base(base_obj))
        )

        # List highlight on viewport select is handled by msgbus + depsgraph
        # (avoid re-scanning layers on every panel redraw)

        box = layout.box()
        box.label(text="1. Setup", icon='MESH_UVSPHERE')

        # Set as Base: blocked for layers; greyed out once this mesh is already Base
        if active is not None and is_sculpt_layer_object(active):
            box.label(text=f"'{active.name}' is a sculpt layer", icon='ERROR')
            box.label(text="Select the original mesh to Set as Base")
            row = box.row()
            row.enabled = False
            row.operator("sculptlayers.setup_base", icon='PINNED', text="Set as Base (blocked)")
        elif active_is_base or has_base:
            row = box.row()
            row.enabled = False
            row.operator("sculptlayers.setup_base", icon='PINNED', text="Set as Base")
        else:
            box.operator("sculptlayers.setup_base", icon='PINNED', text="Set as Base")

        if has_base:
            has_any_layer = any(not i.is_base for i in settings.layers)
            box.label(text="Subdivide only from this Panel", icon='INFO')
            row = box.row(align=True)
            row.enabled = not has_any_layer
            row.operator("sculptlayers.subdivide_base", icon='MOD_SUBSURF', text="Subdivide")
            row.operator("sculptlayers.unsubdivide_base", icon='LOOP_BACK', text="Unsubdivide")
            _lvl = settings.base_multires_level
            _vp_lvl = 0
            try:
                _bm = get_multires(base_obj)
                if _bm is not None:
                    _lvl = _multires_highest_level(_bm)
                    _vp_lvl = int(getattr(_bm, "levels", 0) or 0)
                else:
                    # Multires modifier removed in the modifier panel → level is 0
                    _lvl = 0
            except Exception:
                pass
            box.label(text=f"Level {_lvl}  ·  Viewport {_vp_lvl} (max {MULTIRES_VIEWPORT_MAX})")
        else:
            if active is not None:
                box.label(text=f"Selected: {active.name}", icon='MESH_DATA')
            box.label(text="Select a mesh → Set as Base to start", icon='INFO')
            return

        box = layout.box()
        box.label(text="2. Layers – check boxes to multi-select", icon='OUTLINER_OB_MESH')
        row = box.row()
        row.template_list(
            "SCULPTLAYERS_UL_layers", "",
            settings, "layers",
            settings, "active_index",
            rows=6,
        )

        col = row.column(align=True)
        col.operator("sculptlayers.add_layer", icon='ADD', text="")
        col.operator("sculptlayers.remove_layer", icon='REMOVE', text="")
        col.separator()
        col.operator("sculptlayers.move_layer_up", icon='TRIA_UP', text="")
        col.operator("sculptlayers.move_layer_down", icon='TRIA_DOWN', text="")
        col.separator()
        col.operator("sculptlayers.update_list", icon='FILE_REFRESH', text="")
        col.operator("sculptlayers.clear_list", icon='X', text="")

        row = box.row(align=True)
        row.operator("sculptlayers.select_all_layers", text="Select All")
        row.operator("sculptlayers.deselect_all_layers", text="Deselect All")

        row = box.row(align=True)
        row.operator("sculptlayers.show_all_layers", icon='HIDE_OFF', text="Show All")
        row.operator("sculptlayers.hide_all_layers", icon='HIDE_ON', text="Hide All")

        box.prop(settings, "auto_sculpt_mode")

        if settings.layers and 0 <= settings.active_index < len(settings.layers):
            item = settings.layers[settings.active_index]
            if not item.is_base and not item.is_shell_layer:
                col = box.column(align=True)
                col.operator(
                    "sculptlayers.switch_to_layer",
                    icon='SCULPTMODE_HLT',
                    text="Switch & Sculpt",
                )

        # Apply buttons stacked: This → Selected → All
        layout.separator()
        col = layout.column(align=True)
        col.operator(
            "sculptlayers.apply_layer",
            icon='CHECKMARK',
            text="Apply This Layer → Base",
        )
        col.operator(
            "sculptlayers.apply_selected",
            icon='CHECKBOX_HLT',
            text="Apply Selected Layers → Base",
        )
        col.operator(
            "sculptlayers.apply_all",
            icon='IMPORT',
            text="Apply All Layers → Base",
        )

        layout.separator()
        box = layout.box()
        box.label(text="Options")
        box.prop(settings, "offset_distance")

