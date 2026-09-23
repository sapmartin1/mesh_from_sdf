# Porting notes: Mesh from SDF (Blender 4.x, ModernGL) -> SDF Fusion (Blender 5.x)

This is the record of what was inspected, what broke, what was decided and
what was kept.  Upstream base commit: `61ab4ca` of
https://github.com/TLabAltoh/mesh_from_sdf (MIT).

## 1. What the upstream add-on is made of

| Upstream file | Role | Fate in the port |
|---|---|---|
| `__init__.py` (1150 lines) | `bl_info`, per-primitive `CollectionProperty` lists on the Scene, hierarchy UIList, add/remove/reorder operators, `create_context()` for ModernGL at `register()`, depsgraph handler that rewrites SSBOs | Replaced by `props.py`, `ops.py`, `ui.py`, `handlers.py` |
| `shader/common.py` (411 lines) | GLSL library: `sdBox`, `sdSphere`, `sdCylinder`, `sdCappedCone`, `sdCappedTorus`, ..., `opSmoothUnion`, `opRoundUnion`, `opChampferUnion`, `opStairsUnion` and difference/intersection variants | **Kept as maths.** Ported 1:1 to numpy (`sdf_ref.py`, the reference) and to Geometry Nodes groups (`nodes.py`) |
| `shader/factory.py` | Generates the GLSL `getDist()` from the hierarchy | Replaced by `nodes.build_field()` which chains the same operators in the same order (new shape first, accumulated result second) |
| `shader/buffer_factory.py` (1476 lines) | numpy -> ModernGL SSBOs per primitive, incremental updates | Not needed: shape transforms are read by Object Info nodes, scalar settings are written into node sockets |
| `raymarching.py` | Fullscreen raymarch in a `gpu.types.GPUShader` whose SSBOs are bound through ModernGL | Removed (see 2.2); the live preview is now the real mesh |
| `render_engine.py` | Custom `RenderEngine` that draws the raymarch | Removed; no custom render engine, the result renders in EEVEE/Cycles like any mesh |
| `marching_cube.py`, `marching_tables.py` | GL 4.3 compute-shader marching cubes into SSBOs, chunked 128^3, `mesh.from_pydata` | Replaced by Volume Cube -> Grid to Mesh (OpenVDB) |
| `pointer.py` | Per-primitive PropertyGroups + proxy meshes built with `bpy.ops.mesh.primitive_*` in edit mode | Replaced by `SDFShapeSettings` and `bmesh` proxy generation (no operator/mode juggling) |
| `gizmo/*.py` | Arrow gizmos per primitive parameter | Dropped for v1: sizes are the object's scale, so the standard transform gizmos do the job |
| `util/moderngl.py`, `util/pymodule.py` | ModernGL context + pip installer | Removed |

## 2. Blender 5.x / macOS incompatibilities found

### 2.1 Confirmed, fundamental (platform)

* **ModernGL cannot provide OpenGL 4.3 on macOS.**  Measured on this Apple
  M5 machine with ModernGL 5.12: `ctx.version_code == 410`,
  `GL_VERSION = "4.1 Metal - 90.5"`, and `ctx.compute_shader()` fails with
  "cannot create shader".  Upstream asserts `version_code >= 430` in
  `create_context()`, which runs at `register()`, so the add-on cannot even
  be enabled on a Mac.  Compute shaders and SSBOs simply do not exist in
  Apple's OpenGL.
* **Blender on macOS runs on Metal**, and `raymarching.py` relies on
  ModernGL binding SSBOs into the *same OpenGL context* that Blender's
  `gpu.types.GPUShader` compiled into.  That interop has no Metal equivalent.
* **Blender's own `gpu` module cannot host the upstream design either.**
  Checked on 5.1: `gpu.compute.dispatch` exists and `GPUShaderCreateInfo`
  offers `compute_source`, `image`, `uniform_buf`, `push_constant`, but there
  is **no storage-buffer binding** (`storage_buf`) in the Python API, so the
  atomic-append marching cubes (`atomicAdd(count)`, `triangles[index] = ...`)
  cannot be expressed.  Only image outputs would be possible, which would
  still leave the triangle extraction to the CPU.

Because of the three points above, "port the engine as is" is impossible on
the target hardware; the evaluation pipeline had to be replaced.

### 2.2 Blender API changes that would have needed patching anyway

* `bpy.app.binary_path_python` (used by `util/pymodule.py`) was removed in
  Blender 2.91; `sys.executable` is the replacement.
* Absolute imports (`from mesh_from_sdf.raymarching import *`) break under
  the extension system, where the package is `bl_ext.<repo>.<id>`.  The port
  uses relative imports everywhere.
* `bl_info` add-ons are legacy; the port ships a `blender_manifest.toml`
  extension (`blender_version_min = "5.0.0"`, MIT license, no wheels).
* `gpu.types.GPUShader(vertexcode, fragcode)` legacy strings with raw
  `uniform`/`in`/`out` declarations and `#version` lines are not accepted by
  the 5.x create-info pipeline on Metal; `bgl` was removed in 5.0.
* Blender 5.2 changed Geometry Nodes modifier input access
  (`modifier["socket"]` -> `modifier.properties.inputs...`) and the socket
  identifiers of the Compare / Random Value nodes.  The port deliberately
  uses neither: all values live in nodes inside the tree, and comparisons use
  Math nodes (`LESS_THAN`), so the same code runs on 5.0, 5.1 and 5.2.
* `UILayout.template_list(columns=...)` is deprecated in 5.1; not used.

## 3. The replacement: Geometry Nodes as the SDF engine

Blender 5.0 promoted the volume-grid nodes out of experimental
("Volume grids can now be processed directly with the new grid socket").
The port compiles the fusion into one Geometry Nodes tree per fusion object:

```
for each shape (list order):
  Object Info (Relative) -> Separate Transform -> Combine Transform (scale 1,1,1)
      -> Invert Matrix -> Transform Point(Position)      # rigid inverse
      -> "SDFF <Primitive>" group (Position, Scale, Rounding, Param) -> Distance
  "SDFF Op <Operation> <BlendType>" group (Distance, Accumulated, Blend, Steps)
Join Geometry(proxy meshes) -> Bounding Box -> padded Min/Max, cubic voxels
Volume Cube (Density = -distance, Background -1) -> Get Named Grid("density")
  -> Grid to Mesh (Threshold 0, Adaptivity) -> Set Shade Smooth -> Output
```

Why this satisfies the brief:

* **Live, non-destructive:** the Object Info nodes make the tree depend on
  the source objects, so moving, rotating or scaling a shape re-evaluates
  the mesh through the depsgraph, with no Python handler in the loop.
* **Cross-platform:** no GPU API is touched.  Field evaluation is
  multithreaded C++ and meshing is OpenVDB, on Metal, Vulkan or OpenGL alike.
* **Preview vs final:** a single `RESOLUTION` integer node; the presets
  Low/Medium/High map to 32/64/128 voxels along the longest axis and
  *Convert to Mesh* temporarily raises it to *Final Resolution*.
  Measured on the M5 for box + sphere (smooth union): 64 -> 2 ms,
  128 -> 8 ms, 192 -> 22 ms, 256 -> 53 ms per evaluation, so no throttling
  was necessary; *Live Update* can still pause the modifier.
* **Same maths:** every primitive/operator group reproduces the upstream GLSL
  formula.  `tests/run_tests.py` samples the node field on thousands of
  random points and compares against `sdf_ref.py` (max error ~2e-6).

Deviations from upstream, on purpose:

* Z is the height axis of cylinders and cones (Blender is Z-up; upstream was
  Y-up from GLSL conventions).
* Sizes come from the object's *scale* instead of separate width/height
  properties, so the normal transform tools and gizmos edit them.  Boxes and
  cylinder heights are exact under non-uniform scale; spheres, torus and cone
  radii use the normalised-space approximation (exact for uniform XY scale).
* Upstream scaled the round/chamfer *intersection* result by 0.5; the port
  keeps the true hg_sdf formulas so distances stay metric.
* Blend, blend type, steps and resolution live on the fusion (one slider
  for the whole model, like Spline's Shape Blend); a per-shape
  *Custom Blend* override keeps upstream's per-object flexibility.
* Deleting a shape object with X is enough; a depsgraph handler prunes it.
* New in 1.1: per-shape colours blended along with the distances (Chisel
  style).  Every op group also mixes an RGBA colour with the smooth-blend
  factor `h` (hard booleans use a step, round/chamfer/steps borrow the smooth
  factor); the colour field is evaluated at the mesh vertices with Store
  Named Attribute ("Color") and a generated material reads it.

## 3b. 1.2.0 additions (issues found in review by Martin / ChatGPT)

* **Render visibility:** *Convert to Mesh* with *Hide Fusion Setup* now also
  sets *Disable in Renders* on the fusion; *Show Fusion Setup* reverts both.
* **Animated settings:** property `update` callbacks only fire on user edits,
  so keyframed values never reached the node tree.  Every scalar/colour node
  value is now driven by its property (`SUM`/`MAX` drivers with a single
  `SINGLE_PROP` variable, no Python expressions, so they work with
  auto-run scripts disabled).  Drivers are cleared and recreated on rebuild.
* **Apply Scale/Rotation/Location:** sizes come from the object scale, so
  Ctrl+A silently changed the field.  A depsgraph handler now detects a
  transformed proxy mesh, recovers the affine transform by least squares
  against the unit proxy, folds it into the object matrix and restores the
  unit proxy.  Hand-edited proxies (non-affine) are left alone.
* **Per-shape materials:** op groups now blend colour, a surface vector
  (metallic, roughness, transmission), an extra vector (IOR, emission
  strength) and an emission colour with the same factor as the distances.
  They are stored as mesh attributes and the generated "SDF Fusion Material"
  feeds them into a Principled BSDF.  *Use Shape Material* syncs a shape's
  values from its own material's Principled BSDF (handler on material and
  object updates).  Blending whole node trees per region is not possible on a
  single mesh; this is the standard attribute-driven equivalent.
* **New primitives:** capsule, square pyramid (exact port of upstream
  `sdPyramid`, Z-up) and regular N-gon prism (closed-form polygon SDF,
  replaces upstream's per-edge loop, which Geometry Nodes cannot express).
* **Size fields** (object dimensions) per shape, **Duplicate Fusion**, and a
  handler that gives Shift+D copies their own node tree and adopts their
  duplicated shapes.  Custom size gizmos were deliberately not added: the
  standard Scale tool already provides per-axis handles for these shapes.

## 3c. 1.3.0: human-facing fixes (reported by Martin)

* **"Subtract only works on one of the two shapes."**  Evaluation is ordered,
  and the first shape is the base, so switching the first shape to Subtract
  did nothing.  Shapes are now kept unions-first, then Subtract, then
  Intersect (stable sort, *Cutters Last*, on by default), so whichever shape
  is switched becomes a cutter of the whole result.  The panel warns when no
  Union shape is left.
* **"It low-polies the selected object."**  A cutter's surface leaves the
  fused mesh, and what remained visible was its 32-segment wire proxy, which
  looked like a low-poly version of the shape and hid the cut.  Shapes are
  now drawn as bounds guides (sphere / cylinder / cone / capsule / box
  outlines); Wire remains available per fusion.
* **Shift+D on a shape** now joins the copy to the same fusion (handler
  adopts shapes that are parented to a fusion but not listed; linked
  duplicates get their own proxy mesh).

## 3d. 1.4.0: per-shape Radius / Blend and the ramp family (designed with Martin)

* One fusion-wide Blend made multi-shape models hard to tune, and the single
  smooth-min parameter mixed two things: how far a blend reaches and how much
  material it adds.  Both are now **per shape**: *Radius* (reach) and *Blend*
  (fill, 0..1), with master multipliers on the fusion.
* Smooth / Round / Chamfer are replaced by one family, a p-norm
  generalisation of upstream's `opRoundUnion`:
  `max(r, min(d0, d1)) - (u^p + v^p)^(1/p)` with `p >= 2`.  `p = 2` is exactly
  upstream's round union (a circular quarter-pipe) and is the fullest the ramp
  gets; `p -> inf` is the hard boolean.  *Blend* is the depth of the fill at
  the seam relative to the quarter-pipe, `p = ln 2 / -ln(1 - (1 - 1/sqrt 2) t)`,
  so the slider is linear in "amount".  The fillet is concave, tangent to both
  surfaces and bounded by Radius.  The p-norm is normalised by the larger term
  so high exponents cannot overflow in single precision.
  Two earlier attempts were rejected in review: a two-parameter polynomial
  (overshoots into ledges), and letting the family run on to `p = 1` (1.4.0):
  a nearly straight profile revolved around a round shape becomes a cone-like
  skirt that reads as bulging outward in 3D even though its 2D section is
  harmless.  The straight bevel survives only as the explicit *Flat Bevel*
  mode; *Steps* stays as a mode too.
* Radius and Blend travel with the field as a vector that is mixed like the
  colours, so every seam sees the settings of both shapes meeting there.  The
  *Seams* rule combines them: **Sharper Wins** (default, makes blending a true
  per-object property independent of list order), Average, Softer Wins, or
  Newer Shape (the pre 1.4 behaviour).
* Bug fixed along the way: the first shape of a fusion created away from the
  world origin landed at double the offset (its placement used the new
  fusion's not-yet-evaluated world matrix).  Shapes are now placed through
  their local matrix after a view-layer update; covered by placement tests.
* Fusions saved by older versions are migrated on load (and on the first
  depsgraph update after an upgrade): the old Blend distance becomes each
  shape's Radius with full Blend, Chamfer -> Flat Bevel mode, None -> Radius 0
  (1.4.0 fusions get their Blend remapped onto the new range);
  fusions that used per-shape overrides keep the old seam rule so they look
  the same.

## 3e. 1.5.0: editable and imported mesh shapes (requested by Martin)

* Primitives are analytic formulas; editing their guide meshes could never
  affect the field, which surprised a mesh modeller.  A new **Mesh** shape
  kind feeds a real, editable mesh through Blender 5's native *Mesh to SDF
  Grid* node and samples the grid with *Sample Grid* at the field position,
  so the same ramp / colour / material operators apply.  Object Info already
  delivers the geometry in fusion space, so no transform handling (and no
  scale approximation) is needed; Apply Transform is therefore allowed on
  mesh shapes and the repair handler skips them.
* The grid's voxel size is the fusion's own voxel divided by *Mesh Detail*;
  its narrow band is sized from the largest blend Radius so smooth blends are
  never truncated by the band.
* Operators: *Mesh* (editable cube), *Use Selected Objects* (adopt imported
  or modelled objects, keeping world transforms), *Make Editable* (primitive
  -> mesh shape, geometry kept), *Release from Fusion* (back to a normal
  object).  The panel warns when a mesh shape is not closed.
* Measured: an editable cube's field matches the analytic box within 0.2
  voxels; Mesh Detail 2 halves that again.

## 4. Verified

* `tests/run_tests.py` on Blender 5.1.0 / macOS 26 / Apple M5: 187 checks,
  all passing (primitive maths for all 8 primitives, all 15 operation x
  blend combinations, the Phase 1 acceptance flow, analytic volumes, colour
  and material blending, animation drivers, Apply Transform repair,
  duplicates, timings).
* `blender --command extension build` / `validate` succeed, and the ZIP
  installs/enables/disables cleanly through the extension system
  (`bl_ext.user_default.sdf_fusion`).
* `tests/gui_smoke.py` drove the installed extension in a real Blender 5.1
  window (Metal backend): panel drawn, shapes added through the operators,
  blend / blend type / subtract changed live, Convert to Mesh produced a
  68k-face mesh without modifiers.  Screenshots: `docs/ui_*.png`.
* Workbench render of several fusions (`docs/preview.png`).

## 5. Known limitations / future work

* No per-parameter gizmos (upstream had arrow gizmos); the Scale tool and
  the Size fields cover the common cases.
* Upstream's truncated pyramid, quadratic Bezier tube and GLSL-file
  primitives are not ported yet.
* Materials: the fused mesh has no UVs (marching-cubes style output).
* Native SDF-grid alternative: Blender 5 also has *Mesh to SDF Grid* and
  *SDF Grid Boolean* nodes.  They were not used because they only offer hard
  booleans and would sample the proxy meshes; the analytic field gives exact
  smooth blends and needs no voxelisation of the sources.  The tree could
  still be extended with *SDF Grid Fillet* / *Offset* as post-processing.
