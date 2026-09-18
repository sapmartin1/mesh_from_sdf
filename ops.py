# SPDX-License-Identifier: MIT
"""Operators: add shapes, manage the fusion, convert to a plain mesh."""

import math

import bmesh
import bpy
import numpy as np
from bpy.props import BoolProperty, EnumProperty
from bpy.types import Operator
from mathutils import Matrix, Vector

from . import nodes
from .props import PRIMITIVE_ITEMS

COLOR_MATERIAL_NAME = "SDF Fusion Material"

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
    'CAPSULE': "SDF Capsule",
    'PYRAMID': "SDF Pyramid",
    'PRISM': "SDF Prism",
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


def _pyramid_bmesh(bm):
    v = [bm.verts.new(c) for c in ((-1, -1, -1), (1, -1, -1), (1, 1, -1), (-1, 1, -1), (0, 0, 1))]
    bm.faces.new((v[0], v[3], v[2], v[1]))
    for a, b_ in ((0, 1), (1, 2), (2, 3), (3, 0)):
        bm.faces.new((v[a], v[b_], v[4]))


def _prism_bmesh(bm, sides):
    # a vertex on +Y, matching the closed-form polygon SDF
    top, bottom = [], []
    for k in range(sides):
        a = math.pi / 2.0 + 2.0 * math.pi * k / sides
        bottom.append(bm.verts.new((math.cos(a), math.sin(a), -1.0)))
        top.append(bm.verts.new((math.cos(a), math.sin(a), 1.0)))
    bm.faces.new(list(reversed(bottom)))
    bm.faces.new(top)
    for k in range(sides):
        j = (k + 1) % sides
        bm.faces.new((bottom[k], bottom[j], top[j], top[k]))


def fill_proxy_mesh(mesh, primitive, tube=0.25, top_radius=0.0, sides=6):
    """Unit proxy geometry matching the SDF primitive (Z-up, size 2)."""
    bm = bmesh.new()
    if primitive == 'PYRAMID':
        _pyramid_bmesh(bm)
    elif primitive == 'PRISM':
        _prism_bmesh(bm, max(3, int(sides)))
    elif primitive == 'CAPSULE':
        # the capsule is inscribed in this cylinder (its ends are rounded by the radius)
        bmesh.ops.create_cone(bm, cap_ends=True, cap_tris=False, segments=32,
                              radius1=1.0, radius2=1.0, depth=2.0)
    elif primitive == 'BOX':
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
    fill_proxy_mesh(shape_ob.data, st.primitive, st.tube, st.top_radius, st.sides)


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
    """A Principled material fed by the fusion's blended attributes."""
    mat = bpy.data.materials.get(name)
    if mat is not None and material_reads_color_attribute(mat) and mat.get('sdff_material_version') == 2:
        return mat
    mat = mat or bpy.data.materials.new(name)
    mat.use_nodes = True
    nt = mat.node_tree
    nt.nodes.clear()
    out = nt.nodes.new('ShaderNodeOutputMaterial')
    out.location = (500, 0)
    bsdf = nt.nodes.new('ShaderNodeBsdfPrincipled')
    bsdf.location = (100, 0)
    col = nt.nodes.new('ShaderNodeVertexColor')
    col.layer_name = nodes.COLOR_ATTRIBUTE
    col.location = (-400, 200)
    nt.links.new(col.outputs['Color'], bsdf.inputs['Base Color'])
    surf = nt.nodes.new('ShaderNodeAttribute')
    surf.attribute_type = 'GEOMETRY'
    surf.attribute_name = nodes.SURFACE_ATTRIBUTE
    surf.location = (-600, 0)
    sep = nt.nodes.new('ShaderNodeSeparateXYZ')
    sep.location = (-400, 0)
    nt.links.new(surf.outputs['Vector'], sep.inputs[0])
    nt.links.new(sep.outputs['X'], bsdf.inputs['Metallic'])
    nt.links.new(sep.outputs['Y'], bsdf.inputs['Roughness'])
    nt.links.new(sep.outputs['Z'], bsdf.inputs['Transmission Weight'])
    extra = nt.nodes.new('ShaderNodeAttribute')
    extra.attribute_type = 'GEOMETRY'
    extra.attribute_name = nodes.EXTRA_ATTRIBUTE
    extra.location = (-600, -250)
    sep2 = nt.nodes.new('ShaderNodeSeparateXYZ')
    sep2.location = (-400, -250)
    nt.links.new(extra.outputs['Vector'], sep2.inputs[0])
    nt.links.new(sep2.outputs['X'], bsdf.inputs['IOR'])
    nt.links.new(sep2.outputs['Y'], bsdf.inputs['Emission Strength'])
    emis = nt.nodes.new('ShaderNodeVertexColor')
    emis.layer_name = nodes.EMISSION_ATTRIBUTE
    emis.location = (-400, -450)
    nt.links.new(emis.outputs['Color'], bsdf.inputs['Emission Color'])
    nt.links.new(bsdf.outputs['BSDF'], out.inputs['Surface'])
    mat.diffuse_color = (0.8, 0.8, 0.8, 1.0)
    mat['sdff_material_version'] = 2
    return mat


def _principled(mat):
    if mat is None or not mat.use_nodes or mat.node_tree is None:
        return None
    for n in mat.node_tree.nodes:
        if n.bl_idname == 'ShaderNodeBsdfPrincipled':
            return n
    return None


def shape_material_values(shape):
    """Surface values of a shape's own material (Principled BSDF inputs, or viewport colour)."""
    mat = shape.active_material
    if mat is None:
        return None
    bsdf = _principled(mat)
    if bsdf is None:
        c = mat.diffuse_color
        return {'color': (c[0], c[1], c[2], 1.0), 'metallic': mat.metallic, 'roughness': mat.roughness}
    def val(name, default):
        s = bsdf.inputs.get(name)
        return s.default_value if s is not None else default
    bc = val('Base Color', (0.8, 0.8, 0.8, 1.0))
    ec = val('Emission Color', (1.0, 1.0, 1.0, 1.0))
    return {
        'color': (bc[0], bc[1], bc[2], 1.0),
        'metallic': float(val('Metallic', 0.0)),
        'roughness': float(val('Roughness', 0.5)),
        'transmission': float(val('Transmission Weight', 0.0)),
        'ior': float(val('IOR', 1.45)),
        'emission_color': (ec[0], ec[1], ec[2], 1.0),
        'emission_strength': float(val('Emission Strength', 0.0)),
    }


def sync_shape_from_material(shape):
    """Copy the shape material's values into the shape settings; True if anything changed."""
    vals = shape_material_values(shape)
    if vals is None:
        return False
    st = shape.sdf_shape
    changed = False
    for key, v in vals.items():
        cur = getattr(st, key)
        if isinstance(v, tuple):
            if any(abs(a - b_) > 1e-6 for a, b_ in zip(cur, v)):
                setattr(st, key, v)
                changed = True
        elif abs(cur - v) > 1e-6:
            setattr(st, key, v)
            changed = True
    return changed


def sync_fusion_materials(fusion):
    changed = False
    for ref in fusion.sdf_fusion.shapes:
        sh = ref.object
        if sh is not None and sh.sdf_shape.use_material:
            changed |= sync_shape_from_material(sh)
    return changed


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
    fill_proxy_mesh(mesh, primitive, st.tube, st.top_radius, st.sides)
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
        if ob is not None and ob.sdf_shape.enabled and ob.sdf_shape.fusion != fusion and ob.parent == fusion:
            # a shape duplicated together with its fusion (Shift+D): adopt it
            ob.sdf_shape.fusion = fusion
            changed = True
            continue
        if ob is None or not ob.sdf_shape.enabled or ob.sdf_shape.fusion != fusion:
            refs.remove(i)
            changed = True
    return changed


def duplicate_fusion(context, fusion, offset):
    """Deep-copy a fusion with its shapes into an independent fusion."""
    new_fusion = fusion.copy()
    new_fusion.data = fusion.data.copy()
    _link_like(new_fusion, fusion, context)
    mod = new_fusion.modifiers.get(nodes.MODIFIER_NAME)
    if mod is not None:
        mod.node_group = None          # never share the original's tree
    new_fusion.sdf_fusion.shapes.clear()
    for ref in fusion.sdf_fusion.shapes:
        sh = ref.object
        if sh is None:
            continue
        nsh = sh.copy()
        nsh.data = sh.data.copy()
        _link_like(nsh, sh, context)
        nsh.parent = new_fusion
        nsh.matrix_parent_inverse = sh.matrix_parent_inverse.copy()
        nsh.sdf_shape.fusion = new_fusion
        new_fusion.sdf_fusion.shapes.add().object = nsh
    new_fusion.location = fusion.location + Vector(offset)
    nodes.rebuild(new_fusion)
    context.scene.sdf_active_fusion = new_fusion
    return new_fusion


def _strip_empty_material_slots(mesh):
    """Set Material leaves an empty first slot; drop unused empty slots."""
    used = set(p.material_index for p in mesh.polygons)
    for i in range(len(mesh.materials) - 1, -1, -1):
        if mesh.materials[i] is None and i not in used:
            mesh.materials.pop(index=i)
            for p in mesh.polygons:
                if p.material_index > i:
                    p.material_index -= 1


def show_setup(fusion):
    """Undo what Convert's Hide Setup did."""
    try:
        fusion.hide_set(False)
    except RuntimeError:
        pass
    fusion.hide_render = False
    for ref in fusion.sdf_fusion.shapes:
        if ref.object is not None:
            try:
                ref.object.hide_set(False)
            except RuntimeError:
                pass


def setup_is_hidden(fusion):
    try:
        return fusion.hide_get() or fusion.hide_render
    except RuntimeError:
        return fusion.hide_render


_UNIT_CACHE = {}


def unit_proxy_vertices(primitive, tube, top_radius, sides):
    key = (primitive, round(tube, 6), round(top_radius, 6), int(sides))
    v = _UNIT_CACHE.get(key)
    if v is None:
        me = bpy.data.meshes.new('_sdff_unit')
        fill_proxy_mesh(me, primitive, tube, top_radius, sides)
        arr = np.empty(len(me.vertices) * 3)
        me.vertices.foreach_get('co', arr)
        v = arr.reshape(-1, 3).copy()
        bpy.data.meshes.remove(me)
        _UNIT_CACHE[key] = v
    return v


def repair_applied_transform(shape):
    """Apply Scale/Rotation/Location bakes a transform into the proxy mesh,
    which would silently change the SDF (sizes come from the object scale).
    Recover that affine transform from the vertices, fold it back into the
    object and restore the unit proxy, so nothing visibly changes and the
    field stays consistent.  Returns True when a repair happened."""
    st = shape.sdf_shape
    me = shape.data
    if me is None:
        return False
    ref = unit_proxy_vertices(st.primitive, st.tube, st.top_radius, st.sides)
    if len(me.vertices) != len(ref):
        return False
    cur = np.empty(len(ref) * 3)
    me.vertices.foreach_get('co', cur)
    cur = cur.reshape(-1, 3)
    if np.allclose(cur, ref, atol=1e-6):
        return False
    A = np.c_[ref, np.ones(len(ref))]
    MT, _res, rank, _sv = np.linalg.lstsq(A, cur, rcond=None)
    if rank < 4:
        return False
    if np.abs(A @ MT - cur).max() > 1e-4 * max(1.0, float(np.abs(cur).max())):
        return False                        # hand-edited, not an affine change
    M = Matrix.Identity(4)
    for r in range(3):
        for c in range(4):
            M[r][c] = float(MT[c][r])
    shape.matrix_world = shape.matrix_world @ M
    fill_proxy_mesh(me, st.primitive, st.tube, st.top_radius, st.sides)
    return True


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
            ob.hide_render = True          # never render over the baked mesh
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


class SDFF_OT_show_setup(Operator):
    bl_idname = "sdf_fusion.show_setup"
    bl_label = "Show Fusion Setup"
    bl_description = "Unhide the fusion and its shapes (viewport and render) to keep editing"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return find_fusion(context) is not None

    def execute(self, context):
        show_setup(find_fusion(context))
        return {'FINISHED'}


class SDFF_OT_duplicate_fusion(Operator):
    bl_idname = "sdf_fusion.duplicate_fusion"
    bl_label = "Duplicate Fusion"
    bl_description = "Copy this fusion and all its shapes into a new, independent fusion"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return find_fusion(context) is not None

    def execute(self, context):
        fusion = find_fusion(context)
        dg = context.evaluated_depsgraph_get()
        ev = fusion.evaluated_get(dg)
        xs = [v.co.x for v in ev.data.vertices]
        width = (max(xs) - min(xs)) if xs else 2.0
        new_fusion = duplicate_fusion(context, fusion, (width * 1.2, 0.0, 0.0))
        for o in context.view_layer.objects:
            o.select_set(False)
        new_fusion.select_set(True)
        context.view_layer.objects.active = new_fusion
        return {'FINISHED'}


class SDFF_OT_color_from_material(Operator):
    bl_idname = "sdf_fusion.color_from_material"
    bl_label = "From Material"
    bl_description = "Copy colour, metallic, roughness, transmission, IOR and emission from the shape's own material"
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
            if shape_material_values(sh) is not None:
                sync_shape_from_material(sh)
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
    SDFF_OT_duplicate_fusion,
    SDFF_OT_show_setup,
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
