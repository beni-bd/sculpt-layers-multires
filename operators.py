"""Operators for setup, layers, apply, visibility, and list management."""

import bpy
from bpy.props import BoolProperty, IntProperty
from bpy.types import Operator

from .constants import (
    COLLECTION_NAME,
    SHELL_LAYER_NAME,
    LAYER_00_NAME,
    MULTIRES_VIEWPORT_MAX,
    BASE_FLAG,
)
from . import core
# Operator bodies were written against a single-module layout; import helpers
# into this namespace. Mutable state must still be accessed via core.* (e.g.
# core._is_updating) so assignments affect the shared module.
from .core import *  # noqa: F401,F403 — public names only

# import * skips leading-underscore names; bind private helpers explicitly
from .core import (
    _default_offset_distance,
    _delete_layer00_for_base,
    _delete_shell_layer_for_base,
    _ensure_panel_context_after_layer_remove,
    _layer_entry_missing,
    _multires_highest_level,
    _object_alive,
    _on_active_index_update,
    _reselect_layer_keep_panel,
    _restore_multires_levels,
    _restore_viewport_levels_snap,
    _set_multires_levels,
    _snapshot_multires_levels,
    _sync_shell_layer_from_list,
    _zero_and_swap_layer_locations,
)

class SCULPTLAYERS_OT_setup_base(Operator):
    """Set selected mesh as Base. Each mesh has its own independent layer data."""
    bl_idname = "sculptlayers.setup_base"
    bl_label = "Set as Base"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = get_active_mesh(context)
        if obj is None:
            return False
        # Already a Base → grey out
        if is_registered_base(obj):
            return False
        # Sculpt layer objects never become Base
        if is_sculpt_layer_object(obj):
            return False
        return True

    def execute(self, context):
        # uses core._is_updating
        obj = get_active_mesh(context)
        if not obj:
            self.report({'ERROR'}, "Select a mesh object first")
            return {'CANCELLED'}
        if is_registered_base(obj):
            self.report({'INFO'}, f"'{obj.name}' is already set as Base.")
            return {'CANCELLED'}

        # Never allow layer / Layer_00 objects to become a Base
        if object_in_sculpt_layers_collection(obj):
            self.report(
                {'ERROR'},
                f"'{obj.name}' is in the \"{COLLECTION_NAME}\" collection "
                "(a sculpt layer). Select the original mesh, not a layer."
            )
            return {'CANCELLED'}
        try:
            if obj.get("sculpt_layers_base"):
                self.report(
                    {'ERROR'},
                    f"'{obj.name}' is a sculpt layer of '{obj.get('sculpt_layers_base')}'. "
                    "Select the original Base mesh instead."
                )
                return {'CANCELLED'}
        except Exception:
            pass

        settings = get_object_settings(obj)
        if settings is None:
            self.report({'ERROR'}, "Object has no sculpt_layers property")
            return {'CANCELLED'}

        core._is_updating = True
        try:
            # Only clear THIS object's previous layer data (other bases are untouched)
            for item in list(settings.layers):
                if item.is_shell_layer and item.object and _object_alive(item.object):
                    try:
                        bpy.data.objects.remove(item.object, do_unlink=True)
                    except ReferenceError:
                        pass
                elif not item.is_base and item.object and _object_alive(item.object):
                    try:
                        bpy.data.objects.remove(item.object, do_unlink=True)
                    except ReferenceError:
                        pass
            _delete_shell_layer_for_base(obj)
            _delete_layer00_for_base(obj)
            settings.layers.clear()

            ensure_multires(obj)
            # Keep base in its own collection; layers go to Sculpt LAYERS

            base_item = settings.layers.add()
            base_item.name = "Base"
            base_item.object = obj
            base_item.is_base = True
            base_item.is_shell_layer = False
            base_item.is_selected = False
            base_item.strength = 1.0

            settings.base_vertex_count = len(obj.data.vertices)
            base_mod = get_multires(obj)
            target = _multires_highest_level(base_mod) if base_mod else 0
            settings.base_multires_level = target
            settings.offset_distance = _default_offset_distance(obj)
            mark_as_base(obj)
            # Layer_00 is created on first Add Layer (plain Multires duplicate)
        finally:
            core._is_updating = False

        settings.active_index = 0
        self.report(
            {'INFO'},
            f"Base = '{obj.name}' (Multires level {settings.base_multires_level}). "
            f"Add Layer creates Layer_00 + first sculpt layer."
        )
        return {'FINISHED'}



class SCULPTLAYERS_OT_subdivide_base(Operator):
    """Add one Multires subdivision level on the Base (viewport capped at 3).
    Only available before the first Add Layer."""
    bl_idname = "sculptlayers.subdivide_base"
    bl_label = "Subdivide Base"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        base_obj, settings = get_settings_for_context(context)
        if settings is None or base_obj is None:
            return False
        if not is_registered_base(base_obj):
            return False
        # Only before any Shell Layer / Layer_00 / sculpt layer exists
        return not any(not i.is_base for i in settings.layers)

    def execute(self, context):
        base_obj, settings = get_settings_for_context(context)
        if settings is None or base_obj is None or not is_registered_base(base_obj):
            self.report({'ERROR'}, "Set as Base first.")
            return {'CANCELLED'}

        if any(not i.is_base for i in settings.layers):
            self.report({'ERROR'}, "Subdivide is locked after the first layer is created.")
            return {'CANCELLED'}

        # Prefer listed Base object
        base_item = find_item_by_flag(settings, is_base=True)
        if base_item and base_item.object:
            base_obj = base_item.object

        if context.mode != 'OBJECT':
            try:
                bpy.ops.object.mode_set(mode='OBJECT')
            except Exception:
                pass

        bpy.ops.object.select_all(action='DESELECT')
        base_obj.hide_set(False)
        base_obj.hide_viewport = False
        base_obj.select_set(True)
        context.view_layer.objects.active = base_obj

        mod = ensure_multires(base_obj)
        mod_name = mod.name if mod else "Multires"
        try:
            bpy.ops.object.multires_subdivide(modifier=mod_name)
        except Exception as e:
            self.report({'ERROR'}, f"Subdivide failed: {e}")
            return {'CANCELLED'}

        mod = get_multires(base_obj)
        highest = _multires_highest_level(mod) if mod else 0
        settings.base_multires_level = highest
        if mod:
            _set_multires_levels(mod, highest, base_viewport=min(MULTIRES_VIEWPORT_MAX, highest))

        self.report(
            {'INFO'},
            f"Base Multires level {highest} (viewport {min(MULTIRES_VIEWPORT_MAX, highest)})"
        )
        return {'FINISHED'}


class SCULPTLAYERS_OT_unsubdivide_base(Operator):
    """Remove one Multires subdivision level on the Base (viewport capped at 3).
    Only available before the first Add Layer."""
    bl_idname = "sculptlayers.unsubdivide_base"
    bl_label = "Unsubdivide Base"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        base_obj, settings = get_settings_for_context(context)
        if settings is None or base_obj is None:
            return False
        if not is_registered_base(base_obj):
            return False
        return not any(not i.is_base for i in settings.layers)

    def execute(self, context):
        base_obj, settings = get_settings_for_context(context)
        if settings is None or base_obj is None or not is_registered_base(base_obj):
            self.report({'ERROR'}, "Set as Base first.")
            return {'CANCELLED'}

        if any(not i.is_base for i in settings.layers):
            self.report({'ERROR'}, "Unsubdivide is locked after the first layer is created.")
            return {'CANCELLED'}

        base_item = find_item_by_flag(settings, is_base=True)
        if base_item and base_item.object:
            base_obj = base_item.object

        mod = get_multires(base_obj)
        if mod is None or _multires_highest_level(mod) < 1:
            self.report({'INFO'}, "Base has no Multires levels to remove.")
            return {'CANCELLED'}

        if context.mode != 'OBJECT':
            try:
                bpy.ops.object.mode_set(mode='OBJECT')
            except Exception:
                pass

        bpy.ops.object.select_all(action='DESELECT')
        base_obj.hide_set(False)
        base_obj.hide_viewport = False
        base_obj.select_set(True)
        context.view_layer.objects.active = base_obj

        # Remove one top Multires level:
        # set display/sculpt to highest-1, then Delete Higher (drops the top level).
        # (multires_unsubdivide rebuilds the cage and is the wrong tool here.)
        mod_name = mod.name
        before = _multires_highest_level(mod)
        target = max(0, before - 1)
        mod.levels = target
        mod.sculpt_levels = target
        mod.render_levels = target
        try:
            bpy.ops.object.multires_higher_levels_delete(modifier=mod_name)
        except Exception as e:
            self.report({'ERROR'}, f"Unsubdivide (delete higher) failed: {e}")
            return {'CANCELLED'}

        mod = get_multires(base_obj)
        highest = _multires_highest_level(mod) if mod else 0
        # Safety: if delete higher did nothing, force level props down
        if highest > target and mod:
            try:
                mod.levels = target
                mod.sculpt_levels = target
                mod.render_levels = target
                bpy.ops.object.multires_higher_levels_delete(modifier=mod.name)
                highest = _multires_highest_level(mod) if mod else target
            except Exception:
                highest = target
        settings.base_multires_level = highest
        if mod and highest > 0:
            _set_multires_levels(mod, highest, base_viewport=min(MULTIRES_VIEWPORT_MAX, highest))
        elif mod:
            mod.levels = 0
            mod.sculpt_levels = 0
            mod.render_levels = 0

        self.report(
            {'INFO'},
            f"Base Multires {before} → {highest} "
            f"(viewport {min(MULTIRES_VIEWPORT_MAX, highest) if highest else 0})"
        )
        return {'FINISHED'}


class SCULPTLAYERS_OT_add_layer(Operator):
    """Duplicate Base as a new sculpt layer. Creates Layer_00 on the first add."""
    bl_idname = "sculptlayers.add_layer"
    bl_label = "Add Sculpt Layer"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        base_obj, settings = get_settings_for_context(context)
        if settings is None or base_obj is None or not is_registered_base(base_obj):
            self.report({'ERROR'}, "No Base set for this mesh. Select it and click 'Set as Base'.")
            return {'CANCELLED'}

        base_item = find_item_by_flag(settings, is_base=True)
        if base_item is None or base_item.object is None:
            self.report({'ERROR'}, "No Base set. Click 'Set as Base' first.")
            return {'CANCELLED'}

        base_obj = base_item.object
        col = get_or_create_collection()

        # Layer_00 + Layer Shell on first sculpt-layer add
        layer0_item = find_item_by_flag(settings, is_shell_layer=True)
        if layer0_item is None or not _object_alive(layer0_item.object):
            create_shell_layer_from_base(base_obj, settings, col)
        else:
            rebuild_shell_layer_if_needed(context)
            pin_system_layers_in_list(settings)

        # Ensure shell exists (rebuild if missing)
        shell_item = find_item_by_flag(settings, is_layer00=True)
        if shell_item is None or not _object_alive(shell_item.object):
            base_mod = get_multires(base_obj)
            target = _multires_highest_level(base_mod) if base_mod else settings.base_multires_level
            try:
                create_layer00_from_base(base_obj, settings, col, target)
            except Exception as e:
                self.report({'ERROR'}, f"Layer_00 missing and recreate failed: {e}")
                return {'CANCELLED'}
            shell_item = find_item_by_flag(settings, is_layer00=True)
            if shell_item is None or not _object_alive(shell_item.object):
                self.report({'ERROR'}, "Layer_00 could not be created.")
                return {'CANCELLED'}

        shell_obj = shell_item.object

        sculpt_count = len([
            i for i in settings.layers
            if not i.is_base and not i.is_shell_layer and not getattr(i, "is_layer00", False)
        ])
        layer_number = sculpt_count + 1

        if context.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')

        # Viewport 0 on Shell → cheap duplicate → restore Shell → set layer levels
        shell_level_snap = _snapshot_multires_levels([shell_obj])
        shell_mod_pre = get_multires(shell_obj)
        if shell_mod_pre is not None:
            try:
                shell_mod_pre.levels = 0
            except Exception:
                pass

        bpy.ops.object.select_all(action='DESELECT')
        shell_obj.hide_viewport = False
        shell_obj.hide_set(False)
        shell_obj.hide_select = False
        shell_obj.select_set(True)
        context.view_layer.objects.active = shell_obj
        bpy.ops.object.duplicate(linked=False)

        _restore_multires_levels(shell_level_snap)

        new_obj = context.active_object
        new_obj.name = f"{base_obj.name}_L{layer_number:02d}"
        mark_layer_owner(new_obj, base_obj)
        try:
            if "sculpt_layers_is_layer00" in new_obj:
                del new_obj["sculpt_layers_is_layer00"]
        except Exception:
            pass

        # Match Shell Multires: full grids; viewport = min(3, Base/Shell viewport)
        layer_mod = get_multires(new_obj)
        shell_mod = get_multires(shell_obj)
        highest = _multires_highest_level(shell_mod) if shell_mod else settings.base_multires_level
        if highest <= 0 and settings.base_multires_level > 0:
            highest = settings.base_multires_level
        base_mod = get_multires(base_obj)
        base_vp = int(base_mod.levels) if base_mod else (
            int(shell_mod.levels) if shell_mod else highest
        )
        if layer_mod and highest > 0:
            _set_multires_levels(layer_mod, highest, base_viewport=base_vp)
        if highest > 0:
            settings.base_multires_level = highest

        move_to_collection(new_obj, col)

        if new_obj.data.shape_keys:
            new_obj.shape_key_clear()

        # Offset relative to the last sculpt layer (not Base × N)
        try:
            dist = float(settings.offset_distance)
        except Exception:
            dist = _default_offset_distance(base_obj)
        last_layer = None
        for _it in settings.layers:
            if _it.is_base or _it.is_shell_layer or getattr(_it, "is_layer00", False):
                continue
            if _it.object is not None and _object_alive(_it.object) and _it.object != new_obj:
                last_layer = _it.object
        try:
            if last_layer is not None:
                new_obj.matrix_world = last_layer.matrix_world.copy()
                new_obj.location.x += dist
            elif dist != 0.0:
                # First sculpt layer: offset from Base
                new_obj.location.x += dist
        except Exception:
            if dist != 0.0:
                try:
                    new_obj.location.x += dist
                except Exception:
                    pass

        item = settings.layers.add()
        item.name = new_obj.name
        item.object = new_obj
        item.is_base = False
        item.is_shell_layer = False
        item.is_layer00 = False
        item.is_selected = True
        item.strength = 1.0

        mark_as_base(base_obj)
        base_item = find_item_by_flag(settings, is_base=True)
        if base_item is not None:
            base_item.object = base_obj
            base_item.is_base = True

        # Re-hide shell; pin system rows
        shell_obj.hide_viewport = True
        shell_obj.hide_set(True)
        shell_obj.hide_select = True
        pin_system_layers_in_list(settings)
        settings.active_index = len(settings.layers) - 1

        try:
            bpy.ops.object.select_all(action='DESELECT')
            new_obj.hide_viewport = False
            new_obj.hide_set(False)
            new_obj.hide_select = False
            new_obj.select_set(True)
            context.view_layer.objects.active = new_obj
        except Exception:
            pass

        if getattr(settings, "auto_sculpt_mode", False):
            try:
                bpy.ops.object.mode_set(mode='SCULPT')
            except Exception as e:
                print(f"Sculpt Layers: could not enter Sculpt Mode: {e}")

        self.report(
            {'INFO'},
            f"Created '{new_obj.name}' from Layer_00 "
            f"(level {settings.base_multires_level})"
            + (" — Sculpt Mode" if getattr(settings, "auto_sculpt_mode", False) else "")
        )
        return {'FINISHED'}


class SCULPTLAYERS_OT_remove_layer(Operator):
    """Remove selected layer + its shape key on Layer_00, then reshape Base from Layer_00"""
    bl_idname = "sculptlayers.remove_layer"
    bl_label = "Remove Layer"
    bl_options = {'REGISTER', 'UNDO'}

    delete_object: BoolProperty(name="Also Delete Object", default=True)

    def execute(self, context):
        base_obj, settings = get_settings_for_context(context)
        if settings is None:
            self.report({'ERROR'}, "No Base for this mesh. Select it and click Set as Base.")
            return {'CANCELLED'}
        idx = settings.active_index
        if idx < 0 or idx >= len(settings.layers):
            return {'CANCELLED'}

        item = settings.layers[idx]
        if item.is_base or item.is_shell_layer or getattr(item, "is_layer00", False):
            self.report({'WARNING'}, "Cannot remove Base or Layer_00 this way.")
            return {'CANCELLED'}
        if getattr(item, "is_layer00", False):
            self.report({'WARNING'}, "Cannot remove Layer Shell (template for new layers).")
            return {'CANCELLED'}

        layer_name = item.name
        layer0_item = find_item_by_flag(settings, is_shell_layer=True)
        base_item = find_item_by_flag(settings, is_base=True)
        layer0 = layer0_item.object if layer0_item else None
        base = base_item.object if base_item else base_obj

        sk_name = item.object.name if item.object else item.name
        had_shape_key = False
        if layer0 and _object_alive(layer0):
            if layer0.data.shape_keys and sk_name in layer0.data.shape_keys.key_blocks:
                had_shape_key = True
            delete_shape_key_by_name(layer0, sk_name)

        obj = item.object
        settings.layers.remove(idx)

        if self.delete_object and obj and _object_alive(obj):
            try:
                bpy.data.objects.remove(obj, do_unlink=True)
            except (ReferenceError, RuntimeError):
                pass

        # After removing the shape key, push Layer_00 back onto Base so Base
        # no longer includes the deleted layer's displacement
        reshaped = False
        if had_shape_key and layer0 and _object_alive(layer0) and base and _object_alive(base):
            base_multires = get_multires(base)
            if base_multires:
                try:
                    _reshape_base_from_shell_layer(context, base, layer0, base_multires)
                    reshaped = True
                except Exception as e:
                    self.report(
                        {'WARNING'},
                        f"Removed '{layer_name}' but reshape failed: {e}"
                    )
                    _ensure_panel_context_after_layer_remove(
                        context, settings, base, [idx]
                    )
                    return {'FINISHED'}

        # Select another list entry + Base so the sidebar stays visible
        _ensure_panel_context_after_layer_remove(context, settings, base, [idx])

        if reshaped:
            self.report(
                {'INFO'},
                f"Removed '{layer_name}' + shape key → Shell Layer reshaped to Base"
            )
        else:
            self.report({'INFO'}, f"Removed '{layer_name}'")
        return {'FINISHED'}


class SCULPTLAYERS_OT_move_layer_up(Operator):
    """Move the active sculpt layer up in the list (and its shape key on Layer_00)"""
    bl_idname = "sculptlayers.move_layer_up"
    bl_label = "Move Layer Up"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        base_obj, settings = get_settings_for_context(context)
        if settings is None:
            self.report({'ERROR'}, "No Base for this mesh. Select it and click Set as Base.")
            return {'CANCELLED'}

        idx = settings.active_index
        if idx < 0 or idx >= len(settings.layers):
            return {'CANCELLED'}

        item = settings.layers[idx]
        if item.is_base or item.is_shell_layer or getattr(item, "is_layer00", False):
            self.report({'WARNING'}, "Base, Shell Layer, and Layer_00 are pinned and cannot be moved.")
            return {'CANCELLED'}

        # Find previous sculpt-layer index (skip system rows)
        prev = idx - 1
        while prev >= 0 and (
            settings.layers[prev].is_base
            or settings.layers[prev].is_shell_layer
            or getattr(settings.layers[prev], "is_layer00", False)
        ):
            prev -= 1
        if prev < 0:
            self.report({'INFO'}, "Already at the top of sculpt layers")
            return {'CANCELLED'}

        layer_obj = item.object
        other_item = settings.layers[prev]
        other_obj = other_item.object
        layer_name = item.name if item.name else (layer_obj.name if layer_obj else "")
        try:
            layer_name_key = layer_obj.name if layer_obj else layer_name
        except Exception:
            layer_name_key = layer_name

        # Viewport Multires → 0, swap locations, then restore levels
        snap_a, snap_b = _zero_and_swap_layer_locations(layer_obj, other_obj)
        # uses core._is_updating / core._pending_list_select_name
        core._pending_list_select_name = None
        core._is_updating = True
        try:
            settings.layers.move(idx, prev)
            settings.active_index = prev
        finally:
            core._is_updating = False

        layer0_item = find_item_by_flag(settings, is_shell_layer=True)
        layer0 = layer0_item.object if layer0_item else None
        if layer0 and _object_alive(layer0):
            sync_shape_key_order_to_layers(context, settings, layer0)

        moved = None
        if prev < len(settings.layers):
            moved = settings.layers[prev].object
        if moved is None or not _object_alive(moved):
            moved = bpy.data.objects.get(layer_name_key) or layer_obj

        # Restore each object's own Multires viewport levels
        _restore_viewport_levels_snap(layer_obj, snap_a)
        _restore_viewport_levels_snap(other_obj, snap_b)

        _reselect_layer_keep_panel(context, moved, index=prev, settings=settings)

        # Deferred second pass (shape_key_move / msgbus can clear selection after we return)
        target_name = moved.name if moved and _object_alive(moved) else layer_name_key
        target_idx = prev

        def _keep_sel():
            obj = bpy.data.objects.get(target_name)
            if obj is None:
                return None
            _reselect_layer_keep_panel(bpy.context, obj, index=target_idx, settings=settings)
            return None

        try:
            if not bpy.app.timers.is_registered(_keep_sel):
                bpy.app.timers.register(_keep_sel, first_interval=0.05)
        except Exception:
            pass

        self.report({'INFO'}, f"Moved '{layer_name}' up")
        return {'FINISHED'}


class SCULPTLAYERS_OT_move_layer_down(Operator):
    """Move the active sculpt layer down in the list (and its shape key on Layer_00)"""
    bl_idname = "sculptlayers.move_layer_down"
    bl_label = "Move Layer Down"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        base_obj, settings = get_settings_for_context(context)
        if settings is None:
            self.report({'ERROR'}, "No Base for this mesh. Select it and click Set as Base.")
            return {'CANCELLED'}

        idx = settings.active_index
        if idx < 0 or idx >= len(settings.layers):
            return {'CANCELLED'}

        item = settings.layers[idx]
        if item.is_base or item.is_shell_layer or getattr(item, "is_layer00", False):
            self.report({'WARNING'}, "Base, Shell Layer, and Layer_00 are pinned and cannot be moved.")
            return {'CANCELLED'}

        # Find next sculpt-layer index
        nxt = idx + 1
        while nxt < len(settings.layers) and (
            settings.layers[nxt].is_base
            or settings.layers[nxt].is_shell_layer
            or getattr(settings.layers[nxt], "is_layer00", False)
        ):
            nxt += 1
        if nxt >= len(settings.layers):
            self.report({'INFO'}, "Already at the bottom of sculpt layers")
            return {'CANCELLED'}

        layer_obj = item.object
        other_item = settings.layers[nxt]
        other_obj = other_item.object
        layer_name = item.name if item.name else (layer_obj.name if layer_obj else "")
        try:
            layer_name_key = layer_obj.name if layer_obj else layer_name
        except Exception:
            layer_name_key = layer_name

        # Viewport Multires → 0, swap locations, then restore levels
        snap_a, snap_b = _zero_and_swap_layer_locations(layer_obj, other_obj)
        # uses core._is_updating / core._pending_list_select_name
        core._pending_list_select_name = None
        core._is_updating = True
        try:
            settings.layers.move(idx, nxt)
            settings.active_index = nxt
        finally:
            core._is_updating = False

        layer0_item = find_item_by_flag(settings, is_shell_layer=True)
        layer0 = layer0_item.object if layer0_item else None
        if layer0 and _object_alive(layer0):
            sync_shape_key_order_to_layers(context, settings, layer0)

        moved = None
        if nxt < len(settings.layers):
            moved = settings.layers[nxt].object
        if moved is None or not _object_alive(moved):
            moved = bpy.data.objects.get(layer_name_key) or layer_obj

        _restore_viewport_levels_snap(layer_obj, snap_a)
        _restore_viewport_levels_snap(other_obj, snap_b)

        _reselect_layer_keep_panel(context, moved, index=nxt, settings=settings)

        target_name = moved.name if moved and _object_alive(moved) else layer_name_key
        target_idx = nxt

        def _keep_sel():
            obj = bpy.data.objects.get(target_name)
            if obj is None:
                return None
            _reselect_layer_keep_panel(bpy.context, obj, index=target_idx, settings=settings)
            return None

        try:
            if not bpy.app.timers.is_registered(_keep_sel):
                bpy.app.timers.register(_keep_sel, first_interval=0.05)
        except Exception:
            pass

        self.report({'INFO'}, f"Moved '{layer_name}' down")
        return {'FINISHED'}


class SCULPTLAYERS_OT_switch_to_layer(Operator):
    """Select the active list layer in the viewport"""
    bl_idname = "sculptlayers.switch_to_layer"
    bl_label = "Select in Viewport"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        base_obj, settings = get_settings_for_context(context)
        if settings is None:
            self.report({'ERROR'}, "No Base for this mesh. Select it and click Set as Base.")
            return {'CANCELLED'}
        idx = settings.active_index
        if idx < 0 or idx >= len(settings.layers):
            return {'CANCELLED'}

        item = settings.layers[idx]
        if not item.object:
            self.report({'ERROR'}, "Layer object is missing")
            return {'CANCELLED'}

        # Trigger the same path as list selection
        _on_active_index_update(settings, context)

        obj = item.object
        if getattr(item, "is_layer00", False):
            self.report({'WARNING'}, "Layer_00 is a template — pick a sculpt layer.")
            return {'CANCELLED'}
        if not item.is_base and not item.is_shell_layer:
            try:
                if context.mode != 'SCULPT':
                    bpy.ops.object.mode_set(mode='SCULPT')
            except Exception:
                pass
            self.report({'INFO'}, f"Sculpting on '{obj.name}'")
        else:
            self.report({'INFO'}, f"Selected '{obj.name}'")
        return {'FINISHED'}


def _iter_layer_collections(layer_col):
    """Recursively walk a view layer's LayerCollection tree."""
    yield layer_col
    for child in layer_col.children:
        yield from _iter_layer_collections(child)


def _make_selectable(obj, context):
    """
    Make sure nothing at the OBJECT level *or* the COLLECTION level is blocking
    selection: unexclude/unhide every LayerCollection that holds this object's
    collections in the current view layer, unhide the collections themselves,
    and clear the object's own hide/select flags. Without this, obj.select_set()
    can silently fail (or the object just never counts as selected) if its
    collection is excluded or eye-icon-hidden, even though the object's own
    hide_viewport/hide_select look fine.
    """
    for col in obj.users_collection:
        try:
            col.hide_viewport = False
        except Exception:
            pass
    try:
        for lc in _iter_layer_collections(context.view_layer.layer_collection):
            if lc.collection in obj.users_collection:
                try:
                    lc.exclude = False
                except Exception:
                    pass
                try:
                    lc.hide_viewport = False
                except Exception:
                    pass
    except Exception:
        pass
    obj.hide_viewport = False
    try:
        obj.hide_set(False)
    except Exception:
        pass
    obj.hide_select = False


def _get_view3d_override(context):
    """
    Find a real window/screen/area(VIEW_3D)/region('WINDOW') to override context with.
    join_shapes / mode_set / multires_reshape can fail their poll() with
    'context is incorrect' if called from a context that isn't guaranteed to have
    a live window+screen+VIEW_3D area (e.g. a button click routed through a
    modifier/operator chain, deferred call, or a non-3D-view region).
    """
    wm = context.window_manager
    for window in wm.windows:
        screen = window.screen
        for area in screen.areas:
            if area.type == 'VIEW_3D':
                for region in area.regions:
                    if region.type == 'WINDOW':
                        return {
                            "window": window,
                            "screen": screen,
                            "area": area,
                            "region": region,
                        }
    return None


def _force_object_mode(context):
    """
    Force Object Mode and RAISE if it doesn't actually take, instead of
    silently continuing in the wrong mode (which is what previously caused
    join_shapes to fail its poll with a confusing 'context is incorrect').
    """
    if context.mode == 'OBJECT':
        return
    override = _get_view3d_override(context)
    try:
        if override:
            with context.temp_override(**override):
                bpy.ops.object.mode_set(mode='OBJECT')
        else:
            bpy.ops.object.mode_set(mode='OBJECT')
    except Exception as e:
        raise RuntimeError(f"Could not switch to Object Mode before join_shapes: {e}")
    if context.mode != 'OBJECT':
        raise RuntimeError(
            f"Still in '{context.mode}' after mode_set(OBJECT); "
            "cannot run join_shapes."
        )


def _join_shapes_layer_to_shell_layer(context, layer_obj, layer0, strength):
    """
    Select sculpt layer → active Layer_00 → join_shapes → set strength.
    Layer_00 stays at Base transform (never moved/offset).
    Works with vertices-only Layer_00 (user-verified manual workflow).
    """
    _force_object_mode(context)

    # Resolve Base for transform lock
    base = None
    try:
        base_name = layer0.get("sculpt_layers_base")
        if base_name:
            base = bpy.data.objects.get(base_name)
    except Exception:
        pass
    if base is None:
        # Fallback: same location as layer0 was intended to match
        base = layer0

    layer_mod = get_multires(layer_obj)
    _level_snap = _snapshot_multires_levels([layer_obj])
    if layer_mod:
        highest = _multires_highest_level(layer_mod)
        if highest > 0:
            layer_mod.levels = highest
            layer_mod.sculpt_levels = highest
            layer_mod.render_levels = highest

    n_layer = evaluated_vertex_count(layer_obj, context)
    n_l0 = len(layer0.data.vertices)
    if n_layer != n_l0:
        raise RuntimeError(
            f"Evaluated vertex mismatch: {layer_obj.name}={n_layer}, Shell Layer={n_l0}. "
            "Do not change Multires subdivision levels after setup."
        )

    ensure_basis_shape_key(layer0)
    sk_name = layer_obj.name
    delete_shape_key_by_name(layer0, sk_name)

    # Pin Layer_00 to Base transform before join_shapes
    if base is not None and base != layer0:
        layer0.matrix_world = base.matrix_world.copy()

    was_l0_vp = layer0.hide_viewport
    was_l0_h = layer0.hide_get()
    was_l_vp = layer_obj.hide_viewport
    was_l_h = layer_obj.hide_get()

    try:
        _make_selectable(layer0, context)
        _make_selectable(layer_obj, context)
        context.view_layer.update()

        for o in list(context.selected_objects):
            try:
                o.select_set(False)
            except Exception:
                pass
        layer_obj.select_set(True)
        layer0.select_set(True)
        context.view_layer.objects.active = layer0
        context.view_layer.update()

        if not layer_obj.select_get() or not layer0.select_get():
            raise RuntimeError(
                f"Could not select '{layer_obj.name}' and 'Shell Layer' for join_shapes "
                "(check they aren't excluded from the view layer)."
            )
        if context.view_layer.objects.active != layer0:
            raise RuntimeError("Shell Layer did not become the active object for join_shapes.")

        override = _get_view3d_override(context)
        if override:
            with context.temp_override(**override):
                bpy.ops.object.join_shapes()
        else:
            bpy.ops.object.join_shapes()

        keys = layer0.data.shape_keys
        if keys is None:
            raise RuntimeError(f"join_shapes created no shape key for '{sk_name}'")

        sk = keys.key_blocks.get(sk_name)
        if sk is None:
            for kb in reversed(list(keys.key_blocks)):
                if kb.name != "Basis":
                    sk = kb
                    sk.name = sk_name
                    break
        if sk is None:
            raise RuntimeError(f"Shape key for '{sk_name}' not found after join_shapes")

        leftovers = [
            kb.name for kb in keys.key_blocks
            if kb.name.startswith(sk_name + ".") and kb.name[len(sk_name) + 1:].isdigit()
        ]
        for name in leftovers:
            if name in keys.key_blocks:
                layer0.shape_key_remove(keys.key_blocks[name])

        sk = layer0.data.shape_keys.key_blocks[sk_name]
        sk.slider_min = -1.0
        sk.slider_max = 1.0
        sk.value = float(strength)
    finally:
        # Keep Layer_00 locked to Base; restore hide state
        if base is not None and base != layer0:
            try:
                layer0.matrix_world = base.matrix_world.copy()
            except Exception:
                pass
        layer0.hide_viewport = was_l0_vp
        layer0.hide_set(was_l0_h)
        layer_obj.hide_viewport = was_l_vp
        layer_obj.hide_set(was_l_h)
        try:
            layer_obj.select_set(False)
        except Exception:
            pass
        _restore_multires_levels(_level_snap)



def _mute_non_multires_modifiers(obj):
    """Disable viewport/render for all modifiers except Multires; return snap list."""
    if obj is None:
        return []
    saved = []
    try:
        for mod in obj.modifiers:
            if mod.type == 'MULTIRES':
                continue
            try:
                saved.append((mod.name, bool(mod.show_viewport), bool(mod.show_render)))
                mod.show_viewport = False
                mod.show_render = False
            except Exception:
                pass
    except Exception:
        pass
    return saved


def _restore_non_multires_modifiers(obj, saved):
    """Restore modifier visibility from _mute_non_multires_modifiers."""
    if obj is None or not saved:
        return
    try:
        mods = obj.modifiers
    except Exception:
        return
    for name, show_vp, show_rn in saved:
        mod = mods.get(name)
        if mod is None:
            continue
        try:
            mod.show_viewport = show_vp
            mod.show_render = show_rn
        except Exception:
            pass

def _mute_shape_keys(obj):
    """Set all shape-key values to 0; return list of (name, value) to restore."""
    if obj is None or getattr(obj, "data", None) is None:
        return []
    sks = obj.data.shape_keys
    if sks is None:
        return []
    saved = []
    for kb in sks.key_blocks:
        # Skip Basis (index 0) value is typically unused, but still save all
        try:
            saved.append((kb.name, float(kb.value)))
            if kb.name != "Basis":
                kb.value = 0.0
        except Exception:
            pass
    return saved


def _restore_shape_keys(obj, saved):
    """Restore shape-key values from _mute_shape_keys snapshot."""
    if obj is None or not saved:
        return
    sks = getattr(obj.data, "shape_keys", None)
    if sks is None:
        return
    blocks = sks.key_blocks
    for name, val in saved:
        kb = blocks.get(name)
        if kb is None:
            continue
        try:
            kb.value = val
        except Exception:
            pass

def _reshape_base_from_shell_layer(context, base, layer0, multires_mod):
    """
    Select Layer_00 → active Base → Multires Reshape.
    Layer_00 transform is forced to Base (addon must not move Layer_00).
    Works with vertices-only Layer_00 when selection is correct.
    """
    _force_object_mode(context)

    _level_snap = _snapshot_multires_levels([base, layer0])
    highest = _multires_highest_level(multires_mod)
    if highest > 0:
        multires_mod.levels = highest
        multires_mod.sculpt_levels = highest
        multires_mod.render_levels = highest

    # Critical: Layer_00 must sit on Base — never leave it offset
    layer0.matrix_world = base.matrix_world.copy()

    was_l0_vp = layer0.hide_viewport
    was_l0_h = layer0.hide_get()

    # Base shape keys + non-Multires modifiers off during Reshape, then restored
    base_sk_snap = _mute_shape_keys(base)
    base_mod_snap = _mute_non_multires_modifiers(base)

    try:
        _make_selectable(layer0, context)
        _make_selectable(base, context)
        context.view_layer.update()

        for o in list(context.selected_objects):
            try:
                o.select_set(False)
            except Exception:
                pass
        layer0.select_set(True)
        base.select_set(True)
        context.view_layer.objects.active = base

        if not layer0.select_get() or not base.select_get():
            raise RuntimeError("Could not select Shell Layer and Base for reshape")

        override = _get_view3d_override(context)
        if override:
            with context.temp_override(**override):
                bpy.ops.object.multires_reshape(modifier=multires_mod.name)
        else:
            bpy.ops.object.multires_reshape(modifier=multires_mod.name)
    finally:
        try:
            layer0.matrix_world = base.matrix_world.copy()
        except Exception:
            pass
        layer0.hide_viewport = was_l0_vp
        layer0.hide_set(was_l0_h)
        try:
            layer0.select_set(False)
        except Exception:
            pass
        _restore_multires_levels(_level_snap)
        _restore_shape_keys(base, base_sk_snap)
        _restore_non_multires_modifiers(base, base_mod_snap)


class SCULPTLAYERS_OT_apply_layer(Operator):
    """New from Objects on Layer_00 (with strength) → reshape Base from Layer_00"""
    bl_idname = "sculptlayers.apply_layer"
    bl_label = "Apply Layer → Base"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        base_obj, settings = get_settings_for_context(context)
        if settings is None:
            self.report({'ERROR'}, "No Base for this mesh. Select it and click Set as Base.")
            return {'CANCELLED'}
        rebuild_shell_layer_if_needed(context)

        base_item = find_item_by_flag(settings, is_base=True)
        layer0_item = find_item_by_flag(settings, is_shell_layer=True)

        if not base_item or not base_item.object:
            self.report({'ERROR'}, "No Base. Use 'Set as Base' first.")
            return {'CANCELLED'}
        if not layer0_item or not layer0_item.object:
            self.report({'ERROR'}, "Shell Layer missing. Add a sculpt layer first.")
            return {'CANCELLED'}

        base = base_item.object
        layer0 = layer0_item.object
        base_multires = get_multires(base)
        if not base_multires:
            self.report({'ERROR'}, "Base has no Multiresolution modifier")
            return {'CANCELLED'}

        idx = settings.active_index
        if idx < 0 or idx >= len(settings.layers):
            self.report({'ERROR'}, "No layer selected in the list")
            return {'CANCELLED'}

        layer_item = settings.layers[idx]
        if layer_item.is_base or layer_item.is_shell_layer:
            self.report({'WARNING'}, "Select a sculpt layer in the list first")
            return {'CANCELLED'}
        if not layer_item.object:
            self.report({'ERROR'}, "Layer object is missing")
            return {'CANCELLED'}

        layer = layer_item.object
        strength = layer_item.strength

        # Snapshot Multires levels (especially Base viewport) before apply bumps them
        snap_objs = [base, layer0, layer]
        for it in settings.layers:
            if it.object and it.object not in snap_objs:
                snap_objs.append(it.object)
        level_snap = _snapshot_multires_levels(snap_objs)

        try:
            _join_shapes_layer_to_shell_layer(context, layer, layer0, strength)
            _reshape_base_from_shell_layer(context, base, layer0, base_multires)
        except Exception as e:
            _restore_multires_levels(level_snap)
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}

        # Restore Base + layers viewport/sculpt/render levels from before apply
        _restore_multires_levels(level_snap)

        # Keep applied layer selected so the panel stays visible
        _reselect_layer_keep_panel(context, layer, index=idx, settings=settings)

        self.report(
            {'INFO'},
            f"Applied '{layer.name}' (strength {strength:.2f}) via shape key → Shell Layer → Base"
        )
        return {'FINISHED'}


class SCULPTLAYERS_OT_apply_all(Operator):
    """Apply all *visible* sculpt layers as shape keys on Layer_00 then reshape Base.
    Hidden layers are skipped (their shape keys are zeroed so they do not affect reshape).
    """
    bl_idname = "sculptlayers.apply_all"
    bl_label = "Apply All Layers → Base"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        base_obj, settings = get_settings_for_context(context)
        if settings is None:
            self.report({'ERROR'}, "No Base for this mesh. Select it and click Set as Base.")
            return {'CANCELLED'}
        rebuild_shell_layer_if_needed(context)

        base_item = find_item_by_flag(settings, is_base=True)
        layer0_item = find_item_by_flag(settings, is_shell_layer=True)

        if not base_item or not base_item.object:
            self.report({'ERROR'}, "No Base registered")
            return {'CANCELLED'}
        if not layer0_item or not layer0_item.object:
            self.report({'ERROR'}, "Shell Layer is missing")
            return {'CANCELLED'}

        base = base_item.object
        layer0 = layer0_item.object
        base_multires = get_multires(base)
        if not base_multires:
            self.report({'ERROR'}, "Base has no Multiresolution modifier")
            return {'CANCELLED'}

        snap_objs = [base, layer0]
        for it in settings.layers:
            if it.object and it.object not in snap_objs:
                snap_objs.append(it.object)
        level_snap = _snapshot_multires_levels(snap_objs)

        applied = 0
        skipped_hidden = 0
        for item in settings.layers:
            if item.is_base or item.is_shell_layer or getattr(item, "is_layer00", False) or not item.object:
                continue

            # Skip hidden layers — do not apply them
            obj = item.object
            is_hidden = False
            try:
                is_hidden = bool(obj.hide_viewport or obj.hide_get())
            except Exception:
                pass

            if is_hidden:
                skipped_hidden += 1
                # Zero any existing shape key so it does not contribute to reshape
                sk_name = obj.name
                if layer0.data.shape_keys and sk_name in layer0.data.shape_keys.key_blocks:
                    sk = layer0.data.shape_keys.key_blocks[sk_name]
                    sk.value = 0.0
                continue

            try:
                _join_shapes_layer_to_shell_layer(
                    context, obj, layer0, item.strength
                )
                applied += 1
            except Exception as e:
                _restore_multires_levels(level_snap)
                self.report({'ERROR'}, f"{item.name}: {e}")
                return {'CANCELLED'}

        if applied == 0:
            _restore_multires_levels(level_snap)
            self.report(
                {'WARNING'},
                f"No visible layers to apply"
                + (f" ({skipped_hidden} hidden skipped)" if skipped_hidden else "")
            )
            return {'CANCELLED'}

        try:
            _reshape_base_from_shell_layer(context, base, layer0, base_multires)
        except Exception as e:
            _restore_multires_levels(level_snap)
            self.report({'ERROR'}, f"Reshape failed: {e}")
            return {'CANCELLED'}

        _restore_multires_levels(level_snap)

        msg = f"Applied {applied} visible layer(s) → Shell Layer → Base"
        if skipped_hidden:
            msg += f" ({skipped_hidden} hidden skipped)"
        self.report({'INFO'}, msg)
        return {'FINISHED'}


class SCULPTLAYERS_OT_apply_selected(Operator):
    """Apply checked layers as shape keys on Layer_00 then reshape Base once"""
    bl_idname = "sculptlayers.apply_selected"
    bl_label = "Apply Selected Layers → Base"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        base_obj, settings = get_settings_for_context(context)
        if settings is None:
            self.report({'ERROR'}, "No Base for this mesh. Select it and click Set as Base.")
            return {'CANCELLED'}
        rebuild_shell_layer_if_needed(context)

        base_item = find_item_by_flag(settings, is_base=True)
        layer0_item = find_item_by_flag(settings, is_shell_layer=True)

        if not base_item or not base_item.object:
            self.report({'ERROR'}, "No Base registered")
            return {'CANCELLED'}
        if not layer0_item or not layer0_item.object:
            self.report({'ERROR'}, "Shell Layer is missing. Add a sculpt layer first.")
            return {'CANCELLED'}

        base = base_item.object
        layer0 = layer0_item.object
        base_multires = get_multires(base)
        if not base_multires:
            self.report({'ERROR'}, "Base has no Multiresolution modifier")
            return {'CANCELLED'}

        selected = [
            item for item in settings.layers
            if item.is_selected and not item.is_base and not item.is_shell_layer and item.object
        ]
        if not selected:
            self.report({'WARNING'}, "No sculpt layers checked. Tick the checkbox on layers to apply.")
            return {'CANCELLED'}

        snap_objs = [base, layer0]
        for it in settings.layers:
            if it.object and it.object not in snap_objs:
                snap_objs.append(it.object)
        level_snap = _snapshot_multires_levels(snap_objs)

        applied = 0
        for item in selected:
            try:
                _join_shapes_layer_to_shell_layer(
                    context, item.object, layer0, item.strength
                )
                applied += 1
            except Exception as e:
                _restore_multires_levels(level_snap)
                self.report({'ERROR'}, f"{item.name}: {e}")
                return {'CANCELLED'}

        try:
            _reshape_base_from_shell_layer(context, base, layer0, base_multires)
        except Exception as e:
            _restore_multires_levels(level_snap)
            self.report({'ERROR'}, f"Reshape failed: {e}")
            return {'CANCELLED'}

        _restore_multires_levels(level_snap)

        self.report({'INFO'}, f"Applied {applied} selected layer(s) as shape keys → Shell Layer → Base")
        return {'FINISHED'}


class SCULPTLAYERS_OT_select_all_layers(Operator):
    """Check all sculpt layers for multi-select"""
    bl_idname = "sculptlayers.select_all_layers"
    bl_label = "Select All Layers"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        base_obj, settings = get_settings_for_context(context)
        if settings is None:
            self.report({'ERROR'}, "No Base for this mesh. Select it and click Set as Base.")
            return {'CANCELLED'}
        count = 0
        for item in settings.layers:
            if not item.is_base and not item.is_shell_layer:
                item.is_selected = True
                count += 1
        self.report({'INFO'}, f"Selected {count} layer(s)")
        return {'FINISHED'}


class SCULPTLAYERS_OT_deselect_all_layers(Operator):
    """Uncheck all sculpt layers"""
    bl_idname = "sculptlayers.deselect_all_layers"
    bl_label = "Deselect All Layers"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        base_obj, settings = get_settings_for_context(context)
        if settings is None:
            self.report({'ERROR'}, "No Base for this mesh. Select it and click Set as Base.")
            return {'CANCELLED'}
        for item in settings.layers:
            item.is_selected = False
        return {'FINISHED'}


class SCULPTLAYERS_OT_clear_list(Operator):
    bl_idname = "sculptlayers.clear_list"
    bl_label = "Clear Layer List"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        base_obj, settings = get_settings_for_context(context)
        if settings is None:
            self.report({'ERROR'}, "No Base for this mesh. Select it and click Set as Base.")
            return {'CANCELLED'}
        reset_addon(settings, delete_layer_objects=True, base_obj=base_obj)
        return {'FINISHED'}


class SCULPTLAYERS_OT_update_list(Operator):
    """Clean dead layers, sync order + strength on Shell Layer, reshape Base."""
    bl_idname = "sculptlayers.update_list"
    bl_label = "Update List"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        base_obj, settings = get_settings_for_context(context)
        if settings is None:
            self.report({'ERROR'}, "No Base for this mesh. Select it and click Set as Base.")
            return {'CANCELLED'}

        def _live_shell():
            it = find_item_by_flag(settings, is_shell_layer=True)
            return it.object if it and _object_alive(it.object) else None

        base_item = find_item_by_flag(settings, is_base=True)
        base = (
            base_item.object
            if base_item and _object_alive(base_item.object)
            else base_obj
        )

        removed = 0
        removed_indices = []
        removed_applied_key = False

        # --- 1) Drop missing / out-of-collection entries (and delete orphans) ---
        for i in range(len(settings.layers) - 1, -1, -1):
            item = settings.layers[i]
            if item.is_base:
                continue

            missing = _layer_entry_missing(item)
            not_in_collection = False
            if not missing and item.object is not None:
                not_in_collection = not object_in_sculpt_layers_collection(item.object)

            if not missing and not not_in_collection:
                continue

            sk_name = item.name
            obj_ref = item.object if item.object and _object_alive(item.object) else None
            if obj_ref is not None:
                sk_name = obj_ref.name
            is_l0 = item.is_shell_layer
            shell = _live_shell()
            if not is_l0 and shell and sk_name:
                try:
                    if (
                        shell.data.shape_keys
                        and sk_name in shell.data.shape_keys.key_blocks
                    ):
                        removed_applied_key = True
                except Exception:
                    pass
                delete_shape_key_by_name(shell, sk_name)

            if obj_ref is not None:
                try:
                    bpy.data.objects.remove(obj_ref, do_unlink=True)
                except Exception:
                    pass

            settings.layers.remove(i)
            removed_indices.append(i)
            removed += 1

        pin_system_layers_in_list(settings)

        # --- 2) Sync order + strength on Shell Layer, reshape Base ---
        shell = _live_shell()
        synced, reshaped = 0, False
        if shell is not None:
            try:
                synced, reshaped = _sync_shell_layer_from_list(
                    context, settings, shell, base
                )
            except Exception as e:
                self.report({'WARNING'}, f"Sync/reshape issue: {e}")
        elif removed_applied_key and base and _object_alive(base):
            # Shell gone after cleanup — nothing to reshape
            pass

        if removed_indices:
            _ensure_panel_context_after_layer_remove(
                context, settings, base, removed_indices or [0]
            )

        parts = []
        if removed:
            parts.append(f"removed {removed}")
        if synced:
            parts.append(f"synced {synced} strength(s)")
        if reshaped:
            parts.append("reshaped Base")
        if not parts:
            parts.append("list up to date")
        self.report({'INFO'}, "Refresh: " + ", ".join(parts))
        return {'FINISHED'}



class SCULPTLAYERS_OT_toggle_hide(Operator):
    """Toggle viewport visibility of a layer. Keeps list selection on that layer."""
    bl_idname = "sculptlayers.toggle_hide"
    bl_label = "Toggle Hide"
    bl_options = {'REGISTER', 'UNDO'}

    index: IntProperty(default=-1)

    def execute(self, context):
        # uses core._is_updating
        base_obj, settings = get_settings_for_context(context)
        if settings is None:
            self.report({'ERROR'}, "No Base for this mesh. Select it and click Set as Base.")
            return {'CANCELLED'}
        idx = self.index if self.index >= 0 else settings.active_index
        if idx < 0 or idx >= len(settings.layers):
            return {'CANCELLED'}
        item = settings.layers[idx]
        if not item.object:
            self.report({'ERROR'}, "Layer object is missing")
            return {'CANCELLED'}

        obj = item.object
        # Toggle both outliner hide and local hide so visibility stays in sync
        currently_hidden = bool(obj.hide_viewport or obj.hide_get())
        new_hidden = not currently_hidden
        obj.hide_viewport = new_hidden
        obj.hide_set(new_hidden)

        # Keep this layer selected in the addon list (avoid active_index update
        # callback, which would force-unhide the object)
        core._is_updating = True
        try:
            if settings.active_index != idx:
                settings.active_index = idx
            # If we hid the active viewport object, keep Base active so the
            # panel still resolves this mesh's layers and stays fully visible
            if new_hidden and context.view_layer.objects.active == obj:
                if base_obj is not None and _object_alive(base_obj):
                    try:
                        base_obj.select_set(True)
                        context.view_layer.objects.active = base_obj
                    except Exception:
                        pass
        finally:
            core._is_updating = False

        return {'FINISHED'}


class SCULPTLAYERS_OT_hide_all_layers(Operator):
    bl_idname = "sculptlayers.hide_all_layers"
    bl_label = "Hide All Layers"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        base_obj, settings = get_settings_for_context(context)
        if settings is None:
            self.report({'ERROR'}, "No Base for this mesh. Select it and click Set as Base.")
            return {'CANCELLED'}
        count = 0
        for item in settings.layers:
            if item.is_base or not item.object:
                continue
            item.object.hide_viewport = True
            item.object.hide_set(True)
            count += 1
        self.report({'INFO'}, f"Hidden {count} layer(s)")
        return {'FINISHED'}


class SCULPTLAYERS_OT_show_all_layers(Operator):
    bl_idname = "sculptlayers.show_all_layers"
    bl_label = "Show All Layers"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        base_obj, settings = get_settings_for_context(context)
        if settings is None:
            self.report({'ERROR'}, "No Base for this mesh. Select it and click Set as Base.")
            return {'CANCELLED'}
        count = 0
        for item in settings.layers:
            if not item.object:
                continue
            if item.is_shell_layer or getattr(item, "is_layer00", False):
                item.object.hide_viewport = True
                item.object.hide_set(True)
                continue
            item.object.hide_viewport = False
            item.object.hide_set(False)
            count += 1
        self.report({'INFO'}, f"Shown {count} layer(s)")
        return {'FINISHED'}

