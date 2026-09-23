# SPDX-License-Identifier: MIT
"""
Geometry Nodes "compiler" for SDF Fusion.

The original *Mesh from SDF* add-on evaluated its distance field with GLSL
compute shaders through ModernGL (OpenGL 4.3 + SSBOs).  That path cannot exist
on macOS / Metal, so this module builds the very same distance field as a
Geometry Nodes tree instead:

    for every shape:  Object Info -> rigid inverse -> Transform Point
                      -> primitive node group  (sdBox, sdSphere, ...)
    chain:            op node group (union / subtract / intersect, blended)
    grid:             Volume Cube (density = -distance) -> Get Named Grid
                      -> Grid to Mesh (threshold 0) -> Set Shade Smooth

Blender evaluates that tree natively (multithreaded C++, OpenVDB meshing), so
the result updates live whenever a source object moves and the same tree is
used for the final high-resolution bake.  The maths is a 1:1 port of the
upstream GLSL library (see ``sdf_ref.py`` for the readable reference).
"""

import bpy

GROUP_VERSION = 5
COLOR_ATTRIBUTE = "Color"
SURFACE_ATTRIBUTE = "SDF Surface"     # (metallic, roughness, transmission)
EXTRA_ATTRIBUTE = "SDF Extra"         # (ior, emission strength, 0)
EMISSION_ATTRIBUTE = "SDF Emission"   # emission colour
MODIFIER_NAME = "SDF Fusion"
TREE_PREFIX = "SDFF Tree"
GRID_NAME = "density"
EPS_BLEND = 1e-4
SQRT05 = 0.70710678118

PRIMITIVES = ('BOX', 'SPHERE', 'CYLINDER', 'TORUS', 'CONE', 'CAPSULE', 'PYRAMID', 'PRISM', 'MESH')
OPERATIONS = ('UNION', 'SUBTRACT', 'INTERSECT')


# ----------------------------------------------------------------------------
# tiny node-building DSL
# ----------------------------------------------------------------------------

class _Builder:
    """Wraps a node tree with helpers that accept sockets *or* constants."""

    def __init__(self, tree):
        self.tree = tree
        self._count = 0

    def node(self, idname, name=None, **props):
        n = self.tree.nodes.new(idname)
        if name:
            n.name = name
            n.label = name
        for k, v in props.items():
            setattr(n, k, v)
        # crude grid layout so the tree stays readable when opened by a user
        self._count += 1
        col = self._count % 24
        row = self._count // 24
        n.location = (col * 190.0, -row * 260.0)
        return n

    def link(self, src, dst):
        if isinstance(src, bpy.types.NodeSocket):
            self.tree.links.new(src, dst)
        elif src is not None:
            dst.default_value = src

    def math(self, op, a, b=None, c=None, name=None):
        n = self.node('ShaderNodeMath', name, operation=op)
        self.link(a, n.inputs[0])
        if b is not None:
            self.link(b, n.inputs[1])
        if c is not None:
            self.link(c, n.inputs[2])
        return n.outputs[0]

    def vmath(self, op, a, b=None, scale=None):
        n = self.node('ShaderNodeVectorMath', operation=op)
        self.link(a, n.inputs[0])
        if b is not None:
            self.link(b, n.inputs[1])
        if scale is not None:
            self.link(scale, n.inputs[3])
        return n.outputs[0]

    def vmath_f(self, op, a, b=None):
        """Vector Math variants with a scalar result (LENGTH, DOT_PRODUCT...)."""
        n = self.node('ShaderNodeVectorMath', operation=op)
        self.link(a, n.inputs[0])
        if b is not None:
            self.link(b, n.inputs[1])
        return n.outputs[1]

    def sep(self, v):
        n = self.node('ShaderNodeSeparateXYZ')
        self.link(v, n.inputs[0])
        return n.outputs[0], n.outputs[1], n.outputs[2]

    def comb(self, x, y, z):
        n = self.node('ShaderNodeCombineXYZ')
        self.link(x, n.inputs[0])
        self.link(y, n.inputs[1])
        self.link(z, n.inputs[2])
        return n.outputs[0]

    def value(self, v, name=None):
        n = self.node('ShaderNodeValue', name)
        n.outputs[0].default_value = float(v)
        return n.outputs[0]

    def integer(self, v, name=None):
        n = self.node('FunctionNodeInputInt', name)
        n.integer = int(v)
        return n.outputs[0]

    def group(self, node_group, name=None):
        n = self.node('GeometryNodeGroup', name)
        n.node_tree = node_group
        return n

    # convenience compositions -------------------------------------------------
    def min3(self, x, y, z):
        return self.math('MINIMUM', self.math('MINIMUM', x, y), z)

    def max3(self, x, y, z):
        return self.math('MAXIMUM', self.math('MAXIMUM', x, y), z)

    def clamp01(self, x):
        return self.math('MINIMUM', self.math('MAXIMUM', x, 0.0), 1.0)

    def mix(self, a, b, t):
        # a + (b - a) * t
        return self.math('MULTIPLY_ADD', self.math('SUBTRACT', b, a), t, a)

    def neg(self, x):
        return self.math('MULTIPLY', x, -1.0)

    def mix_color(self, a, b, t):
        """Mix RGBA: result = a * (1 - t) + b * t."""
        n = self.node('ShaderNodeMix')
        n.data_type = 'RGBA'
        sock = {s.identifier: s for s in n.inputs}
        self.link(t, sock['Factor_Float'])
        self.link(a, sock['A_Color'])
        self.link(b, sock['B_Color'])
        return next(s for s in n.outputs if s.identifier == 'Result_Color')

    def color_input(self, rgba, name=None):
        n = self.node('FunctionNodeInputColor', name)
        set_color_node(n, rgba)
        return n.outputs[0]

    def vector_input(self, xyz, name=None):
        n = self.node('FunctionNodeInputVector', name)
        n.vector = tuple(xyz)
        return n.outputs[0]

    def mix_vector(self, a, b, t):
        n = self.node('ShaderNodeMix')
        n.data_type = 'VECTOR'
        sock = {s.identifier: s for s in n.inputs}
        self.link(t, sock['Factor_Float'])
        self.link(a, sock['A_Vector'])
        self.link(b, sock['B_Vector'])
        return next(s for s in n.outputs if s.identifier == 'Result_Vector')


def set_color_node(node, rgba):
    rgba = tuple(rgba) if len(rgba) == 4 else tuple(rgba) + (1.0,)
    try:
        node.value = rgba
    except AttributeError:
        node.color = rgba


def _new_group(name, inputs, outputs):
    ng = bpy.data.node_groups.new(name, 'GeometryNodeTree')
    for nm, stype, default in inputs:
        s = ng.interface.new_socket(nm, in_out='INPUT', socket_type=stype)
        if default is not None:
            s.default_value = default
    for nm, stype in outputs:
        ng.interface.new_socket(nm, in_out='OUTPUT', socket_type=stype)
    b = _Builder(ng)
    gi = b.node('NodeGroupInput')
    go = b.node('NodeGroupOutput')
    return ng, b, gi, go


_PRIM_INPUTS = [
    ('Position', 'NodeSocketVector', None),
    ('Scale', 'NodeSocketVector', (1.0, 1.0, 1.0)),
    ('Rounding', 'NodeSocketFloat', 0.0),
    ('Param', 'NodeSocketFloat', 0.0),
]
_PRIM_OUTPUTS = [('Distance', 'NodeSocketFloat')]


# ----------------------------------------------------------------------------
# primitive node groups (ports of sdBox / sdSphere / sdCylinder / sdCappedTorus
# / sdCappedCone, Z-up, evaluated in the shape's unit frame)
# ----------------------------------------------------------------------------

def _build_box(name):
    ng, b, gi, go = _new_group(name, _PRIM_INPUTS, _PRIM_OUTPUTS)
    P, S, R = gi.outputs['Position'], gi.outputs['Scale'], gi.outputs['Rounding']
    half = b.vmath('MAXIMUM', b.vmath('SUBTRACT', S, b.comb(R, R, R)), (0.0, 0.0, 0.0))
    q = b.vmath('SUBTRACT', b.vmath('ABSOLUTE', P), half)
    outside = b.vmath_f('LENGTH', b.vmath('MAXIMUM', q, (0.0, 0.0, 0.0)))
    qx, qy, qz = b.sep(q)
    inside = b.math('MINIMUM', b.max3(qx, qy, qz), 0.0)
    d = b.math('SUBTRACT', b.math('ADD', outside, inside), R)
    b.link(d, go.inputs['Distance'])
    return ng


def _build_sphere(name):
    ng, b, gi, go = _new_group(name, _PRIM_INPUTS, _PRIM_OUTPUTS)
    P, S = gi.outputs['Position'], gi.outputs['Scale']
    sx, sy, sz = b.sep(S)
    smin = b.min3(sx, sy, sz)
    pn = b.vmath('DIVIDE', P, S)
    d = b.math('MULTIPLY', b.math('SUBTRACT', b.vmath_f('LENGTH', pn), 1.0), smin)
    b.link(d, go.inputs['Distance'])
    return ng


def _build_cylinder(name):
    ng, b, gi, go = _new_group(name, _PRIM_INPUTS, _PRIM_OUTPUTS)
    P, S, R = gi.outputs['Position'], gi.outputs['Scale'], gi.outputs['Rounding']
    px, py, pz = b.sep(P)
    sx, sy, sz = b.sep(S)
    rmin = b.math('MINIMUM', sx, sy)
    radial_n = b.vmath_f('LENGTH', b.comb(b.math('DIVIDE', px, sx), b.math('DIVIDE', py, sy), 0.0))
    radial = b.math('MULTIPLY', b.math('SUBTRACT', radial_n, 1.0), rmin)
    axial = b.math('SUBTRACT', b.math('ABSOLUTE', pz), sz)
    dx = b.math('ADD', radial, R)
    dy = b.math('ADD', axial, R)
    inside = b.math('MINIMUM', b.math('MAXIMUM', dx, dy), 0.0)
    outside = b.vmath_f('LENGTH', b.comb(b.math('MAXIMUM', dx, 0.0), b.math('MAXIMUM', dy, 0.0), 0.0))
    d = b.math('SUBTRACT', b.math('ADD', inside, outside), R)
    b.link(d, go.inputs['Distance'])
    return ng


def _build_torus(name):
    ng, b, gi, go = _new_group(name, _PRIM_INPUTS, _PRIM_OUTPUTS)
    P, S, tube = gi.outputs['Position'], gi.outputs['Scale'], gi.outputs['Param']
    sx, sy, sz = b.sep(S)
    smin = b.min3(sx, sy, sz)
    pn = b.vmath('DIVIDE', P, S)
    pnx, pny, pnz = b.sep(pn)
    qx = b.math('SUBTRACT', b.vmath_f('LENGTH', b.comb(pnx, pny, 0.0)), 1.0)
    d = b.math('MULTIPLY', b.math('SUBTRACT', b.vmath_f('LENGTH', b.comb(qx, pnz, 0.0)), tube), smin)
    b.link(d, go.inputs['Distance'])
    return ng


def _build_cone(name):
    ng, b, gi, go = _new_group(name, _PRIM_INPUTS, _PRIM_OUTPUTS)
    P, S, r1 = gi.outputs['Position'], gi.outputs['Scale'], gi.outputs['Param']
    sx, sy, sz = b.sep(S)
    smin = b.min3(sx, sy, sz)
    pn = b.vmath('DIVIDE', P, S)
    pnx, pny, qz = b.sep(pn)
    qx = b.vmath_f('LENGTH', b.comb(pnx, pny, 0.0))
    # h = 1, r0 = 1 (base at z=-1), r1 = Param (top at z=+1)
    lt = b.math('LESS_THAN', qz, 0.0)                       # 1 when below the mid plane
    r0_minus_r1 = b.math('SUBTRACT', 1.0, r1)
    rsel = b.math('MULTIPLY_ADD', r0_minus_r1, lt, r1)       # r1 + (r0 - r1) * lt
    cax = b.math('SUBTRACT', qx, b.math('MINIMUM', qx, rsel))
    cay = b.math('SUBTRACT', b.math('ABSOLUTE', qz), 1.0)
    k2x = b.math('SUBTRACT', r1, 1.0)                        # r1 - r0
    dotk = b.math('MULTIPLY_ADD', b.math('SUBTRACT', r1, qx), k2x,
                  b.math('MULTIPLY', b.math('SUBTRACT', 1.0, qz), 2.0))
    dot2k2 = b.math('MULTIPLY_ADD', k2x, k2x, 4.0)
    t = b.clamp01(b.math('DIVIDE', dotk, dot2k2))
    cbx = b.math('MULTIPLY_ADD', k2x, t, b.math('SUBTRACT', qx, r1))
    cby = b.math('MULTIPLY_ADD', t, 2.0, b.math('SUBTRACT', qz, 1.0))
    both = b.math('MULTIPLY', b.math('LESS_THAN', cbx, 0.0), b.math('LESS_THAN', cay, 0.0))
    sgn = b.math('MULTIPLY_ADD', both, -2.0, 1.0)
    dca = b.math('MULTIPLY_ADD', cax, cax, b.math('MULTIPLY', cay, cay))
    dcb = b.math('MULTIPLY_ADD', cbx, cbx, b.math('MULTIPLY', cby, cby))
    d = b.math('MULTIPLY', b.math('MULTIPLY', sgn, b.math('SQRT', b.math('MINIMUM', dca, dcb))), smin)
    b.link(d, go.inputs['Distance'])
    return ng


def _build_cylinder_like(name, capsule):
    """Cylinder; the capsule variant rounds by the smallest scale (its radius)."""
    ng, b, gi, go = _new_group(name, _PRIM_INPUTS, _PRIM_OUTPUTS)
    P, S = gi.outputs['Position'], gi.outputs['Scale']
    px, py, pz = b.sep(P)
    sx, sy, sz = b.sep(S)
    R = b.min3(sx, sy, sz) if capsule else gi.outputs['Rounding']
    rmin = b.math('MINIMUM', sx, sy)
    radial_n = b.vmath_f('LENGTH', b.comb(b.math('DIVIDE', px, sx), b.math('DIVIDE', py, sy), 0.0))
    radial = b.math('MULTIPLY', b.math('SUBTRACT', radial_n, 1.0), rmin)
    axial = b.math('SUBTRACT', b.math('ABSOLUTE', pz), sz)
    dx = b.math('ADD', radial, R)
    dy = b.math('ADD', axial, R)
    inside = b.math('MINIMUM', b.math('MAXIMUM', dx, dy), 0.0)
    outside = b.vmath_f('LENGTH', b.comb(b.math('MAXIMUM', dx, 0.0), b.math('MAXIMUM', dy, 0.0), 0.0))
    d = b.math('SUBTRACT', b.math('ADD', inside, outside), R)
    b.link(d, go.inputs['Distance'])
    return ng


def _build_capsule(name):
    return _build_cylinder_like(name, capsule=True)


def _build_pyramid(name):
    """Port of upstream sdPyramid (Y-up) remapped to Z-up; hw = sx, hd = sy, hh = sz."""
    ng, b, gi, go = _new_group(name, _PRIM_INPUTS, _PRIM_OUTPUTS)
    P, S = gi.outputs['Position'], gi.outputs['Scale']
    px, py, pz = b.sep(P)
    hw, hd, hh = b.sep(S)
    x = b.math('ABSOLUTE', px)
    y = b.math('ADD', pz, hh)
    z = b.math('ABSOLUTE', py)
    Pv = b.comb(x, y, z)
    corner = b.comb(hw, 0.0, hd)
    hh2 = b.math('MULTIPLY', hh, 2.0)
    hi = b.comb(hw, hh2, hd)
    rel = b.vmath('SUBTRACT', Pv, corner)
    d1 = b.comb(b.math('MAXIMUM', b.math('SUBTRACT', x, hw), 0.0), y, b.math('MAXIMUM', b.math('SUBTRACT', z, hd), 0.0))
    n1 = b.comb(0.0, hd, hh2)
    k1 = b.vmath_f('DOT_PRODUCT', n1, n1)
    h1 = b.math('DIVIDE', b.vmath_f('DOT_PRODUCT', rel, n1), k1)
    n2 = b.comb(k1, b.math('MULTIPLY', hh2, hw), b.math('MULTIPLY', b.math('MULTIPLY', hd, hw), -1.0))
    m1 = b.math('DIVIDE', b.vmath_f('DOT_PRODUCT', rel, n2), b.vmath_f('DOT_PRODUCT', n2, n2))
    q2 = b.vmath('SUBTRACT', b.vmath('SUBTRACT', Pv, b.vmath('SCALE', n1, scale=h1)), b.vmath('SCALE', n2, scale=b.math('MAXIMUM', m1, 0.0)))
    d2 = b.vmath('SUBTRACT', Pv, b.vmath('MAXIMUM', b.vmath('MINIMUM', q2, hi), (0.0, 0.0, 0.0)))
    n3 = b.comb(hh2, hw, 0.0)
    k2 = b.vmath_f('DOT_PRODUCT', n3, n3)
    h2 = b.math('DIVIDE', b.vmath_f('DOT_PRODUCT', rel, n3), k2)
    n4 = b.comb(b.math('MULTIPLY', b.math('MULTIPLY', hw, hd), -1.0), b.math('MULTIPLY', hh2, hd), k2)
    m2 = b.math('DIVIDE', b.vmath_f('DOT_PRODUCT', rel, n4), b.vmath_f('DOT_PRODUCT', n4, n4))
    q3 = b.vmath('SUBTRACT', b.vmath('SUBTRACT', Pv, b.vmath('SCALE', n3, scale=h2)), b.vmath('SCALE', n4, scale=b.math('MAXIMUM', m2, 0.0)))
    d3 = b.vmath('SUBTRACT', Pv, b.vmath('MAXIMUM', b.vmath('MINIMUM', q3, hi), (0.0, 0.0, 0.0)))
    dd = b.min3(b.vmath_f('DOT_PRODUCT', d1, d1), b.vmath_f('DOT_PRODUCT', d2, d2), b.vmath_f('DOT_PRODUCT', d3, d3))
    d = b.math('SQRT', dd)
    inside = b.math('LESS_THAN', b.math('MAXIMUM', b.math('MAXIMUM', h1, h2), b.neg(y)), 0.0)
    signed = b.math('MULTIPLY', d, b.math('MULTIPLY_ADD', inside, -2.0, 1.0))
    b.link(signed, go.inputs['Distance'])
    return ng


def _build_prism(name):
    """Regular N-gon prism (Param = sides), circumradius = XY scale, half height = Z scale."""
    ng, b, gi, go = _new_group(name, _PRIM_INPUTS, _PRIM_OUTPUTS)
    P, S, R = gi.outputs['Position'], gi.outputs['Scale'], gi.outputs['Rounding']
    nsides = b.math('MAXIMUM', b.math('ROUND', gi.outputs['Param']), 3.0)
    px, py, pz = b.sep(P)
    sx, sy, sz = b.sep(S)
    rmin = b.math('MINIMUM', sx, sy)
    pnx = b.math('DIVIDE', px, sx)
    pny = b.math('DIVIDE', py, sy)
    an = b.math('DIVIDE', 3.14159265358979, nsides)
    ca = b.math('COSINE', an)
    sa = b.math('SINE', an)
    ang = b.math('ARCTAN2', pnx, pny)
    bn = b.math('SUBTRACT', b.math('FLOORED_MODULO', ang, b.math('MULTIPLY', an, 2.0)), an)
    L = b.vmath_f('LENGTH', b.comb(pnx, pny, 0.0))
    qx = b.math('SUBTRACT', b.math('MULTIPLY', L, b.math('COSINE', bn)), ca)
    qy0 = b.math('SUBTRACT', b.math('MULTIPLY', L, b.math('ABSOLUTE', b.math('SINE', bn))), sa)
    qy = b.math('ADD', qy0, b.math('MINIMUM', b.math('MAXIMUM', b.neg(qy0), 0.0), sa))
    sgn = b.math('MULTIPLY_ADD', b.math('LESS_THAN', qx, 0.0), -2.0, 1.0)
    d2 = b.math('MULTIPLY', b.math('MULTIPLY', b.math('SQRT', b.math('MULTIPLY_ADD', qx, qx, b.math('MULTIPLY', qy, qy))), sgn), rmin)
    axial = b.math('SUBTRACT', b.math('ABSOLUTE', pz), sz)
    dx = b.math('ADD', d2, R)
    dy = b.math('ADD', axial, R)
    inside = b.math('MINIMUM', b.math('MAXIMUM', dx, dy), 0.0)
    outside = b.vmath_f('LENGTH', b.comb(b.math('MAXIMUM', dx, 0.0), b.math('MAXIMUM', dy, 0.0), 0.0))
    d = b.math('SUBTRACT', b.math('ADD', inside, outside), R)
    b.link(d, go.inputs['Distance'])
    return ng


_PRIM_BUILDERS = {
    'CAPSULE': _build_capsule,
    'PYRAMID': _build_pyramid,
    'PRISM': _build_prism,
    'BOX': _build_box,
    'SPHERE': _build_sphere,
    'CYLINDER': _build_cylinder,
    'TORUS': _build_torus,
    'CONE': _build_cone,
}


def primitive_group(primitive):
    name = f"SDFF {primitive.title()} v{GROUP_VERSION}"
    ng = bpy.data.node_groups.get(name)
    if ng is None:
        ng = _PRIM_BUILDERS[primitive](name)
    return ng


# ----------------------------------------------------------------------------
# boolean / blend node groups
#
# Convention: ``Distance`` is the NEW shape (d0), ``Accumulated`` is the result
# so far (d1); SUBTRACT removes the new shape from the accumulated result.
#
# RAMP mode is a p-norm generalisation of hg_sdf's round union (upstream
# ``opRoundUnion``):   u = max(r - d0, 0),  v = max(r - d1, 0)
#     union = max(r, min(d0, d1)) - (u^p + v^p)^(1/p),      p >= 2
# p = 2 is the circular quarter-pipe and the FULLEST the ramp ever gets;
# p -> inf is the hard boolean.  Blend (fill, 0..1) is the depth of the fill
# at the seam relative to that quarter-pipe, which gives
#     p = ln 2 / -ln(1 - (1 - 1/sqrt 2) * fill).
# The ramp therefore always curves inward, leaves both surfaces tangentially
# and starts exactly ``r`` from the seam: Radius (reach) and Blend (amount) are
# independent and the blend can never look like it bulges.  (Profiles flatter
# than the circle, 1 <= p < 2, turn into cone-like skirts around round shapes
# in 3D and were rejected in review.)  CHAMFER is the explicit flat bevel
# (p = 1); STEPS keeps upstream's opStairsUnion with the radius as its size.
#
# Radius and fill are per shape.  They travel with the field as a vector
# (radius, fill, 0) that is mixed like the colours, so at every seam the op
# sees the settings of the new shape and of whichever shape dominates the
# accumulated result there; the seam rule decides how the two combine.
# ----------------------------------------------------------------------------

MODES = ('RAMP', 'STEPS', 'CHAMFER')
RAMP_K = 1.0 - 0.7071067811865476        # seam depth of a unit quarter-pipe
LN2 = 0.6931471805599453
SEAM_RULES = ('SHARPER', 'AVERAGE', 'SOFTER', 'LATEST')
MIN_FILL = 0.02

_OP_INPUTS = [
    ('Distance', 'NodeSocketFloat', 0.0),
    ('Accumulated', 'NodeSocketFloat', 0.0),
    ('Params', 'NodeSocketVector', (0.25, 0.5, 0.0)),
    ('Accumulated Params', 'NodeSocketVector', (0.25, 0.5, 0.0)),
    ('Steps', 'NodeSocketFloat', 1.0),
    ('Color', 'NodeSocketColor', (0.8, 0.8, 0.8, 1.0)),
    ('Accumulated Color', 'NodeSocketColor', (0.8, 0.8, 0.8, 1.0)),
    ('Surface', 'NodeSocketVector', (0.0, 0.5, 0.0)),
    ('Accumulated Surface', 'NodeSocketVector', (0.0, 0.5, 0.0)),
    ('Extra', 'NodeSocketVector', (1.45, 0.0, 0.0)),
    ('Accumulated Extra', 'NodeSocketVector', (1.45, 0.0, 0.0)),
    ('Emission', 'NodeSocketColor', (0.0, 0.0, 0.0, 1.0)),
    ('Accumulated Emission', 'NodeSocketColor', (0.0, 0.0, 0.0, 1.0)),
]
_OP_OUTPUTS = [('Result', 'NodeSocketFloat'), ('Result Params', 'NodeSocketVector'),
               ('Result Color', 'NodeSocketColor'),
               ('Result Surface', 'NodeSocketVector'), ('Result Extra', 'NodeSocketVector'),
               ('Result Emission', 'NodeSocketColor')]


def _build_op(name, operation, mode, rule):
    ng, b, gi, go = _new_group(name, _OP_INPUTS, _OP_OUTPUTS)
    d0, d1 = gi.outputs['Distance'], gi.outputs['Accumulated']
    c0, c1 = gi.outputs['Color'], gi.outputs['Accumulated Color']
    pn, pa = gi.outputs['Params'], gi.outputs['Accumulated Params']
    rn, tn, _zn = b.sep(pn)
    ra, ta, _za = b.sep(pa)
    if rule == 'SHARPER':
        rj, tj = b.math('MINIMUM', rn, ra), b.math('MINIMUM', tn, ta)
    elif rule == 'SOFTER':
        rj, tj = b.math('MAXIMUM', rn, ra), b.math('MAXIMUM', tn, ta)
    elif rule == 'AVERAGE':
        rj = b.math('MULTIPLY', b.math('ADD', rn, ra), 0.5)
        tj = b.math('MULTIPLY', b.math('ADD', tn, ta), 0.5)
    else:                                   # LATEST: the new shape decides
        rj, tj = rn, tn
    r = b.math('MAXIMUM', rj, EPS_BLEND)
    t = b.math('MINIMUM', b.math('MAXIMUM', tj, MIN_FILL), 1.0)
    n = b.math('MAXIMUM', gi.outputs['Steps'], 1.0)

    # weight of the NEW shape for colours, surface values and blend settings:
    # a cross-fade as wide as the radius (a step when the radius is zero)
    if operation == 'UNION':
        hc = b.clamp01(b.math('MULTIPLY_ADD', b.math('DIVIDE', b.math('SUBTRACT', d1, d0), r), 0.5, 0.5))
    elif operation == 'SUBTRACT':
        hc = b.clamp01(b.math('MULTIPLY_ADD', b.math('DIVIDE', b.math('ADD', d1, d0), r), -0.5, 0.5))
    else:
        hc = b.clamp01(b.math('MULTIPLY_ADD', b.math('DIVIDE', b.math('SUBTRACT', d1, d0), r), -0.5, 0.5))
    b.link(b.mix_vector(pa, pn, hc), go.inputs['Result Params'])
    b.link(b.mix_color(c1, c0, hc), go.inputs['Result Color'])
    b.link(b.mix_vector(gi.outputs['Accumulated Surface'], gi.outputs['Surface'], hc), go.inputs['Result Surface'])
    b.link(b.mix_vector(gi.outputs['Accumulated Extra'], gi.outputs['Extra'], hc), go.inputs['Result Extra'])
    b.link(b.mix_color(gi.outputs['Accumulated Emission'], gi.outputs['Emission'], hc), go.inputs['Result Emission'])

    if mode in ('RAMP', 'CHAMFER'):
        if mode == 'RAMP':
            ln_inner = b.math('LOGARITHM', b.math('SUBTRACT', 1.0, b.math('MULTIPLY', t, RAMP_K)), 2.718281828459045)
            p = b.math('DIVIDE', -LN2, ln_inner)
            inv_p = b.math('DIVIDE', ln_inner, -LN2)

            def lp(u, v):
                # p-norm of (u, v), normalised by the larger one so u^p never overflows
                m = b.math('MAXIMUM', b.math('MAXIMUM', u, v), 1e-9)
                s = b.math('ADD', b.math('POWER', b.math('DIVIDE', u, m), p),
                           b.math('POWER', b.math('DIVIDE', v, m), p))
                return b.math('MULTIPLY', m, b.math('POWER', s, inv_p))
        else:
            def lp(u, v):
                return b.math('ADD', u, v)
        if operation == 'UNION':
            u = b.math('MAXIMUM', b.math('SUBTRACT', r, d0), 0.0)
            v = b.math('MAXIMUM', b.math('SUBTRACT', r, d1), 0.0)
            res = b.math('SUBTRACT', b.math('MAXIMUM', r, b.math('MINIMUM', d0, d1)), lp(u, v))
        elif operation == 'SUBTRACT':
            u = b.math('MAXIMUM', b.math('SUBTRACT', r, d0), 0.0)
            v = b.math('MAXIMUM', b.math('ADD', r, d1), 0.0)
            res = b.math('ADD', b.math('MINIMUM', b.neg(r), b.math('MAXIMUM', b.neg(d0), d1)), lp(u, v))
        else:
            u = b.math('MAXIMUM', b.math('ADD', r, d0), 0.0)
            v = b.math('MAXIMUM', b.math('ADD', r, d1), 0.0)
            res = b.math('ADD', b.math('MINIMUM', b.neg(r), b.math('MAXIMUM', d0, d1)), lp(u, v))
    elif mode == 'STEPS':
        def stairs_union(a, c):
            s_ = b.math('DIVIDE', r, n)
            u = b.math('SUBTRACT', c, r)
            m = b.math('FLOORED_MODULO', b.math('ADD', b.math('SUBTRACT', u, a), s_), b.math('MULTIPLY', s_, 2.0))
            tt = b.math('ABSOLUTE', b.math('SUBTRACT', m, s_))
            stair = b.math('MULTIPLY', b.math('ADD', b.math('ADD', u, a), tt), 0.5)
            return b.math('MINIMUM', b.math('MINIMUM', a, c), stair)
        if operation == 'UNION':
            res = stairs_union(d0, d1)
        elif operation == 'SUBTRACT':
            res = b.neg(stairs_union(b.neg(d1), d0))
        else:
            res = b.neg(stairs_union(b.neg(d0), b.neg(d1)))
    else:
        raise ValueError(mode)

    b.link(res, go.inputs['Result'])
    return ng


def op_group(operation, mode, rule):
    name = f"SDFF Op {operation.title()} {mode.title()} {rule.title()} v{GROUP_VERSION}"
    ng = bpy.data.node_groups.get(name)
    if ng is None:
        ng = _build_op(name, operation, mode, rule)
    return ng


# ----------------------------------------------------------------------------
# fusion tree
# ----------------------------------------------------------------------------

def shape_param(shape_settings):
    """The single free parameter of a primitive (tube ratio / cone top radius)."""
    if shape_settings.primitive == 'TORUS':
        return shape_settings.tube
    if shape_settings.primitive == 'CONE':
        return shape_settings.top_radius
    if shape_settings.primitive == 'PRISM':
        return float(shape_settings.sides)
    return 0.0


def iter_shapes(fusion_ob):
    """Valid, included shape objects of a fusion, in evaluation order."""
    for ref in fusion_ob.sdf_fusion.shapes:
        ob = ref.object
        if ob is None:
            continue
        st = ob.sdf_shape
        if not st.enabled or not st.include:
            continue
        yield ob


OP_ORDER = {'UNION': 0, 'SUBTRACT': 1, 'INTERSECT': 2}


def apply_auto_order(fusion_ob):
    """Keep cutters after all unions (stable), so Subtract / Intersect always act
    on the whole result no matter which shape the user switched."""
    fs = fusion_ob.sdf_fusion
    if not fs.auto_order or len(fs.shapes) < 2:
        return False
    active = fs.shapes[fs.active_shape_index].object if 0 <= fs.active_shape_index < len(fs.shapes) else None
    current = [r.object for r in fs.shapes]

    def key(item):
        i, ob = item
        if ob is None:
            return (3, i)
        return (OP_ORDER.get(ob.sdf_shape.operation, 0), i)
    desired = [ob for _i, ob in sorted(enumerate(current), key=key)]
    if desired == current:
        return False
    for t, ob in enumerate(desired):
        c = next(i for i in range(t, len(fs.shapes)) if fs.shapes[i].object == ob)
        if c != t:
            fs.shapes.move(c, t)
    if active is not None:
        for i, r in enumerate(fs.shapes):
            if r.object == active:
                fs['active_shape_index'] = i
                break
    return True


def get_modifier(fusion_ob, create=True):
    mod = fusion_ob.modifiers.get(MODIFIER_NAME)
    if mod is None and create:
        mod = fusion_ob.modifiers.new(MODIFIER_NAME, 'NODES')
    return mod


def get_tree(fusion_ob, create=True):
    mod = get_modifier(fusion_ob, create)
    if mod is None:
        return None
    tree = mod.node_group
    if tree is not None and tree.users > 1 and create:
        # shared with another fusion (e.g. after Shift+D): give this one its own
        mod.node_group = None
        tree = None
    if tree is None and create:
        tree = bpy.data.node_groups.new(f"{TREE_PREFIX}: {fusion_ob.name}", 'GeometryNodeTree')
        tree.is_modifier = True
        mod.node_group = tree
    return tree


def tree_is_shared(fusion_ob):
    mod = get_modifier(fusion_ob, create=False)
    return mod is not None and mod.node_group is not None and mod.node_group.users > 1


def _ensure_interface(tree):
    has_in = any(i.item_type == 'SOCKET' and i.in_out == 'INPUT' for i in tree.interface.items_tree)
    has_out = any(i.item_type == 'SOCKET' and i.in_out == 'OUTPUT' for i in tree.interface.items_tree)
    if not has_in:
        tree.interface.new_socket('Geometry', in_out='INPUT', socket_type='NodeSocketGeometry')
    if not has_out:
        tree.interface.new_socket('Geometry', in_out='OUTPUT', socket_type='NodeSocketGeometry')


def object_info_nodes(b, shapes):
    """One Object Info node per shape (transform + geometry in fusion space)."""
    out = []
    for i, sh in enumerate(shapes):
        oi = b.node('GeometryNodeObjectInfo', f'OBJ_{i}')
        oi.transform_space = 'RELATIVE'
        oi.inputs['Object'].default_value = sh
        oi.inputs['As Instance'].default_value = False
        out.append(oi)
    return out


def bounds_pipeline(b, obj_nodes, resolution, pad_radius, global_params, settings):
    """Padded bounds of all shapes, the cubic voxel size, and the voxel size /
    band width used for mesh shapes' distance fields."""
    join = b.node('GeometryNodeJoinGeometry', 'BOUNDS_JOIN')
    for oi in obj_nodes:
        b.tree.links.new(oi.outputs['Geometry'], join.inputs[0])
    bbox = b.node('GeometryNodeBoundBox')
    b.link(join.outputs[0], bbox.inputs[0])
    mn, mx = bbox.outputs['Min'], bbox.outputs['Max']
    mn, mx = mirror_bounds(b, settings, mn, mx)
    ex, ey, ez = b.sep(b.vmath('SUBTRACT', mx, mn))
    voxel0 = b.math('DIVIDE', b.max3(ex, ey, ez), resolution)
    gscale_r, _gscale_t, _gz = b.sep(global_params)
    reach = b.math('MULTIPLY', pad_radius, gscale_r)
    shell = b.value(settings.shell, 'SHELL')
    pad = b.math('ADD', b.math('ADD', reach, shell), b.math('MULTIPLY_ADD', voxel0, 2.0, 0.001))
    padv = b.comb(pad, pad, pad)
    mn2 = b.vmath('SUBTRACT', mn, padv)
    mx2 = b.vmath('ADD', mx, padv)
    ex2, ey2, ez2 = b.sep(b.vmath('SUBTRACT', mx2, mn2))
    voxel = b.math('DIVIDE', b.max3(ex2, ey2, ez2), resolution)
    detail = b.value(1.0, 'MESH_DETAIL')
    mesh_voxel = b.math('MAXIMUM', b.math('DIVIDE', voxel, b.math('MAXIMUM', detail, 0.05)), 1e-5)
    # the narrow band must cover the blend reach, or blends get truncated
    band_f = b.math('MINIMUM', b.math('ADD', b.math('DIVIDE', reach, mesh_voxel), 4.0), 256.0)
    band = b.node('FunctionNodeFloatToInt')
    band.rounding_mode = 'CEILING'
    b.link(band_f, band.inputs[0])
    return {'min': mn2, 'max': mx2, 'extent': (ex2, ey2, ez2), 'voxel': voxel,
            'mesh_voxel': mesh_voxel, 'mesh_band': band.outputs[0]}


def fold_position(b, settings, position):
    """Mirror: evaluate the field at |x| / |y| / |z| so one side is reflected."""
    if not (settings.mirror_x or settings.mirror_y or settings.mirror_z):
        return position
    x, y, z = b.sep(position)
    if settings.mirror_x:
        x = b.math('ABSOLUTE', x)
    if settings.mirror_y:
        y = b.math('ABSOLUTE', y)
    if settings.mirror_z:
        z = b.math('ABSOLUTE', z)
    return b.comb(x, y, z)


def mirror_bounds(b, settings, mn, mx):
    """Symmetric bounds on mirrored axes."""
    if not (settings.mirror_x or settings.mirror_y or settings.mirror_z):
        return mn, mx
    lo = list(b.sep(mn))
    hi = list(b.sep(mx))
    for i, on in enumerate((settings.mirror_x, settings.mirror_y, settings.mirror_z)):
        if on:
            ext = b.math('MAXIMUM', b.math('ABSOLUTE', lo[i]), b.math('ABSOLUTE', hi[i]))
            lo[i] = b.neg(ext)
            hi[i] = ext
    return b.comb(*lo), b.comb(*hi)


def build_field(b, fusion_ob, shapes, position, global_params, global_steps, obj_nodes, bounds):
    """Emit the distance field for ``shapes``; returns the accumulated socket.

    Shared by the modifier tree and by the test-suite's field sampler.
    """
    settings = fusion_ob.sdf_fusion
    mode, rule = settings.mode, settings.seam_rule
    acc = None
    acc_params = None
    acc_color = None
    acc_surface = None
    acc_extra = None
    acc_emission = None
    for i, sh in enumerate(shapes):
        st = sh.sdf_shape
        color = b.color_input(st.color, f'COLOR_{i}')
        surface = b.vector_input((st.metallic, st.roughness, st.transmission), f'SURFACE_{i}')
        extra = b.vector_input((st.ior, st.emission_strength, 0.0), f'EXTRA_{i}')
        emission = b.color_input(st.emission_color, f'EMISSION_{i}')
        params = b.vmath('MULTIPLY', b.vector_input((st.radius, st.fill, 0.0), f'PARAMS_{i}'), global_params)
        oi = obj_nodes[i]
        if st.primitive == 'MESH':
            # any closed mesh: Blender's own mesh -> SDF grid, sampled at Position.
            # Object Info already put the geometry in fusion space, so no
            # transform handling (and no scale approximation) is needed.
            m2s = b.node('GeometryNodeMeshToSDFGrid', f'MESH_SDF_{i}')
            b.link(oi.outputs['Geometry'], m2s.inputs['Mesh'])
            b.link(bounds['mesh_voxel'], m2s.inputs['Voxel Size'])
            b.link(bounds['mesh_band'], m2s.inputs['Band Width'])
            smp = b.node('GeometryNodeSampleGrid')
            smp.data_type = 'FLOAT'
            b.link(m2s.outputs['SDF Grid'], smp.inputs['Grid'])
            b.link(position, smp.inputs['Position'])
            try:
                smp.inputs['Interpolation'].default_value = 'TRILINEAR'
            except Exception:
                pass
            d = smp.outputs['Value']
        else:
            sep = b.node('FunctionNodeSeparateTransform')
            b.link(oi.outputs['Transform'], sep.inputs[0])
            rigid = b.node('FunctionNodeCombineTransform')
            b.link(sep.outputs['Translation'], rigid.inputs['Translation'])
            b.link(sep.outputs['Rotation'], rigid.inputs['Rotation'])
            rigid.inputs['Scale'].default_value = (1.0, 1.0, 1.0)
            inv = b.node('FunctionNodeInvertMatrix')
            b.link(rigid.outputs[0], inv.inputs[0])
            tp = b.node('FunctionNodeTransformPoint')
            b.link(position, tp.inputs['Vector'])
            b.link(inv.outputs['Matrix'], tp.inputs['Transform'])

            prim = b.group(primitive_group(st.primitive), f'PRIM_{i}')
            b.link(tp.outputs[0], prim.inputs['Position'])
            b.link(sep.outputs['Scale'], prim.inputs['Scale'])
            prim.inputs['Rounding'].default_value = st.rounding
            prim.inputs['Param'].default_value = shape_param(st)
            d = prim.outputs['Distance']

        if acc is None:
            acc = d
            acc_params = params
            acc_color, acc_surface, acc_extra, acc_emission = color, surface, extra, emission
            continue
        opn = b.group(op_group(st.operation, mode, rule), f'OP_{i}')
        b.link(d, opn.inputs['Distance'])
        b.link(acc, opn.inputs['Accumulated'])
        b.link(params, opn.inputs['Params'])
        b.link(acc_params, opn.inputs['Accumulated Params'])
        b.link(global_steps, opn.inputs['Steps'])
        acc_params = opn.outputs['Result Params']
        b.link(color, opn.inputs['Color'])
        b.link(acc_color, opn.inputs['Accumulated Color'])
        b.link(surface, opn.inputs['Surface'])
        b.link(acc_surface, opn.inputs['Accumulated Surface'])
        b.link(extra, opn.inputs['Extra'])
        b.link(acc_extra, opn.inputs['Accumulated Extra'])
        b.link(emission, opn.inputs['Emission'])
        b.link(acc_emission, opn.inputs['Accumulated Emission'])
        acc_color = opn.outputs['Result Color']
        acc_surface = opn.outputs['Result Surface']
        acc_extra = opn.outputs['Result Extra']
        acc_emission = opn.outputs['Result Emission']
        acc = opn.outputs['Result']
    return acc, (acc_color, acc_surface, acc_extra, acc_emission)


def max_radius(fusion_ob):
    m = 0.0
    for sh in iter_shapes(fusion_ob):
        m = max(m, sh.sdf_shape.radius)
    return m


def rebuild(fusion_ob):
    """(Re)generate the whole modifier tree from the fusion's shape list."""
    settings = fusion_ob.sdf_fusion
    apply_auto_order(fusion_ob)
    tree = get_tree(fusion_ob)
    if tree.animation_data is not None:
        tree.animation_data_clear()        # drivers of the nodes we are about to delete
    tree.nodes.clear()
    _ensure_interface(tree)
    b = _Builder(tree)
    gi = b.node('NodeGroupInput')
    go = b.node('NodeGroupOutput')

    shapes = list(iter_shapes(fusion_ob))
    if not shapes:
        b.link(gi.outputs[0], go.inputs[0])
        return tree

    gparams = b.vector_input((settings.radius_scale, settings.fill_scale, 1.0), 'GLOBAL_PARAMS')
    steps = b.value(float(settings.steps), 'GLOBAL_STEPS')
    resolution = b.integer(settings.live_resolution(), 'RESOLUTION')
    adaptivity = b.value(settings.adaptivity, 'ADAPTIVITY')
    pad_radius = b.value(max_radius(fusion_ob), 'PAD_RADIUS')
    position = b.node('GeometryNodeInputPosition').outputs[0]

    # bounds: union of the shape meshes (already in fusion-local space)
    obj_nodes = object_info_nodes(b, shapes)
    bounds = bounds_pipeline(b, obj_nodes, resolution, pad_radius, gparams, settings)
    tree.nodes['MESH_DETAIL'].outputs[0].default_value = settings.mesh_detail
    mn2, mx2 = bounds['min'], bounds['max']
    ex2, ey2, ez2 = bounds['extent']
    voxel = bounds['voxel']

    position = fold_position(b, settings, position)
    acc, (acc_color, acc_surface, acc_extra, acc_emission) = build_field(
        b, fusion_ob, shapes, position, gparams, steps, obj_nodes, bounds)
    # Hollow: keep only a wall of thickness SHELL around the surface
    shell = tree.nodes['SHELL'].outputs[0]
    hollow = b.math('SUBTRACT', b.math('ABSOLUTE', acc), shell)
    on = b.math('GREATER_THAN', shell, 0.0)
    acc = b.math('MULTIPLY_ADD', b.math('SUBTRACT', hollow, acc), on, acc)

    def res_axis(extent):
        f = b.math('MAXIMUM', b.math('ADD', b.math('DIVIDE', extent, voxel), 1.0), 2.0)
        n = b.node('FunctionNodeFloatToInt')
        n.rounding_mode = 'CEILING'
        b.link(f, n.inputs[0])
        return n.outputs[0]

    density = b.neg(acc)
    vc = b.node('GeometryNodeVolumeCube', 'VOLUME_CUBE')
    b.link(density, vc.inputs['Density'])
    vc.inputs['Background'].default_value = -1.0
    b.link(mn2, vc.inputs['Min'])
    b.link(mx2, vc.inputs['Max'])
    b.link(res_axis(ex2), vc.inputs['Resolution X'])
    b.link(res_axis(ey2), vc.inputs['Resolution Y'])
    b.link(res_axis(ez2), vc.inputs['Resolution Z'])

    grid = b.node('GeometryNodeGetNamedGrid')
    grid.data_type = 'FLOAT'
    grid.inputs['Name'].default_value = GRID_NAME
    b.link(vc.outputs['Volume'], grid.inputs['Volume'])

    g2m = b.node('GeometryNodeGridToMesh', 'GRID_TO_MESH')
    b.link(grid.outputs['Grid'], g2m.inputs['Grid'])
    g2m.inputs['Threshold'].default_value = 0.0
    b.link(adaptivity, g2m.inputs['Adaptivity'])

    smooth = b.node('GeometryNodeSetShadeSmooth')
    b.link(g2m.outputs['Mesh'], smooth.inputs[0])
    smooth.inputs['Shade Smooth'].default_value = True
    mesh_out = smooth.outputs[0]

    if settings.blend_colors:
        # Evaluate the blended colour field at the mesh vertices and store it
        # as a colour attribute the material can read.
        store = b.node('GeometryNodeStoreNamedAttribute', 'STORE_COLOR')
        store.data_type = 'FLOAT_COLOR'
        store.domain = 'POINT'
        store.inputs['Name'].default_value = COLOR_ATTRIBUTE
        b.link(mesh_out, store.inputs['Geometry'])
        b.link(acc_color, store.inputs['Value'])
        for nm, dtype, value in ((SURFACE_ATTRIBUTE, 'FLOAT_VECTOR', acc_surface),
                                 (EXTRA_ATTRIBUTE, 'FLOAT_VECTOR', acc_extra),
                                 (EMISSION_ATTRIBUTE, 'FLOAT_COLOR', acc_emission)):
            st_ = b.node('GeometryNodeStoreNamedAttribute')
            st_.data_type = dtype
            st_.domain = 'POINT'
            st_.inputs['Name'].default_value = nm
            b.link(store.outputs[0], st_.inputs['Geometry'])
            b.link(value, st_.inputs['Value'])
            store = st_
        # Solid-mode "Attribute" colouring only reads the mesh's *active*
        # colour layer, which a layer created in nodes never is.  Joining the
        # (empty) original mesh first makes the result inherit its active
        # colour layer name, so the blend shows in Solid mode too.
        ensure_color_layer(fusion_ob)
        join_out = b.node('GeometryNodeJoinGeometry', 'COLOR_JOIN')
        # The last-linked input becomes the first joined component, and the
        # joined mesh inherits that component's active colour layer.  The
        # fusion's own mesh is a single "carrier" vertex with the layer set.
        tree.links.new(store.outputs[0], join_out.inputs[0])
        tree.links.new(gi.outputs[0], join_out.inputs[0])
        neighbors = b.node('GeometryNodeInputMeshVertexNeighbors')
        loose = b.math('LESS_THAN', neighbors.outputs['Face Count'], 0.5)
        delete = b.node('GeometryNodeDeleteGeometry', 'COLOR_CARRIER_DELETE')
        delete.domain = 'POINT'
        delete.mode = 'ALL'
        b.link(join_out.outputs[0], delete.inputs['Geometry'])
        b.link(loose, delete.inputs['Selection'])
        mesh_out = delete.outputs[0]

    # Grid to Mesh output carries no material; apply the fusion object's own.
    setmat = b.node('GeometryNodeSetMaterial', 'SET_MATERIAL')
    b.link(mesh_out, setmat.inputs['Geometry'])
    setmat.inputs['Material'].default_value = fusion_ob.active_material
    b.link(setmat.outputs[0], go.inputs[0])

    add_drivers(tree, fusion_ob, shapes)
    mod = get_modifier(fusion_ob)
    mod.show_viewport = settings.live
    return tree


# ----------------------------------------------------------------------------
# drivers: keep node values in sync with (possibly animated) properties
# ----------------------------------------------------------------------------

def _drive(target, prop, id_obj, data_path, index=-1):
    """Drive ``target.prop`` (array element ``index``) by ``id_obj.data_path`` with no Python."""
    fc = target.driver_add(prop, index) if index >= 0 else target.driver_add(prop)
    drv = fc.driver
    drv.type = 'SUM'
    for v in list(drv.variables):
        drv.variables.remove(v)
    var = drv.variables.new()
    var.name = 'v'
    var.type = 'SINGLE_PROP'
    t = var.targets[0]
    t.id_type = 'OBJECT'
    t.id = id_obj
    t.data_path = data_path
    return fc


def _param_path(shape_settings):
    return {'TORUS': 'sdf_shape.tube', 'CONE': 'sdf_shape.top_radius', 'PRISM': 'sdf_shape.sides'}.get(
        shape_settings.primitive)


def add_drivers(tree, fusion_ob, shapes):
    """Wire every scalar/colour node value to its property so animation and
    drivers on the properties reach the evaluated tree."""
    nodes = tree.nodes
    _drive(nodes['GLOBAL_PARAMS'], 'vector', fusion_ob, 'sdf_fusion.radius_scale', 0)
    _drive(nodes['GLOBAL_PARAMS'], 'vector', fusion_ob, 'sdf_fusion.fill_scale', 1)
    _drive(nodes['GLOBAL_STEPS'].outputs[0], 'default_value', fusion_ob, 'sdf_fusion.steps')
    _drive(nodes['ADAPTIVITY'].outputs[0], 'default_value', fusion_ob, 'sdf_fusion.adaptivity')
    if 'MESH_DETAIL' in nodes:
        _drive(nodes['MESH_DETAIL'].outputs[0], 'default_value', fusion_ob, 'sdf_fusion.mesh_detail')
    if 'SHELL' in nodes:
        _drive(nodes['SHELL'].outputs[0], 'default_value', fusion_ob, 'sdf_fusion.shell')
    for i, sh in enumerate(shapes):
        st = sh.sdf_shape
        pnode = nodes.get(f'PARAMS_{i}')
        if pnode is not None:
            _drive(pnode, 'vector', sh, 'sdf_shape.radius', 0)
            _drive(pnode, 'vector', sh, 'sdf_shape.fill', 1)
        prim = nodes.get(f'PRIM_{i}')
        if prim is not None:
            _drive(prim.inputs['Rounding'], 'default_value', sh, 'sdf_shape.rounding')
            ppath = _param_path(st)
            if ppath:
                _drive(prim.inputs['Param'], 'default_value', sh, ppath)
        cnode = nodes.get(f'COLOR_{i}')
        if cnode is not None:
            prop = 'value' if hasattr(cnode, 'value') else 'color'
            for c in range(4):
                _drive(cnode, prop, sh, f'sdf_shape.color[{c}]', c)
        snode = nodes.get(f'SURFACE_{i}')
        if snode is not None:
            for c, path in enumerate(('sdf_shape.metallic', 'sdf_shape.roughness', 'sdf_shape.transmission')):
                _drive(snode, 'vector', sh, path, c)
        xnode = nodes.get(f'EXTRA_{i}')
        if xnode is not None:
            _drive(xnode, 'vector', sh, 'sdf_shape.ior', 0)
            _drive(xnode, 'vector', sh, 'sdf_shape.emission_strength', 1)
        enode = nodes.get(f'EMISSION_{i}')
        if enode is not None:
            prop = 'value' if hasattr(enode, 'value') else 'color'
            for c in range(4):
                _drive(enode, prop, sh, f'sdf_shape.emission_color[{c}]', c)
    fc = nodes['PAD_RADIUS'].outputs[0].driver_add('default_value')
    drv = fc.driver
    drv.type = 'MAX'
    for v in list(drv.variables):
        drv.variables.remove(v)
    for k, sh in enumerate(shapes):
        var = drv.variables.new()
        var.name = f'r{k}'
        var.type = 'SINGLE_PROP'
        var.targets[0].id_type = 'OBJECT'
        var.targets[0].id = sh
        var.targets[0].data_path = 'sdf_shape.radius'


def _shape_index(fusion_ob, shape_ob):
    for i, sh in enumerate(iter_shapes(fusion_ob)):
        if sh == shape_ob:
            return i
    return -1


def update_values(fusion_ob):
    """Push scalar settings into the existing tree without rebuilding it."""
    settings = fusion_ob.sdf_fusion
    tree = get_tree(fusion_ob, create=False)
    if tree is None:
        return
    nodes = tree.nodes
    needed = ('GLOBAL_PARAMS', 'GLOBAL_STEPS', 'RESOLUTION', 'ADAPTIVITY', 'PAD_RADIUS')
    if any(n not in nodes for n in needed):
        if any(True for _ in iter_shapes(fusion_ob)):
            rebuild(fusion_ob)
        return
    nodes['GLOBAL_PARAMS'].vector = (settings.radius_scale, settings.fill_scale, 1.0)
    nodes['GLOBAL_STEPS'].outputs[0].default_value = float(settings.steps)
    nodes['RESOLUTION'].integer = settings.live_resolution()
    nodes['ADAPTIVITY'].outputs[0].default_value = settings.adaptivity
    nodes['PAD_RADIUS'].outputs[0].default_value = max_radius(fusion_ob)
    if 'MESH_DETAIL' in nodes:
        nodes['MESH_DETAIL'].outputs[0].default_value = settings.mesh_detail
    if 'SHELL' in nodes:
        nodes['SHELL'].outputs[0].default_value = settings.shell
    sync_material(fusion_ob)


def update_shape_values(fusion_ob, shape_ob):
    tree = get_tree(fusion_ob, create=False)
    if tree is None:
        return
    i = _shape_index(fusion_ob, shape_ob)
    if i < 0:
        return
    st = shape_ob.sdf_shape
    prim = tree.nodes.get(f'PRIM_{i}')
    if prim is None and st.primitive != 'MESH':
        rebuild(fusion_ob)
        return
    if prim is not None:
        prim.inputs['Rounding'].default_value = st.rounding
        prim.inputs['Param'].default_value = shape_param(st)
    cnode = tree.nodes.get(f'COLOR_{i}')
    if cnode is not None:
        set_color_node(cnode, st.color)
    snode = tree.nodes.get(f'SURFACE_{i}')
    if snode is not None:
        snode.vector = (st.metallic, st.roughness, st.transmission)
    xnode = tree.nodes.get(f'EXTRA_{i}')
    if xnode is not None:
        xnode.vector = (st.ior, st.emission_strength, 0.0)
    enode = tree.nodes.get(f'EMISSION_{i}')
    if enode is not None:
        set_color_node(enode, st.emission_color)
    pnode = tree.nodes.get(f'PARAMS_{i}')
    if pnode is None:
        rebuild(fusion_ob)
        return
    pnode.vector = (st.radius, st.fill, 0.0)
    pad = tree.nodes.get('PAD_RADIUS')
    if pad is not None:
        pad.outputs[0].default_value = max_radius(fusion_ob)


def ensure_color_layer(fusion_ob):
    """Give the fusion's own (empty) mesh an active 'Color' layer."""
    me = fusion_ob.data
    if me is None or not hasattr(me, 'color_attributes'):
        return
    if len(me.vertices) == 0:
        # one carrier vertex; it is removed again inside the node tree
        me.from_pydata([(0.0, 0.0, 0.0)], [], [])
    ca = me.color_attributes.get(COLOR_ATTRIBUTE)
    if ca is None:
        ca = me.color_attributes.new(COLOR_ATTRIBUTE, 'FLOAT_COLOR', 'POINT')
    try:
        me.color_attributes.active_color = ca
        me.color_attributes.render_color_index = me.color_attributes.find(COLOR_ATTRIBUTE)
    except Exception:
        pass


def sync_material(fusion_ob):
    """Keep the Set Material node equal to the fusion object's active material."""
    tree = get_tree(fusion_ob, create=False)
    if tree is None:
        return False
    node = tree.nodes.get('SET_MATERIAL')
    if node is None:
        return False
    mat = fusion_ob.active_material
    if node.inputs['Material'].default_value != mat:
        node.inputs['Material'].default_value = mat
        return True
    return False


def set_resolution(fusion_ob, resolution):
    tree = get_tree(fusion_ob, create=False)
    if tree is None or 'RESOLUTION' not in tree.nodes:
        return False
    tree.nodes['RESOLUTION'].integer = int(resolution)
    return True


def evaluate_mesh(fusion_ob, resolution=None, context=None):
    """Evaluate the fusion (optionally at another resolution) into a new Mesh datablock."""
    context = context or bpy.context
    settings = fusion_ob.sdf_fusion
    mod = get_modifier(fusion_ob, create=False)
    prev_show = mod.show_viewport if mod else True
    if mod is not None and not prev_show:
        mod.show_viewport = True
    if resolution is not None:
        set_resolution(fusion_ob, resolution)
    try:
        depsgraph = context.evaluated_depsgraph_get()
        ev = fusion_ob.evaluated_get(depsgraph)
        mesh = bpy.data.meshes.new_from_object(ev, preserve_all_data_layers=True, depsgraph=depsgraph)
    finally:
        if resolution is not None:
            set_resolution(fusion_ob, settings.live_resolution())
        if mod is not None and not prev_show:
            mod.show_viewport = False
    return mesh


def build_field_sampler(fusion_ob, sampler_ob, attribute_name='sdf'):
    """Test helper: store the fusion's distance field on ``sampler_ob``'s points.

    ``sampler_ob`` must share the fusion's world matrix so 'Relative' object
    transforms line up.
    """
    settings = fusion_ob.sdf_fusion
    mod = sampler_ob.modifiers.get('SDFF Sampler') or sampler_ob.modifiers.new('SDFF Sampler', 'NODES')
    tree = bpy.data.node_groups.new('SDFF Sampler', 'GeometryNodeTree')
    tree.is_modifier = True
    mod.node_group = tree
    _ensure_interface(tree)
    b = _Builder(tree)
    gi = b.node('NodeGroupInput')
    go = b.node('NodeGroupOutput')
    shapes = list(iter_shapes(fusion_ob))
    gparams = b.vector_input((settings.radius_scale, settings.fill_scale, 1.0), 'GLOBAL_PARAMS')
    steps = b.value(float(settings.steps), 'GLOBAL_STEPS')
    position = b.node('GeometryNodeInputPosition').outputs[0]
    resolution = b.integer(settings.live_resolution(), 'RESOLUTION')
    pad_radius = b.value(max_radius(fusion_ob), 'PAD_RADIUS')
    obj_nodes = object_info_nodes(b, shapes)
    bounds = bounds_pipeline(b, obj_nodes, resolution, pad_radius, gparams, settings)
    tree.nodes['MESH_DETAIL'].outputs[0].default_value = settings.mesh_detail
    position = fold_position(b, settings, position)
    acc, _extras = build_field(b, fusion_ob, shapes, position, gparams, steps, obj_nodes, bounds)
    store = b.node('GeometryNodeStoreNamedAttribute')
    store.data_type = 'FLOAT'
    store.domain = 'POINT'
    b.link(gi.outputs[0], store.inputs['Geometry'])
    store.inputs['Name'].default_value = attribute_name
    b.link(acc, store.inputs['Value'])
    b.link(store.outputs[0], go.inputs[0])
    return tree
