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
            'radius': st.radius,
            'fill': st.fill,
            'sides': st.sides,
        })
    return shapes


def fusion_params(fusion):
    fs = fusion.sdf_fusion
    return {'mode': fs.mode, 'seam_rule': fs.seam_rule, 'steps': fs.steps,
            'radius_scale': fs.radius_scale, 'fill_scale': fs.fill_scale}


def set_blend(fusion, radius, fill=1.0):
    """Give every shape of the fusion the same Radius / Blend."""
    for ref in fusion.sdf_fusion.shapes:
        if ref.object is not None:
            ref.object.sdf_shape.radius = radius
            ref.object.sdf_shape.fill = fill


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
        # (primitive, operation, radius, fill, rounding)
        ('BOX', 'UNION', 0.30, 0.50, 0.15),
        ('SPHERE', 'UNION', 0.30, 0.50, 0.0),
        ('CYLINDER', 'SUBTRACT', 0.25, 0.80, 0.1),
        ('TORUS', 'UNION', 0.20, 1.00, 0.0),
        ('CONE', 'INTERSECT', 0.40, 0.30, 0.0),
        ('BOX', 'SUBTRACT', 0.00, 0.50, 0.0),
        ('SPHERE', 'INTERSECT', 0.50, 0.10, 0.0),
        ('CYLINDER', 'UNION', 0.30, 0.65, 0.0),
        ('CONE', 'SUBTRACT', 0.35, 0.05, 0.0),
        ('TORUS', 'INTERSECT', 0.30, 0.50, 0.0),
        ('CAPSULE', 'UNION', 0.30, 0.50, 0.0),
        ('PYRAMID', 'UNION', 0.40, 0.90, 0.0),
        ('PRISM', 'SUBTRACT', 0.20, 0.50, 0.12),
        ('PRISM', 'UNION', 0.05, 0.50, 0.0),
    ]
    reset_scene()
    fusion = ops.create_fusion(C, Vector((0.5, -0.3, 0.2)))
    fusion.rotation_euler = Euler((0.2, 0.1, 0.7))
    fs = fusion.sdf_fusion
    fs.radius_scale = 1.2
    fs.fill_scale = 0.9
    fs.steps = 3
    for prim, op, radius, fill, rounding in configs:
        sh = ops.add_shape(C, fusion, prim, Vector((rng.uniform(-1.5, 1.5), rng.uniform(-1.5, 1.5), rng.uniform(-1.5, 1.5))))
        sh.rotation_euler = Euler((rng.uniform(-3, 3), rng.uniform(-3, 3), rng.uniform(-3, 3)))
        if prim in {'BOX', 'CYLINDER', 'PYRAMID', 'PRISM', 'CAPSULE'}:
            sh.scale = (rng.uniform(0.5, 1.6), rng.uniform(0.5, 1.6), rng.uniform(0.5, 1.6))
        else:
            sh.scale = (rng.uniform(0.5, 1.6),) * 3 if rng.random() < 0.5 else (rng.uniform(0.5, 1.6), rng.uniform(0.5, 1.6), rng.uniform(0.5, 1.6))
        st = sh.sdf_shape
        st.operation = op
        st.radius = radius
        st.fill = fill
        st.rounding = rounding
        st.tube = 0.3
        st.top_radius = 0.4
        st.sides = 5 if prim == 'PRISM' and op == 'SUBTRACT' else 6
    C.view_layer.update()

    npts = 6000
    pts = np.array([[rng.uniform(-4, 4) for _ in range(3)] for _ in range(npts)])
    mode_ids = {m: i for i, m in enumerate(nodes.MODES)}
    rule_ids = {r: i for i, r in enumerate(nodes.SEAM_RULES)}
    for mode in nodes.MODES:
        for rule in nodes.SEAM_RULES:
            fs['mode'] = mode_ids[mode]
            fs['seam_rule'] = rule_ids[rule]
            nodes.rebuild(fusion)
            got = sample_field(fusion, pts)
            exp = sdf_ref.evaluate_fusion(pts, reference_shapes(fusion), fusion_params(fusion))
            err = np.abs(got - exp)
            check(np.isfinite(got).all() and err.max() < 2e-3,
                  f"14-shape fusion, mode {mode:5s} seams {rule:8s} matches reference (max err {err.max():.2e})")
    fs['mode'] = 0
    fs['seam_rule'] = 0

    # every primitive on its own (through include toggles)
    all_shapes = list(nodes.iter_shapes(fusion))
    for keep in all_shapes:
        for sh in all_shapes:
            sh.sdf_shape['include'] = (sh == keep)
        nodes.rebuild(fusion)
        got1 = sample_field(fusion, pts)
        exp1 = sdf_ref.evaluate_fusion(pts, reference_shapes(fusion), fusion_params(fusion))
        e1 = np.abs(got1 - exp1).max()
        check(e1 < 2e-3, f"primitive {keep.sdf_shape.primitive} alone matches reference (max err {e1:.2e})")
    for sh in all_shapes:
        sh.sdf_shape['include'] = True
    nodes.rebuild(fusion)

    # every operation x mode x several fills on a box + sphere pair
    reset_scene()
    fusion = ops.create_fusion(C, Vector((0, 0, 0)))
    fs = fusion.sdf_fusion
    a = ops.add_shape(C, fusion, 'BOX', Vector((0, 0, 0)))
    b = ops.add_shape(C, fusion, 'SPHERE', Vector((1.1, 0.2, 0.5)))
    b.scale = (0.9, 0.9, 0.9)
    pts2 = np.array([[rng.uniform(-2.5, 2.5) for _ in range(3)] for _ in range(4000)])
    fs['steps'] = 3
    for op in nodes.OPERATIONS:
        for mode in nodes.MODES:
            for fill in ((0.02, 0.5, 1.0) if mode == 'RAMP' else (1.0,)):
                b.sdf_shape['operation'] = list(nodes.OPERATIONS).index(op)
                fs['mode'] = mode_ids[mode]
                for sh in (a, b):
                    sh.sdf_shape['radius'] = 0.35
                    sh.sdf_shape['fill'] = fill
                nodes.rebuild(fusion)
                got2 = sample_field(fusion, pts2)
                exp2 = sdf_ref.evaluate_fusion(pts2, reference_shapes(fusion), fusion_params(fusion))
                e2 = np.abs(got2 - exp2).max()
                check(np.isfinite(got2).all() and e2 < 2e-3, f"op {op:9s} mode {mode:5s} fill {fill:4.2f} matches reference (max err {e2:.2e})")

    # the ramp reproduces upstream's round operators at Blend 0.5 and the chamfer surface at 1.0
    d0 = np.array([rng.uniform(-1, 1) for _ in range(2000)])
    d1 = np.array([rng.uniform(-1, 1) for _ in range(2000)])
    check(np.allclose(sdf_ref.op_ramp_union(d0, d1, 0.3, 1.0), sdf_ref.op_round_union(d0, d1, 0.3), atol=1e-9)
          and np.allclose(sdf_ref.op_ramp_intersection(d0, d1, 0.3, 1.0), sdf_ref.op_round_intersection(d0, d1, 0.3), atol=1e-9)
          and np.allclose(sdf_ref.op_ramp_difference(d0, d1, 0.3, 1.0), sdf_ref.op_round_difference(d0, d1, 0.3), atol=1e-9),
          "ramp at Blend 1.0 equals upstream's round union / intersection / difference")
    flat = sdf_ref.combine(d0, d1, 'UNION', 'CHAMFER', 0.3, 1.0)
    chamf = sdf_ref.op_chamfer_union(d0, d1, 0.3)
    check(np.all((flat < 0) == (chamf < 0)), "Flat Bevel mode has the same surface as upstream's chamfer union")
    # Blend is linear in the depth of the fill at the seam, relative to the quarter-pipe
    for fill in (0.25, 0.5, 0.75, 1.0):
        p = sdf_ref.fill_to_p(fill)
        depth = 1.0 - 2.0 ** (-1.0 / p)            # seam depth of the unit ramp
        check(abs(depth - fill * sdf_ref.RAMP_K) < 1e-9 and p >= 2.0 - 1e-9, f"Blend {fill:.2f}: seam depth is {fill:.2f} x quarter-pipe (p = {p:.2f} >= 2)")


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
    set_blend(fusion, 0.0)
    s0 = mesh_stats(evaluated_mesh(fusion))
    check(s0['verts'] > 0, f"hard union produces a mesh ({s0['verts']} verts, {s0['faces']} faces)")
    check(s0['islands'] == 1, "hard union is one connected surface")
    check(abs(s0['volume'] - 8.0) < 8.0 * 0.5 and s0['volume'] > 8.0, f"union volume larger than the cube alone ({s0['volume']:.3f})")

    # 3. smooth union  5. adjust blend radius  6. objects melt
    set_blend(fusion, 0.6)
    set_blend(fusion, 0.0)
    sA = mesh_stats(evaluated_mesh(fusion))
    set_blend(fusion, 0.6)
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
    set_blend(fusion, 0.0)
    check(fillet_vertices(fs.live_resolution()) == 0, "with Blend 0 no fillet vertices exist")
    set_blend(fusion, 0.6)

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
    set_blend(fusion, 0.0)
    sub = mesh_stats(evaluated_mesh(fusion))
    check(0 < sub['volume'] < 8.0, f"subtract removes material from the cube ({sub['volume']:.3f} < 8)")
    sphere.sdf_shape.operation = 'INTERSECT'
    inter = mesh_stats(evaluated_mesh(fusion))
    check(0 < inter['volume'] < min(8.0, 4.0 / 3.0 * math.pi * 0.8 ** 3), f"intersect keeps only the overlap ({inter['volume']:.3f})")
    sphere.sdf_shape.operation = 'UNION'
    set_blend(fusion, 0.6)

    # smooth subtract also blends (soft edge) -> volume differs from hard subtract
    sphere.sdf_shape.operation = 'SUBTRACT'
    set_blend(fusion, 0.4)
    ssub = mesh_stats(evaluated_mesh(fusion))
    check(ssub['volume'] < sub['volume'] - 0.02, f"smooth subtract carves a soft, wider cut ({ssub['volume']:.3f} < {sub['volume']:.3f})")
    sphere.sdf_shape.operation = 'UNION'
    set_blend(fusion, 0.6)

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


def test_placement():
    print("\n[11] shapes land at the 3D cursor, also for a brand-new fusion away from the origin")
    reset_scene()
    C.scene.cursor.location = (2.0, 1.0, 0.5)
    bpy.ops.sdf_fusion.add_shape(primitive='BOX')
    box = C.active_object
    fusion = ops.find_fusion(C)
    C.view_layer.update()
    check((box.matrix_world.translation - Vector((2.0, 1.0, 0.5))).length < 1e-5, f"first shape of a new fusion sits at the cursor ({tuple(round(v, 3) for v in box.matrix_world.translation)})")
    check((fusion.matrix_world.translation - Vector((2.0, 1.0, 0.5))).length < 1e-5, "the new fusion sits at the cursor too")
    C.scene.cursor.location = (3.5, 1.0, 1.2)
    bpy.ops.sdf_fusion.add_shape(primitive='SPHERE')
    sph = C.active_object
    C.view_layer.update()
    check((sph.matrix_world.translation - Vector((3.5, 1.0, 1.2))).length < 1e-5, "second shape sits at the moved cursor")
    me = evaluated_mesh(fusion)
    co = np.array([(fusion.matrix_world @ v.co)[:] for v in me.vertices])
    check(abs(co[:, 0].min() - 1.0) < 0.05 and abs(co[:, 2].min() - (-0.5)) < 0.05, f"fused mesh is where the shapes are (world min x {co[:, 0].min():.2f}, min z {co[:, 2].min():.2f})")
    # a rotated, moved fusion: new shapes still land at the cursor in world space
    fusion.rotation_euler = Euler((0.3, 0.2, 0.9))
    fusion.location = (-1.0, 2.0, 0.3)
    C.scene.cursor.location = (0.4, -0.6, 2.0)
    bpy.ops.sdf_fusion.add_shape(primitive='CYLINDER')
    cyl = C.active_object
    C.view_layer.update()
    check((cyl.matrix_world.translation - Vector((0.4, -0.6, 2.0))).length < 1e-5, "shape added to a moved / rotated fusion sits at the cursor")
    C.scene.cursor.location = (0, 0, 0)


def test_mesh_shapes():
    print("\n[12] editable / imported mesh shapes")
    rng = random.Random(11)

    def sdf_at(fusion, pts):
        return sample_field(fusion, pts)

    # (a) an editable cube matches the analytic box within the voxel size
    reset_scene()
    fa = ops.create_fusion(C, Vector((0, 0, 0)))
    ops.add_shape(C, fa, 'BOX', Vector((0, 0, 0)))
    fb = ops.create_fusion(C, Vector((0, 0, 0)))
    cube = ops.add_shape(C, fb, 'MESH', Vector((0, 0, 0)))
    check(cube.sdf_shape.primitive == 'MESH' and len(cube.data.polygons) == 6 and cube.display_type == 'WIRE', "Mesh button adds an editable cube shape")
    pts = np.array([[rng.uniform(-1.6, 1.6) for _ in range(3)] for _ in range(4000)])
    exact = sdf_at(fa, pts)
    got = sdf_at(fb, pts)
    near = np.abs(exact) < 0.3
    voxel = (2.0 + 2 * (0.25 + 0.001)) / 64 * 1.1
    err = np.abs(got - exact)[near]
    check(err.max() < 2.0 * voxel, f"mesh cube field matches the analytic box within 2 voxels (max err {err.max():.3f}, voxel {voxel:.3f})")
    sb = mesh_stats(evaluated_mesh(fb))
    check(abs(sb['volume'] - 8.0) < 0.25 and sb['islands'] == 1, f"mesh cube fuses to a closed cube ({sb['volume']:.3f} ~ 8)")
    fb.sdf_fusion.mesh_detail = 2.0
    err2 = np.abs(sdf_at(fb, pts) - exact)[near]
    check(err2.max() <= err.max() + 1e-6, f"Mesh Detail 2 is at least as accurate (max err {err2.max():.3f})")
    fb.sdf_fusion.mesh_detail = 1.0

    # (b) loop-cut style edit: subdivide the vertical edges and push the new loop out
    bm = bmesh.new()
    bm.from_mesh(cube.data)
    vertical = [e for e in bm.edges if abs(e.verts[0].co.z - e.verts[1].co.z) > 1.5]
    res = bmesh.ops.subdivide_edges(bm, edges=vertical, cuts=1)
    for v in res['geom_inner']:
        if isinstance(v, bmesh.types.BMVert):
            v.co.x *= 1.4
            v.co.y *= 1.4
    bm.to_mesh(cube.data)
    bm.free()
    cube.data.update()
    sb2 = mesh_stats(evaluated_mesh(fb))
    check(sb2['volume'] > 8.0 + 0.8 and sb2['islands'] == 1, f"editing the mesh's vertices changes the fusion live ({sb2['volume']:.3f} > 8)")
    check(ops.mesh_is_closed(cube.data), "edited cube is still closed")

    # (c) an imported object (icosphere) becomes a shape and blends with a box
    reset_scene()
    fusion = ops.create_fusion(C, Vector((0, 0, 0)))
    box = ops.add_shape(C, fusion, 'BOX', Vector((0, 0, 0)))
    me = bpy.data.meshes.new('imported')
    bm = bmesh.new()
    bmesh.ops.create_icosphere(bm, subdivisions=3, radius=0.9)
    bm.to_mesh(me)
    bm.free()
    imp = bpy.data.objects.new('Imported', me)
    C.scene.collection.objects.link(imp)
    imp.location = (1.3, 0.0, 0.7)
    imp.rotation_euler = Euler((0.4, 0.2, 0.1))
    for o in C.view_layer.objects:
        o.select_set(o == imp)
    C.view_layer.objects.active = imp
    C.scene.sdf_active_fusion = fusion
    C.view_layer.update()                     # location/rotation set above must be evaluated first
    world_before = imp.matrix_world.copy()
    bpy.ops.sdf_fusion.adopt_selected()
    C.view_layer.update()
    check(imp.sdf_shape.enabled and imp.sdf_shape.fusion == fusion and imp.parent == fusion and imp.sdf_shape.primitive == 'MESH', "Use Selected Objects turns the imported mesh into a shape")
    check((imp.matrix_world.translation - world_before.translation).length < 1e-5, "adopted object keeps its world transform")
    set_blend(fusion, 0.6)
    me_f = evaluated_mesh(fusion)
    st_f = mesh_stats(me_f)
    check(st_f['volume'] > 8.0 + 1.5 and st_f['islands'] == 1, f"box + imported sphere fuse into one surface ({st_f['volume']:.3f})")
    co = np.array([v.co[:] for v in me_f.vertices])
    d_box = sdf_ref.sd_box(co, (1, 1, 1))
    d_sph = np.linalg.norm(co - np.array([1.3, 0.0, 0.7]), axis=1) - 0.9
    fillet = int(np.sum((d_box > 0.05) & (d_sph > 0.05)))
    check(fillet > 50, f"the blend fills a fillet between box and imported mesh ({fillet} verts outside both)")

    # (d) Apply Scale on a mesh shape is simply allowed (geometry is used as is)
    v_before = mesh_stats(evaluated_mesh(fusion))['volume']
    imp.scale = (1.3, 1.0, 0.8)
    v_scaled = mesh_stats(evaluated_mesh(fusion))['volume']
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
    C.view_layer.update()
    C.evaluated_depsgraph_get()
    v_applied = mesh_stats(evaluated_mesh(fusion))['volume']
    check(abs(v_applied - v_scaled) < 0.05 and np.allclose(imp.scale, (1, 1, 1)), f"Apply Scale on a mesh shape keeps the result ({v_scaled:.3f} -> {v_applied:.3f}) and is not undone")

    # (e) release: the object becomes an ordinary object again
    world_before = imp.matrix_world.copy()
    bpy.ops.sdf_fusion.release_shape()
    C.view_layer.update()
    check(not imp.sdf_shape.enabled and imp.parent is None and not imp.hide_render and imp.display_type == 'TEXTURED', "Release returns the object to a normal mesh")
    check((imp.matrix_world.translation - world_before.translation).length < 1e-5, "released object stays where it was")
    check(abs(mesh_stats(evaluated_mesh(fusion))['volume'] - 8.0) < 0.05, "fusion is back to the box alone")

    # (f) Make Editable on a primitive keeps its look
    sph = ops.add_shape(C, fusion, 'SPHERE', Vector((1.2, 0, 0.6)))
    sph.scale = (0.8, 0.8, 0.8)
    v_prim = mesh_stats(evaluated_mesh(fusion))['volume']
    C.view_layer.objects.active = sph
    sph.select_set(True)
    bpy.ops.sdf_fusion.make_editable()
    v_mesh = mesh_stats(evaluated_mesh(fusion))['volume']
    check(sph.sdf_shape.primitive == 'MESH' and abs(v_mesh - v_prim) / v_prim < 0.03, f"Make Editable keeps the shape ({v_prim:.3f} -> {v_mesh:.3f})")
    sph.sdf_shape.primitive = 'SPHERE'
    check(abs(mesh_stats(evaluated_mesh(fusion))['volume'] - v_prim) < 1e-3, "switching back to Sphere restores the exact primitive")

    # (g) open meshes are detected
    plane = bpy.data.meshes.new('plane')
    plane.from_pydata([(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)], [], [(0, 1, 2, 3)])
    check(not ops.mesh_is_closed(plane) and ops.mesh_is_closed(box.data), "closed-mesh check tells a plane from a cube")

    # (h) convert with a mesh shape
    sph.sdf_shape.primitive = 'MESH'
    bpy.ops.sdf_fusion.convert()
    result = C.active_object
    check(result.type == 'MESH' and len(result.modifiers) == 0 and mesh_stats(result.data)['islands'] == 1, "Convert to Mesh works with editable shapes")


def test_mirror_and_hollow():
    print("\n[13] mirror and hollow")
    reset_scene()
    fusion = ops.create_fusion(C, Vector((0, 0, 0)))
    box = ops.add_shape(C, fusion, 'BOX', Vector((1.5, 0.0, 0.0)))
    box.scale = (0.5, 0.5, 0.5)
    fs = fusion.sdf_fusion
    s0 = mesh_stats(evaluated_mesh(fusion))
    fs.mirror_x = True
    s1 = mesh_stats(evaluated_mesh(fusion))
    check(abs(s1['volume'] - 2 * s0['volume']) < 0.1 * s0['volume'] and s1['islands'] == 2 and s1['min'][0] < -1.9, f"Mirror X reflects the model ({s0['volume']:.3f} -> {s1['volume']:.3f}, 2 islands, min x {s1['min'][0]:.2f})")
    fs.mirror_z = True
    box.location.z = 1.0
    s2 = mesh_stats(evaluated_mesh(fusion))
    check(abs(s2['volume'] - 4 * s0['volume']) < 0.2 * s0['volume'] and s2['islands'] == 4, f"Mirror X+Z gives four copies ({s2['volume']:.3f})")
    fs.mirror_x = False
    fs.mirror_z = False
    box.location = (0, 0, 0)
    box.scale = (1, 1, 1)
    fs.shell = 0.2
    s3 = mesh_stats(evaluated_mesh(fusion))
    expect = (2.4 ** 3 - 1.6 ** 3)
    check(abs(s3['volume'] - expect) / expect < 0.05 and s3['islands'] == 2, f"Hollow 0.2 makes a wall (volume {s3['volume']:.3f} ~ {expect:.3f}, inner + outer surface)")
    fs.shell = 0.0
    s4 = mesh_stats(evaluated_mesh(fusion))
    check(abs(s4['volume'] - 8.0) < 0.05 and s4['islands'] == 1, "Hollow 0 is solid again")


def test_cutters_and_guides():
    print("\n[9] cutters always cut, guide display, Shift+D on a shape")
    reset_scene()
    fusion = ops.create_fusion(C, Vector((0, 0, 0)))
    box = ops.add_shape(C, fusion, 'BOX', Vector((0, 0, 0)))
    sph = ops.add_shape(C, fusion, 'SPHERE', Vector((1.0, 0.0, 0.6)))
    fs = fusion.sdf_fusion
    set_blend(fusion, 0.0)
    sphere_vol = 4.0 / 3.0 * math.pi
    # pressing Subtract on the FIRST shape must carve the other one
    box.sdf_shape.operation = 'SUBTRACT'
    order = [r.object for r in fs.shapes]
    check(order == [sph, box], "a shape switched to Subtract moves behind the unions")
    s = mesh_stats(evaluated_mesh(fusion))
    check(0.2 < s['volume'] < sphere_vol - 0.2, f"Subtract on the first shape carves the other one ({s['volume']:.3f} < {sphere_vol:.3f})")
    box.sdf_shape.operation = 'INTERSECT'
    s2 = mesh_stats(evaluated_mesh(fusion))
    check(0.2 < s2['volume'] < sphere_vol - 0.2 and abs(s['volume'] + s2['volume'] - sphere_vol) < 0.05,
          f"Intersect keeps exactly what Subtract removed ({s2['volume']:.3f} + {s['volume']:.3f} = sphere)")
    box.sdf_shape.operation = 'UNION'
    sph.sdf_shape.operation = 'SUBTRACT'
    s3 = mesh_stats(evaluated_mesh(fusion))
    check(s3['volume'] < 8.0 - 0.2 and [r.object for r in fs.shapes] == [box, sph], f"Subtract on the other shape carves the box ({s3['volume']:.3f} < 8)")
    # manual ordering still available
    fs.auto_order = False
    fs.shapes.move(1, 0)
    nodes.rebuild(fusion)
    s4 = mesh_stats(evaluated_mesh(fusion))
    check(s4['volume'] > 8.0, "with Cutters Last off the order is strictly manual (first shape acts as base)")
    fs.auto_order = True
    check([r.object for r in fs.shapes] == [box, sph], "turning Cutters Last back on re-sorts the list")

    # guides
    check(box.display_type == 'BOUNDS' and box.display_bounds_type == 'BOX' and sph.display_bounds_type == 'SPHERE', "shapes are drawn as bounds guides by default")
    pyr = ops.add_shape(C, fusion, 'PYRAMID', Vector((0, 2, 0)))
    check(pyr.display_type == 'WIRE', "pyramid keeps its small wire proxy")
    fs.guide_display = 'WIRE'
    check(box.display_type == 'WIRE' and sph.display_type == 'WIRE', "Guides: Wire switches all shapes")
    fs.guide_display = 'BOUNDS'
    sph.sdf_shape.primitive = 'CAPSULE'
    check(sph.display_bounds_type == 'CAPSULE', "changing the primitive updates the guide")
    sph.sdf_shape.primitive = 'SPHERE'

    # Shift+D on one shape adds the copy to the same fusion
    n_before = len(fs.shapes)
    for o in C.view_layer.objects:
        o.select_set(o == box)
    C.view_layer.objects.active = box
    try:
        bpy.ops.object.duplicate()
        C.view_layer.update()
        C.evaluated_depsgraph_get()
        copies = [o for o in C.scene.objects if o.sdf_shape.enabled and o.parent == fusion and o not in (box, sph, pyr)]
        check(len(copies) == 1 and len(fs.shapes) == n_before + 1 and copies[0] in [r.object for r in fs.shapes], "Shift+D copy joined the same fusion")
        cp = copies[0]
        check(cp.data != box.data and cp.sdf_shape.fusion == fusion, "copy has its own proxy mesh and fusion link")
        before = mesh_stats(evaluated_mesh(fusion))
        cp.location.x -= 3.0
        after = mesh_stats(evaluated_mesh(fusion))
        check(after['min'][0] < before['min'][0] - 2.5, "moving the copy changes the fused result")
        check([r.object.sdf_shape.operation for r in fs.shapes] == sorted([r.object.sdf_shape.operation for r in fs.shapes], key=lambda o: nodes.OP_ORDER[o]), "list stays unions-first after adoption")
    except RuntimeError as e:
        print("       (object.duplicate unavailable headless:", e, ")")


def test_duplicate():
    print("\n[6] duplicate fusion / Shift+D repair")
    reset_scene()
    fusion = ops.create_fusion(C, Vector((0, 0, 0)))
    box = ops.add_shape(C, fusion, 'BOX', Vector((0, 0, 0)))
    sph = ops.add_shape(C, fusion, 'SPHERE', Vector((1.2, 0, 0.6)))
    fs = fusion.sdf_fusion
    set_blend(fusion, 0.5)
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
    set_blend(fusion, 0.8)
    fs.radius_scale = 0.0
    v0 = mesh_stats(evaluated_mesh(fusion))['volume']
    fs.radius_scale = 1.0
    v8 = mesh_stats(evaluated_mesh(fusion))['volume']
    fs.radius_scale = 0.0
    fusion.keyframe_insert('sdf_fusion.radius_scale', frame=1)
    fs.radius_scale = 1.0
    fusion.keyframe_insert('sdf_fusion.radius_scale', frame=21)
    scene.frame_set(21)
    va = mesh_stats(evaluated_mesh(fusion))['volume']
    scene.frame_set(1)
    vb = mesh_stats(evaluated_mesh(fusion))['volume']
    scene.frame_set(11)
    vm = mesh_stats(evaluated_mesh(fusion))['volume']
    check(abs(va - v8) < 1e-3 and abs(vb - v0) < 1e-3, f"keyframed Radius multiplier drives the node tree (frame 21: {va:.3f} vs {v8:.3f}, frame 1: {vb:.3f} vs {v0:.3f})")
    check(vb < vm < va, f"in-between frame interpolates ({vb:.3f} < {vm:.3f} < {va:.3f})")
    fusion.animation_data_clear()
    fs.radius_scale = 1.0

    # a per-shape Radius can be keyframed too
    set_blend(fusion, 0.0)
    h0 = mesh_stats(evaluated_mesh(fusion))['volume']
    sph.sdf_shape.radius = 0.0
    sph.keyframe_insert('sdf_shape.radius', frame=1)
    sph.sdf_shape.radius = 0.7
    sph.keyframe_insert('sdf_shape.radius', frame=21)
    fs.seam_rule = 'LATEST'
    scene.frame_set(21)
    h21 = mesh_stats(evaluated_mesh(fusion))['volume']
    scene.frame_set(1)
    h1 = mesh_stats(evaluated_mesh(fusion))['volume']
    check(abs(h1 - h0) < 1e-3 and h21 > h0 + 0.05, f"keyframed per-shape Radius drives the node tree ({h1:.3f} -> {h21:.3f})")
    sph.animation_data_clear()
    fs.seam_rule = 'SHARPER'
    set_blend(fusion, 0.5)

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


def test_ramp_family():
    print("\n[10] ramp blend family: Radius / Blend per shape, seam rules, migration")
    reset_scene()
    fusion = ops.create_fusion(C, Vector((0, 0, 0)))
    box = ops.add_shape(C, fusion, 'BOX', Vector((0, 0, 0)))
    sph = ops.add_shape(C, fusion, 'SPHERE', Vector((1.3, 0.0, 0.7)))
    sph.scale = (0.8, 0.8, 0.8)
    fs = fusion.sdf_fusion
    fs.quality = 'HIGH'
    check(abs(box.sdf_shape.radius - 0.25) < 1e-6 and abs(box.sdf_shape.fill - 1.0) < 1e-6 and fs.mode == 'RAMP' and fs.seam_rule == 'SHARPER',
          "new shapes start with Radius 0.25 / Blend 1.0 (full quarter-pipe), ramp mode, sharper-wins seams")

    def vol():
        return mesh_stats(evaluated_mesh(fusion))['volume']
    set_blend(fusion, 0.0)
    hard = vol()
    vols = []
    for fill in (0.05, 0.25, 0.5, 0.75, 1.0):
        set_blend(fusion, 0.6, fill)
        vols.append(vol())
    check(all(b_ > a_ + 1e-3 for a_, b_ in zip(vols, vols[1:])) and vols[0] > hard - 0.02,
          "Blend (fill) adds material monotonically: " + " < ".join(f"{v:.3f}" for v in vols))
    rv = []
    for radius in (0.1, 0.3, 0.6, 0.9):
        set_blend(fusion, radius, 1.0)
        rv.append(vol())
    check(all(b_ > a_ + 1e-3 for a_, b_ in zip(rv, rv[1:])), "Radius adds material monotonically: " + " < ".join(f"{v:.3f}" for v in rv))
    set_blend(fusion, 0.6, 1.0)
    fs.radius_scale = 0.0
    check(abs(vol() - hard) < 2e-3, "Radius multiplier 0 gives the hard boolean")
    fs.radius_scale = 0.5
    half = vol()
    set_blend(fusion, 0.3, 1.0)
    fs.radius_scale = 1.0
    check(abs(vol() - half) < 1e-4, "Radius multiplier scales every shape's Radius")

    # the ramp is bounded: it never reaches further than Radius and never beyond the flat bevel
    for fill in (0.3, 0.6, 1.0):
        set_blend(fusion, 0.6, fill)
        me = evaluated_mesh(fusion)
        co = np.array([v.co[:] for v in me.vertices])
        shapes = reference_shapes(fusion)
        hom = np.c_[co, np.ones(len(co))]
        d_box = sdf_ref.primitive_distance('BOX', (hom @ shapes[0]['matrix_inv_rigid'].T)[:, :3], shapes[0]['scale'])
        d_sph = sdf_ref.primitive_distance('SPHERE', (hom @ shapes[1]['matrix_inv_rigid'].T)[:, :3], shapes[1]['scale'])
        fil = (d_box > 0.02) & (d_sph > 0.02)
        tol = 0.03
        # never further than Radius, and never fuller than the circular quarter-pipe:
        # (r - d0)^2 + (r - d1)^2 >= r^2 on the surface  <=>  the ramp always curves inward
        circ = np.sqrt((0.6 - d_box[fil]) ** 2 + (0.6 - d_sph[fil]) ** 2)
        ok = fil.sum() > 10 and d_box[fil].max() < 0.6 + tol and d_sph[fil].max() < 0.6 + tol and circ.min() > 0.6 - tol
        check(ok, f"Blend {fill:.1f}: ramp stays within Radius and never fuller than the quarter-pipe ({int(fil.sum())} fillet verts, min {circ.min():.3f} >= 0.6)")

    # per-object blending: a delicate knob keeps tight seams whatever its neighbours use
    knob = ops.add_shape(C, fusion, 'SPHERE', Vector((-0.55, 0.0, 1.05)))
    knob.scale = (0.3, 0.3, 0.3)
    box.sdf_shape.radius = 0.7
    sph.sdf_shape.radius = 0.7
    knob.sdf_shape.radius = 0.02

    def knob_fillet():
        me = evaluated_mesh(fusion)
        co = np.array([v.co[:] for v in me.vertices])
        shp = {s_['primitive'] + str(i): s_ for i, s_ in enumerate(reference_shapes(fusion))}
        hom = np.c_[co, np.ones(len(co))]
        order = [r.object for r in fs.shapes]
        refs = reference_shapes(fusion)
        kb = refs[order.index(knob)]
        bx = refs[order.index(box)]
        sp = refs[order.index(sph)]
        d_k = sdf_ref.primitive_distance('SPHERE', (hom @ kb['matrix_inv_rigid'].T)[:, :3], kb['scale'])
        d_b = sdf_ref.primitive_distance('BOX', (hom @ bx['matrix_inv_rigid'].T)[:, :3], bx['scale'])
        d_s = sdf_ref.primitive_distance('SPHERE', (hom @ sp['matrix_inv_rigid'].T)[:, :3], sp['scale'])
        # fillet vertices around the knob, outside the reach of the big box / sphere fillet
        return int(np.sum((d_k > 0.04) & (d_b > 0.04) & (d_k < 0.5) & (d_s > 0.75)))
    fs.auto_order = False
    fs.seam_rule = 'SHARPER'
    tight_last = knob_fillet()
    fs.shapes.move(2, 0)                       # knob first: neighbours come later in the list
    nodes.rebuild(fusion)
    tight_first = knob_fillet()
    fs.seam_rule = 'LATEST'
    loose_first = knob_fillet()
    check(tight_last < 40 and tight_first < 40, f"Sharper Wins keeps the knob's seam tight in any list position ({tight_last} / {tight_first} fillet verts)")
    check(loose_first > 5 * max(tight_first, 1) and loose_first > 200, f"the old order-dependent rule would have swallowed it ({loose_first} fillet verts)")
    fs.seam_rule = 'SHARPER'
    big_before = mesh_stats(evaluated_mesh(fusion))['volume']
    knob.sdf_shape.radius = 0.01
    check(abs(mesh_stats(evaluated_mesh(fusion))['volume'] - big_before) < 0.02, "tweaking the knob leaves the box / sphere seam alone")
    fs.auto_order = True

    # migration of a pre-1.4 fusion
    reset_scene()
    fusion = ops.create_fusion(C, Vector((0, 0, 0)))
    a = ops.add_shape(C, fusion, 'BOX', Vector((0, 0, 0)))
    b = ops.add_shape(C, fusion, 'SPHERE', Vector((1.2, 0, 0.6)))
    c = ops.add_shape(C, fusion, 'CYLINDER', Vector((-1.0, 0, 0.8)))
    fs = fusion.sdf_fusion
    fs['data_version'] = 0
    fs['blend'] = 0.4
    fs['blend_type'] = 2                        # CHAMFER
    b.sdf_shape['use_custom_blend'] = True
    b.sdf_shape['blend'] = 0.1
    b.sdf_shape['blend_type'] = 4               # NONE
    c.sdf_shape['use_custom_blend'] = True
    c.sdf_shape['blend'] = 0.15
    c.sdf_shape['blend_type'] = 0               # SMOOTH
    for sh in (a, b, c):
        sh.sdf_shape['radius'] = 9.0            # junk that the migration must overwrite
    check(ops.migrate_fusion(fusion), "legacy fusion detected and migrated")
    check(abs(a.sdf_shape.radius - 0.4) < 1e-6 and abs(a.sdf_shape.fill - 1.0) < 1e-6 and fs.mode == 'CHAMFER', "global Chamfer 0.4 -> Radius 0.4 in Flat Bevel mode")
    check(b.sdf_shape.radius == 0.0 and abs(c.sdf_shape.radius - 0.15) < 1e-6 and abs(c.sdf_shape.fill - 1.0) < 1e-6, "per-shape overrides -> None = Radius 0, Smooth 0.15 = Radius 0.15 / full Blend")
    check(fs.seam_rule == 'LATEST' and fs.data_version == ops.DATA_VERSION, "overrides keep the old seam behaviour; version stamped")
    check(not ops.migrate_fusion(fusion), "migration runs only once")
    # 1.4.0 fusions: fill 0.5 was the quarter-pipe, 1.0 the flat bevel
    fs['data_version'] = 2
    fs['mode'] = 0
    a.sdf_shape['fill'] = 0.5
    b.sdf_shape['fill'] = 0.9
    c.sdf_shape['fill'] = 0.25
    p_old = 1.0 / 0.25
    check(ops.migrate_fusion(fusion) and abs(a.sdf_shape.fill - 1.0) < 1e-6 and abs(b.sdf_shape.fill - 1.0) < 1e-6
          and abs(sdf_ref.fill_to_p(c.sdf_shape.fill) - p_old) < 1e-6,
          f"1.4.0 fills remapped: quarter-pipe and flatter -> 1.0, 0.25 keeps its curve (now {c.sdf_shape.fill:.3f})")
    check(mesh_stats(evaluated_mesh(fusion))['verts'] > 0, "migrated fusion evaluates")


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
    set_blend(fusion, 0.6)
    set_blend(fusion, 0.6)
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

    set_blend(fusion, 0.0)
    cols2, co2, _ = colours()
    mid2 = np.sum((cols2[:, 0] > 0.3) & (cols2[:, 0] < 0.8))
    check(mid2 == 0, "hard union switches colour sharply (no in-between vertices)")
    set_blend(fusion, 0.6)

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
    set_blend(fusion, 0.6)
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
    for test in (test_field_matches_reference, test_acceptance_flow, test_primitive_volumes, test_placement, test_mesh_shapes, test_mirror_and_hollow, test_cutters_and_guides, test_duplicate, test_animation_and_apply, test_ramp_family, test_color_blending, test_material_blending, test_timing):
        try:
            t_start = time.perf_counter()
            test()
            print(f"       ({test.__name__}: {time.perf_counter() - t_start:.1f}s)")
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
