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
                if ob.sdf_shape.enabled and ob.type == 'MESH' and ob.data.name in updated_meshes:
                    ops.repair_applied_transform(ob)
        for ob in scene.objects:
            fs = ob.sdf_fusion
            if not fs.enabled:
                continue
            if ops.prune_fusion(ob) or nodes.tree_is_shared(ob):
                nodes.rebuild(ob)
            else:
                nodes.sync_material(ob)
                if fs.blend_colors and (depsgraph.id_type_updated('MATERIAL') or depsgraph.id_type_updated('OBJECT')):
                    ops.sync_fusion_materials(ob)
    finally:
        _busy = False


def register():
    if depsgraph_update_post not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(depsgraph_update_post)


def unregister():
    if depsgraph_update_post in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(depsgraph_update_post)
