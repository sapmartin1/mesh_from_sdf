# SPDX-License-Identifier: MIT
"""Application handlers keeping fusions consistent when shapes are deleted."""

import bpy
from bpy.app.handlers import persistent

from . import nodes, ops

_busy = False


@persistent
def depsgraph_update_post(scene, depsgraph):
    global _busy
    if _busy:
        return
    # Cheap per-fusion bookkeeping: prune deleted shapes, mirror the material.
    _busy = True
    try:
        updated_meshes = {u.id.name for u in depsgraph.updates
                          if u.id.id_type == 'MESH' and u.is_updated_geometry}
        if updated_meshes:
            for ob in scene.objects:
                if not (ob.sdf_shape.enabled and ob.type == 'MESH' and ob.data.name in updated_meshes):
                    continue
                if ob.sdf_shape.primitive == 'MESH':
                    continue
                if ob.mode == 'EDIT':
                    # editing a primitive's guide in Edit Mode: it becomes an editable mesh shape
                    ops.promote_to_mesh(ob)
                elif ops.repair_applied_transform(ob) == 'EDITED':
                    ops.promote_to_mesh(ob)
        for fusion in ops.adopt_orphan_shapes(scene):
            ops.apply_guide_display(fusion)
            nodes.rebuild(fusion)
        for ob in scene.objects:
            fs = ob.sdf_fusion
            if not fs.enabled:
                continue
            if fs.data_version < ops.DATA_VERSION:
                ops.migrate_fusion(ob)
            if ops.prune_fusion(ob) or nodes.tree_is_shared(ob):
                nodes.rebuild(ob)
            else:
                nodes.sync_material(ob)
                if fs.blend_colors and (depsgraph.id_type_updated('MATERIAL') or depsgraph.id_type_updated('OBJECT')):
                    ops.sync_fusion_materials(ob)
    finally:
        _busy = False


@persistent
def load_post(*_args):
    """Upgrade fusions saved by older versions as soon as a file is opened."""
    for ob in bpy.data.objects:
        if ob.sdf_fusion.enabled and ob.sdf_fusion.data_version < ops.DATA_VERSION:
            try:
                ops.migrate_fusion(ob)
            except Exception as exc:      # never block file loading
                print("SDF Fusion: migration failed for", ob.name, exc)


def register():
    if depsgraph_update_post not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(depsgraph_update_post)
    if load_post not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(load_post)


def unregister():
    if load_post in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(load_post)
    if depsgraph_update_post in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(depsgraph_update_post)
