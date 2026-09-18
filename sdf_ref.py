# SPDX-License-Identifier: MIT
"""
Reference (CPU / numpy) implementation of the SDF primitives and blend operators.

These functions are a direct port of the GLSL library that shipped with the
original *Mesh from SDF* add-on (``shader/common.py`` in the upstream repository,
MIT licensed, Copyright (c) 2025 TLabAltoh).  They are the single source of
truth for the maths: the Geometry Nodes groups built in ``nodes.py`` implement
exactly the same formulas, and the test-suite checks the node groups against
this module.

Conventions
-----------
* Blender is Z-up.  Upstream GLSL used Y as the "height" axis for cylinders
  and cones; here every primitive is authored with Z as its height axis so the
  proxy meshes created with ``bpy.ops.mesh.primitive_*`` line up 1:1.
* Every primitive is evaluated in the *unit* frame of the source object
  (after the inverse of the object's rigid transform).  The object's scale is
  passed separately as ``s = (sx, sy, sz)`` so anisotropic scale can be handled
  exactly for boxes and cylinders and approximately for the round primitives.
* Positive distances are outside, negative inside (the usual SDF convention).

The module only depends on numpy so it can be imported outside Blender.
"""

import numpy as np

# ----------------------------------------------------------------------------
# small helpers (vectorised over an (N, 3) array of points)
# ----------------------------------------------------------------------------

def _length(v):
    return np.sqrt(np.sum(v * v, axis=-1))


def _dot2(v):
    return np.sum(v * v, axis=-1)


def _clamp(x, lo, hi):
    return np.minimum(np.maximum(x, lo), hi)


def _mix(a, b, t):
    return a * (1.0 - t) + b * t


# ----------------------------------------------------------------------------
# primitives
# ----------------------------------------------------------------------------

def sd_box(p, s, rounding=0.0):
    """Box with half extents ``s`` (the object scale) and edge rounding.

    Port of upstream ``sdBox`` (without the per-corner rounding, which v1 does
    not expose) combined with ``opRound``.
    """
    b = np.maximum(np.asarray(s, dtype=np.float64) - rounding, 0.0)
    q = np.abs(p) - b
    outside = _length(np.maximum(q, 0.0))
    inside = np.minimum(np.max(q, axis=-1), 0.0)
    return outside + inside - rounding


def sd_sphere(p, s):
    """Sphere of radius ``s`` (uniform) or ellipsoid bound for anisotropic scale."""
    s = np.asarray(s, dtype=np.float64)
    smin = float(np.min(s))
    # exact when the scale is uniform; a conservative bound otherwise
    return (_length(p / s) - 1.0) * smin


def sd_cylinder(p, s, rounding=0.0):
    """Capped cylinder, Z axis, radius ``s.xy`` and half height ``s.z``.

    Port of upstream ``sdCylinder`` (which was Y-up) rotated to Z-up, with
    ``opRound`` applied.
    """
    s = np.asarray(s, dtype=np.float64)
    r_min = float(min(s[0], s[1]))
    radial = (_length(p[..., :2] / s[:2]) - 1.0) * r_min   # exact when sx == sy
    axial = np.abs(p[..., 2]) - s[2]
    d = np.stack([radial + rounding, axial + rounding], axis=-1)
    return np.minimum(np.maximum(d[..., 0], d[..., 1]), 0.0) + _length(np.maximum(d, 0.0)) - rounding


def sd_torus(p, s, tube=0.25):
    """Torus in the XY plane, major radius ``s.xy`` and tube radius ``tube``.

    ``tube`` is expressed as a fraction of the major radius, like Blender's
    torus primitive (major 1.0 / minor 0.25 by default).
    """
    s = np.asarray(s, dtype=np.float64)
    smin = float(np.min(s))
    q = np.stack([_length(p[..., :2] / s[:2]) - 1.0, p[..., 2] / s[2]], axis=-1)
    return (_length(q) - tube) * smin


def sd_cone(p, s, top_radius=0.0):
    """Capped cone, Z axis, base radius 1, top radius ``top_radius`` (0..1), half height 1.

    Port of upstream ``sdCappedCone`` (Y-up) rotated to Z-up, evaluated in the
    normalised frame and scaled back by the smallest scale factor.
    """
    s = np.asarray(s, dtype=np.float64)
    smin = float(np.min(s))
    pn = p / s
    h = 1.0
    r0 = 1.0            # bottom radius (z = -h)
    r1 = float(top_radius)  # top radius (z = +h)
    q = np.stack([_length(pn[..., :2]), pn[..., 2]], axis=-1)
    k1 = np.array([r1, h])
    k2 = np.array([r1 - r0, 2.0 * h])
    rsel = np.where(q[..., 1] < 0.0, r0, r1)
    ca = np.stack([q[..., 0] - np.minimum(q[..., 0], rsel), np.abs(q[..., 1]) - h], axis=-1)
    t = _clamp(np.sum((k1 - q) * k2, axis=-1) / _dot2(k2), 0.0, 1.0)
    cb = q - k1 + k2 * t[..., None]
    sgn = np.where((cb[..., 0] < 0.0) & (ca[..., 1] < 0.0), -1.0, 1.0)
    return sgn * np.sqrt(np.minimum(_dot2(ca), _dot2(cb))) * smin


def sd_capsule(p, s):
    """Capsule along Z: radius min(s.x, s.y), total half height s.z (a cylinder rounded by its radius)."""
    s = np.asarray(s, dtype=np.float64)
    return sd_cylinder(p, s, rounding=float(np.min(s)))


def sd_pyramid(p, s):
    """Square pyramid: base half extents s.x, s.y at z = -s.z, apex at z = +s.z.

    Port of upstream ``sdPyramid(p, hw, hd, hh)`` (Y-up) with axes remapped to
    Blender's Z-up.  Exact under non-uniform scale.
    """
    s = np.asarray(s, dtype=np.float64)
    hw, hd, hh = float(s[0]), float(s[1]), float(s[2])
    x = np.abs(p[..., 0])
    y = p[..., 2] + hh            # upstream height axis, base at 0
    z = np.abs(p[..., 1])
    P = np.stack([x, y, z], axis=-1)
    corner = np.array([hw, 0.0, hd])
    lo = np.zeros(3)
    hi = np.array([hw, 2.0 * hh, hd])
    d1 = np.stack([np.maximum(x - hw, 0.0), y, np.maximum(z - hd, 0.0)], axis=-1)
    n1 = np.array([0.0, hd, 2.0 * hh])
    k1 = float(n1 @ n1)
    h1 = ((P - corner) @ n1) / k1
    n2 = np.array([k1, 2.0 * hh * hw, -hd * hw])
    m1 = ((P - corner) @ n2) / float(n2 @ n2)
    d2 = P - np.clip(P - n1 * h1[..., None] - n2 * np.maximum(m1, 0.0)[..., None], lo, hi)
    n3 = np.array([2.0 * hh, hw, 0.0])
    k2 = float(n3 @ n3)
    h2 = ((P - corner) @ n3) / k2
    n4 = np.array([-hw * hd, 2.0 * hh * hd, k2])
    m2 = ((P - corner) @ n4) / float(n4 @ n4)
    d3 = P - np.clip(P - n3 * h2[..., None] - n4 * np.maximum(m2, 0.0)[..., None], lo, hi)
    d = np.sqrt(np.minimum(np.minimum(_dot2(d1), _dot2(d2)), _dot2(d3)))
    inside = np.maximum(np.maximum(h1, h2), -y) < 0.0
    return np.where(inside, -d, d)


def sd_prism(p, s, sides=6, rounding=0.0):
    """Regular N-gon prism along Z: circumradius s.xy, half height s.z, optional edge rounding.

    The 2D polygon is Inigo Quilez's closed-form regular polygon (a vertex on +Y),
    extruded like the cylinder; radial part exact for uniform XY scale.
    """
    s = np.asarray(s, dtype=np.float64)
    n = max(3, int(round(sides)))
    rmin = float(min(s[0], s[1]))
    pn = p[..., :2] / s[:2]
    an = np.pi / n
    ca, sa = np.cos(an), np.sin(an)
    bn = np.mod(np.arctan2(pn[..., 0], pn[..., 1]), 2.0 * an) - an
    L = _length(pn)
    qx = L * np.cos(bn) - ca
    qy = L * np.abs(np.sin(bn)) - sa
    qy = qy + np.clip(-qy, 0.0, sa)
    d2 = np.sqrt(qx * qx + qy * qy) * np.where(qx < 0.0, -1.0, 1.0) * rmin
    axial = np.abs(p[..., 2]) - s[2]
    dx = d2 + rounding
    dy = axial + rounding
    d = np.stack([dx, dy], axis=-1)
    return np.minimum(np.maximum(dx, dy), 0.0) + _length(np.maximum(d, 0.0)) - rounding


# ----------------------------------------------------------------------------
# boolean operators (port of upstream opUnion / opDifference / opIntersection
# and the Smooth / Round / Champfer / Stairs families)
#
# Convention (matches upstream): ``d0`` is the *new* shape, ``d1`` is the
# accumulated result, so ``difference`` subtracts the new shape from the result.
# ----------------------------------------------------------------------------

def op_union(d0, d1):
    return np.minimum(d0, d1)


def op_difference(d0, d1):
    return np.maximum(-d0, d1)


def op_intersection(d0, d1):
    return np.maximum(d0, d1)


def op_smooth_union(d0, d1, k):
    h = _clamp(0.5 + 0.5 * (d1 - d0) / k, 0.0, 1.0)
    return _mix(d1, d0, h) - k * h * (1.0 - h)


def op_smooth_difference(d0, d1, k):
    h = _clamp(0.5 - 0.5 * (d1 + d0) / k, 0.0, 1.0)
    return _mix(d1, -d0, h) + k * h * (1.0 - h)


def op_smooth_intersection(d0, d1, k):
    h = _clamp(0.5 - 0.5 * (d1 - d0) / k, 0.0, 1.0)
    return _mix(d1, d0, h) + k * h * (1.0 - h)


def op_round_union(d0, d1, r):
    u = np.maximum(np.stack([r - d0, r - d1], axis=-1), 0.0)
    return np.maximum(r, np.minimum(d0, d1)) - _length(u)


def op_round_intersection(d0, d1, r):
    u = np.maximum(np.stack([r + d0, r + d1], axis=-1), 0.0)
    return np.minimum(-r, np.maximum(d0, d1)) + _length(u)


def op_round_difference(d0, d1, r):
    # hg_sdf: fOpDifferenceRound(base, sub) = fOpIntersectionRound(base, -sub)
    u = np.maximum(np.stack([r - d0, r + d1], axis=-1), 0.0)
    return np.minimum(-r, np.maximum(-d0, d1)) + _length(u)


_SQRT05 = 0.70710678118


def op_chamfer_union(d0, d1, s):
    return np.minimum(np.minimum(d0, d1), (d0 - s + d1) * _SQRT05)


def op_chamfer_intersection(d0, d1, s):
    return np.maximum(np.maximum(d0, d1), (d0 + s + d1) * _SQRT05)


def op_chamfer_difference(d0, d1, s):
    # intersection(result, -new)
    return op_chamfer_intersection(-d0, d1, s)


def op_stairs_union(d0, d1, s, n):
    _s = s / n
    u = d1 - s
    return np.minimum(np.minimum(d0, d1), 0.5 * (u + d0 + np.abs(np.mod(u - d0 + _s, 2.0 * _s) - _s)))


def op_stairs_difference(d0, d1, s, n):
    # hg_sdf: fOpDifferenceStairs(base, sub) = -fOpUnionStairs(-base, sub)
    return -op_stairs_union(-d1, d0, s, n)


def op_stairs_intersection(d0, d1, s, n):
    return -op_stairs_union(-d0, -d1, s, n)


EPS_BLEND = 1e-4
OPERATIONS = ('UNION', 'SUBTRACT', 'INTERSECT')
BLEND_TYPES = ('NONE', 'SMOOTH', 'ROUND', 'CHAMFER', 'STEPS')


def combine(d_new, d_acc, operation, blend_type='NONE', blend=0.0, steps=1):
    """Combine the distance of a new shape with the accumulated result."""
    blend = max(float(blend), EPS_BLEND)
    if blend_type == 'NONE':
        if operation == 'UNION':
            return op_union(d_new, d_acc)
        if operation == 'SUBTRACT':
            return op_difference(d_new, d_acc)
        return op_intersection(d_new, d_acc)
    if blend_type == 'SMOOTH':
        if operation == 'UNION':
            return op_smooth_union(d_new, d_acc, blend)
        if operation == 'SUBTRACT':
            return op_smooth_difference(d_new, d_acc, blend)
        return op_smooth_intersection(d_new, d_acc, blend)
    if blend_type == 'ROUND':
        if operation == 'UNION':
            return op_round_union(d_new, d_acc, blend)
        if operation == 'SUBTRACT':
            return op_round_difference(d_new, d_acc, blend)
        return op_round_intersection(d_new, d_acc, blend)
    if blend_type == 'CHAMFER':
        if operation == 'UNION':
            return op_chamfer_union(d_new, d_acc, blend)
        if operation == 'SUBTRACT':
            return op_chamfer_difference(d_new, d_acc, blend)
        return op_chamfer_intersection(d_new, d_acc, blend)
    if blend_type == 'STEPS':
        n = float(max(1, int(steps)))
        if operation == 'UNION':
            return op_stairs_union(d_new, d_acc, blend, n)
        if operation == 'SUBTRACT':
            return op_stairs_difference(d_new, d_acc, blend, n)
        return op_stairs_intersection(d_new, d_acc, blend, n)
    raise ValueError(blend_type)


def primitive_distance(primitive, p_local, scale, rounding=0.0, tube=0.25, top_radius=0.0, sides=6):
    """Dispatch helper mirroring the node groups."""
    if primitive == 'CAPSULE':
        return sd_capsule(p_local, scale)
    if primitive == 'PYRAMID':
        return sd_pyramid(p_local, scale)
    if primitive == 'PRISM':
        return sd_prism(p_local, scale, sides, rounding)
    if primitive == 'BOX':
        return sd_box(p_local, scale, rounding)
    if primitive == 'SPHERE':
        return sd_sphere(p_local, scale)
    if primitive == 'CYLINDER':
        return sd_cylinder(p_local, scale, rounding)
    if primitive == 'TORUS':
        return sd_torus(p_local, scale, tube)
    if primitive == 'CONE':
        return sd_cone(p_local, scale, top_radius)
    raise ValueError(primitive)


def evaluate_fusion(points, shapes, global_blend, global_blend_type, global_steps=1):
    """Evaluate a whole fusion at ``points`` (N, 3) in fusion-local space.

    ``shapes`` is a list of dicts with keys: ``matrix_inv_rigid`` (4x4 numpy,
    inverse of the rigid part of the shape's transform relative to the fusion),
    ``scale`` (3,), ``primitive``, ``operation``, and optionally
    ``blend``/``blend_type``/``steps`` overrides, ``rounding``, ``tube``,
    ``top_radius``.
    """
    acc = None
    pts_h = np.concatenate([points, np.ones((points.shape[0], 1))], axis=-1)
    for i, sh in enumerate(shapes):
        p_local = (pts_h @ np.asarray(sh['matrix_inv_rigid']).T)[..., :3]
        d = primitive_distance(sh['primitive'], p_local, sh['scale'],
                               sh.get('rounding', 0.0), sh.get('tube', 0.25), sh.get('top_radius', 0.0),
                               sh.get('sides', 6))
        if acc is None:
            acc = d
            continue
        blend = sh.get('blend', global_blend) if sh.get('use_custom_blend') else global_blend
        btype = sh.get('blend_type', global_blend_type) if sh.get('use_custom_blend') else global_blend_type
        steps = sh.get('steps', global_steps) if sh.get('use_custom_blend') else global_steps
        acc = combine(d, acc, sh['operation'], btype, blend, steps)
    return acc
