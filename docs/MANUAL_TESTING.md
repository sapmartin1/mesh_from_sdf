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

## 1.4 additions

- [ ] Select a shape: **Radius** and **Blend** sliders sit right under the
      operation buttons.  Blend 1.0 is a full quarter-pipe; lower values hug
      the inner corner.  Small Radius + full Blend looks like a tight rounded
      bevel; large Radius + low Blend is a long gentle lean.  Around a sphere
      the blend is always a waisted neck, never a cone-like skirt.
- [ ] Four shapes, one delicate: lower only its Radius; its seams tighten,
      all other seams stay as they were, whatever its position in the list.
- [ ] Drag **Radius x** on the fusion from 0 to 2: everything melts
      proportionally; 0 gives hard booleans.
- [ ] Open a scene made with 1.3 or earlier: it looks the same as before and
      the shapes now show Radius / Blend values.

## 1.5 additions (editable meshes)

- [ ] Click **Mesh**, press Tab, Ctrl+R to loop cut, move the loop outward:
      the fused surface follows while you drag.
- [ ] Import or model any closed object, select it, click **Use Selected
      Objects**: it joins the fusion in place, blends with a box, and is still
      editable with Tab.  An open mesh shows a red warning in the shape box.
- [ ] Select a Sphere shape, click **Make Editable**, Tab, pull a vertex:
      the fused sphere deforms.  Set Primitive back to Sphere: exact again.
- [ ] Raise **Mesh Detail** to 2: edges of mesh shapes get crisper; the
      viewport stays interactive.
- [ ] **Release from Fusion** on an imported shape: it becomes a normal,
      renderable object again where it stands.

## 1.10 additions (straight edges)

- [ ] Rotate a box shape 30 degrees, pull one corner in Edit Mode: with
      Shading Exact (default) every face shades perfectly flat and the
      creases are crisp lines, at Low quality too.  Switch Shading to Smooth
      or Auto Smooth to see the old wavy / saw-tooth looks.
- [ ] Convert with Precise Edges on: in Edit Mode the crease vertices lie on
      a perfectly straight line; with it off they zig-zag.

## 1.12 additions

- [ ] Add a Cylinder (primitive), Tab, select the top cap, S 1.8: the fused
      cylinder flares live and the shape box now says "Editable mesh".
      Set Primitive back to Cylinder: exact again.

## 1.13 additions (native placement)

- [ ] With a shape selected, Properties > Object tab shows "SDF Shape";
      with the fusion selected it shows "SDF Fusion", and the Modifier tab
      shows the SDF Fusion panel above the modifier stack.
- [ ] Modifiers sub-panel > Twist / Bend: the fusion becomes active and the
      Properties editor jumps to its Modifier tab with Twist / Bend open.
- [ ] Right-click an object in the viewport: SDF Fusion submenu.

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
