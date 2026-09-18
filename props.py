# SPDX-License-Identifier: MIT
"""Property groups: per-shape settings, per-fusion settings, scene state."""

import bpy
from bpy.props import (BoolProperty, CollectionProperty, EnumProperty, FloatProperty, FloatVectorProperty,
                       IntProperty, PointerProperty)
from bpy.types import PropertyGroup

from . import nodes

PRIMITIVE_ITEMS = (
    ('BOX', 'Box', 'Box (scale = half extents)', 'MESH_CUBE', 0),
    ('SPHERE', 'Sphere', 'Sphere (scale = radius)', 'MESH_UVSPHERE', 1),
    ('CYLINDER', 'Cylinder', 'Cylinder along Z (scale XY = radius, Z = half height)', 'MESH_CYLINDER', 2),
    ('TORUS', 'Torus', 'Torus in the XY plane (scale = major radius)', 'MESH_TORUS', 3),
    ('CONE', 'Cone', 'Cone along Z (scale XY = base radius, Z = half height)', 'MESH_CONE', 4),
)

OPERATION_ITEMS = (
    ('UNION', 'Union', 'Add this shape to the result', 'SELECT_EXTEND', 0),
    ('SUBTRACT', 'Subtract', 'Cut this shape out of the result', 'SELECT_SUBTRACT', 1),
    ('INTERSECT', 'Intersect', 'Keep only the overlap with the result', 'SELECT_INTERSECT', 2),
)

BLEND_ITEMS = (
    ('SMOOTH', 'Smooth', 'Smooth (polynomial) blend, the classic "melting" look'),
    ('ROUND', 'Round', 'Rounded (circular) fillet between the shapes'),
    ('CHAMFER', 'Chamfer', 'Flat 45 degree chamfer between the shapes'),
    ('STEPS', 'Steps', 'Stair-stepped transition'),
    ('NONE', 'None', 'Hard boolean, no blending'),
)

QUALITY_ITEMS = (
    ('LOW', 'Low', 'Fast preview (32 voxels along the longest axis)'),
    ('MEDIUM', 'Medium', 'Balanced preview (64 voxels)'),
    ('HIGH', 'High', 'Detailed preview (128 voxels)'),
    ('CUSTOM', 'Custom', 'Use the Resolution value below'),
)
QUALITY_RESOLUTION = {'LOW': 32, 'MEDIUM': 64, 'HIGH': 128}


# --- update callbacks ---------------------------------------------------------

def _shape_fusion(self):
    ob = self.id_data
    fusion = ob.sdf_shape.fusion
    if fusion is None or not fusion.sdf_fusion.enabled:
        return None
    return fusion


def _shape_rebuild(self, context):
    fusion = _shape_fusion(self)
    if fusion is not None:
        nodes.rebuild(fusion)


def _shape_values(self, context):
    fusion = _shape_fusion(self)
    if fusion is not None:
        nodes.update_shape_values(fusion, self.id_data)


def _shape_param(self, context):
    """Torus tube / cone top radius also change the proxy mesh."""
    from . import ops
    ob = self.id_data
    ops.refresh_proxy_mesh(ob)
    _shape_values(self, context)


def _shape_primitive(self, context):
    from . import ops
    ob = self.id_data
    ops.refresh_proxy_mesh(ob)
    _shape_rebuild(self, context)


def _fusion_rebuild(self, context):
    if self.enabled:
        nodes.rebuild(self.id_data)


def _fusion_values(self, context):
    if self.enabled:
        nodes.update_values(self.id_data)


def _fusion_colors(self, context):
    if not self.enabled:
        return
    from . import ops
    nodes.rebuild(self.id_data)
    if self.blend_colors:
        ops.ensure_color_material(self.id_data)
        ops.show_attribute_colors(context)


def _fusion_live(self, context):
    mod = nodes.get_modifier(self.id_data, create=False)
    if mod is not None:
        mod.show_viewport = self.live


def _active_index_update(self, context):
    """Clicking a row in the shape list selects that object."""
    fusion = self.id_data
    if not (0 <= self.active_shape_index < len(self.shapes)):
        return
    ob = self.shapes[self.active_shape_index].object
    if ob is None or context.view_layer is None:
        return
    try:
        for o in context.view_layer.objects:
            o.select_set(o == ob)
        context.view_layer.objects.active = ob
    except Exception:
        pass


# --- property groups ------------------------------------------------------------

class SDFShapeSettings(PropertyGroup):
    enabled: BoolProperty(default=False, options={'HIDDEN'})
    fusion: PointerProperty(type=bpy.types.Object, options={'HIDDEN'})
    include: BoolProperty(
        name="Enabled", default=True,
        description="Include this shape in the fusion", update=_shape_rebuild)
    primitive: EnumProperty(
        name="Primitive", items=PRIMITIVE_ITEMS, default='BOX', update=_shape_primitive)
    operation: EnumProperty(
        name="Operation", items=OPERATION_ITEMS, default='UNION', update=_shape_rebuild)
    rounding: FloatProperty(
        name="Round Edges", default=0.0, min=0.0, soft_max=1.0, subtype='DISTANCE',
        description="Round the edges of boxes and cylinders", update=_shape_values)
    tube: FloatProperty(
        name="Tube", default=0.25, min=0.01, max=1.0,
        description="Torus tube radius as a fraction of the major radius", update=_shape_param)
    top_radius: FloatProperty(
        name="Top Radius", default=0.0, min=0.0, max=1.0,
        description="Cone top radius as a fraction of the base radius (0 = sharp cone)",
        update=_shape_param)
    use_custom_blend: BoolProperty(
        name="Custom Blend", default=False,
        description="Override the fusion-wide blend for this shape", update=_shape_rebuild)
    blend: FloatProperty(
        name="Blend", default=0.25, min=0.0, soft_max=2.0, subtype='DISTANCE',
        description="Blend radius used when this shape is combined", update=_shape_values)
    blend_type: EnumProperty(
        name="Blend Type", items=BLEND_ITEMS, default='SMOOTH', update=_shape_rebuild)
    steps: IntProperty(
        name="Steps", default=3, min=1, max=32, update=_shape_values)
    color: FloatVectorProperty(
        name="Color", subtype='COLOR', size=4, min=0.0, max=1.0,
        default=(0.8, 0.8, 0.8, 1.0),
        description="Colour of this shape when the fusion blends colours",
        update=_shape_values)


class SDFShapeRef(PropertyGroup):
    object: PointerProperty(type=bpy.types.Object)


class SDFFusionSettings(PropertyGroup):
    enabled: BoolProperty(default=False, options={'HIDDEN'})
    shapes: CollectionProperty(type=SDFShapeRef)
    active_shape_index: IntProperty(default=0, update=_active_index_update)
    blend: FloatProperty(
        name="Blend", default=0.25, min=0.0, soft_max=2.0, subtype='DISTANCE',
        description="Blend radius: how far the shapes melt into each other", update=_fusion_values)
    blend_type: EnumProperty(
        name="Blend Type", items=BLEND_ITEMS, default='SMOOTH', update=_fusion_rebuild)
    steps: IntProperty(
        name="Steps", default=3, min=1, max=32,
        description="Number of steps for the Steps blend", update=_fusion_values)
    quality: EnumProperty(
        name="Quality", items=QUALITY_ITEMS, default='MEDIUM',
        description="Preview resolution while modelling", update=_fusion_values)
    resolution: IntProperty(
        name="Resolution", default=64, min=8, soft_max=256, max=512,
        description="Voxels along the longest axis of the fusion (Custom quality)",
        update=_fusion_values)
    final_resolution: IntProperty(
        name="Final Resolution", default=192, min=8, soft_max=384, max=1024,
        description="Voxels along the longest axis used by Convert to Mesh")
    adaptivity: FloatProperty(
        name="Adaptivity", default=0.0, min=0.0, max=1.0,
        description="Merge flat areas into larger polygons (0 = uniform mesh)", update=_fusion_values)
    blend_colors: BoolProperty(
        name="Blend Colors", default=False,
        description="Give every shape its own colour and cross-fade them over the blend "
                    "(stored as the 'Color' attribute; the fusion material reads it)",
        update=_fusion_colors)
    live: BoolProperty(
        name="Live Update", default=True,
        description="Recompute the fused mesh while editing (disable on slow scenes)",
        update=_fusion_live)

    def live_resolution(self):
        if self.quality == 'CUSTOM':
            return int(self.resolution)
        return QUALITY_RESOLUTION[self.quality]


def _poll_fusion(self, ob):
    return ob is not None and ob.sdf_fusion.enabled


classes = (SDFShapeSettings, SDFShapeRef, SDFFusionSettings)


def register():
    for c in classes:
        bpy.utils.register_class(c)
    bpy.types.Object.sdf_shape = PointerProperty(type=SDFShapeSettings)
    bpy.types.Object.sdf_fusion = PointerProperty(type=SDFFusionSettings)
    bpy.types.Scene.sdf_active_fusion = PointerProperty(
        type=bpy.types.Object, name="Active Fusion", poll=_poll_fusion)


def unregister():
    del bpy.types.Scene.sdf_active_fusion
    del bpy.types.Object.sdf_fusion
    del bpy.types.Object.sdf_shape
    for c in reversed(classes):
        bpy.utils.unregister_class(c)
