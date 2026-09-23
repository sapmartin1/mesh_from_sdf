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
    ('CAPSULE', 'Capsule', 'Capsule along Z (scale XY = radius, Z = half length)', 'MESH_CAPSULE', 5),
    ('PYRAMID', 'Pyramid', 'Square pyramid (scale XY = base half extents, Z = half height)', 'CONE', 6),
    ('PRISM', 'Prism', 'Regular N-gon prism along Z (scale XY = radius, Z = half height)', 'MESH_ICOSPHERE', 7),
    ('MESH', 'Mesh (editable)', 'Any closed mesh: edit its vertices, loop cut, sculpt or import it, the fusion follows',
     'MESH_DATA', 8),
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

MODE_ITEMS = (
    ('RAMP', 'Ramp', 'Concave ramp between the shapes; Radius sets its reach, Blend how much it fills in', 0),
    ('STEPS', 'Steps', 'Stair-stepped transition of size Radius', 1),
    ('CHAMFER', 'Flat Bevel', 'Straight bevel of size Radius (around round shapes this forms a cone-like skirt)', 2),
)

SEAM_ITEMS = (
    ('SHARPER', 'Sharper Wins', 'A seam uses the smaller Radius / Blend of the two shapes meeting there, '
                                'so a delicate shape stays crisp whatever surrounds it', 0),
    ('AVERAGE', 'Average', 'A seam uses the average of the two shapes', 1),
    ('SOFTER', 'Softer Wins', 'A seam uses the larger Radius / Blend of the two shapes', 2),
    ('LATEST', 'Newer Shape', 'A seam uses the settings of the shape lower in the list (pre 1.4 behaviour)', 3),
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


def _shape_use_material(self, context):
    from . import ops
    if self.use_material:
        ops.sync_shape_from_material(self.id_data)
    _shape_values(self, context)


def _shape_param(self, context):
    """Torus tube / cone top radius also change the proxy mesh."""
    from . import ops
    ob = self.id_data
    ops.refresh_proxy_mesh(ob)
    _shape_values(self, context)


def _shape_primitive(self, context):
    from . import ops
    ob = self.id_data
    if ob.sdf_shape.primitive != 'MESH':
        ops.refresh_proxy_mesh(ob)          # switching TO a mesh keeps the current geometry
    if ob.sdf_shape.fusion is not None:
        ops.apply_guide_display(ob.sdf_shape.fusion, ob)
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


def _fusion_guides(self, context):
    from . import ops
    ops.apply_guide_display(self.id_data)


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
    sides: IntProperty(
        name="Sides", default=6, min=3, max=32,
        description="Number of sides of the prism", update=_shape_param)
    radius: FloatProperty(
        name="Radius", default=0.25, min=0.0, soft_max=2.0, subtype='DISTANCE',
        description="How far from the seam this shape's blend reaches along and into its neighbours",
        update=_shape_values)
    fill: FloatProperty(
        name="Blend", default=1.0, min=0.0, max=1.0, subtype='FACTOR',
        description="How much the blend ramp fills in: 1.0 is a full quarter-pipe, lower values "
                    "hug the inner corner, 0 is a sharp seam. The ramp always curves inward",
        update=_shape_values)
    # --- legacy (pre 1.4) settings, only read once by the migration ---
    use_custom_blend: BoolProperty(default=False, options={'HIDDEN'})
    blend: FloatProperty(default=0.25, min=0.0, options={'HIDDEN'})
    blend_type: EnumProperty(items=BLEND_ITEMS, default='SMOOTH', options={'HIDDEN'})
    steps: IntProperty(default=3, min=1, max=32, options={'HIDDEN'})
    color: FloatVectorProperty(
        name="Color", subtype='COLOR', size=4, min=0.0, max=1.0,
        default=(0.8, 0.8, 0.8, 1.0),
        description="Colour of this shape when the fusion blends materials",
        update=_shape_values)
    use_material: BoolProperty(
        name="Use Shape Material", default=False,
        description="Take colour, metallic, roughness, transmission, IOR and emission from the "
                    "Principled BSDF of this shape's own material (kept in sync automatically)",
        update=_shape_use_material)
    metallic: FloatProperty(name="Metallic", default=0.0, min=0.0, max=1.0, update=_shape_values)
    roughness: FloatProperty(name="Roughness", default=0.5, min=0.0, max=1.0, update=_shape_values)
    transmission: FloatProperty(name="Transmission", default=0.0, min=0.0, max=1.0, update=_shape_values)
    ior: FloatProperty(name="IOR", default=1.45, min=1.0, soft_max=4.0, update=_shape_values)
    emission_color: FloatVectorProperty(
        name="Emission", subtype='COLOR', size=4, min=0.0, max=1.0,
        default=(1.0, 1.0, 1.0, 1.0), update=_shape_values)
    emission_strength: FloatProperty(name="Emission Strength", default=0.0, min=0.0, soft_max=10.0, update=_shape_values)


class SDFShapeRef(PropertyGroup):
    object: PointerProperty(type=bpy.types.Object)


class SDFFusionSettings(PropertyGroup):
    enabled: BoolProperty(default=False, options={'HIDDEN'})
    shapes: CollectionProperty(type=SDFShapeRef)
    active_shape_index: IntProperty(default=0, update=_active_index_update)
    radius_scale: FloatProperty(
        name="Radius \u00d7", default=1.0, min=0.0, soft_max=3.0,
        description="Multiplies the Radius of every shape: 0 gives hard booleans, above 1 melts "
                    "the whole model further",
        update=_fusion_values)
    fill_scale: FloatProperty(
        name="Blend \u00d7", default=1.0, min=0.0, soft_max=2.0,
        description="Multiplies the Blend (ramp fullness) of every shape",
        update=_fusion_values)
    mode: EnumProperty(
        name="Blend Mode", items=MODE_ITEMS, default='RAMP', update=_fusion_rebuild)
    seam_rule: EnumProperty(
        name="Seams", items=SEAM_ITEMS, default='SHARPER',
        description="Whose Radius and Blend apply where two shapes meet",
        update=_fusion_rebuild)
    steps: IntProperty(
        name="Steps", default=3, min=1, max=32,
        description="Number of steps for the Steps blend mode", update=_fusion_values)
    mesh_detail: FloatProperty(
        name="Mesh Detail", default=1.0, min=0.25, soft_max=3.0, max=8.0,
        description="Resolution of mesh shapes' distance fields relative to the fusion grid: "
                    "1 matches it, 2 is twice as fine (slower)",
        update=_fusion_values)
    data_version: IntProperty(default=0, options={'HIDDEN'})
    # --- legacy (pre 1.4) settings, only read once by the migration ---
    blend: FloatProperty(default=0.25, min=0.0, options={'HIDDEN'})
    blend_type: EnumProperty(items=BLEND_ITEMS, default='SMOOTH', options={'HIDDEN'})
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
        name="Blend Materials", default=False,
        description="Give every shape its own colour, metallic, roughness, transmission, IOR and "
                    "emission and cross-fade them over the blend (stored as mesh attributes that "
                    "the generated fusion material reads)",
        update=_fusion_colors)
    auto_order: BoolProperty(
        name="Cutters Last", default=True,
        description="Always apply Subtract and Intersect shapes after all Union shapes, so a "
                    "cutter carves the whole result whichever shape you switch. Turn off for "
                    "manual, strictly top-to-bottom ordering",
        update=_fusion_rebuild)
    guide_display: EnumProperty(
        name="Guides", default='BOUNDS',
        items=(('BOUNDS', 'Bounds', 'Show source shapes as light outline guides'),
               ('WIRE', 'Wire', 'Show source shapes as wireframe meshes')),
        description="How the source shapes are drawn in the viewport",
        update=_fusion_guides)
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
