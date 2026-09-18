# Manual testing checklist (Blender 5.2 LTS, Apple Silicon)

Everything below was exercised headlessly on Blender 5.1.0 on an M5 MacBook,
but the interactive UI can only be judged by hand.  Please run through this
on your M5 Mac with Blender 5.2 and note anything that misbehaves.

## Install

- [ ] Drag `sdf_fusion-1.0.0.zip` into Blender 5.2.  It installs and enables
      without errors (check the Info editor / system console).
- [ ] The **SDF Fusion** tab appears in the 3D Viewport sidebar (N).

## Phase 1 acceptance test

- [ ] Click **Box**.  An orange-free, smooth grey mesh cube appears with a
      wireframe "SDF Box" inside it.
- [ ] Move the 3D cursor (Shift+RMB) about 1.3 units to the side, click
      **Sphere**.  The fused mesh now shows both shapes.
- [ ] With the sphere selected, press G and drag it partially into the cube:
      the fused mesh follows *while dragging* (not only after release).
- [ ] Set **Blend Type** to *Smooth* (default) and drag **Blend** from 0 to
      about 0.6: the shapes melt into each other, the neck between them
      fills in smoothly.  The viewport stays interactive on Medium quality.
- [ ] Scale (S) and rotate (R) either shape: the result updates live.
- [ ] Select the sphere, set **Operation** to *Subtract* and *Intersect*.
- [ ] Try *Round*, *Chamfer* and *Steps* blend types.
- [ ] Click **Convert to Mesh**.  A new "SDF Fusion Mesh" object is
      selected, has no modifiers, enters Edit Mode normally, and the fusion
      setup is hidden (unhide with Alt+H to keep editing).

## Colours

- [ ] Select the box, pick a red **Color**; select the sphere, pick blue.
- [ ] Turn on **Blend Colors** in the fusion box.  The viewport switches to
      attribute colours; red and blue cross-fade where the shapes melt.
- [ ] Press Z > Material Preview: the blend also shows through the
      "SDF Fusion Colors" material.  Drag Blend: the colour transition widens.
- [ ] Set the sphere to *Subtract*: the carved surface is blue.
- [ ] Convert to Mesh, then check the mesh still shows the colours in
      Material Preview and in a Cycles render.

## 1.2 additions

- [ ] Add a Capsule, a Pyramid and a Prism (set Sides to 3, 5, 8); the wire
      proxies match the fused shapes.
- [ ] Type values into the **Size** fields of a shape; the result follows.
- [ ] Keyframe **Blend** (hover, press I) at two frames; scrub the timeline:
      the fusion animates.  Same for a shape's Color.
- [ ] Select a shape, scale it, press Ctrl+A > Scale: nothing changes visibly
      and the object scale is back.  Same with Rotation.
- [ ] Click the duplicate icon next to the fusion name: an independent copy
      appears beside it.  Also select a fusion plus its shapes and Shift+D.
- [ ] Give the box and the sphere real materials (Material tab), open the
      **Shape Material** sub-panel, enable **Use Shape Material** on both,
      turn on **Blend Materials**; in Material Preview (Z) metallic,
      roughness and emission blend across the seam.  Change a material's
      roughness: the fusion follows.
- [ ] Convert, then render (F12): only the baked mesh renders, with the
      blended material.

## 1.3 additions

- [ ] Box + sphere overlapping.  Select the **box**, press Subtract: the box
      carves the sphere.  Press Union again, select the **sphere**, press
      Subtract: the sphere carves the box.  Same with Intersect.
- [ ] The cutter shows as a light outline (three circles for a sphere) and the
      carved surface is clearly visible behind it.  *Guides: Wire* brings the
      wireframes back.
- [ ] Select one shape, Shift+D, move: the copy is part of the same fusion
      and appears in the Shapes list.

## UI / workflow

- [ ] Clicking a row in the *Shapes* sub-panel selects that shape.
- [ ] Reordering shapes with the arrows changes which shape is subtracted
      from which.
- [ ] Deleting a shape with X updates the fusion (no stale geometry).
- [ ] Undo (Ctrl+Z) after adding a shape removes it cleanly and the fusion
      still evaluates.
- [ ] Save, reopen the .blend: the fusion still updates live and the panel
      shows the right settings.
- [ ] Quality *High* with 5-6 shapes stays usable; if not, *Live Update*
      off pauses it and Convert still works.
- [ ] Final Resolution 256+ on a large fusion: memory and time acceptable
      (2 shapes at 256 took ~50 ms headless on the M5).

## Rendering

- [ ] The fused mesh renders in EEVEE and Cycles with a material assigned to
      the fusion object; the wireframe source shapes are excluded from renders
      (they have *Disable in Renders* set).
