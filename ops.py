# SPDX-License-Identifier: MIT
"""Operators: add shapes, manage the fusion, convert to a plain mesh."""

import math

import bmesh
import bpy
from bpy.props import BoolProperty, EnumProperty
from bpy.types import Operator
from mathutils import Matrix, Vector

from . import nodes
from .props import PRIMITIVE_ITEMS

COLOR_MATERIAL_NAME = "SDF Fusion Colors"

# default colours handed to new shapes (cycled), pleasant and distinct
PALETTE = (
    (0.95, 0.55, 0.20, 1.0),
    (0.25, 0.60, 0.95, 1.0),
    (0.35, 0.80, 0.45, 1.0),
    (0.90, 0.35, 0.45, 1.0),
    (0.80, 0.75, 0.30, 1.0),
    (0.60, 0.45, 0.90, 1.0),
)

PROXY_NAMES = {
    'BOX': "SDF Box",
    'SPHERE': "SDF Sphere",
    'CYLINDER': "SDF Cylinder",
    'TORUS': "SDF Torus",
    'CONE': "SDF Cone",
}


# --- helpers ----------------------------------------------------------------------

def find_fusion(context):
    """The fusion the panel and the add operators act on."""
    ob = context.active_object
    if ob is not None:
        if ob.sdf_fusion.enabled:
            return ob
        if ob.sdf_shape.enabled and ob.sdf_shape.fusion is not None and ob.sdf_shape.fusion.sdf_fusion.enabled:
            return ob.sdf_shape.fusion
    scene = context.scene
    f = scene.sdf_active_fusion
    if f is not None and f.sdf_fusion.enabled and f.name in scene.objects:
        return f
    for o in scene.objects:
        if o.sdf_fusion.enabled:
            return o
    return None


def active_shape(context):
    ob = context.active_object
    if ob is not None and ob.sdf_shape.enabled and ob.sdf_shape.fusion is not None:
        return ob
    return None


def _torus_bmesh(bm, major=1.0, minor=0.25, segments=32, rings=16):
    verts = []
    for i in range(segments):
        a = 2.0 * math.pi * i / segments
        ca, sa = math.cos(a), math.sin(a)
        ring = []
        for j in range(rings):
            t = 2.0 * math.pi * j / rings
            r = major + minor * math.cos(t)
            ring.append(bm.verts.new((r * ca, r * sa, minor * math.sin(t))))
        verts.append(ring)
    for i in range(segments):
        for j in range(rings):
            a = verts[i][j]
            b = verts[(i + 1) % segments][j]
            c = verts[(i + 1) % segments][(j + 1) % rings]
            d = verts[i][(j + 1) % rings]
            bm.faces.new((a, b, c, d))


def fill_proxy_mesh(mesh, primitive, tube=0.25, top_radius=0.0):
    """Unit proxy geometry matching the SDF primitive (Z-up, size 2)."""
    bm = bmesh.new()
    if primitive == 'BOX':
        bmesh.ops.create_cube(bm, size=2.0)
    elif primitive == 'SPHERE':
        bmesh.ops.create_uvsphere(bm, u_segments=32, v_segments=16, radius=1.0)
    elif primitive == 'CYLINDER':
        bmesh.ops.create_cone(bm, cap_ends=True, cap_tris=False, segments=32,
                              radius1=1.0, radius2=1.0, depth=2.0)
    elif primitive == 'CONE':
        bmesh.ops.create_cone(bm, cap_ends=True, cap_tris=False, segments=32,
                              radius1=1.0, radius2=max(top_radius, 0.0), depth=2.0)
    elif primitive == 'TORUS':
        _torus_bmesh(bm, 1.0, max(tube, 0.01))
    bm.to_mesh(mesh)
    bm.free()
    mesh.update()


def refresh_proxy_mesh(shape_ob):
    st = shape_ob.sdf_shape
    if shape_ob.type != 'MESH' or not st.enabled:
        return
    fill_proxy_mesh(shape_ob.data, st.primitive, st.tube, st.top_radius)


def _link_like(ob, template, context):
    """Link ``ob`` into the collection(s) of ``template`` (or the active one)."""
    cols = list(template.users_collection) if template is not None else []
    if not cols:
        cols = [context.collection or context.scene.collection]
    for col in cols:
        if ob.name not in col.objects:
            col.objects.link(ob)


def material_reads_color_attribute(mat):
    if mat is None or not mat.use_nodes or mat.node_tree is None:
        return False
    return any(n.bl_idname == 'ShaderNodeVertexColor' for n in mat.node_tree.nodes)


def make_color_material(name=COLOR_MATERIAL_NAME):
    """A material whose base colour comes from the fusion's 'Color' attribute."""
    mat = bpy.data.materials.get(name)
    if mat is not None and material_reads_color_attribute(mat):
        return mat
    mat = mat or bpy.data.materials.new(name)
    mat.use_nodes = True
    nt = mat.node_tree
    nt.nodes.clear()
    out = nt.nodes.new('ShaderNodeOutputMaterial')
    out.location = (400, 0)
    bsdf = nt.nodes.new('ShaderNodeBsdfPrincipled')
    bsdf.location = (0, 0)
    attr = nt.nodes.new('ShaderNodeVertexColor')
    attr.layer_name = nodes.COLOR_ATTRIBUTE
    attr.location = (-300, 0)
    nt.links.new(attr.outputs['Color'], bsdf.inputs['Base Color'])
    nt.links.new(bsdf.outputs['BSDF'], out.inputs['Surface'])
    mat.diffuse_color = (0.8, 0.8, 0.8, 1.0)
    return mat


def ensure_color_material(fusion, force=False):
    """Make sure the fusion's material shows the blended colours."""
    if not force and material_reads_color_attribute(fusion.active_material):
        return fusion.active_material
    mat = make_color_material()
    if len(fusion.data.materials) == 0:
        fusion.data.materials.append(mat)
    elif force or fusion.active_material is None:
        fusion.data.materials[0] = mat
    else:
        return fusion.active_material   # user material kept; panel offers a button
    nodes.sync_material(fusion)
    return mat


def show_attribute_colors(context):
    """Solid-mode viewports colour by attribute so the blend is visible at once."""
    screen = getattr(context, 'screen', None)
    if screen is None:
        return
    for area in screen.areas:
        if area.type == 'VIEW_3D':
            shading = area.spaces.active.shading
            if shading.type == 'SOLID':
                shading.color_type = 'VERTEX'


def shape_material_color(shape):
    """Best guess of a shape's material colour (Principled base colour or viewport colour)."""
    mat = shape.active_material
    if mat is None:
        return None
    if mat.use_nodes and mat.node_tree is not None:
        for n in mat.node_tree.nodes:
            if n.bl_idname == 'ShaderNodeBsdfPrincipled':
                c = n.inputs['Base Color'].default_value
                return (c[0], c[1], c[2], 1.0)
    c = mat.diffuse_color
    return (c[0], c[1], c[2], 1.0)


def create_fusion(context, location=None):
    scene = context.scene
    mesh = bpy.data.meshes.new("SDF Fusion")
    fusion = bpy.data.objects.new("SDF Fusion", mesh)
    _link_like(fusion, None, context)
    if location is not None:
        fusion.location = location
    fusion.sdf_fusion.enabled = True
    scene.sdf_active_fusion = fusion
    nodes.rebuild(fusion)
    return fusion


def add_shape(context, fusion, primitive, location):
    st_defaults = {}
    mesh = bpy.data.meshes.new(PROXY_NAMES[primitive])
    shape = bpy.data.objects.new(PROXY_NAMES[primitive], mesh)
    _link_like(shape, fusion, context)
    st = shape.sdf_shape
    st.enabled = True
    st.fusion = fusion
    st['color'] = PALETTE[len(fusion.sdf_fusion.shapes) % len(PALETTE)]
    # primitive assignment triggers the proxy refresh; silence it by filling first
    st['primitive'] = [i for i, it in enumerate(PRIMITIVE_ITEMS) if it[0] == primitive][0]
    fill_proxy_mesh(mesh, primitive, st.tube, st.top_radius)
    shape.display_type = 'WIRE'
    shape.hide_render = True
    shape.parent = fusion
    shape.matrix_parent_inverse = Matrix.Identity(4)
    shape.matrix_world = Matrix.Translation(Vector(location))

    ref = fusion.sdf_fusion.shapes.add()
    ref.object = shape
    fusion.sdf_fusion.active_shape_index = len(fusion.sdf_fusion.shapes) - 1
    nodes.rebuild(fusion)
    return shape


def remove_shape(fusion, shape):
    refs = fusion.sdf_fusion.shapes
    for i in range(len(refs) - 1, -1, -1):
        if refs[i].object == shape or refs[i].object is None:
            refs.remove(i)
    fusion.sdf_fusion.active_shape_index = min(fusion.sdf_fusion.active_shape_index, max(0, len(refs) - 1))
    mesh = shape.data
    bpy.data.objects.remove(shape, do_unlink=True)
    if mesh is not None and mesh.users == 0:
        bpy.data.meshes.remove(mesh)
    nodes.rebuild(fusion)


def prune_fusion(fusion):
    """Drop references to deleted objects; returns True if something changed."""
    refs = fusion.sdf_fusion.shapes
    changed = False
    for i in range(len(refs) - 1, -1, -1):
        ob = refs[i].object
        if ob is None or not ob.sdf_shape.enabled or ob.sdf_shape.fusion != fusion:
            refs.remove(i)
            changed = True
    return changed


def _strip_empty_material_slots(mesh):
    """Set Material leaves an empty first slot; drop unused empty slots."""
    used = set(p.material_index for p in mesh.polygons)
    for i in range(len(mesh.materials) - 1, -1, -1):
        if mesh.materials[i] is None and i not in used:
            mesh.materials.pop(index=i)
            for p in mesh.polygons:
                if p.material_index > i:
                    p.material_index -= 1


def convert_to_mesh(context, fusion, resolution, keep_setup=True, hide_setup=True):
    mesh = nodes.evaluate_mesh(fusion, resolution, context)
    _strip_empty_material_slots(mesh)
    mesh.name = f"{fusion.name} Mesh"
    result = bpy.data.objects.new(f"{fusion.name} Mesh", mesh)
    _link_like(result, fusion, context)
    result.matrix_world = fusion.matrix_world.copy()
    shapes = [r.object for r in fusion.sdf_fusion.shapes if r.object is not None]
    if not keep_setup:
        for sh in shapes:
            remove_shape(fusion, sh)
        fmesh = fusion.data
        bpy.data.objects.remove(fusion, do_unlink=True)
        if fmesh is not None and fmesh.users == 0:
            bpy.data.meshes.remove(fmesh)
    elif hide_setup:
        for ob in shapes + [fusion]:
            try:
                ob.hide_set(True)
            except RuntimeError:
                pass
    for o in context.view_layer.objects:
        o.select_set(False)
    result.select_set(True)
    context.view_layer.objects.active = result
    return result


# --- operators --------------------------------------------------------------------

class SDFF_OT_add_shape(Operator):
    bl_idname = "sdf_fusion.add_shape"
    bl_label = "Add SDF Shape"
    bl_description = "Add a primitive to the active fusion (creates a fusion if none exists)"
    bl_options = {'REGISTER', 'UNDO'}

    primitive: EnumProperty(name="Primitive", items=PRIMITIVE_ITEMS, default='BOX')

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT'

    def execute(self, context):
        cursor = Vector(context.scene.cursor.location)
        fusion = find_fusion(context)
        if fusion is None:
            fusion = create_fusion(context, cursor)
        else:
            context.scene.sdf_active_fusion = fusion
        shape = add_shape(context, fusion, self.primitive, cursor)
        for o in context.view_layer.objects:
            o.select_set(False)
        shape.select_set(True)
        context.view_layer.objects.active = shape
        return {'FINISHED'}


class SDFF_OT_new_fusion(Operator):
    bl_idname = "sdf_fusion.new_fusion"
    bl_label = "New Fusion"
    bl_description = "Start a new, separate SDF fusion at the 3D cursor"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        fusion = create_fusion(context, Vector(context.scene.cursor.location))
        for o in context.view_layer.objects:
            o.select_set(False)
        fusion.select_set(True)
        context.view_layer.objects.active = fusion
        return {'FINISHED'}


class SDFF_OT_remove_shape(Operator):
    bl_idname = "sdf_fusion.remove_shape"
    bl_label = "Remove Shape"
    bl_description = "Remove the active shape from its fusion and delete it"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return active_shape(context) is not None

    def execute(self, context):
        shape = active_shape(context)
        fusion = shape.sdf_shape.fusion
        remove_shape(fusion, shape)
        context.view_layer.objects.active = fusion
        fusion.select_set(True)
        return {'FINISHED'}


class SDFF_OT_move_shape(Operator):
    bl_idname = "sdf_fusion.move_shape"
    bl_label = "Move Shape"
    bl_description = "Change the evaluation order of the active shape"
    bl_options = {'REGISTER', 'UNDO'}

    direction: EnumProperty(items=(('UP', 'Up', ''), ('DOWN', 'Down', '')), default='UP')

    @classmethod
    def poll(cls, context):
        return find_fusion(context) is not None

    def execute(self, context):
        fusion = find_fusion(context)
        fs = fusion.sdf_fusion
        i = fs.active_shape_index
        j = i - 1 if self.direction == 'UP' else i + 1
        if not (0 <= i < len(fs.shapes)) or not (0 <= j < len(fs.shapes)):
            return {'CANCELLED'}
        fs.shapes.move(i, j)
        fs.active_shape_index = j
        nodes.rebuild(fusion)
        return {'FINISHED'}


class SDFF_OT_rebuild(Operator):
    bl_idname = "sdf_fusion.rebuild"
    bl_label = "Rebuild"
    bl_description = "Regenerate the fusion's node tree (use if it ever gets out of sync)"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return find_fusion(context) is not None

    def execute(self, context):
        fusion = find_fusion(context)
        prune_fusion(fusion)
        for ref in fusion.sdf_fusion.shapes:
            refresh_proxy_mesh(ref.object)
        nodes.rebuild(fusion)
        return {'FINISHED'}


class SDFF_OT_select_fusion(Operator):
    bl_idname = "sdf_fusion.select_fusion"
    bl_label = "Select Fusion"
    bl_description = "Make the fusion result object active"

    @classmethod
    def poll(cls, context):
        return find_fusion(context) is not None

    def execute(self, context):
        fusion = find_fusion(context)
        for o in context.view_layer.objects:
            o.select_set(False)
        fusion.select_set(True)
        context.view_layer.objects.active = fusion
        return {'FINISHED'}


class SDFF_OT_color_from_material(Operator):
    bl_idname = "sdf_fusion.color_from_material"
    bl_label = "Color from Material"
    bl_description = "Copy the colour of the shape's own material into its fusion colour"
    bl_options = {'REGISTER', 'UNDO'}

    all_shapes: BoolProperty(name="All Shapes", default=False)

    @classmethod
    def poll(cls, context):
        return find_fusion(context) is not None

    def execute(self, context):
        fusion = find_fusion(context)
        shapes = list(nodes.iter_shapes(fusion)) if self.all_shapes else [active_shape(context)]
        done = 0
        for sh in shapes:
            if sh is None:
                continue
            c = shape_material_color(sh)
            if c is not None:
                sh.sdf_shape.color = c
                done += 1
        if done == 0:
            self.report({'WARNING'}, "The shape has no material to take a colour from")
            return {'CANCELLED'}
        return {'FINISHED'}


class SDFF_OT_setup_color_material(Operator):
    bl_idname = "sdf_fusion.setup_color_material"
    bl_label = "Use Color Material"
    bl_description = "Assign a material that shows the blended shape colours (replaces the fusion's material slot)"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return find_fusion(context) is not None

    def execute(self, context):
        fusion = find_fusion(context)
        fusion.sdf_fusion.blend_colors = True
        ensure_color_material(fusion, force=True)
        show_attribute_colors(context)
        return {'FINISHED'}


class SDFF_OT_convert(Operator):
    bl_idname = "sdf_fusion.convert"
    bl_label = "Convert to Mesh"
    bl_description = "Bake the fusion at Final Resolution into a regular, editable mesh object"
    bl_options = {'REGISTER', 'UNDO'}

    keep_setup: BoolProperty(
        name="Keep Fusion Setup", default=True,
        description="Keep the fusion and its source shapes so you can keep editing")
    hide_setup: BoolProperty(
        name="Hide Fusion Setup", default=True,
        description="Hide the fusion and its shapes after converting")

    @classmethod
    def poll(cls, context):
        f = find_fusion(context)
        return f is not None and any(True for _ in nodes.iter_shapes(f))

    def execute(self, context):
        fusion = find_fusion(context)
        result = convert_to_mesh(context, fusion, fusion.sdf_fusion.final_resolution,
                                 self.keep_setup, self.hide_setup)
        self.report({'INFO'}, f"Converted: {len(result.data.vertices)} vertices, {len(result.data.polygons)} faces")
        return {'FINISHED'}


classes = (
    SDFF_OT_add_shape,
    SDFF_OT_new_fusion,
    SDFF_OT_remove_shape,
    SDFF_OT_move_shape,
    SDFF_OT_rebuild,
    SDFF_OT_select_fusion,
    SDFF_OT_color_from_material,
    SDFF_OT_setup_color_material,
    SDFF_OT_convert,
)


def register():
    for c in classes:
        bpy.utils.register_class(c)


def unregister():
    for c in reversed(classes):
        bpy.utils.unregister_class(c)
