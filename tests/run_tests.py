# SPDX-License-Identifier: MIT
"""
Headless test-suite for SDF Fusion.

    /Applications/Blender.app/Contents/MacOS/Blender -b --python tests/run_tests.py

Loads the add-on straight from the repository (no install needed), then:
  1. checks every primitive / operation / blend node group against the numpy
     reference implementation (sdf_ref.py) on thousands of random points,
  2. runs the Phase 1 acceptance flow (box + sphere, smooth union, blend
     slider, live transforms, subtract / intersect, convert to mesh),
  3. reports evaluation timings.
"""

import importlib.util
import math
import os
import random
import sys
import time
import traceback

import bpy
import bmesh
import numpy as np
from mathutils import Euler, Matrix, Vector

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = 'sdf_fusion'


def load_package():
    if PKG in sys.modules:
        return sys.modules[PKG]
    spec = importlib.util.spec_from_file_location(PKG, os.path.join(ROOT, '__init__.py'),
                                                  submodule_search_locations=[ROOT])
    mod = importlib.util.module_from_spec(spec)
    sys.modules[PKG] = mod
    spec.loader.exec_module(mod)
    return mod


# If the packaged extension is installed and enabled in this Blender, disable it
# for the test run so the source tree is the only registered copy.
for _mod in list(bpy.context.preferences.addons.keys()):
    if _mod.endswith('.sdf_fusion') or _mod == 'sdf_fusion':
        bpy.ops.preferences.addon_disable(module=_mod)

sdf_fusion = load_package()
sdf_fusion.register()
from sdf_fusion import nodes, ops, sdf_ref  # noqa: E402

C = bpy.context
FAILURES = []
PASSES = []


def check(cond, msg):
    if cond:
        PASSES.append(msg)
        print("  ok   ", msg)
    else:
        FAILURES.append(msg)
        print("  FAIL ", msg)


def reset_scene():
    for ob in list(bpy.data.objects):
        bpy.data.objects.remove(ob, do_unlink=True)
    for me in list(bpy.data.meshes):
        if me.users == 0:
            bpy.data.meshes.remove(me)
    for ng in list(bpy.data.node_groups):
        bpy.data.node_groups.remove(ng)
    C.scene.cursor.location = (0, 0, 0)


def evaluated_mesh(ob):
    dg = C.evaluated_depsgraph_get()
    return ob.evaluated_get(dg).data


def mesh_stats(me):
    bm = bmesh.new()
    bm.from_mesh(me)
    vol = bm.calc_volume(signed=True)
    bm.free()
    if len(me.vertices) == 0:
        return {'verts': 0, 'faces': 0, 'volume': 0.0, 'min': None, 'max': None, 'islands': 0}
    co = np.array([v.co[:] for v in me.vertices])
    # island count via union-find on edges
    parent = list(range(len(me.vertices)))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a
    for e in me.edges:
        a, b = find(e.vertices[0]), find(e.vertices[1])
        if a != b:
            parent[a] = b
    islands = len({find(i) for i in range(len(me.vertices))})
    return {'verts': len(me.vertices), 'faces': len(me.polygons), 'volume': vol,
            'min': co.min(axis=0), 'max': co.max(axis=0), 'islands': islands}


# ----------------------------------------------------------------------------
# 1. node groups vs numpy reference
# ----------------------------------------------------------------------------

def reference_shapes(fusion):
    shapes = []
    finv = fusion.matrix_world.inverted()
    for sh in nodes.iter_shapes(fusion):
        rel = finv @ sh.matrix_world
        loc, rot, scale = rel.decompose()
        rigid = Matrix.Translation(loc) @ rot.to_matrix().to_4x4()
        st = sh.sdf_shape
        shapes.append({
            'matrix_inv_rigid': np.array([list(r) for r in rigid.inverted()]),
            'scale': np.array(scale[:]),
            'primitive': st.primitive,
            'operation': st.operation,
            'rounding': st.rounding,
            'tube': st.tube,
            'top_radius': st.top_radius,
            'use_custom_blend': st.use_custom_blend,
            'blend': st.blend,
            'blend_type': st.blend_type,
            'steps': st.steps,
            'sides': st.sides,
        })
    return shapes


def sample_field(fusion, points):
    me = bpy.data.meshes.new('sampler')
    me.from_pydata([tuple(p) for p in points], [], [])
    sampler = bpy.data.objects.new('sampler', me)
    C.scene.collection.objects.link(sampler)
    sampler.matrix_world = fusion.matrix_world.copy()
    nodes.build_field_sampler(fusion, sampler, 'sdf')
    ev = evaluated_mesh(sampler)
    attr = ev.attributes['sdf']
    vals = np.array([a.value for a in attr.data])
    bpy.data.objects.remove(sampler, do_unlink=True)
    return vals


def test_field_matches_reference():
    print("\n[1] node groups vs numpy reference")
    rng = random.Random(7)
    configs = [
        # (primitive, operation, custom?, blend_type, blend, steps, rounding)
        ('BOX', 'UNION', False, 'SMOOTH', 0.3, 3, 0.15),
        ('SPHERE', 'UNION', False, 'SMOOTH', 0.3, 3, 0.0),
        ('CYLINDER', 'SUBTRACT', True, 'ROUND', 0.25, 3, 0.1),
        ('TORUS', 'UNION', True, 'CHAMFER', 0.2, 3, 0.0),
        ('CONE', 'INTERSECT', True, 'STEPS', 0.4, 4, 0.0),
        ('BOX', 'SUBTRACT', True, 'NONE', 0.0, 1, 0.0),
        ('SPHERE', 'INTERSECT', True, 'SMOOTH', 0.5, 1, 0.0),
        ('CYLINDER', 'UNION', True, 'STEPS', 0.3, 2, 0.0),
        ('CONE', 'SUBTRACT', True, 'CHAMFER', 0.35, 1, 0.0),
        ('TORUS', 'INTERSECT', True, 'ROUND', 0.3, 1, 0.0),
        ('CAPSULE', 'UNION', False, 'SMOOTH', 0.3, 1, 0.0),
        ('PYRAMID', 'UNION', True, 'SMOOTH', 0.4, 1, 0.0),
        ('PRISM', 'SUBTRACT', True, 'ROUND', 0.2, 1, 0.12),
        ('PRISM', 'UNION', True, 'NONE', 0.0, 1, 0.0),
    ]
    reset_scene()
    fusion = ops.create_fusion(C, Vector((0.5, -0.3, 0.2)))
    fusion.rotation_euler = Euler((0.2, 0.1, 0.7))
    fusion.scale = (1.0, 1.0, 1.0)
    fs = fusion.sdf_fusion
    fs.blend = 0.3
    fs.blend_type = 'SMOOTH'
    fs.steps = 3
    for prim, op, custom, btype, blend, steps, rounding in configs:
        sh = ops.add_shape(C, fusion, prim, Vector((rng.uniform(-1.5, 1.5), rng.uniform(-1.5, 1.5), rng.uniform(-1.5, 1.5))))
        sh.rotation_euler = Euler((rng.uniform(-3, 3), rng.uniform(-3, 3), rng.uniform(-3, 3)))
        if prim in {'BOX', 'CYLINDER', 'PYRAMID', 'PRISM', 'CAPSULE'}:
            sh.scale = (rng.uniform(0.5, 1.6), rng.uniform(0.5, 1.6), rng.uniform(0.5, 1.6))
        else:
            sh.scale = (rng.uniform(0.5, 1.6),) * 3 if rng.random() < 0.5 else (rng.uniform(0.5, 1.6), rng.uniform(0.5, 1.6), rng.uniform(0.5, 1.6))
        st = sh.sdf_shape
        st.operation = op
        st.use_custom_blend = custom
        st.blend_type = btype
        st.blend = blend
        st.steps = steps
        st.rounding = rounding
        st.tube = 0.3
        st.top_radius = 0.4
        st.sides = 5 if prim == 'PRISM' and op == 'SUBTRACT' else 6
    C.view_layer.update()

    npts = 6000
    pts = np.array([[rng.uniform(-4, 4) for _ in range(3)] for _ in range(npts)])
    got = sample_field(fusion, pts)
    shapes = reference_shapes(fusion)

    # per-primitive check: evaluate each shape alone through the sampler vs reference
    for i, sh in enumerate(nodes.iter_shapes(fusion)):
        pass
    exp = sdf_ref.evaluate_fusion(pts, shapes, fs.blend, fs.blend_type, fs.steps)
    err = np.abs(got - exp)
    check(np.isfinite(got).all(), "field has no NaN/inf")
    check(err.max() < 2e-3, f"full fusion field matches reference (max err {err.max():.2e}, mean {err.mean():.2e})")

    # every primitive on its own (through include toggles)
    all_shapes = list(nodes.iter_shapes(fusion))
    for keep in all_shapes:
        for sh in all_shapes:
            sh.sdf_shape['include'] = (sh == keep)
        nodes.rebuild(fusion)
        got1 = sample_field(fusion, pts)
        exp1 = sdf_ref.evaluate_fusion(pts, reference_shapes(fusion), fs.blend, fs.blend_type, fs.steps)
        e1 = np.abs(got1 - exp1).max()
        check(e1 < 2e-3, f"primitive {keep.sdf_shape.primitive} alone matches reference (max err {e1:.2e})")
    for sh in all_shapes:
        sh.sdf_shape['include'] = True
    nodes.rebuild(fusion)

    # every (operation, blend type) pair on a box+sphere pair
    reset_scene()
    fusion = ops.create_fusion(C, Vector((0, 0, 0)))
    fs = fusion.sdf_fusion
    a = ops.add_shape(C, fusion, 'BOX', Vector((0, 0, 0)))
    b = ops.add_shape(C, fusion, 'SPHERE', Vector((1.1, 0.2, 0.5)))
    b.scale = (0.9, 0.9, 0.9)
    pts2 = np.array([[rng.uniform(-2.5, 2.5) for _ in range(3)] for _ in range(4000)])
    for op in nodes.OPERATIONS:
        for bt in nodes.BLEND_TYPES:
            b.sdf_shape['operation'] = list(nodes.OPERATIONS).index(op)
            fs['blend_type'] = [it[0] for it in sdf_fusion.props.BLEND_ITEMS].index(bt)
            fs['blend'] = 0.35
            fs['steps'] = 3
            nodes.rebuild(fusion)
            got2 = sample_field(fusion, pts2)
            exp2 = sdf_ref.evaluate_fusion(pts2, reference_shapes(fusion), 0.35, bt, 3)
            e2 = np.abs(got2 - exp2).max()
            check(e2 < 2e-3, f"op {op:9s} blend {bt:8s} matches reference (max err {e2:.2e})")


# ----------------------------------------------------------------------------
# 2. Phase 1 acceptance flow
# ----------------------------------------------------------------------------

def test_acceptance_flow():
    print("\n[2] acceptance flow: box + sphere smooth union, live transforms, convert")
    reset_scene()
    C.scene.cursor.location = (0, 0, 0)
    # 1. add an SDF cube  2. add an SDF sphere
    bpy.ops.sdf_fusion.add_shape(primitive='BOX')
    box = C.active_object
    C.scene.cursor.location = (1.3, 0.0, 0.7)
    bpy.ops.sdf_fusion.add_shape(primitive='SPHERE')
    sphere = C.active_object
    sphere.scale = (0.8, 0.8, 0.8)
    fusion = ops.find_fusion(C)
    check(fusion is not None and fusion.sdf_fusion.enabled, "fusion object created automatically")
    check(box.sdf_shape.enabled and sphere.sdf_shape.enabled, "shapes flagged as SDF shapes")
    check(box.parent == fusion and sphere.parent == fusion, "shapes parented to the fusion")
    check(len(fusion.sdf_fusion.shapes) == 2, "fusion lists both shapes")
    mod = fusion.modifiers.get(nodes.MODIFIER_NAME)
    check(mod is not None and mod.type == 'NODES' and mod.node_group is not None, "geometry nodes modifier attached")

    fs = fusion.sdf_fusion
    # hard union first
    fs.blend_type = 'NONE'
    s0 = mesh_stats(evaluated_mesh(fusion))
    check(s0['verts'] > 0, f"hard union produces a mesh ({s0['verts']} verts, {s0['faces']} faces)")
    check(s0['islands'] == 1, "hard union is one connected surface")
    check(abs(s0['volume'] - 8.0) < 8.0 * 0.5 and s0['volume'] > 8.0, f"union volume larger than the cube alone ({s0['volume']:.3f})")

    # 3. smooth union  5. adjust blend radius  6. objects melt
    fs.blend_type = 'SMOOTH'
    fs.blend = 0.0
    sA = mesh_stats(evaluated_mesh(fusion))
    fs.blend = 0.6
    sB = mesh_stats(evaluated_mesh(fusion))
    check(sB['volume'] > sA['volume'] + 0.05, f"raising Blend adds material between the shapes ({sA['volume']:.3f} -> {sB['volume']:.3f})")

    def fillet_vertices(res):
        me = evaluated_mesh(fusion)
        co = np.array([v.co[:] for v in me.vertices])
        finv = fusion.matrix_world.inverted()
        shapes = reference_shapes(fusion)
        d_box = sdf_ref.primitive_distance('BOX', (np.c_[co, np.ones(len(co))] @ shapes[0]['matrix_inv_rigid'].T)[:, :3], shapes[0]['scale'])
        d_sph = sdf_ref.primitive_distance('SPHERE', (np.c_[co, np.ones(len(co))] @ shapes[1]['matrix_inv_rigid'].T)[:, :3], shapes[1]['scale'])
        return int(np.sum((d_box > 0.03) & (d_sph > 0.03)))
    n_fillet = fillet_vertices(fs.live_resolution())
    check(n_fillet > 10, f"blended surface has vertices outside both source shapes (fillet region: {n_fillet} verts)")
    fs.blend = 0.0
    check(fillet_vertices(fs.live_resolution()) == 0, "with Blend 0 no fillet vertices exist")
    fs.blend = 0.6

    # 7. moving / scaling / rotating a source object updates the result
    before = mesh_stats(evaluated_mesh(fusion))
    sphere.location.x += 1.0
    after = mesh_stats(evaluated_mesh(fusion))
    check(after['max'][0] > before['max'][0] + 0.9, f"moving the sphere moves the fused result (max x {before['max'][0]:.2f} -> {after['max'][0]:.2f})")
    box.scale = (1.5, 1.0, 1.0)
    after2 = mesh_stats(evaluated_mesh(fusion))
    check(after2['min'][0] < before['min'][0] - 0.4, f"scaling the box grows the fused result (min x {before['min'][0]:.2f} -> {after2['min'][0]:.2f})")
    box.rotation_euler = Euler((0, 0, math.radians(45)))
    after3 = mesh_stats(evaluated_mesh(fusion))
    check(after3['min'][1] < after2['min'][1] - 0.3, f"rotating the box changes the fused result (min y {after2['min'][1]:.2f} -> {after3['min'][1]:.2f})")
    box.rotation_euler = Euler((0, 0, 0))
    box.scale = (1, 1, 1)
    sphere.location.x -= 1.0

    # subtract / intersect
    sphere.sdf_shape.operation = 'SUBTRACT'
    fs.blend_type = 'NONE'
    sub = mesh_stats(evaluated_mesh(fusion))
    check(0 < sub['volume'] < 8.0, f"subtract removes material from the cube ({sub['volume']:.3f} < 8)")
    sphere.sdf_shape.operation = 'INTERSECT'
    inter = mesh_stats(evaluated_mesh(fusion))
    check(0 < inter['volume'] < min(8.0, 4.0 / 3.0 * math.pi * 0.8 ** 3), f"intersect keeps only the overlap ({inter['volume']:.3f})")
    sphere.sdf_shape.operation = 'UNION'
    fs.blend_type = 'SMOOTH'

    # smooth subtract also blends (soft edge) -> volume differs from hard subtract
    sphere.sdf_shape.operation = 'SUBTRACT'
    fs.blend = 0.4
    ssub = mesh_stats(evaluated_mesh(fusion))
    check(ssub['volume'] < sub['volume'] - 0.02, f"smooth subtract carves a soft, wider cut ({ssub['volume']:.3f} < {sub['volume']:.3f})")
    sphere.sdf_shape.operation = 'UNION'
    fs.blend = 0.6

    # material assigned to the fusion object reaches the generated mesh
    mat = bpy.data.materials.new('FusionMat')
    fusion.data.materials.append(mat)
    C.view_layer.update()
    C.evaluated_depsgraph_get()
    me_m = evaluated_mesh(fusion)
    used = {me_m.materials[p.material_index] for p in me_m.polygons}
    check(len(used) == 1 and next(iter(used)).name == mat.name, "fusion material is applied to the generated mesh")

    # quality presets change the resolution actually used
    fs.quality = 'LOW'
    low = mesh_stats(evaluated_mesh(fusion))
    fs.quality = 'HIGH'
    high = mesh_stats(evaluated_mesh(fusion))
    fs.quality = 'MEDIUM'
    check(high['verts'] > low['verts'] * 4, f"quality presets change mesh density (LOW {low['verts']} verts, HIGH {high['verts']} verts)")

    # live toggle
    fs.live = False
    check(mod.show_viewport is False, "Live Update off disables the modifier in the viewport")
    fs.live = True
    check(mod.show_viewport is True, "Live Update on re-enables the modifier")

    # 8/9. convert to a regular mesh
    preview_verts = mesh_stats(evaluated_mesh(fusion))['verts']
    fs.final_resolution = 160
    bpy.ops.sdf_fusion.convert()
    result = C.active_object
    check(result is not None and result.type == 'MESH' and result != fusion, "convert creates a new mesh object")
    check(len(result.modifiers) == 0, "converted object has no modifiers")
    rs = mesh_stats(result.data)
    check(rs['faces'] > 0 and rs['islands'] == 1, f"converted mesh is a single closed surface ({rs['verts']} verts, {rs['faces']} faces)")
    check(rs['verts'] > preview_verts * 2, f"converted mesh uses the final resolution ({rs['verts']} > {preview_verts} preview verts)")
    check(abs(rs['volume'] - sB['volume']) / sB['volume'] < 0.05, f"converted volume matches the preview ({rs['volume']:.3f} vs {sB['volume']:.3f})")
    check(fusion.hide_get() and box.hide_get(), "fusion setup hidden after convert (kept for further edits)")
    check(fusion.hide_render and box.hide_render, "hidden setup is also disabled in renders")
    C.view_layer.objects.active = fusion
    bpy.ops.sdf_fusion.show_setup()
    check(not fusion.hide_get() and not fusion.hide_render and not box.hide_get(), "Show Fusion Setup restores viewport and render visibility")
    for ob in (fusion, box, sphere):
        ob.hide_set(True)
    fusion.hide_render = True
    check(nodes.get_tree(fusion, create=False).nodes['RESOLUTION'].integer == fs.live_resolution(), "preview resolution restored after convert")
    check(all(p.use_smooth for p in result.data.polygons), "converted mesh is shade smooth")
    check(len(result.data.materials) == 1 and result.data.materials[0] == mat and all(p.material_index == 0 for p in result.data.polygons), "converted mesh keeps the material (single clean slot)")

    # deleting a shape object prunes the fusion via the depsgraph handler
    fusion.hide_set(False)
    box.hide_set(False)
    sphere.hide_set(False)
    bpy.data.objects.remove(sphere, do_unlink=True)
    C.view_layer.update()
    C.evaluated_depsgraph_get()
    check(len(fusion.sdf_fusion.shapes) == 1, "deleted shape pruned from the fusion")
    s_single = mesh_stats(evaluated_mesh(fusion))
    check(abs(s_single['volume'] - 8.0) < 0.5, f"remaining cube still evaluates ({s_single['volume']:.3f} ~ 8)")

    # remove operator on the last shape -> empty fusion still valid
    C.view_layer.objects.active = box
    box.select_set(True)
    bpy.ops.sdf_fusion.remove_shape()
    check(len(fusion.sdf_fusion.shapes) == 0, "remove_shape empties the fusion")
    check(mesh_stats(evaluated_mesh(fusion))['verts'] == 0, "empty fusion evaluates to an empty mesh without errors")


def test_primitive_volumes():
    print("\n[3] each primitive alone: closed mesh with the expected volume")
    expected = {
        'BOX': 8.0,
        'SPHERE': 4.0 / 3.0 * math.pi,
        'CYLINDER': math.pi * 2.0,
        'TORUS': 2.0 * math.pi ** 2 * 1.0 * 0.25 ** 2,
        'CONE': math.pi * 1.0 ** 2 * 2.0 / 3.0,
        'CAPSULE': 4.0 / 3.0 * math.pi,                      # unit capsule (r = h) is a sphere
        'PYRAMID': 4.0 * 2.0 / 3.0,
        'PRISM': 3.0 * math.sqrt(3.0) / 2.0 * 2.0,           # hexagon, circumradius 1, height 2
    }
    for prim, vol in expected.items():
        reset_scene()
        fusion = ops.create_fusion(C, Vector((0, 0, 0)))
        fusion.sdf_fusion.quality = 'HIGH'
        ops.add_shape(C, fusion, prim, Vector((0, 0, 0)))
        s = mesh_stats(evaluated_mesh(fusion))
        rel = abs(s['volume'] - vol) / vol
        check(s['islands'] == 1 and rel < 0.06, f"{prim}: volume {s['volume']:.3f} vs analytic {vol:.3f} (rel err {rel:.1%}), {s['islands']} island")


def test_duplicate():
    print("\n[6] duplicate fusion / Shift+D repair")
    reset_scene()
    fusion = ops.create_fusion(C, Vector((0, 0, 0)))
    box = ops.add_shape(C, fusion, 'BOX', Vector((0, 0, 0)))
    sph = ops.add_shape(C, fusion, 'SPHERE', Vector((1.2, 0, 0.6)))
    fs = fusion.sdf_fusion
    fs.blend = 0.5
    C.view_layer.objects.active = fusion
    bpy.ops.sdf_fusion.duplicate_fusion()
    copy = C.active_object
    check(copy is not None and copy != fusion and copy.sdf_fusion.enabled, "Duplicate Fusion creates a new fusion object")
    check(len(copy.sdf_fusion.shapes) == 2 and all(r.object.parent == copy and r.object.sdf_shape.fusion == copy for r in copy.sdf_fusion.shapes), "copied shapes belong to the copy")
    check(copy.modifiers[nodes.MODIFIER_NAME].node_group != fusion.modifiers[nodes.MODIFIER_NAME].node_group, "copy has its own node tree")
    s_orig = mesh_stats(evaluated_mesh(fusion))
    s_copy = mesh_stats(evaluated_mesh(copy))
    check(abs(s_orig['volume'] - s_copy['volume']) < 1e-3 and copy.location.x > fusion.location.x + 1.0, "copy evaluates identically, placed beside the original")
    copy_sph = [r.object for r in copy.sdf_fusion.shapes if r.object.sdf_shape.primitive == 'SPHERE'][0]
    copy_sph.location.x += 1.0
    s_orig2 = mesh_stats(evaluated_mesh(fusion))
    s_copy2 = mesh_stats(evaluated_mesh(copy))
    check(abs(s_orig2['volume'] - s_orig['volume']) < 1e-6 and s_copy2['max'][0] > s_copy['max'][0] + 0.9, "editing the copy leaves the original untouched")

    # Shift+D style duplicate of fusion + shapes through Blender's own operator
    for o in C.view_layer.objects:
        o.select_set(o in (fusion, box, sph))
    C.view_layer.objects.active = fusion
    try:
        bpy.ops.object.duplicate()
        dup = [o for o in C.scene.objects if o.sdf_fusion.enabled and o not in (fusion, copy)]
        check(len(dup) == 1, "Blender duplicate produced one more fusion")
        d = dup[0]
        d.location.x -= 6.0
        C.view_layer.update()
        C.evaluated_depsgraph_get()      # handler: split shared tree / adopt shapes
        check(d.modifiers[nodes.MODIFIER_NAME].node_group != fusion.modifiers[nodes.MODIFIER_NAME].node_group, "handler gave the Shift+D copy its own tree")
        check(len(d.sdf_fusion.shapes) == 2 and all(r.object.sdf_shape.fusion == d for r in d.sdf_fusion.shapes), "Shift+D copy owns its duplicated shapes")
        sd = mesh_stats(evaluated_mesh(d))
        check(abs(sd['volume'] - s_orig['volume']) < 1e-3, "Shift+D copy evaluates like the original")
    except RuntimeError as e:
        print("       (object.duplicate unavailable headless:", e, ")")


def test_animation_and_apply():
    print("\n[7] keyframed settings and Apply Transform repair")
    reset_scene()
    scene = C.scene
    fusion = ops.create_fusion(C, Vector((0, 0, 0)))
    box = ops.add_shape(C, fusion, 'BOX', Vector((0, 0, 0)))
    sph = ops.add_shape(C, fusion, 'SPHERE', Vector((1.2, 0, 0.6)))
    fs = fusion.sdf_fusion
    fs.blend = 0.0
    v0 = mesh_stats(evaluated_mesh(fusion))['volume']
    fs.blend = 0.8
    v8 = mesh_stats(evaluated_mesh(fusion))['volume']
    fs.blend = 0.0
    fusion.keyframe_insert('sdf_fusion.blend', frame=1)
    fs.blend = 0.8
    fusion.keyframe_insert('sdf_fusion.blend', frame=21)
    scene.frame_set(21)
    va = mesh_stats(evaluated_mesh(fusion))['volume']
    scene.frame_set(1)
    vb = mesh_stats(evaluated_mesh(fusion))['volume']
    scene.frame_set(11)
    vm = mesh_stats(evaluated_mesh(fusion))['volume']
    check(abs(va - v8) < 1e-3 and abs(vb - v0) < 1e-3, f"keyframed Blend drives the node tree (frame 21: {va:.3f} vs {v8:.3f}, frame 1: {vb:.3f} vs {v0:.3f})")
    check(vb < vm < va, f"in-between frame interpolates ({vb:.3f} < {vm:.3f} < {va:.3f})")
    fusion.animation_data_clear()
    fs.blend = 0.5

    # keyframed shape colour reaches the mesh
    fs.blend_colors = True
    box.sdf_shape.color = (1.0, 0.0, 0.0, 1.0)
    box.keyframe_insert('sdf_shape.color', frame=1)
    box.sdf_shape.color = (0.0, 0.0, 1.0, 1.0)
    box.keyframe_insert('sdf_shape.color', frame=21)
    scene.frame_set(21)
    me = evaluated_mesh(fusion)
    cols = np.array([c.color[:] for c in me.color_attributes[nodes.COLOR_ATTRIBUTE].data])
    co = np.array([v.co[:] for v in me.vertices])
    far = cols[co[:, 0] < -0.6][:, :3]
    check(len(far) > 0 and np.allclose(far, (0, 0, 1), atol=0.03), "keyframed shape colour drives the vertex colours")
    box.animation_data_clear()
    scene.frame_set(1)
    fs.blend_colors = False

    # Apply Scale / Rotation on a shape must not change the fused result
    box.scale = (2.0, 1.0, 0.5)
    box.rotation_euler = Euler((0, 0, math.radians(30)))
    before = mesh_stats(evaluated_mesh(fusion))
    for o in C.view_layer.objects:
        o.select_set(o == box)
    C.view_layer.objects.active = box
    bpy.ops.object.transform_apply(location=False, rotation=True, scale=True)
    C.view_layer.update()
    C.evaluated_depsgraph_get()          # handler repairs the proxy
    after = mesh_stats(evaluated_mesh(fusion))
    check(np.allclose(box.scale, (2.0, 1.0, 0.5), atol=1e-4), f"Apply Scale folded back into the object scale ({tuple(round(s, 3) for s in box.scale)})")
    check(abs(box.rotation_euler.z - math.radians(30)) < 1e-4, "Apply Rotation folded back into the object rotation")
    check(abs(after['volume'] - before['volume']) < 1e-3 and np.allclose(after['min'], before['min'], atol=1e-3), f"fused result unchanged by Apply Transform ({before['volume']:.3f} -> {after['volume']:.3f})")
    ref = ops.unit_proxy_vertices('BOX', 0.25, 0.0, 6)
    cur = np.array([v.co[:] for v in box.data.vertices])
    check(np.allclose(cur, ref, atol=1e-6), "proxy mesh restored to the unit primitive")


def test_color_blending():
    print("\n[5] colour blending across the seam")
    reset_scene()
    fusion = ops.create_fusion(C, Vector((0, 0, 0)))
    box = ops.add_shape(C, fusion, 'BOX', Vector((0, 0, 0)))
    sph = ops.add_shape(C, fusion, 'SPHERE', Vector((1.3, 0.0, 0.7)))
    sph.scale = (0.8, 0.8, 0.8)
    RED, BLUE, GREEN = (1.0, 0.1, 0.1, 1.0), (0.1, 0.2, 1.0, 1.0), (0.1, 1.0, 0.1, 1.0)
    box.sdf_shape.color = RED
    sph.sdf_shape.color = BLUE
    fs = fusion.sdf_fusion
    fs.blend = 0.6
    fs.blend_type = 'SMOOTH'
    fs.quality = 'HIGH'
    fs.blend_colors = True

    def colours():
        me = evaluated_mesh(fusion)
        attr = me.color_attributes.get(nodes.COLOR_ATTRIBUTE)
        if attr is None:
            return None, None, me
        cols = np.array([c.color[:] for c in attr.data])
        co = np.array([v.co[:] for v in me.vertices])
        return cols, co, me

    cols, co, me = colours()
    check(cols is not None, "generated mesh carries the 'Color' attribute")
    far_box = cols[co[:, 0] < -0.6][:, :3]
    far_sph = cols[co[:, 0] > 1.75][:, :3]
    check(len(far_box) > 0 and np.allclose(far_box, RED[:3], atol=0.02), "box side has the box colour")
    check(len(far_sph) > 0 and np.allclose(far_sph, BLUE[:3], atol=0.02), "sphere side has the sphere colour")
    mid = np.sum((cols[:, 0] > 0.3) & (cols[:, 0] < 0.8))
    check(mid > 30, f"colours cross-fade at the seam ({mid} in-between vertices)")
    check(ops.material_reads_color_attribute(fusion.active_material), "fusion got a material that reads the Color attribute")
    used = {me.materials[p.material_index].name for p in me.polygons}
    check(used == {ops.COLOR_MATERIAL_NAME}, "generated mesh uses the colour material")

    fs.blend_type = 'NONE'
    cols2, co2, _ = colours()
    mid2 = np.sum((cols2[:, 0] > 0.3) & (cols2[:, 0] < 0.8))
    check(mid2 == 0, "hard union switches colour sharply (no in-between vertices)")
    fs.blend_type = 'SMOOTH'

    box.sdf_shape.color = GREEN
    cols3, co3, _ = colours()
    far_box3 = cols3[co3[:, 0] < -0.6][:, :3]
    check(np.allclose(far_box3, GREEN[:3], atol=0.02), "changing a shape colour updates the mesh in place")
    box.sdf_shape.color = RED

    # subtract: the carved surface shows the cutter's colour
    sph.sdf_shape.operation = 'SUBTRACT'
    cols4, co4, _ = colours()
    shapes4 = reference_shapes(fusion)
    hom = np.c_[co4, np.ones(len(co4))]
    d_box = sdf_ref.primitive_distance('BOX', (hom @ shapes4[0]['matrix_inv_rigid'].T)[:, :3], shapes4[0]['scale'])
    d_sph = sdf_ref.primitive_distance('SPHERE', (hom @ shapes4[1]['matrix_inv_rigid'].T)[:, :3], shapes4[1]['scale'])
    carved = cols4[(np.abs(d_sph) < 0.03) & (d_box < -0.3)][:, :3]     # on the cutter, inside the box
    # the cut cross-fades towards the box colour near its rim (blend 0.6), so judge the mean
    check(len(carved) > 20 and carved[:, 2].mean() > 0.6 and carved[:, 0].mean() < 0.5,
          f"smooth subtract paints the cut with the cutter colour ({len(carved)} cut vertices, mean rgb {carved.mean(axis=0).round(2)})")
    sph.sdf_shape.operation = 'UNION'

    # Solid-mode viewport display (Workbench, colour by attribute, flat light)
    scene = C.scene
    cam_data = bpy.data.cameras.new('cam')
    cam = bpy.data.objects.new('cam', cam_data)
    scene.collection.objects.link(cam)
    cam.location = (0.65, -9.0, 0.35)
    cam.rotation_euler = Euler((math.radians(90), 0, 0))
    scene.camera = cam
    scene.render.engine = 'BLENDER_WORKBENCH'
    scene.display.shading.light = 'FLAT'
    scene.display.shading.color_type = 'VERTEX'
    scene.view_settings.view_transform = 'Standard'
    scene.render.resolution_x, scene.render.resolution_y = 160, 120
    scene.render.resolution_percentage = 100
    out = os.path.join(bpy.app.tempdir, 'sdff_color_test.png')
    scene.render.filepath = out
    bpy.ops.render.render(write_still=True)
    img = bpy.data.images.load(out)
    px = np.array(img.pixels[:]).reshape(-1, 4)
    reddish = np.sum((px[:, 0] > 0.6) & (px[:, 2] < 0.45))
    bluish = np.sum((px[:, 2] > 0.6) & (px[:, 0] < 0.45))
    check(reddish > 100 and bluish > 100, f"Solid view (Attribute colour) shows both colours ({reddish} red, {bluish} blue pixels)")
    bpy.data.objects.remove(cam, do_unlink=True)

    bpy.ops.sdf_fusion.convert()
    result = C.active_object
    check(nodes.COLOR_ATTRIBUTE in result.data.color_attributes, "converted mesh keeps the Color attribute")
    check(result.data.materials[0] is not None and ops.material_reads_color_attribute(result.data.materials[0]),
          "converted mesh keeps the colour material")

    # switching colours off removes the attribute again
    fusion.hide_set(False)
    fs.blend_colors = False
    me5 = evaluated_mesh(fusion)
    check(nodes.COLOR_ATTRIBUTE not in me5.color_attributes, "Blend Colors off removes the attribute")


def test_material_blending():
    print("\n[8] per-shape materials: metallic / roughness / transmission / IOR / emission")
    reset_scene()
    fusion = ops.create_fusion(C, Vector((0, 0, 0)))
    box = ops.add_shape(C, fusion, 'BOX', Vector((0, 0, 0)))
    sph = ops.add_shape(C, fusion, 'SPHERE', Vector((1.3, 0.0, 0.7)))
    sph.scale = (0.8, 0.8, 0.8)
    fs = fusion.sdf_fusion
    fs.blend = 0.6
    fs.quality = 'HIGH'

    def principled_mat(name, color, metallic, roughness, transmission, ior, emission, strength):
        m = bpy.data.materials.new(name)
        m.use_nodes = True
        b = next(n for n in m.node_tree.nodes if n.bl_idname == 'ShaderNodeBsdfPrincipled')
        b.inputs['Base Color'].default_value = color
        b.inputs['Metallic'].default_value = metallic
        b.inputs['Roughness'].default_value = roughness
        b.inputs['Transmission Weight'].default_value = transmission
        b.inputs['IOR'].default_value = ior
        b.inputs['Emission Color'].default_value = emission
        b.inputs['Emission Strength'].default_value = strength
        return m
    mA = principled_mat('A', (1, 0.1, 0.1, 1), 1.0, 0.2, 0.0, 1.45, (0, 0, 0, 1), 0.0)
    mB = principled_mat('B', (0.1, 0.2, 1, 1), 0.0, 0.9, 0.5, 1.6, (0, 1, 0, 1), 2.0)
    box.data.materials.append(mA)
    sph.data.materials.append(mB)
    box.sdf_shape.use_material = True
    sph.sdf_shape.use_material = True
    check(abs(box.sdf_shape.metallic - 1.0) < 1e-6 and abs(sph.sdf_shape.roughness - 0.9) < 1e-6 and abs(sph.sdf_shape.ior - 1.6) < 1e-6,
          "Use Shape Material pulls Principled values into the shape")
    fs.blend_colors = True
    me = evaluated_mesh(fusion)
    names = {a.name for a in me.attributes}
    check({nodes.COLOR_ATTRIBUTE, nodes.SURFACE_ATTRIBUTE, nodes.EXTRA_ATTRIBUTE, nodes.EMISSION_ATTRIBUTE} <= names, "surface, extra and emission attributes stored on the mesh")
    co = np.array([v.co[:] for v in me.vertices])
    surf = np.array([a.vector[:] for a in me.attributes[nodes.SURFACE_ATTRIBUTE].data])
    extra = np.array([a.vector[:] for a in me.attributes[nodes.EXTRA_ATTRIBUTE].data])
    emis = np.array([a.color[:] for a in me.attributes[nodes.EMISSION_ATTRIBUTE].data])
    far_a, far_b = co[:, 0] < -0.6, co[:, 0] > 1.75
    check(np.allclose(surf[far_a], (1.0, 0.2, 0.0), atol=0.02) and np.allclose(surf[far_b], (0.0, 0.9, 0.5), atol=0.02), "metallic / roughness / transmission per side")
    check(np.allclose(extra[far_a][:, :2], (1.45, 0.0), atol=0.02) and np.allclose(extra[far_b][:, :2], (1.6, 2.0), atol=0.02), "IOR / emission strength per side")
    check(np.allclose(emis[far_b][:, :3], (0, 1, 0), atol=0.02), "emission colour per side")
    mid = np.sum((surf[:, 0] > 0.2) & (surf[:, 0] < 0.8))
    check(mid > 30, f"metallic cross-fades across the seam ({mid} in-between vertices)")
    mat = fusion.active_material
    kinds = {n.bl_idname for n in mat.node_tree.nodes}
    check('ShaderNodeAttribute' in kinds and 'ShaderNodeVertexColor' in kinds and mat.name == ops.COLOR_MATERIAL_NAME, "fusion material reads the blended attributes")
    bsdf = next(n for n in mat.node_tree.nodes if n.bl_idname == 'ShaderNodeBsdfPrincipled')
    check(all(bsdf.inputs[k].is_linked for k in ('Base Color', 'Metallic', 'Roughness', 'Transmission Weight', 'IOR', 'Emission Color', 'Emission Strength')), "all Principled inputs are driven")

    # editing the shape's material propagates automatically
    next(n for n in mA.node_tree.nodes if n.bl_idname == 'ShaderNodeBsdfPrincipled').inputs['Roughness'].default_value = 0.7
    C.view_layer.update()
    C.evaluated_depsgraph_get()
    me2 = evaluated_mesh(fusion)
    surf2 = np.array([a.vector[:] for a in me2.attributes[nodes.SURFACE_ATTRIBUTE].data])
    co2 = np.array([v.co[:] for v in me2.vertices])
    check(np.allclose(surf2[co2[:, 0] < -0.6][:, 1], 0.7, atol=0.02), "changing the shape material updates the fusion automatically")

    bpy.ops.sdf_fusion.convert()
    result = C.active_object
    check({nodes.SURFACE_ATTRIBUTE, nodes.EXTRA_ATTRIBUTE, nodes.EMISSION_ATTRIBUTE} <= {a.name for a in result.data.attributes}, "converted mesh keeps the material attributes")


def test_timing():
    print("\n[4] timings (box + sphere smooth union)")
    reset_scene()
    fusion = ops.create_fusion(C, Vector((0, 0, 0)))
    ops.add_shape(C, fusion, 'BOX', Vector((0, 0, 0)))
    ops.add_shape(C, fusion, 'SPHERE', Vector((1.2, 0, 0.6)))
    fs = fusion.sdf_fusion
    fs.quality = 'CUSTOM'
    for res in (32, 64, 128, 192, 256):
        fs.resolution = res
        t = time.perf_counter()
        me = evaluated_mesh(fusion)
        dt = time.perf_counter() - t
        print(f"       resolution {res:4d}: {dt*1000:8.1f} ms  ({len(me.vertices)} verts)")
    PASSES.append("timings collected")


def main():
    t0 = time.perf_counter()
    for test in (test_field_matches_reference, test_acceptance_flow, test_primitive_volumes, test_duplicate, test_animation_and_apply, test_color_blending, test_material_blending, test_timing):
        try:
            test()
        except Exception:
            traceback.print_exc()
            FAILURES.append(f"{test.__name__} raised an exception")
    print(f"\n{len(PASSES)} passed, {len(FAILURES)} failed in {time.perf_counter() - t0:.1f}s")
    for f in FAILURES:
        print("  FAILED:", f)
    sdf_fusion.unregister()
    sys.exit(1 if FAILURES else 0)


if __name__ == '__main__':
    main()
