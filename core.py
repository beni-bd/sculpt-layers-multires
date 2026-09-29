"""Core helpers: Multires, layers, selection sync, depsgraph handler."""

import time

import bpy
from bpy.app.handlers import persistent

from .constants import (
    COLLECTION_NAME,
    SHELL_LAYER_NAME,
    LAYER_00_NAME,
    MULTIRES_VIEWPORT_MAX,
    BASE_FLAG,
)

# ---------------------------------------------------------------------------
# Mutable module state
# ---------------------------------------------------------------------------

_is_updating = False
_HANDLER_MIN_INTERVAL = 0.2  # seconds
_last_handler_time = 0.0
_pending_select_name = None
_select_timer_registered = False
_msgbus_owner = object()
_last_list_sync_ptr = 0
_pending_list_select_name = None

def _apply_pending_list_select():
    """Deferred viewport select after list click — keeps UIList instant."""
    global _pending_list_select_name, _is_updating, _last_list_sync_ptr
    name = _pending_list_select_name
    _pending_list_select_name = None
    if not name:
        return None
    obj = bpy.data.objects.get(name)
    if obj is None:
        return None
    ctx = bpy.context
    try:
        if ctx.view_layer.objects.active == obj and obj.select_get():
            return None
    except Exception:
        pass

    _is_updating = True
    try:
        # NO mode_set, NO hide_set — those are the main lag sources with Multires.
        # Only flip active + select; leave visibility to the eye icons.
        prev = ctx.view_layer.objects.active
        if prev is not None and prev != obj:
            try:
                prev.select_set(False)
            except Exception:
                pass
        try:
            if obj.hide_select:
                obj.hide_select = False
        except Exception:
            pass
        try:
            obj.select_set(True)
            ctx.view_layer.objects.active = obj
            _last_list_sync_ptr = obj.as_pointer()
        except Exception:
            pass
    finally:
        _is_updating = False
    return None


def _on_active_index_update(self, context):
    """List row click: update highlight immediately, defer viewport select."""
    global _is_updating, _pending_list_select_name
    if _is_updating:
        return

    settings = self
    idx = settings.active_index
    if idx < 0 or idx >= len(settings.layers):
        return

    item = settings.layers[idx]
    obj = item.object
    if obj is None:
        return

    try:
        if context.view_layer.objects.active == obj and obj.select_get():
            return
    except Exception:
        pass

    # Queue lightweight select for next tick (list stays responsive)
    try:
        _pending_list_select_name = obj.name
    except Exception:
        return
    try:
        if not bpy.app.timers.is_registered(_apply_pending_list_select):
            bpy.app.timers.register(_apply_pending_list_select, first_interval=0.0)
    except Exception:
        # Timer unavailable — apply inline (still no mode_set/hide)
        _apply_pending_list_select()



def get_active_mesh(context):
    obj = context.active_object
    if obj is None or obj.type != 'MESH':
        return None
    return obj


def get_object_settings(obj):
    """Return the per-object SculptLayersSettings, or None."""
    if obj is None or not hasattr(obj, "sculpt_layers"):
        return None
    try:
        return obj.sculpt_layers
    except Exception:
        return None


def object_in_sculpt_layers_collection(obj):
    """True if the object lives in the addon 'Sculpt LAYERS' collection.

    Perf: compares collection identity (fast pointer check) instead of
    string-comparing every collection name the object belongs to, and
    looks the addon collection up once via the O(1) bpy.data.collections
    dict instead of scanning obj.users_collection by name. Falls out
    immediately if the addon collection doesn't exist yet. Called from
    is_sculpt_layer_object(), which runs on every panel redraw.
    """
    if obj is None:
        return False
    try:
        col = bpy.data.collections.get(COLLECTION_NAME)
        if col is None:
            return False
        return col in obj.users_collection
    except Exception:
        return False



def clear_object_sculpt_layers(obj):
    """
    Wipe sculpt_layers data on an object.
    Critical after duplicating a Base — Blender copies PropertyGroups and
    can remap the is_base pointer onto the duplicate, making a layer look like a Base.
    """
    if obj is None:
        return
    settings = get_object_settings(obj)
    if settings is None:
        return
    try:
        settings.layers.clear()
        settings.active_index = 0
        settings.base_vertex_count = 0
        settings.base_multires_level = 0
    except Exception:
        pass



def _default_offset_distance(base_obj):
    """Base mesh X dimension × 1.5 (world size), fallback 4.0."""
    if base_obj is None:
        return 4.0
    try:
        # dimensions is object-space size with scale applied
        x = float(base_obj.dimensions.x)
        if x > 1e-8:
            return x * 1.5
    except Exception:
        pass
    return 4.0


def mark_as_base(obj):
    """Persistently mark this object as a registered Base (survives duplicates/pointer issues)."""
    if obj is None:
        return
    try:
        obj[BASE_FLAG] = True
        # Make sure it is not tagged as a layer
        if "sculpt_layers_base" in obj:
            del obj["sculpt_layers_base"]
    except Exception:
        pass


def unmark_as_base(obj):
    if obj is None:
        return
    try:
        if BASE_FLAG in obj:
            del obj[BASE_FLAG]
    except Exception:
        pass


def is_registered_base(obj):
    """
    True if this mesh was explicitly Set as Base.
    Uses a custom property flag (reliable) plus the layers list.
    Objects tagged as layers never qualify.
    """
    if obj is None or obj.type != 'MESH':
        return False
    # Explicit layer ownership tag always wins — never a Base
    try:
        if obj.get("sculpt_layers_base"):
            return False
    except Exception:
        pass

    # Primary: persistent flag set by Set as Base (hot path — most calls)
    try:
        if obj.get(BASE_FLAG):
            return True
    except Exception:
        pass

    # Fallback: scan layers list (name-safe pointer compare)
    settings = get_object_settings(obj)
    if settings is None:
        return False
    layers = settings.layers
    if len(layers) == 0:
        return False
    for item in layers:
        if not item.is_base:
            continue
        ref = item.object
        if ref is None:
            continue
        try:
            if ref == obj or ref.name == obj.name:
                return True
        except ReferenceError:
            continue
    return False


def is_sculpt_layer_object(obj):
    """True if this object is a sculpt layer / Layer_00 (not a Base)."""
    if obj is None:
        return False
    try:
        # Base flag wins — never treat a marked Base as a layer
        if obj.get(BASE_FLAG):
            return False
        # Fast path: ownership tag set on every layer / Layer_00
        if obj.get("sculpt_layers_base"):
            return True
    except Exception:
        pass
    # Slow path: in Sculpt LAYERS collection and not a registered base
    return object_in_sculpt_layers_collection(obj) and not is_registered_base(obj)


def shell_layer_name_for_base(base_obj):
    """Object name for Shell Layer (reshape target / is_shell_layer) per Base."""
    return f"{base_obj.name}_ShellLayer"


def mark_layer_owner(layer_obj, base_obj):
    """Store which base owns this layer object (for reverse lookup)."""
    if layer_obj is None or base_obj is None:
        return
    try:
        layer_obj["sculpt_layers_base"] = base_obj.name
    except Exception:
        pass
    # Never let a layer carry the Base flag (duplication can copy custom props)
    unmark_as_base(layer_obj)
    # Always strip inherited base data after duplication
    clear_object_sculpt_layers(layer_obj)


def find_base_for_layer_object(obj):
    """Find the Base mesh that owns this layer / Layer_00 object."""
    if obj is None:
        return None
    # Fast path: custom property
    try:
        base_name = obj.get("sculpt_layers_base", None)
        if base_name:
            base = bpy.data.objects.get(base_name)
            if base is not None and is_registered_base(base):
                return base
    except Exception:
        pass
    # Slow path: search all registered bases' layer lists
    for candidate in bpy.data.objects:
        if candidate.type != 'MESH':
            continue
        if not is_registered_base(candidate):
            continue
        settings = get_object_settings(candidate)
        if settings is None or len(settings.layers) == 0:
            continue
        for item in settings.layers:
            if item.object == obj:
                return candidate
    return None


def get_settings_for_context(context):
    """
    Resolve (base_object, settings) from the current viewport selection.
    READ-ONLY — must never mutate scene/selection/properties (panel draw safe).

    - Selecting a Base mesh → that base's layers
    - Selecting a layer / Layer_00 → owner base's layers
    - Otherwise (selected_mesh, None) if not registered
    """
    obj = get_active_mesh(context)
    if obj is None:
        return None, None

    # Fast paths using custom props only (no collection scans)
    try:
        if obj.get(BASE_FLAG):
            return obj, get_object_settings(obj)
    except Exception:
        pass
    try:
        owner = obj.get("sculpt_layers_base")
        if owner:
            base = bpy.data.objects.get(owner)
            if base is not None:
                return base, get_object_settings(base)
            return obj, None
    except Exception:
        pass

    # Slower fallbacks
    if is_sculpt_layer_object(obj):
        base = find_base_for_layer_object(obj)
        if base is not None:
            return base, get_object_settings(base)
        return obj, None

    if is_registered_base(obj):
        return obj, get_object_settings(obj)

    base = find_base_for_layer_object(obj)
    if base is not None:
        return base, get_object_settings(base)

    return obj, None


def _tag_view3d_redraw():
    """Force 3D View / sidebar UIList to refresh after list index change."""
    try:
        wm = bpy.context.window_manager
        if wm is None:
            return
        for window in wm.windows:
            screen = window.screen
            if screen is None:
                continue
            for area in screen.areas:
                if area.type in {'VIEW_3D', 'PROPERTIES'}:
                    area.tag_redraw()
    except Exception:
        pass


def sync_list_selection_from_viewport(context=None, settings=None, active_obj=None):
    """
    When user selects a Base/layer in the viewport, highlight that row in the list.
    Cheap: pointer equality only, skip if same object as last sync.
    """
    global _is_updating, _last_list_sync_ptr
    if _is_updating:
        return

    if context is None:
        try:
            context = bpy.context
        except Exception:
            return

    if active_obj is None:
        try:
            active_obj = context.view_layer.objects.active
        except Exception:
            active_obj = None
        if active_obj is None or getattr(active_obj, "type", None) != 'MESH':
            active_obj = get_active_mesh(context)
    if active_obj is None or getattr(active_obj, "type", None) != 'MESH':
        return

    try:
        ptr = active_obj.as_pointer()
    except Exception:
        return
    if ptr == _last_list_sync_ptr:
        return

    if settings is None:
        # Resolve owner Base settings from the active mesh (layer or base)
        base, settings = get_settings_for_context(context)
        if settings is None:
            # Explicit owner prop on layer objects
            try:
                owner_name = active_obj.get("sculpt_layers_base")
                if owner_name:
                    base = bpy.data.objects.get(owner_name)
                    if base is not None:
                        settings = get_object_settings(base)
            except Exception:
                pass
        if settings is None and is_registered_base(active_obj):
            settings = get_object_settings(active_obj)
        if settings is None:
            base = find_base_for_layer_object(active_obj)
            if base is not None:
                settings = get_object_settings(base)
        if settings is None:
            return

    try:
        for i, item in enumerate(settings.layers):
            ref = item.object
            if ref is None:
                continue
            try:
                if ref.as_pointer() != ptr:
                    continue
            except (ReferenceError, Exception):
                try:
                    if ref != active_obj:
                        continue
                except Exception:
                    continue
            _last_list_sync_ptr = ptr
            if settings.active_index != i:
                _is_updating = True
                try:
                    settings.active_index = i
                finally:
                    _is_updating = False
                _tag_view3d_redraw()
            return
    except Exception:
        pass


def _msgbus_active_object_changed(*args):
    """RNA msgbus: view-layer active object changed → sync list highlight."""
    try:
        active = None
        try:
            active = bpy.context.view_layer.objects.active
        except Exception:
            pass
        if active is not None and getattr(active, "type", None) == 'MESH':
            sync_list_selection_from_viewport(active_obj=active)
        else:
            sync_list_selection_from_viewport()
    except Exception:
        pass


def _register_msgbus():
    """Subscribe to active object changes (viewport selection)."""
    global _msgbus_owner
    try:
        bpy.msgbus.clear_by_owner(_msgbus_owner)
    except Exception:
        pass
    # Prefer LayerObjects.active; fall back to Object name if needed
    subscribed = False
    try:
        bpy.msgbus.subscribe_rna(
            key=(bpy.types.LayerObjects, "active"),
            owner=_msgbus_owner,
            args=(),
            notify=_msgbus_active_object_changed,
        )
        subscribed = True
    except Exception:
        pass
    if not subscribed:
        try:
            bpy.msgbus.subscribe_rna(
                key=(bpy.types.Object, "name"),
                owner=_msgbus_owner,
                args=(),
                notify=_msgbus_active_object_changed,
            )
        except Exception:
            pass


def _unregister_msgbus():
    try:
        bpy.msgbus.clear_by_owner(_msgbus_owner)
    except Exception:
        pass


def iter_registered_bases():
    """Yield (base_obj, settings) for every mesh that is a registered Base.

    Fast path: only objects with the BASE_FLAG custom property (O(n) cheap checks).
    Avoids scanning every mesh's layer list on each call.
    """
    for obj in bpy.data.objects:
        if obj.type != 'MESH':
            continue
        try:
            if not obj.get(BASE_FLAG):
                continue
        except Exception:
            continue
        settings = get_object_settings(obj)
        if settings is not None:
            yield obj, settings


def ensure_multires(obj):
    for mod in obj.modifiers:
        if mod.type == 'MULTIRES':
            return mod
    return obj.modifiers.new(name="Multires", type='MULTIRES')


def get_multires(obj):
    if obj is None:
        return None
    mods = obj.modifiers
    for mod in mods:
        if mod.type == 'MULTIRES':
            return mod
    return None




def _snapshot_multires_levels(objects):
    """Save (levels, sculpt_levels, render_levels) per object name."""
    out = {}
    for obj in objects:
        if obj is None:
            continue
        try:
            mod = get_multires(obj)
            if mod is None:
                continue
            out[obj.name] = (
                int(mod.levels),
                int(mod.sculpt_levels),
                int(mod.render_levels),
            )
        except Exception:
            pass
    return out


def _restore_multires_levels(snapshot):
    """Restore Multires viewport/sculpt/render levels from snapshot."""
    if not snapshot:
        return
    for name, vals in snapshot.items():
        obj = bpy.data.objects.get(name)
        if obj is None:
            continue
        mod = get_multires(obj)
        if mod is None:
            continue
        try:
            lv, sc, rn = vals
            mod.levels = lv
            mod.sculpt_levels = sc
            mod.render_levels = rn
        except Exception:
            pass




def _swap_object_locations(obj_a, obj_b):
    """Swap world transforms of two layer objects."""
    if obj_a is None or obj_b is None:
        return
    if not _object_alive(obj_a) or not _object_alive(obj_b):
        return
    if obj_a == obj_b:
        return
    try:
        mat_a = obj_a.matrix_world.copy()
        mat_b = obj_b.matrix_world.copy()
        obj_a.matrix_world = mat_b
        obj_b.matrix_world = mat_a
    except Exception:
        try:
            loc_a = obj_a.location.copy()
            loc_b = obj_b.location.copy()
            obj_a.location = loc_b
            obj_b.location = loc_a
        except Exception:
            pass


def _zero_and_swap_layer_locations(obj_a, obj_b):
    """Zero Multires viewport on both, swap locations, return snaps to restore."""
    snap_a = _temp_zero_viewport_levels(obj_a)
    snap_b = _temp_zero_viewport_levels(obj_b)
    _swap_object_locations(obj_a, obj_b)
    return snap_a, snap_b

def _temp_zero_viewport_levels(obj):
    """Set Multires viewport levels to 0; return snapshot to restore later."""
    if obj is None:
        return None
    mod = get_multires(obj)
    if mod is None:
        return None
    snap = (int(mod.levels), int(mod.sculpt_levels), int(mod.render_levels))
    try:
        mod.levels = 0
    except Exception:
        pass
    return snap


def _restore_viewport_levels_snap(obj, snap):
    if obj is None or snap is None:
        return
    mod = get_multires(obj)
    if mod is None:
        return
    try:
        lv, sc, rn = snap
        mod.levels = lv
        mod.sculpt_levels = sc
        mod.render_levels = rn
    except Exception:
        pass

def _reselect_layer_keep_panel(context, layer_obj, index=None, settings=None):
    """Force-select layer and keep list index so the sidebar stays visible."""
    global _is_updating, _pending_list_select_name, _last_list_sync_ptr
    _pending_list_select_name = None
    if layer_obj is None or not _object_alive(layer_obj):
        return
    _is_updating = True
    try:
        if context.mode != 'OBJECT':
            try:
                bpy.ops.object.mode_set(mode='OBJECT')
            except Exception:
                pass
        try:
            for o in list(context.selected_objects):
                if o != layer_obj:
                    o.select_set(False)
        except Exception:
            pass
        try:
            layer_obj.hide_select = False
            if layer_obj.hide_viewport:
                layer_obj.hide_viewport = False
            if layer_obj.hide_get():
                layer_obj.hide_set(False)
        except Exception:
            pass
        layer_obj.select_set(True)
        context.view_layer.objects.active = layer_obj
        try:
            _last_list_sync_ptr = layer_obj.as_pointer()
        except Exception:
            pass
        if settings is not None and index is not None:
            if 0 <= index < len(settings.layers):
                settings.active_index = index
    except Exception:
        pass
    finally:
        _is_updating = False

def _set_multires_levels(mod, highest, viewport_max=MULTIRES_VIEWPORT_MAX, base_viewport=None):
    """Set Multires levels: sculpt/render at full highest; viewport capped.

    Viewport = min(highest, viewport_max, base_viewport if given).
    If Base viewport is below the cap (e.g. 2), duplicates use that lower value.
    """
    if mod is None or highest <= 0:
        return
    highest = int(highest)
    vp = min(highest, int(viewport_max))
    if base_viewport is not None:
        try:
            bv = int(base_viewport)
            if bv >= 0:
                vp = min(vp, bv)
        except Exception:
            pass
    mod.levels = vp
    mod.sculpt_levels = highest
    mod.render_levels = highest

def remove_multires(obj):
    if obj is None:
        return
    mods = obj.modifiers
    for m in list(mods):
        if m.type == 'MULTIRES':
            mods.remove(m)


def get_or_create_collection(name=COLLECTION_NAME):
    col = bpy.data.collections.get(name)
    if col is None:
        col = bpy.data.collections.new(name)
        bpy.context.scene.collection.children.link(col)
    return col


def move_to_collection(obj, col):
    if obj is None:
        return
    for c in list(obj.users_collection):
        c.objects.unlink(obj)
    if obj.name not in col.objects:
        col.objects.link(obj)


def find_item_by_flag(settings, is_base=False, is_shell_layer=False, is_layer00=False):
    for item in settings.layers:
        if is_base and item.is_base and _object_alive(item.object):
            return item
        if is_shell_layer and item.is_shell_layer and _object_alive(item.object):
            return item
        if is_layer00 and getattr(item, "is_layer00", False) and _object_alive(item.object):
            return item
    return None


def find_item_by_object(settings, obj):
    if not _object_alive(obj):
        return None
    for i, item in enumerate(settings.layers):
        if item.object == obj:
            return i, item
    return None


def ensure_basis_shape_key(obj):
    if obj.data.shape_keys is None:
        obj.shape_key_add(name="Basis", from_mix=False)
    return obj.data.shape_keys.key_blocks


def delete_shape_key_by_name(obj, sk_name):
    """Remove the exact shape key name, plus any Blender auto-duplicates (name.001, name.002, …)."""
    if obj is None or not sk_name:
        return
    sks = obj.data.shape_keys
    if sks is None:
        return
    keys = sks.key_blocks
    # Collect names first — removing while iterating the collection is unsafe
    prefix = sk_name + "."
    to_remove = []
    for kb in keys:
        n = kb.name
        if n == sk_name:
            to_remove.append(n)
        elif n.startswith(prefix) and n[len(prefix):].isdigit():
            to_remove.append(n)
    for name in to_remove:
        kb = keys.get(name)
        if kb is not None:
            obj.shape_key_remove(kb)


def _object_alive(obj):
    """Return True only if the Blender object still exists in bpy.data.objects."""
    if obj is None:
        return False
    try:
        # Single dict lookup (faster than `in` + index)
        return bpy.data.objects.get(obj.name) is obj
    except (ReferenceError, AttributeError):
        return False


def _layer_entry_missing(item):
    """
    True if this list entry's object was deleted (viewport Del / outliner).

    Important: item.name is a UI label for Base ("Base") and Layer_00 ("Layer_00"),
    not a bpy.data.objects key. Only item.object (PointerProperty) decides liveness.
    """
    if item is None:
        return True
    obj = item.object
    if obj is None:
        return True
    return not _object_alive(obj)


def _pick_list_index_after_remove(settings, removed_indices):
    """Choose a new active_index after deletions so the panel stays usable."""
    n = len(settings.layers)
    if n == 0:
        return 0
    prefer = min(removed_indices) if removed_indices else 0
    return max(0, min(prefer, n - 1))


def _deferred_select_object():
    """Timer callback: select Base/layer next frame after Del-key delete."""
    global _pending_select_name, _select_timer_registered, _is_updating
    _select_timer_registered = False
    name = _pending_select_name
    _pending_select_name = None
    if not name:
        return None
    obj = bpy.data.objects.get(name)
    if obj is None:
        return None
    try:
        ctx = bpy.context
        # Best-effort mode switch
        try:
            if getattr(ctx, "mode", "OBJECT") != "OBJECT":
                bpy.ops.object.mode_set(mode="OBJECT")
        except Exception:
            pass
        vl = ctx.view_layer
        for o in list(getattr(ctx, "selected_objects", []) or []):
            try:
                o.select_set(False)
            except Exception:
                pass
        obj.hide_viewport = False
        try:
            obj.hide_set(False)
        except Exception:
            pass
        obj.hide_select = False
        obj.select_set(True)
        vl.objects.active = obj
        # Tag UI redraw
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                if area.type == "VIEW_3D":
                    area.tag_redraw()
    except Exception:
        pass
    return None  # do not repeat


def _schedule_select_object(obj):
    """Queue viewport selection for next frame (safe after depsgraph/Del)."""
    global _pending_select_name, _select_timer_registered
    if obj is None or not _object_alive(obj):
        return
    try:
        _pending_select_name = obj.name
    except Exception:
        return
    if not _select_timer_registered:
        _select_timer_registered = True
        try:
            bpy.app.timers.register(_deferred_select_object, first_interval=0.01)
        except Exception:
            _select_timer_registered = False
            # Fallback: try immediate
            _deferred_select_object()


def _ensure_panel_context_after_layer_remove(context, settings, base_obj, removed_indices):
    """
    After layer(s) removed from the list:
    - Select another list row
    - Defer selecting Base (or remaining layer) in the viewport so the sidebar
      still resolves this mesh's layers after Del-key delete.
    """
    global _is_updating
    if settings is None or len(settings.layers) == 0:
        return

    new_idx = _pick_list_index_after_remove(settings, removed_indices)
    _is_updating = True
    try:
        settings.active_index = new_idx
    finally:
        _is_updating = False

    # Prefer Base so get_settings_for_context always resolves
    target = None
    if base_obj is not None and _object_alive(base_obj):
        target = base_obj
    else:
        for item in settings.layers:
            if item.is_base and _object_alive(item.object):
                target = item.object
                break
        if target is None:
            for item in settings.layers:
                if not item.is_shell_layer and _object_alive(item.object):
                    target = item.object
                    break
        if target is None:
            for item in settings.layers:
                if _object_alive(item.object):
                    target = item.object
                    break

    if target is None:
        return
    # Immediate attempt + deferred (Del-key path needs the timer)
    try:
        if context is not None:
            for o in list(getattr(context, "selected_objects", []) or []):
                try:
                    o.select_set(False)
                except Exception:
                    pass
            target.hide_viewport = False
            target.select_set(True)
            context.view_layer.objects.active = target
    except Exception:
        pass
    _schedule_select_object(target)


def reset_addon(settings, delete_layer_objects=True, base_obj=None):
    """
    Clear one base's layer list. Optionally delete that base's Layer_00 + sculpt layers.
    Does NOT touch other bases' data.
    """
    if delete_layer_objects:
        for item in list(settings.layers):
            if item.is_base:
                continue
            obj = item.object
            if _object_alive(obj):
                try:
                    bpy.data.objects.remove(obj, do_unlink=True)
                except (ReferenceError, RuntimeError):
                    pass
        if base_obj is not None:
            _delete_shell_layer_for_base(base_obj)
            _delete_layer00_for_base(base_obj)

    settings.layers.clear()
    settings.active_index = 0
    settings.base_vertex_count = 0
    settings.base_multires_level = 0
    if base_obj is not None:
        unmark_as_base(base_obj)


def _delete_shell_layer_for_base(base_obj):
    """Delete only the Layer_00 that belongs to this base."""
    if base_obj is None:
        return
    target_name = shell_layer_name_for_base(base_obj)
    for obj in list(bpy.data.objects):
        try:
            if obj.name == target_name or obj.name.startswith(target_name + "."):
                bpy.data.objects.remove(obj, do_unlink=True)
                continue
            # Also remove by ownership tag
            if obj.get("sculpt_layers_base") == base_obj.name:
                # Only delete if it looks like a Layer_00
                settings = get_object_settings(base_obj)
                if settings:
                    for item in settings.layers:
                        if item.is_shell_layer and item.object == obj:
                            bpy.data.objects.remove(obj, do_unlink=True)
                            break
        except (ReferenceError, RuntimeError, KeyError):
            pass


def layer_00_name_for_base(base_obj):
    """Object name for Layer_00 (Multires template / is_layer00) per Base."""
    return f"{base_obj.name}_L00"


def _delete_layer00_for_base(base_obj):
    """Delete any Layer Shell objects owned by this base."""
    if base_obj is None:
        return
    name = layer_00_name_for_base(base_obj)
    for obj in list(bpy.data.objects):
        try:
            if obj.name == name or obj.name.startswith(name + "."):
                if obj.get("sculpt_layers_base") == base_obj.name or obj.name.startswith(base_obj.name):
                    bpy.data.objects.remove(obj, do_unlink=True)
        except Exception:
            pass
    # Also by owner tag
    for obj in list(bpy.data.objects):
        try:
            if obj.get("sculpt_layers_is_layer00") and obj.get("sculpt_layers_base") == base_obj.name:
                bpy.data.objects.remove(obj, do_unlink=True)
        except Exception:
            pass


def create_layer00_from_base(base_obj, settings, col, target_level=None):
    """
    Create Layer Shell on first Add Layer by plain-duplicating Base Multires.
    Keeps Multires as-is (same levels, no displacement erase, no rebuild).
    """
    for i in range(len(settings.layers) - 1, -1, -1):
        item = settings.layers[i]
        if getattr(item, "is_layer00", False):
            if item.object:
                try:
                    bpy.data.objects.remove(item.object, do_unlink=True)
                except Exception:
                    pass
            settings.layers.remove(i)
    _delete_layer00_for_base(base_obj)

    if bpy.context.mode != 'OBJECT':
        try:
            bpy.ops.object.mode_set(mode='OBJECT')
        except Exception:
            pass

    # Remember Base Multires levels, drop viewport to 0 for a cheap duplicate
    base_mod = get_multires(base_obj)
    highest = _multires_highest_level(base_mod) if base_mod else 0
    if target_level is not None and int(target_level) > highest:
        highest = int(target_level)
    base_vp = int(base_mod.levels) if base_mod else highest
    base_level_snap = _snapshot_multires_levels([base_obj])
    if base_mod is not None:
        try:
            base_mod.levels = 0
        except Exception:
            pass

    bpy.ops.object.select_all(action='DESELECT')
    base_obj.hide_set(False)
    base_obj.hide_viewport = False
    base_obj.hide_select = False
    base_obj.select_set(True)
    bpy.context.view_layer.objects.active = base_obj
    bpy.ops.object.duplicate(linked=False)

    # Restore Base Multires levels immediately after duplicate
    _restore_multires_levels(base_level_snap)

    shell = bpy.context.active_object
    shell.name = layer_00_name_for_base(base_obj)
    mark_layer_owner(shell, base_obj)
    try:
        shell["sculpt_layers_is_layer00"] = True
    except Exception:
        pass

    # Shell Multires: full grids; viewport capped / match Base viewport policy
    layer_mod = get_multires(shell)
    if layer_mod is None:
        layer_mod = ensure_multires(shell)
    if layer_mod and highest > 0:
        _set_multires_levels(layer_mod, highest, base_viewport=base_vp)
    real = _multires_highest_level(layer_mod) if layer_mod else highest
    if real > 0:
        settings.base_multires_level = real

    if shell.data.shape_keys:
        shell.shape_key_clear()

    shell.matrix_world = base_obj.matrix_world.copy()
    move_to_collection(shell, col)

    shell.hide_render = True
    shell.hide_viewport = True
    shell.hide_set(True)
    shell.hide_select = True

    item = settings.layers.add()
    item.name = LAYER_00_NAME
    item.object = shell
    item.is_base = False
    item.is_shell_layer = False
    item.is_layer00 = True
    item.is_selected = False
    item.strength = 1.0
    return shell


def _multires_highest_level(mod):
    """Return the highest subdivision level available on a Multires modifier."""
    if mod is None:
        return 0
    return int(getattr(mod, "total_levels", 0) or getattr(mod, "levels", 0) or 0)


def _strip_to_vertices_only(obj):
    """Remove faces and edges; keep vertices only (coordinates preserved)."""
    if obj is None or obj.type != 'MESH':
        return
    mesh = obj.data
    if mesh is None:
        return
    n = len(mesh.vertices)
    if n == 0:
        return
    if len(mesh.polygons) == 0 and len(mesh.edges) == 0:
        return
    try:
        flat = [0.0] * (n * 3)
        mesh.vertices.foreach_get("co", flat)
        verts = [(flat[i], flat[i + 1], flat[i + 2]) for i in range(0, n * 3, 3)]
        mesh.clear_geometry()
        mesh.from_pydata(verts, [], [])
        mesh.update()
    except Exception as e:
        print(f"Sculpt Layers: strip Shell Layer to verts failed: {e}")


def _bake_shell_layer_mesh_from_base(base_obj, target_level):
    """
    Fast Layer_00 geometry: evaluate Base at full Multires once and bake
    to a real mesh (no duplicate → subdivide → apply loop).
    Temporarily raises Multires preview levels, then restores them.
    Returns a new Mesh datablock with verts > 0, or None.
    """
    if base_obj is None or base_obj.type != 'MESH':
        return None
    base_mod = get_multires(base_obj)
    saved = None
    if base_mod is not None and target_level > 0:
        saved = (base_mod.levels, base_mod.sculpt_levels, base_mod.render_levels)
        try:
            base_mod.levels = target_level
            base_mod.sculpt_levels = target_level
            base_mod.render_levels = target_level
        except Exception:
            saved = None
    mesh = None
    try:
        bpy.context.view_layer.update()
        depsgraph = bpy.context.evaluated_depsgraph_get()
        eval_obj = base_obj.evaluated_get(depsgraph)
        mesh = bpy.data.meshes.new_from_object(
            eval_obj, preserve_all_data_layers=False, depsgraph=depsgraph
        )
        if mesh is not None and len(mesh.vertices) == 0:
            bpy.data.meshes.remove(mesh)
            mesh = None
    except Exception as e:
        print(f"Sculpt Layers: Shell Layer bake failed: {e}")
        if mesh is not None:
            try:
                bpy.data.meshes.remove(mesh)
            except Exception:
                pass
        mesh = None
    finally:
        if saved is not None and base_mod is not None:
            try:
                base_mod.levels, base_mod.sculpt_levels, base_mod.render_levels = saved
            except Exception:
                pass
    return mesh


def create_shell_layer_from_base(base_obj, settings, col):
    """
    Ensure exactly ONE Layer_00 exists.

    Fast path: bake Base evaluated Multires → strip to vertices only.
    (Cannot remove faces before Multires apply — Multires needs faces.)
    Transform locked to Base.
    """
    global _is_updating
    _is_updating = True
    try:
        for i in range(len(settings.layers) - 1, -1, -1):
            item = settings.layers[i]
            if item.is_shell_layer:
                if item.object:
                    try:
                        bpy.data.objects.remove(item.object, do_unlink=True)
                    except ReferenceError:
                        pass
                settings.layers.remove(i)

        _delete_shell_layer_for_base(base_obj)

        if bpy.context.mode != 'OBJECT':
            try:
                bpy.ops.object.mode_set(mode='OBJECT')
            except Exception:
                pass

        base_mod = get_multires(base_obj)
        target = _multires_highest_level(base_mod) if base_mod else 0

        layer0 = None
        baked = _bake_shell_layer_mesh_from_base(base_obj, target)
        if baked is not None and len(baked.vertices) > 0:
            l0_name = shell_layer_name_for_base(base_obj)
            baked.name = l0_name + "_mesh"
            layer0 = bpy.data.objects.new(l0_name, baked)
            layer0.matrix_world = base_obj.matrix_world.copy()
            try:
                bpy.context.scene.collection.objects.link(layer0)
            except Exception:
                pass
            mark_layer_owner(layer0, base_obj)
            _strip_to_vertices_only(layer0)
            if len(layer0.data.vertices) == 0:
                try:
                    bpy.data.objects.remove(layer0, do_unlink=True)
                except Exception:
                    pass
                layer0 = None

        if layer0 is None:
            # Fallback: viewport 0 → duplicate → restore Base → Multires apply
            base_level_snap = _snapshot_multires_levels([base_obj])
            if base_mod is not None:
                try:
                    base_mod.levels = 0
                except Exception:
                    pass
            bpy.ops.object.select_all(action='DESELECT')
            base_obj.hide_set(False)
            base_obj.hide_viewport = False
            base_obj.hide_select = False
            base_obj.select_set(True)
            bpy.context.view_layer.objects.active = base_obj
            bpy.ops.object.duplicate(linked=False)
            _restore_multires_levels(base_level_snap)
            layer0 = bpy.context.active_object
            layer0.name = shell_layer_name_for_base(base_obj)
            mark_layer_owner(layer0, base_obj)
            layer0.matrix_world = base_obj.matrix_world.copy()
            layer0_mod = ensure_multires(layer0)
            if base_mod and target > 0:
                while _multires_highest_level(layer0_mod) < target:
                    try:
                        bpy.ops.object.multires_subdivide(modifier=layer0_mod.name)
                    except Exception:
                        break
                    layer0_mod = get_multires(layer0)
                    if layer0_mod is None:
                        break
                if layer0_mod:
                    layer0_mod.levels = target
                    layer0_mod.sculpt_levels = target
                    layer0_mod.render_levels = target
            layer0_mod = get_multires(layer0)
            if layer0_mod:
                try:
                    bpy.ops.object.modifier_apply(modifier=layer0_mod.name)
                except Exception as e:
                    print(f"Sculpt Layers: could not apply Multires on Shell Layer: {e}")
            if layer0.data.shape_keys:
                layer0.shape_key_clear()
            _strip_to_vertices_only(layer0)

        if layer0.data.shape_keys:
            layer0.shape_key_clear()

        layer0.matrix_world = base_obj.matrix_world.copy()
        move_to_collection(layer0, col)

        layer0.hide_render = True
        layer0.hide_viewport = True
        layer0.hide_set(True)
        layer0.hide_select = False

        item = settings.layers.add()
        item.name = SHELL_LAYER_NAME
        item.object = layer0
        item.is_base = False
        item.is_shell_layer = True
        item.is_selected = False
        item.strength = 1.0

        settings.base_vertex_count = len(base_obj.data.vertices)
        settings.base_multires_level = target
        mark_as_base(base_obj)
        base_item = find_item_by_flag(settings, is_base=True)
        if base_item is not None:
            base_item.object = base_obj
            base_item.is_base = True
        pin_system_layers_in_list(settings)
        return layer0
    finally:
        _is_updating = False


def rebuild_shell_layer_if_needed(context):
    """
    Rebuild Layer_00 only if it already exists and Base topology changed.
    Does not create Layer_00 from scratch (that happens on first Add Layer).
    """
    base, settings = get_settings_for_context(context)
    if settings is None or base is None:
        return False
    base_item = find_item_by_flag(settings, is_base=True)
    if not base_item or not base_item.object:
        return False

    layer0_item = find_item_by_flag(settings, is_shell_layer=True)
    if layer0_item is None:
        return False

    base = base_item.object
    current_count = len(base.data.vertices)

    layer0_ok = (
        _object_alive(layer0_item.object)
        and current_count == settings.base_vertex_count
    )
    if layer0_ok:
        return False

    col = get_or_create_collection()
    create_shell_layer_from_base(base, settings, col)
    return True


def mesh_vertex_count(obj):
    """Safe base-mesh vertex count (ignores Multires / modifiers)."""
    if obj is None or obj.type != 'MESH' or obj.data is None:
        return -1
    return len(obj.data.vertices)


def evaluated_vertex_count(obj, context=None):
    """
    Vertex count of the evaluated mesh (Multires / modifiers applied).
    Layer_00 has Multires applied so data.vertices is already high-res —
    skip expensive to_mesh() in that case.
    Sculpt layers still have Multires → must use depsgraph evaluation.
    """
    if obj is None or obj.type != 'MESH':
        return -1
    # Fast path: no Multires (or Multires already applied) → base mesh is final
    if get_multires(obj) is None:
        return mesh_vertex_count(obj)
    try:
        depsgraph = (context or bpy.context).evaluated_depsgraph_get()
        eval_obj = obj.evaluated_get(depsgraph)
        mesh = eval_obj.to_mesh(preserve_all_data_layers=False, depsgraph=depsgraph)
        try:
            return len(mesh.vertices)
        finally:
            eval_obj.to_mesh_clear()
    except TypeError:
        # Older API signature
        try:
            depsgraph = (context or bpy.context).evaluated_depsgraph_get()
            eval_obj = obj.evaluated_get(depsgraph)
            mesh = eval_obj.to_mesh()
            try:
                return len(mesh.vertices)
            finally:
                eval_obj.to_mesh_clear()
        except Exception:
            return mesh_vertex_count(obj)
    except Exception:
        return mesh_vertex_count(obj)


def pin_layer0_in_list(settings):
    """Back-compat wrapper."""
    pin_system_layers_in_list(settings)


def pin_system_layers_in_list(settings):
    """
    List order: Base → Shell Layer → Layer_00 → sculpt layers.
    """
    def _find(flag):
        for i, item in enumerate(settings.layers):
            if flag == "base" and item.is_base:
                return i
            if flag == "shell_layer" and item.is_shell_layer:
                return i
            if flag == "layer00" and getattr(item, "is_layer00", False):
                return i
        return None

    # Move Base to 0
    bi = _find("base")
    if bi is not None and bi != 0:
        settings.layers.move(bi, 0)

    # Shell Layer to 1 (or 0 if no base)
    bi = _find("base")
    l0 = _find("shell_layer")
    target_l0 = 1 if bi is not None else 0
    if l0 is not None and l0 != target_l0:
        settings.layers.move(l0, target_l0)

    # Layer_00 template to right after Shell Layer (or after Base, or 0)
    bi = _find("base")
    l0 = _find("shell_layer")
    sh = _find("layer00")
    if sh is None:
        return
    if l0 is not None:
        target_sh = l0 + 1 if l0 < sh or True else l0 + 1
        # re-find after moves
        l0 = _find("shell_layer")
        sh = _find("layer00")
        target_sh = (l0 + 1) if l0 is not None else (1 if bi is not None else 0)
    elif bi is not None:
        target_sh = 1
    else:
        target_sh = 0
    sh = _find("layer00")
    if sh is not None and sh != target_sh:
        settings.layers.move(sh, target_sh)


def _shape_key_index(layer0, sk_name):
    """Return index of shape key by name, or -1."""
    if layer0 is None or layer0.data.shape_keys is None:
        return -1
    keys = layer0.data.shape_keys.key_blocks
    return keys.find(sk_name)


def move_shape_key_to_index(context, layer0, sk_name, target_index):
    """
    Move shape key named sk_name so it ends at target_index in key_blocks.
    Basis (index 0) is never moved. target_index is the desired final index.
    """
    if layer0 is None or not sk_name:
        return False
    if layer0.data.shape_keys is None:
        return False
    keys = layer0.data.shape_keys.key_blocks
    cur = keys.find(sk_name)
    if cur < 0:
        return False  # no shape key yet for this layer (not applied)
    # Never move above Basis
    target_index = max(1, min(target_index, len(keys) - 1))
    if cur == target_index:
        return True

    # Make Layer_00 active for the operator.
    # It may need to be temporarily visible for shape_key_move, but its
    # visibility state must be restored afterwards (Layer_00 is normally pinned/hidden).
    was_active = context.view_layer.objects.active
    was_mode = context.mode
    was_hide_viewport = bool(layer0.hide_viewport)
    try:
        was_hide_set = bool(layer0.hide_get())
    except Exception:
        was_hide_set = was_hide_viewport
    try:
        if context.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        layer0.hide_viewport = False
        layer0.hide_set(False)
        context.view_layer.objects.active = layer0

        # Move step by step
        while cur > target_index:
            layer0.active_shape_key_index = cur
            bpy.ops.object.shape_key_move(type='UP')
            cur -= 1
        while cur < target_index:
            layer0.active_shape_key_index = cur
            bpy.ops.object.shape_key_move(type='DOWN')
            cur += 1
        return True
    except Exception:
        return False
    finally:
        try:
            if was_active is not None:
                context.view_layer.objects.active = was_active
            # Restore Layer_00 visibility exactly as it was before reordering.
            layer0.hide_viewport = was_hide_viewport
            layer0.hide_set(was_hide_set)
            if was_mode and was_mode != 'OBJECT':
                try:
                    bpy.ops.object.mode_set(mode=was_mode)
                except Exception:
                    pass
        except Exception:
            pass



def _sync_shell_layer_from_list(context, settings, shell_layer, base):
    """
    Push list order + strength values onto Shell Layer shape keys,
    then Multires-Reshape Base from Shell Layer.
    Returns (ordered_keys, reshaped).
    """
    if shell_layer is None or not _object_alive(shell_layer):
        return 0, False
    # 1) Order shape keys to match list
    try:
        sync_shape_key_order_to_layers(context, settings, shell_layer)
    except Exception as e:
        print(f"Sculpt Layers: shape-key order sync failed: {e}")

    # 2) Strength values (hidden layers → 0 so they do not affect reshape)
    n = 0
    try:
        sks = shell_layer.data.shape_keys
        if sks is not None:
            keys = sks.key_blocks
            for item in settings.layers:
                if item.is_base or item.is_shell_layer or getattr(item, "is_layer00", False):
                    continue
                obj = item.object
                if obj is None or not _object_alive(obj):
                    continue
                kb = keys.get(obj.name)
                if kb is None:
                    continue
                try:
                    hidden = bool(obj.hide_get() or obj.hide_viewport)
                except Exception:
                    hidden = False
                try:
                    kb.value = 0.0 if hidden else float(item.strength)
                    n += 1
                except Exception:
                    pass
    except Exception as e:
        print(f"Sculpt Layers: strength sync failed: {e}")

    # 3) Reshape Base from Shell Layer
    reshaped = False
    if base is not None and _object_alive(base):
        mod = get_multires(base)
        if mod is not None:
            try:
                _reshape_base_from_shell_layer(context, base, shell_layer, mod)
                reshaped = True
            except Exception as e:
                print(f"Sculpt Layers: refresh reshape failed: {e}")
    return n, reshaped


def sync_shape_key_order_to_layers(context, settings, layer0):
    """
    Reorder Layer_00 shape keys to match sculpt-layer list order
    (after Basis). Called after a layer list move.
    """
    if layer0 is None or layer0.data.shape_keys is None:
        return
    keys = layer0.data.shape_keys.key_blocks
    # Desired order: Basis stays 0; then each sculpt layer's key in list order
    sculpt_items = [
        item for item in settings.layers
        if (
            not item.is_base
            and not item.is_shell_layer
            and not getattr(item, "is_layer00", False)
            and item.object
        )
    ]
    # Assign consecutive indices starting at 1
    desired = []
    for item in sculpt_items:
        sk_name = item.object.name
        if keys.find(sk_name) >= 0:
            desired.append(sk_name)

    # Move each key into place from top to bottom
    for i, sk_name in enumerate(desired):
        target = 1 + i
        move_shape_key_to_index(context, layer0, sk_name, target)




def _object_in_any_layer_list(obj):
    """True if any registered Base lists this object as a layer/Base/Shell/L0."""
    if obj is None:
        return False
    try:
        for _base, settings in iter_registered_bases():
            if settings is None:
                continue
            for item in settings.layers:
                ref = item.object
                if ref is None:
                    continue
                try:
                    if ref.as_pointer() == obj.as_pointer():
                        return True
                except Exception:
                    try:
                        if ref == obj:
                            return True
                    except Exception:
                        pass
    except Exception:
        pass
    return False


def _unlink_from_sculpt_layers_collection(obj):
    """Remove object from the addon collection; keep it in the scene."""
    if obj is None:
        return
    try:
        col = bpy.data.collections.get(COLLECTION_NAME)
        if col is not None and obj.name in col.objects:
            col.objects.unlink(obj)
    except Exception:
        pass
    # Ensure the object stays visible in the scene (not orphaned from all collections)
    try:
        if len(obj.users_collection) == 0:
            scene = bpy.context.scene
            if scene is not None:
                scene.collection.objects.link(obj)
    except Exception:
        pass


def _sanitize_duplicated_layer(obj):
    """
    Viewport-duplicated sculpt layers inherit sculpt_layers_base / shell tags
    and stay in the Sculpt LAYERS collection, but they are NOT entries in any
    Base's layer list. Strip tags, clear copied settings, and move them out of
    the addon collection so they behave like ordinary meshes (same idea as
    sanitizing a duplicated Base).
    """
    if obj is None or getattr(obj, "type", None) != 'MESH':
        return False

    has_layer_tag = False
    try:
        if obj.get("sculpt_layers_base") or obj.get("sculpt_layers_is_layer00"):
            has_layer_tag = True
    except Exception:
        return False

    if not has_layer_tag:
        return False

    # Real layers / Shell / Layer_00 are always listed on their Base
    if _object_in_any_layer_list(obj):
        return False

    # Orphaned duplicate — strip all addon state
    clear_object_sculpt_layers(obj)
    unmark_as_base(obj)
    try:
        if "sculpt_layers_base" in obj:
            del obj["sculpt_layers_base"]
    except Exception:
        pass
    try:
        if "sculpt_layers_is_layer00" in obj:
            del obj["sculpt_layers_is_layer00"]
    except Exception:
        pass
    _unlink_from_sculpt_layers_collection(obj)
    return True


def _sanitize_duplicated_base_registration(obj):
    """
    Detect a Base object that was duplicated by Blender and inherited the
    original Base's sculpt-layer PropertyGroup/custom flag.

    Blender duplicates custom properties and per-object PropertyGroups. When
    only the Base is duplicated, the copied `sculpt_layers` collection can
    still point at the ORIGINAL Base's Layer_00 / Shell / sculpt layers.
    Without cleaning that copied state, the duplicate appears to have the
    original Base's layer stack.

    A real Base has:
      - BASE_FLAG on itself
      - a Base list item whose object points to itself
      - layer objects owned by this same Base

    A duplicated Base normally fails the ownership checks above. In that
    case the duplicate becomes a normal, unregistered mesh while the
    original Base and all of its layers remain untouched.
    """
    if obj is None or obj.type != 'MESH':
        return False
    try:
        if not obj.get(BASE_FLAG):
            return False
    except Exception:
        return False

    settings = get_object_settings(obj)
    if settings is None or len(settings.layers) == 0:
        # With no copied layer stack there is not enough information to
        # distinguish a legitimate Base from an ordinary duplicate. Leave
        # it alone; the important case is a copied stack.
        return False

    base_item = find_item_by_flag(settings, is_base=True)

    # If the copied Base item still points to the original Base, this is
    # definitely copied state. If it points to the duplicate, inspect the
    # layer ownership below.
    if base_item is None or base_item.object is None:
        is_copy = True
    else:
        try:
            is_copy = base_item.object != obj
        except ReferenceError:
            is_copy = True

    if not is_copy:
        for item in settings.layers:
            if item.is_base or item.object is None:
                continue
            layer_obj = item.object
            if not _object_alive(layer_obj):
                continue
            try:
                owner_name = layer_obj.get("sculpt_layers_base")
            except Exception:
                owner_name = None
            # A copied Base inherits references to the original Base's
            # layers. Their owner tag therefore identifies the original.
            if owner_name and owner_name != obj.name:
                is_copy = True
                break

    if not is_copy:
        return False

    clear_object_sculpt_layers(obj)
    unmark_as_base(obj)
    try:
        if "sculpt_layers_base" in obj:
            del obj["sculpt_layers_base"]
    except Exception:
        pass
    return True


# ---------------------------------------------------------------------------
# Handlers – deletion only (selection is driven by the addon list)
# ---------------------------------------------------------------------------


@persistent
def sculptlayers_depsgraph_handler(scene, depsgraph=None):
    """Detect deleted layer objects and topology changes.

    Performance (behavior unchanged):
    - Pure Multires sculpt strokes only update MESH → skip dead-object scan.
    - Dead-object scan runs only when OBJECT datablocks change (Del / unlink).
    - Geometry rebuild is throttled.
    - Manual "Update List" still covers collection membership.
    """
    global _is_updating, _last_handler_time
    if _is_updating:
        return

    object_updated = True
    mesh_updated = True
    if depsgraph is not None:
        try:
            object_updated = bool(depsgraph.id_type_updated('OBJECT'))
            mesh_updated = bool(depsgraph.id_type_updated('MESH'))
        except Exception:
            object_updated = True
            mesh_updated = True
        if not object_updated and not mesh_updated:
            return

    # Throttle geometry rebuild (sculpt fires continuous MESH updates)
    do_geometry_check = False
    if mesh_updated:
        now = time.monotonic()
        if (now - _last_handler_time) >= _HANDLER_MIN_INTERVAL:
            _last_handler_time = now
            do_geometry_check = True

    # Nothing useful this frame (e.g. throttled mesh-only stroke)
    if not object_updated and not do_geometry_check:
        return

    # Viewport selection → list highlight (msgbus primary; depsgraph backup on OBJECT change)
    if object_updated:
        try:
            sync_list_selection_from_viewport()
        except Exception:
            pass

        # Blender duplicates a Base's custom properties and per-object
        # PropertyGroup. If the user duplicates only the Base, that copied
        # state can make the new object display the original layer stack.
        # Clean such copied registrations before resolving registered Bases.
        try:
            for candidate in list(bpy.data.objects):
                if candidate is None or getattr(candidate, "type", None) != 'MESH':
                    continue
                # Duplicated Base → strip copied layer stack / Base flag
                if _sanitize_duplicated_base_registration(candidate):
                    continue
                # Duplicated layer/Shell/L0 → strip tags, leave Sculpt LAYERS collection
                _sanitize_duplicated_layer(candidate)
        except Exception:
            pass

    geometry_changed_bases = set()
    if do_geometry_check and depsgraph is not None:
        try:
            for update in depsgraph.updates:
                id_data = update.id
                try:
                    id_data = id_data.original
                except Exception:
                    pass
                if not isinstance(id_data, bpy.types.Object):
                    continue
                if getattr(id_data, "type", None) != 'MESH':
                    continue
                if not getattr(update, "is_updated_geometry", False):
                    continue
                try:
                    if id_data.get(BASE_FLAG):
                        geometry_changed_bases.add(id_data.as_pointer())
                except Exception:
                    pass
        except Exception:
            pass

    # Skip expensive base walk: mesh-only frame with no flagged Base geometry
    if not object_updated and do_geometry_check and not geometry_changed_bases:
        return

    _is_updating = True
    try:
        bases = list(iter_registered_bases())
        if not bases:
            return

        for base_obj, settings in bases:
            if settings is None:
                continue
            n_layers = len(settings.layers)
            if n_layers == 0:
                continue

            # Multires modifier deleted straight from the modifier panel (not
            # via Unsubdivide) → stored level is stale, reset it to 0 so the
            # panel's "Level" readout matches reality.
            if object_updated and _object_alive(base_obj) and settings.base_multires_level != 0:
                try:
                    if get_multires(base_obj) is None:
                        settings.base_multires_level = 0
                except Exception:
                    pass

            to_remove = []  # (index, sk_name, is_shell_layer)
            base_deleted = False

            # Dead pointers + layers no longer in Sculpt LAYERS collection
            if object_updated:
                for i, item in enumerate(settings.layers):
                    if item.is_base:
                        if _layer_entry_missing(item):
                            base_deleted = True
                        continue
                    gone = _layer_entry_missing(item)
                    outside = False
                    if not gone and item.object is not None:
                        try:
                            outside = not object_in_sculpt_layers_collection(item.object)
                        except Exception:
                            outside = False
                    if not gone and not outside:
                        continue
                    # Object deleted in viewport OR moved out of addon collection
                    sk = item.name
                    try:
                        if item.object and _object_alive(item.object):
                            sk = item.object.name
                    except Exception:
                        pass
                    to_remove.append((i, sk, item.is_shell_layer, item.object if not gone else None))

                if base_deleted:
                    reset_addon(settings, delete_layer_objects=True, base_obj=base_obj)
                    continue

                if to_remove:
                    layer0_item = find_item_by_flag(settings, is_shell_layer=True)
                    layer0 = (
                        layer0_item.object
                        if layer0_item and _object_alive(layer0_item.object)
                        else None
                    )
                    base_item = find_item_by_flag(settings, is_base=True)
                    base = (
                        base_item.object
                        if base_item and _object_alive(base_item.object)
                        else base_obj
                    )
                    removed_applied_key = False
                    removed_indices = [t[0] for t in to_remove]

                    for entry in reversed(to_remove):
                        i, sk_name, is_l0 = entry[0], entry[1], entry[2]
                        orphan_obj = entry[3] if len(entry) > 3 else None
                        if not is_l0 and layer0 and sk_name:
                            try:
                                if (
                                    layer0.data.shape_keys
                                    and sk_name in layer0.data.shape_keys.key_blocks
                                ):
                                    removed_applied_key = True
                            except Exception:
                                pass
                            delete_shape_key_by_name(layer0, sk_name)
                        # Delete mesh if it left the collection (Del may already have removed it)
                        if orphan_obj is not None and _object_alive(orphan_obj):
                            try:
                                bpy.data.objects.remove(orphan_obj, do_unlink=True)
                            except Exception:
                                pass
                        if i < len(settings.layers):
                            settings.layers.remove(i)

                    if removed_applied_key and layer0 and base and _object_alive(base):
                        base_multires = get_multires(base)
                        if base_multires:
                            try:
                                _is_updating = False
                                _reshape_base_from_shell_layer(
                                    bpy.context, base, layer0, base_multires
                                )
                            except Exception:
                                pass
                            finally:
                                _is_updating = True

                    _is_updating = False
                    try:
                        _ensure_panel_context_after_layer_remove(
                            bpy.context, settings, base, removed_indices
                        )
                    except Exception:
                        pass
                    finally:
                        _is_updating = True

            if not do_geometry_check:
                continue

            # Topology rebuild only when this Base's geometry actually changed
            try:
                base_ptr = base_obj.as_pointer()
            except Exception:
                base_ptr = None
            if base_ptr is not None and base_ptr not in geometry_changed_bases:
                continue

            base_item = find_item_by_flag(settings, is_base=True)
            layer0_item = find_item_by_flag(settings, is_shell_layer=True)
            if (
                base_item and _object_alive(base_item.object)
                and layer0_item is not None
            ):
                current = mesh_vertex_count(base_item.object)
                if current != settings.base_vertex_count and current > 0:
                    col = get_or_create_collection()
                    _is_updating = False
                    create_shell_layer_from_base(base_item.object, settings, col)
                    _is_updating = True

    finally:
        _is_updating = False


# ---------------------------------------------------------------------------
# Operators
# ---------------------------------------------------------------------------
