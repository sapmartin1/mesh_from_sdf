# SDF Fusion (Blender 5.x)

Smooth, non-destructive SDF modelling for Blender 5.0+: add a box, add a
sphere, overlap them, drag one **Blend** slider and watch them melt together.
The result is a live mesh that updates while you move, rotate or scale the
source shapes, and it bakes to a normal Blender mesh with one click.

This is a Blender 5 port and redesign of the MIT licensed
[Mesh from SDF](https://github.com/TLabAltoh/mesh_from_sdf) add-on by
TLabAltoh.  The original evaluated its distance field with OpenGL 4.3 compute
shaders through ModernGL, which does not exist on macOS / Metal; the port
compiles the very same distance-field maths into a Geometry Nodes tree that
Blender evaluates natively on every platform.  See
[docs/PORTING_NOTES.md](docs/PORTING_NOTES.md) for the full record.

![Preview](docs/preview.png)

![Sidebar panel with a smooth union](docs/ui_smooth_union.png)

![Blended colours](docs/ui_blend_colors.png)

## Requirements

* Blender 5.0 or newer (developed against 5.1, targeted at 5.2 LTS)
* Any platform Blender runs on: no GPU compute, no extra Python packages.
  Tested on macOS / Apple Silicon.

## Installation

1. Download `sdf_fusion-1.1.0.zip` (or build it, see below).
2. Drag and drop the ZIP into a Blender window, or use
   *Edit > Preferences > Get Extensions > (dropdown) > Install from Disk...*
3. Enable **SDF Fusion** if it is not enabled automatically.
4. In the 3D Viewport press **N** and open the **SDF Fusion** tab.

To build the ZIP yourself:

```bash
/Applications/Blender.app/Contents/MacOS/Blender -b --command extension build --source-dir . --output-dir dist
```

## Usage

The sidebar tab is the whole interface:

```
SDF Fusion
  [ Box ] [ Sphere ] [ Cylinder ]
  [ Torus ] [ Cone ] [+]

  <active shape>
    Primitive      Box / Sphere / Cylinder / Torus / Cone
    Operation      Union | Subtract | Intersect
    Round Edges    (box, cylinder)
    Custom Blend   per-shape blend override

  <fusion>
    Blend          slider
    Blend Type     Smooth / Round / Chamfer / Steps / None
    Quality        Low / Medium / High / Custom     (preview resolution)
    Adaptivity     merge flat areas
    Live Update    pause the live mesh on heavy scenes
    Blend Colors   per-shape colours that cross-fade over the blend
    Material       the fusion's material

  Final Resolution
  [ Convert to Mesh ]
```

1. Click **Box**.  A fusion object ("SDF Fusion") is created at the 3D cursor
   together with a wireframe **SDF Box** source shape, and the fused mesh
   appears immediately.
2. Move the 3D cursor (or just move the new shape afterwards), click
   **Sphere**, and drag the sphere so it overlaps the box.
3. Drag **Blend** up: the two shapes melt into each other.  Change **Blend
   Type** for a round fillet, a chamfer or stair steps.
4. Select a shape and set **Operation** to *Subtract* to cut it out of the
   result, or *Intersect* to keep only the overlap.  Shapes are combined in
   list order (see the *Shapes* sub-panel to reorder them).
5. Scale a shape with **S** to change its size: a box's scale is its half
   extents, a sphere's scale is its radius, a cylinder's XY scale is its
   radius and its Z scale its half height.  Rotation and location work as
   for any object.  The result follows live.
6. Set **Quality** low while modelling large scenes, then set **Final
   Resolution** and click **Convert to Mesh**.  A new, ordinary mesh object
   is created at the final resolution; the fusion setup is hidden but kept
   so you can keep editing and convert again.

### Colours

The fused result is one mesh, so it cannot carry one material per source
shape.  Instead each shape has a **Color** (in the shape box; the material
icon next to it copies the colour of the shape's own material).  Turn on
**Blend Colors** in the fusion box: the colours are mixed with the same
factor as the distances, so they cross-fade over the blend width exactly
where the surfaces melt, a hard union switches sharply at the seam, and a
subtraction paints the cut with the cutter's colour.  The result is stored on
the mesh as a `Color` attribute; the add-on assigns a material
("SDF Fusion Colors") that reads it, and switches Solid-mode viewports to
*Color: Attribute* so it shows immediately.  To use your own material, add a
*Color Attribute* node (name `Color`) to it, or click **Use Color Material**.
Convert to Mesh keeps the attribute and the material.

Tips

* The fusion object is a normal mesh object with a Geometry Nodes modifier,
  so you can move or parent the whole fusion, add materials, or stack more
  modifiers (Remesh, Smooth, Decimate) on top.
* Per-shape **Custom Blend** lets one shape use, say, a hard subtraction
  while the rest blend smoothly.
* Deleting a source shape with **X** removes it from the fusion automatically.
* The *Shapes* sub-panel has a rebuild button if a tree ever gets out of sync
  (for example after appending objects from another file).

## Testing

The repository ships a headless test-suite that checks every primitive,
operation and blend type against a numpy reference implementation of the
original GLSL, and walks the full acceptance flow:

```bash
/Applications/Blender.app/Contents/MacOS/Blender -b --python tests/run_tests.py
```

See [docs/MANUAL_TESTING.md](docs/MANUAL_TESTING.md) for what still needs
to be checked by hand in the Blender UI.

## License

MIT, see [LICENSE.md](LICENSE.md).  Copyright (c) 2025 TLabAltoh (original
Mesh from SDF), (c) 2026 Martin Sap (Blender 5 port).
