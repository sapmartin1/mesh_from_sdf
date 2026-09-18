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
        for ob in scene.objects:
            fs = ob.sdf_fusion
            if not fs.enabled:
                continue
            if ops.prune_fusion(ob):
                nodes.rebuild(ob)
            else:
                nodes.sync_material(ob)
    finally:
        _busy = False


def register():
    if depsgraph_update_post not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(depsgraph_update_post)


def unregister():
    if depsgraph_update_post in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(depsgraph_update_post)
