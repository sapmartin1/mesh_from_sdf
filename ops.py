# SPDX-License-Identifier: MIT
"""Operators: add shapes, manage the fusion, convert to a plain mesh."""

import math

import bmesh
import bpy
import numpy as np
from bpy.props import BoolProperty, EnumProperty, StringProperty
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
    'MESH': "SDF Mesh",
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


BOUNDS_TYPE = {
    'BOX': 'BOX', 'SPHERE': 'SPHERE', 'CYLINDER': 'CYLINDER', 'CONE': 'CONE',
    'CAPSULE': 'CAPSULE', 'TORUS': 'CYLINDER',
}


def apply_guide_display(fusion, shape=None):
    """Draw source shapes as unobtrusive guides.  Pyramid and prism keep their
    (tiny) wire mesh because no bounds type matches their silhouette."""
    mode = fusion.sdf_fusion.guide_display
    shapes = [shape] if shape is not None else [r.object for r in fusion.sdf_fusion.shapes]
    for ob in shapes:
        if ob is None:
            continue
        btype = BOUNDS_TYPE.get(ob.sdf_shape.primitive)
        if ob.sdf_shape.primitive == 'MESH':
            ob.display_type = 'WIRE'            # editable: always show the real mesh
            continue
        if mode == 'BOUNDS' and btype is not None:
            ob.display_type = 'BOUNDS'
            ob.display_bounds_type = btype
        else:
            ob.display_type = 'WIRE'


def adopt_orphan_shapes(scene):
    """Shift+D on a shape: the copy is parented to the fusion but not listed yet.
    Returns the fusions that gained shapes."""
    changed = []
    listed = {}
    for ob in scene.objects:
        if not ob.sdf_shape.enabled or ob.parent is None or not ob.parent.sdf_fusion.enabled:
            continue
        fusion = ob.parent
        members = listed.get(fusion.name)
        if members is None:
            members = listed[fusion.name] = {r.object for r in fusion.sdf_fusion.shapes}
        if ob in members:
            continue
        if ob.data is not None and ob.data.users > 1:
            ob.data = ob.data.copy()          # linked duplicate: own proxy mesh
        ob.sdf_shape.fusion = fusion
        fusion.sdf_fusion.shapes.add().object = ob
        members.add(ob)
        if fusion not in changed:
            changed.append(fusion)
    return changed


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


def mesh_is_closed(mesh):
    """True when every edge has exactly two faces (what Mesh to SDF Grid needs)."""
    if mesh is None or len(mesh.polygons) == 0:
        return False
    bm = bmesh.new()
    bm.from_mesh(mesh)
    closed = all(len(e.link_faces) == 2 for e in bm.edges)
    bm.free()
    return closed


def refresh_proxy_mesh(shape_ob):
    st = shape_ob.sdf_shape
    if shape_ob.type != 'MESH' or not st.enabled or st.primitive == 'MESH':
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


DATA_VERSION = 3
_RAMP_K = 1.0 - 1.0 / math.sqrt(2.0)


def migrate_fusion(fusion):
    """Pre 1.4 fusions had one Blend distance and a blend type (optionally
    overridden per shape).  Convert them to per-shape Radius / Blend."""
    fs = fusion.sdf_fusion
    if fs.data_version >= DATA_VERSION:
        return False
    if fs.data_version == 2:
        # 1.4.0 stored fill with 0.5 = quarter-pipe and 1.0 = flat bevel; the ramp
        # now ends at the quarter-pipe (fill 1.0).  Keep the same curve where one exists.
        for ref in fs.shapes:
            sh = ref.object
            if sh is None:
                continue
            old = max(0.0, min(1.0, sh.sdf_shape.fill))
            sh.sdf_shape['fill'] = 1.0 if old >= 0.5 else (1.0 - 2.0 ** (-old)) / _RAMP_K
        fs['data_version'] = DATA_VERSION
        nodes.rebuild(fusion)
        return True
    any_custom = False
    for ref in fs.shapes:
        sh = ref.object
        if sh is None:
            continue
        st = sh.sdf_shape
        if st.use_custom_blend:
            k, bt = st.blend, st.blend_type
            any_custom = True
        else:
            k, bt = fs.blend, fs.blend_type
        st['radius'] = 0.0 if bt == 'NONE' else float(k)
        st['fill'] = 1.0
    fs['mode'] = {'STEPS': 1, 'CHAMFER': 2}.get(fs.blend_type, 0)
    fs['seam_rule'] = 3 if any_custom else 0       # keep the old look when overrides were used
    fs['radius_scale'] = 1.0
    fs['fill_scale'] = 1.0
    fs['data_version'] = DATA_VERSION
    nodes.rebuild(fusion)
    return True


def create_fusion(context, location=None):
    scene = context.scene
    mesh = bpy.data.meshes.new("SDF Fusion")
    fusion = bpy.data.objects.new("SDF Fusion", mesh)
    _link_like(fusion, None, context)
    if location is not None:
        fusion.location = location
    fusion.sdf_fusion.enabled = True
    fusion.sdf_fusion['data_version'] = DATA_VERSION
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
    fill_proxy_mesh(mesh, 'BOX' if primitive == 'MESH' else primitive, st.tube, st.top_radius, st.sides)
    shape.display_type = 'WIRE'
    shape.hide_render = True
    apply_guide_display(fusion, shape)
    # The fusion may have been created a moment ago, so its world matrix can be
    # stale; bring it up to date and place the shape through its local matrix.
    context.view_layer.update()
    shape.parent = fusion
    shape.matrix_parent_inverse = Matrix.Identity(4)
    shape.matrix_basis = fusion.matrix_world.inverted() @ Matrix.Translation(Vector(location))

    ref = fusion.sdf_fusion.shapes.add()
    ref.object = shape
    fusion.sdf_fusion.active_shape_index = len(fusion.sdf_fusion.shapes) - 1
    nodes.rebuild(fusion)
    return shape


def adopt_objects(context, fusion, objects):
    """Turn existing mesh objects (imported, modelled...) into editable shapes
    of ``fusion``, keeping their world transform."""
    context.view_layer.update()
    adopted = []
    for ob in objects:
        if ob is None or ob.type != 'MESH' or ob.sdf_fusion.enabled or ob.sdf_shape.enabled or ob == fusion:
            continue
        mw = ob.matrix_world.copy()
        ob.parent = fusion
        ob.matrix_parent_inverse = Matrix.Identity(4)
        ob.matrix_world = mw
        st = ob.sdf_shape
        st.enabled = True
        st.fusion = fusion
        st['primitive'] = [i for i, it in enumerate(PRIMITIVE_ITEMS) if it[0] == 'MESH'][0]
        st['color'] = PALETTE[len(fusion.sdf_fusion.shapes) % len(PALETTE)]
        ob.display_type = 'WIRE'
        ob.hide_render = True
        fusion.sdf_fusion.shapes.add().object = ob
        adopted.append(ob)
    if adopted:
        fusion.sdf_fusion.active_shape_index = len(fusion.sdf_fusion.shapes) - 1
        nodes.rebuild(fusion)
    return adopted


def release_shape(fusion, shape):
    """Take a shape out of the fusion but keep it as an ordinary object."""
    refs = fusion.sdf_fusion.shapes
    for i in range(len(refs) - 1, -1, -1):
        if refs[i].object == shape or refs[i].object is None:
            refs.remove(i)
    fusion.sdf_fusion.active_shape_index = min(fusion.sdf_fusion.active_shape_index, max(0, len(refs) - 1))
    mw = shape.matrix_world.copy()
    shape.parent = None
    shape.matrix_world = mw
    st = shape.sdf_shape
    st.enabled = False
    st.fusion = None
    shape.display_type = 'TEXTURED'
    shape.hide_render = False
    nodes.rebuild(fusion)


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


def promote_to_mesh(shape):
    """A primitive whose guide mesh was edited becomes an editable mesh shape."""
    shape.sdf_shape.primitive = 'MESH'          # keeps the geometry, rebuilds


def repair_applied_transform(shape):
    """Classify what happened to a primitive's guide mesh.

    Apply Scale/Rotation/Location bakes a transform into the guide mesh,
    which would silently change the SDF (sizes come from the object scale):
    that affine transform is recovered from the vertices, folded back into
    the object and the unit guide restored ('AFFINE').  Anything else the
    user did to the vertices (Edit Mode, sculpting) means they want to edit
    the shape ('EDITED'); the caller turns it into a mesh shape.  Returns
    'SAME', 'AFFINE' or 'EDITED'."""
    st = shape.sdf_shape
    me = shape.data
    if me is None or st.primitive == 'MESH':
        return 'SAME'                       # editable meshes are whatever the user makes them
    ref = unit_proxy_vertices(st.primitive, st.tube, st.top_radius, st.sides)
    if len(me.vertices) != len(ref):
        return 'EDITED'
    cur = np.empty(len(ref) * 3)
    me.vertices.foreach_get('co', cur)
    cur = cur.reshape(-1, 3)
    if np.allclose(cur, ref, atol=1e-6):
        return 'SAME'
    A = np.c_[ref, np.ones(len(ref))]
    MT, _res, rank, _sv = np.linalg.lstsq(A, cur, rcond=None)
    if rank < 4 or np.abs(A @ MT - cur).max() > 1e-4 * max(1.0, float(np.abs(cur).max())):
        return 'EDITED'
    M = Matrix.Identity(4)
    for r in range(3):
        for c in range(4):
            M[r][c] = float(MT[c][r])
    shape.matrix_world = shape.matrix_world @ M
    fill_proxy_mesh(me, st.primitive, st.tube, st.top_radius, st.sides)
    return 'AFFINE'


def dual_contour_bake(context, fusion, guide_mesh, resolution, mesh_detail=None):
    """Re-mesh the fusion with dual contouring so hard edges come out exact.

    Grid extraction (marching cubes) puts vertices on grid edges, which turns
    creases into staircases and makes faces straddle them.  Dual contouring
    instead places ONE vertex per grid cell at the point that best satisfies
    the tangent planes of the surface crossings on that cell's edges (a small
    least-squares / QEF), and connects the vertices of the four cells around
    every crossing edge into a quad.  A crease cell's planes intersect in a
    line, so its vertex lands exactly on the crease; a corner cell's vertex
    lands exactly on the corner; smooth cells land on the surface.  Faces
    never straddle a crease.  The field is the real Geometry Nodes field
    (mesh shapes sampled finer), evaluated once per grid point.
    """
    vox = np.empty(len(guide_mesh.vertices) * 3)
    guide_mesh.vertices.foreach_get('co', vox)
    vox = vox.reshape(-1, 3)
    if len(vox) == 0:
        return None
    lo, hi = vox.min(axis=0), vox.max(axis=0)
    ext = float((hi - lo).max())
    h = ext / max(8, int(resolution))
    margin = 3.0 * h
    lo = lo - margin
    dims = np.ceil((hi + margin - lo) / h).astype(int) + 2
    nx, ny, nz = (int(v) for v in dims)
    if nx * ny * nz > 40_000_000:
        return None                                   # far too large, keep marching cubes
    detail = mesh_detail if mesh_detail is not None else min(8.0, max(1.0, round(fusion.sdf_fusion.mesh_detail)) * 4.0)

    def field(p):
        return nodes.sample_field_at(fusion, p, context, resolution, detail)

    # 1. field at every grid point
    gx, gy, gz = np.meshgrid(np.arange(nx), np.arange(ny), np.arange(nz), indexing='ij')
    grid_pts = lo + np.stack([gx, gy, gz], axis=-1).reshape(-1, 3) * h
    d = field(grid_pts).reshape(nx, ny, nz)

    # 2. surface crossings on grid edges (x, y, z directions)
    cross_pts, cross_cells = [], []
    quads = []
    for axis in range(3):
        a = d
        b = np.roll(d, -1, axis=axis)
        sl = [slice(None)] * 3
        sl[axis] = slice(0, -1)
        a, b = a[tuple(sl)], b[tuple(sl)]
        change = (a < 0) != (b < 0)
        idx = np.argwhere(change)                      # edge start grid index (i, j, k)
        if len(idx) == 0:
            continue
        da, db = a[change], b[change]
        t = da / (da - db)
        p = lo + idx * h
        p[:, axis] += t * h
        cross_pts.append(p)
        # the four cells sharing this edge: cell (i,j,k) spans grid points i..i+1
        o1, o2 = [ax for ax in range(3) if ax != axis]
        cells = np.repeat(idx[:, None, :], 4, axis=1).copy()
        shifts = [(0, 0), (-1, 0), (-1, -1), (0, -1)]
        for q, (s1, s2) in enumerate(shifts):
            cells[:, q, o1] += s1
            cells[:, q, o2] += s2
        cross_cells.append(cells)
        # winding: the cell ring (o1, o2) is right-handed about x and z but
        # left-handed about y; flip that, and flip when the field increases
        flip = (da < 0) ^ (axis == 1)
        quads.append((cells, flip))
    if not cross_pts:
        return None
    P = np.concatenate(cross_pts)
    C = np.concatenate(cross_cells)                    # (m, 4, 3) cell indices per crossing
    # 3. normals at the crossings (tiny stencil so creases are not smeared)
    eps = h / 32.0
    offs = np.array([[eps, 0, 0], [-eps, 0, 0], [0, eps, 0], [0, -eps, 0], [0, 0, eps], [0, 0, -eps]])
    vals = field(np.concatenate([P + o for o in offs])).reshape(6, -1)
    N = np.stack([vals[0] - vals[1], vals[2] - vals[3], vals[4] - vals[5]], axis=1) / (2.0 * eps)
    N /= np.maximum(np.linalg.norm(N, axis=1), 1e-12)[:, None]

    # 4. one vertex per cell: QEF over the crossings on its edges
    cid = (C[..., 0] * ny + C[..., 1]) * nz + C[..., 2]           # (m, 4)
    valid = (C >= 0).all(axis=-1) & (C[..., 0] < nx - 1) & (C[..., 1] < ny - 1) & (C[..., 2] < nz - 1)
    flat_cid = cid[valid]
    Pm = np.repeat(P[:, None, :], 4, axis=1)[valid]
    Nm = np.repeat(N[:, None, :], 4, axis=1)[valid]
    uniq, inv = np.unique(flat_cid, return_inverse=True)
    ncell = len(uniq)
    A = np.zeros((ncell, 3, 3))
    bvec = np.zeros((ncell, 3))
    mass = np.zeros((ncell, 3))
    cnt = np.zeros(ncell)
    np.add.at(A, inv, Nm[:, :, None] * Nm[:, None, :])
    np.add.at(bvec, inv, Nm * np.sum(Nm * Pm, axis=1)[:, None])
    np.add.at(mass, inv, Pm)
    np.add.at(cnt, inv, 1.0)
    mass /= cnt[:, None]
    lam = 0.02
    A += lam * np.eye(3)[None]
    bvec += lam * mass
    V = np.linalg.solve(A, bvec[..., None])[..., 0]
    # keep every vertex inside its cell (guards against ill-conditioned fits)
    ci = np.stack([uniq // (ny * nz), (uniq // nz) % ny, uniq % nz], axis=1)
    cmin = lo + ci * h
    V = np.minimum(np.maximum(V, cmin - 0.05 * h), cmin + 1.05 * h)
    # tangent-plane fits sit O(h^2) off curved surfaces: two Newton steps onto
    # the real field (a no-op for crease / corner vertices already on it)
    for _ in range(2):
        vals = field(np.concatenate([V] + [V + o for o in offs])).reshape(7, -1)
        g = np.stack([vals[1] - vals[2], vals[3] - vals[4], vals[5] - vals[6]], axis=1) / (2.0 * eps)
        g2 = np.maximum(np.sum(g * g, axis=1), 1e-12)
        step = (vals[0] / g2)[:, None] * g
        ln = np.linalg.norm(step, axis=1)
        step *= np.minimum(1.0, (0.75 * h) / np.maximum(ln, 1e-12))[:, None]
        V = V - step

    # 5. quads: the vertices of the four cells around each crossing edge
    lookup = {int(c): i for i, c in enumerate(uniq)}
    faces = []
    for cells, flip in quads:
        ids = (cells[..., 0] * ny + cells[..., 1]) * nz + cells[..., 2]
        ok = (cells >= 0).all(axis=-1).all(axis=-1) & (cells[..., 0] < nx - 1).all(axis=-1) \
            & (cells[..., 1] < ny - 1).all(axis=-1) & (cells[..., 2] < nz - 1).all(axis=-1)
        for row, fl in zip(ids[ok], flip[ok]):
            q = [lookup[int(v)] for v in row]
            if len(set(q)) < 4:
                continue
            faces.append(q if fl else q[::-1])
    if not faces:
        return None
    mesh = bpy.data.meshes.new(guide_mesh.name)
    mesh.from_pydata([tuple(map(float, v)) for v in V], [], faces)
    mesh.validate(verbose=False)
    mesh.update()
    # 6. colours / surface values: nearest vertex of the marching-cubes mesh
    from mathutils.kdtree import KDTree
    src_attrs = [a for a in guide_mesh.attributes if a.domain == 'POINT' and a.data_type in {'FLOAT_COLOR', 'FLOAT_VECTOR', 'FLOAT'}
                 and not a.name.startswith('.') and a.name not in {'position'}]
    if src_attrs and len(guide_mesh.vertices):
        kd = KDTree(len(guide_mesh.vertices))
        for i, v in enumerate(guide_mesh.vertices):
            kd.insert(v.co, i)
        kd.balance()
        nearest = np.array([kd.find(tuple(v))[1] for v in V], dtype=np.int64)
        for a in src_attrs:
            comps = {'FLOAT_COLOR': 4, 'FLOAT_VECTOR': 3, 'FLOAT': 1}[a.data_type]
            key = {'FLOAT_COLOR': 'color', 'FLOAT_VECTOR': 'vector', 'FLOAT': 'value'}[a.data_type]
            buf = np.empty(len(guide_mesh.vertices) * comps)
            a.data.foreach_get(key, buf)
            buf = buf.reshape(-1, comps)[nearest]
            na = mesh.attributes.new(a.name, a.data_type, 'POINT')
            na.data.foreach_set(key, buf.ravel())
        if nodes.COLOR_ATTRIBUTE in mesh.color_attributes:
            mesh.color_attributes.active_color = mesh.color_attributes[nodes.COLOR_ATTRIBUTE]
    if len(guide_mesh.polygons) and len(guide_mesh.materials):
        mi = np.empty(len(guide_mesh.polygons), dtype=np.int32)
        guide_mesh.polygons.foreach_get('material_index', mi)
        used = np.bincount(mi).argmax()
        if used < len(guide_mesh.materials) and guide_mesh.materials[used] is not None:
            mesh.materials.append(guide_mesh.materials[used])
    # Smart Topology: merge coplanar / nearly coplanar faces into larger polygons
    if fusion.sdf_fusion.adaptivity > 0.0:
        import bmesh as _bm
        bm = _bm.new()
        bm.from_mesh(mesh)
        bm.normal_update()
        _bm.ops.dissolve_limit(bm, angle_limit=math.radians(12.0 * fusion.sdf_fusion.adaptivity),
                               use_dissolve_boundaries=False, verts=bm.verts, edges=bm.edges)
        bm.to_mesh(mesh)
        bm.free()
        mesh.update()
    # shading: smooth faces, sharp creases by angle (same rule as the live mesh)
    smooth = np.ones(len(mesh.polygons), dtype=bool)
    mesh.polygons.foreach_set('use_smooth', smooth)
    fs = fusion.sdf_fusion
    if fs.shading == 'FLAT':
        mesh.polygons.foreach_set('use_smooth', np.zeros(len(mesh.polygons), dtype=bool))
    elif fs.shading in ('AUTO', 'FIELD'):
        # dual-contouring faces never straddle a crease, so angle-based sharp
        # edges give exact hard edges in the bake
        import bmesh as _bm
        bm = _bm.new()
        bm.from_mesh(mesh)
        bm.normal_update()
        thr = math.cos(fs.smooth_angle)
        for e in bm.edges:
            if len(e.link_faces) == 2 and e.link_faces[0].normal.dot(e.link_faces[1].normal) < thr:
                e.smooth = False
        bm.to_mesh(mesh)
        bm.free()
    mesh.update()
    return mesh


def _copy_modifier(src, dst_ob):
    nm = dst_ob.modifiers.new(src.name, src.type)
    for p in src.bl_rna.properties:
        if p.is_readonly or p.identifier in {'rna_type', 'name', 'type'}:
            continue
        try:
            setattr(nm, p.identifier, getattr(src, p.identifier))
        except Exception:
            pass
    return nm


def apply_post_modifiers(context, fusion, mesh):
    """Run the fusion's post-modifiers (Twist, Smooth, ...) on a baked mesh."""
    mods = [m for m in post_modifiers(fusion) if m.show_viewport]
    if not mods:
        return mesh
    tmp = bpy.data.objects.new('_sdff_bake', mesh)
    context.scene.collection.objects.link(tmp)
    tmp.matrix_world = fusion.matrix_world.copy()
    for m in mods:
        _copy_modifier(m, tmp)
    dg = context.evaluated_depsgraph_get()
    out = bpy.data.meshes.new_from_object(tmp.evaluated_get(dg), preserve_all_data_layers=True, depsgraph=dg)
    bpy.data.objects.remove(tmp, do_unlink=True)
    bpy.data.meshes.remove(mesh)
    return out


def convert_to_mesh(context, fusion, resolution, keep_setup=True, hide_setup=True, precise=None):
    mesh = nodes.evaluate_mesh(fusion, resolution, context)
    if precise is None:
        precise = fusion.sdf_fusion.precise_convert
    if precise:
        exact = dual_contour_bake(context, fusion, mesh, resolution)
        if exact is not None:
            bpy.data.meshes.remove(mesh)
            mesh = apply_post_modifiers(context, fusion, exact)
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


class SDFF_OT_adopt_selected(Operator):
    bl_idname = "sdf_fusion.adopt_selected"
    bl_label = "Use Selected as Shapes"
    bl_description = "Make the selected mesh objects (imported, modelled...) editable shapes of the fusion"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT' and any(
            o.type == 'MESH' and not o.sdf_shape.enabled and not o.sdf_fusion.enabled for o in context.selected_objects)

    def execute(self, context):
        candidates = [o for o in context.selected_objects if o.type == 'MESH' and not o.sdf_shape.enabled and not o.sdf_fusion.enabled]
        fusion = find_fusion(context)
        if fusion is None:
            fusion = create_fusion(context, Vector(candidates[0].matrix_world.translation))
        else:
            context.scene.sdf_active_fusion = fusion
        adopted = adopt_objects(context, fusion, candidates)
        open_meshes = [o.name for o in adopted if not mesh_is_closed(o.data)]
        if open_meshes:
            self.report({'WARNING'}, "Not closed (field may be unreliable): " + ", ".join(open_meshes))
        self.report({'INFO'}, f"{len(adopted)} object(s) added to {fusion.name}")
        return {'FINISHED'}


class SDFF_OT_make_editable(Operator):
    bl_idname = "sdf_fusion.make_editable"
    bl_label = "Make Editable"
    bl_description = "Turn this primitive into an editable mesh shape (Tab to edit its vertices, loop cut, sculpt)"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        sh = active_shape(context)
        return sh is not None and sh.sdf_shape.primitive != 'MESH'

    def execute(self, context):
        active_shape(context).sdf_shape.primitive = 'MESH'
        return {'FINISHED'}


class SDFF_OT_release_shape(Operator):
    bl_idname = "sdf_fusion.release_shape"
    bl_label = "Release from Fusion"
    bl_description = "Take the active shape out of the fusion but keep it as a normal object"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return active_shape(context) is not None

    def execute(self, context):
        shape = active_shape(context)
        release_shape(shape.sdf_shape.fusion, shape)
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


MODIFIER_PRESETS = (
    ('SIMPLE_DEFORM', 'Twist / Bend', 'Simple Deform: twist, bend, taper or stretch the whole fused mesh', 'MOD_SIMPLEDEFORM', 0),
    ('SMOOTH', 'Smooth', 'Smooth the fused surface', 'MOD_SMOOTH', 1),
    ('SUBSURF', 'Subdivision', 'Subdivision Surface for a softer, denser mesh', 'MOD_SUBSURF', 2),
    ('REMESH', 'Remesh', 'Voxel remesh for even, clean topology', 'MOD_REMESH', 3),
    ('DISPLACE', 'Displace', 'Texture-driven surface detail (noise, bumps)', 'MOD_DISPLACE', 4),
    ('DECIMATE', 'Decimate', 'Reduce the polygon count', 'MOD_DECIM', 5),
    ('SOLIDIFY', 'Solidify', 'Give the surface a thickness (mesh-based alternative to Hollow)', 'MOD_SOLIDIFY', 6),
    ('ARRAY', 'Array', 'Repeat the fused mesh', 'MOD_ARRAY', 7),
)


def add_post_modifier(fusion, mtype):
    """Add a modifier after the SDF Fusion modifier with sensible defaults."""
    names = {t[0]: t[1] for t in MODIFIER_PRESETS}
    mod = fusion.modifiers.new(names.get(mtype, mtype.title()), mtype)
    if mtype == 'SIMPLE_DEFORM':
        mod.deform_method = 'TWIST'
        mod.angle = 0.5
    elif mtype == 'SMOOTH':
        mod.factor = 0.5
        mod.iterations = 5
    elif mtype == 'SUBSURF':
        mod.levels = 1
        mod.render_levels = 2
    elif mtype == 'REMESH':
        mod.mode = 'VOXEL'
        mod.voxel_size = 0.05
    elif mtype == 'DISPLACE':
        tex = bpy.data.textures.get('SDF Fusion Displace') or bpy.data.textures.new('SDF Fusion Displace', 'CLOUDS')
        tex.noise_scale = 0.4
        mod.texture = tex
        mod.strength = 0.1
        mod.texture_coords = 'LOCAL'
    elif mtype == 'DECIMATE':
        mod.ratio = 0.3
    elif mtype == 'SOLIDIFY':
        mod.thickness = 0.05
    elif mtype == 'ARRAY':
        mod.count = 2
    sdf = fusion.modifiers.find(nodes.MODIFIER_NAME)
    if sdf > 0:
        fusion.modifiers.move(sdf, 0)
    return mod


def post_modifiers(fusion):
    return [m for m in fusion.modifiers if m.name != nodes.MODIFIER_NAME]


class SDFF_OT_add_modifier(Operator):
    bl_idname = "sdf_fusion.add_modifier"
    bl_label = "Add Modifier"
    bl_description = "Add a modifier that post-processes the live fused mesh (no conversion needed; Convert bakes it)"
    bl_options = {'REGISTER', 'UNDO'}

    type: EnumProperty(name="Type", items=MODIFIER_PRESETS, default='SIMPLE_DEFORM')

    @classmethod
    def poll(cls, context):
        return find_fusion(context) is not None

    def execute(self, context):
        fusion = find_fusion(context)
        mod = add_post_modifier(fusion, self.type)
        focus_modifiers(context, fusion, mod)
        self.report({'INFO'}, f"{mod.name} added to {fusion.name}; its settings are open in the Modifier tab")
        return {'FINISHED'}


def focus_modifiers(context, fusion, mod=None):
    """Make the fusion the active object and show its modifier stack in the
    Properties editor, so Blender's own modifier UI is what the user edits."""
    try:
        for o in context.view_layer.objects:
            o.select_set(o == fusion)
        context.view_layer.objects.active = fusion
    except RuntimeError:
        pass
    for m in fusion.modifiers:
        m.show_expanded = (mod is None) or (m == mod) or (m.name == nodes.MODIFIER_NAME and mod is None)
    if mod is not None:
        fusion.modifiers.active = mod
    screen = getattr(context, 'screen', None)
    if screen is not None:
        for area in screen.areas:
            if area.type == 'PROPERTIES':
                try:
                    area.spaces.active.context = 'MODIFIER'
                except Exception:
                    pass


class SDFF_OT_edit_modifiers(Operator):
    bl_idname = "sdf_fusion.edit_modifiers"
    bl_label = "Edit Modifiers"
    bl_description = "Select the fusion and open its modifier stack in the Properties editor"
    bl_options = {'REGISTER', 'UNDO'}

    name: StringProperty()

    @classmethod
    def poll(cls, context):
        return find_fusion(context) is not None

    def execute(self, context):
        fusion = find_fusion(context)
        focus_modifiers(context, fusion, fusion.modifiers.get(self.name) if self.name else None)
        return {'FINISHED'}


class SDFF_OT_remove_modifier(Operator):
    bl_idname = "sdf_fusion.remove_modifier"
    bl_label = "Remove Modifier"
    bl_description = "Remove this modifier from the fusion"
    bl_options = {'REGISTER', 'UNDO'}

    name: StringProperty()

    def execute(self, context):
        fusion = find_fusion(context)
        mod = fusion.modifiers.get(self.name) if fusion else None
        if mod is None or mod.name == nodes.MODIFIER_NAME:
            return {'CANCELLED'}
        fusion.modifiers.remove(mod)
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
    SDFF_OT_adopt_selected,
    SDFF_OT_make_editable,
    SDFF_OT_release_shape,
    SDFF_OT_new_fusion,
    SDFF_OT_remove_shape,
    SDFF_OT_move_shape,
    SDFF_OT_rebuild,
    SDFF_OT_select_fusion,
    SDFF_OT_duplicate_fusion,
    SDFF_OT_show_setup,
    SDFF_OT_color_from_material,
    SDFF_OT_setup_color_material,
    SDFF_OT_add_modifier,
    SDFF_OT_edit_modifiers,
    SDFF_OT_remove_modifier,
    SDFF_OT_convert,
)


def register():
    for c in classes:
        bpy.utils.register_class(c)


def unregister():
    for c in reversed(classes):
        bpy.utils.unregister_class(c)
