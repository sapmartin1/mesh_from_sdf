# SPDX-License-Identifier: MIT
"""
GUI smoke test: drives the *installed* SDF Fusion extension inside a real
Blender window and saves screenshots of every step.

    # install first:  blender -b --command extension install-file --repo user_default --enable dist/sdf_fusion-1.2.0.zip
    blender -b --python-expr "import bpy; bpy.ops.wm.save_as_mainfile(filepath='/tmp/sdff.blend')"
    blender /tmp/sdff.blend --python tests/gui_smoke.py -- /tmp/sdff_shots

Opening a .blend file avoids the splash screen.  The panel is temporarily
moved into the sidebar's currently visible tab so it shows in the screenshots
(Python cannot switch sidebar tabs).
"""
import os
import sys
import traceback

import bpy

OUT = sys.argv[sys.argv.index('--') + 1] if '--' in sys.argv else os.path.abspath('gui_shots')
os.makedirs(OUT, exist_ok=True)
MODULE = 'bl_ext.user_default.sdf_fusion'


def say(*a):
    print('[GUI_TEST]', *a, flush=True)


def view3d():
    win = bpy.context.window
    for area in win.screen.areas:
        if area.type == 'VIEW_3D':
            region = next(r for r in area.regions if r.type == 'WINDOW')
            return win, area, region
    return None, None, None


def screenshot(name):
    win, area, region = view3d()
    path = os.path.join(OUT, name)
    with bpy.context.temp_override(window=win, area=area, region=region):
        bpy.ops.screen.screenshot(filepath=path)
    say('screenshot', path, os.path.exists(path))


def frame_view():
    from math import radians
    from mathutils import Euler
    win, area, region = view3d()
    r3d = area.spaces.active.region_3d
    r3d.view_perspective = 'PERSP'
    r3d.view_rotation = Euler((radians(63), 0, radians(38))).to_quaternion()
    r3d.view_location = (0.6, 0.0, 0.4)
    r3d.view_distance = 7.5


def move_panel_into_visible_tab():
    """Re-register the panel in the tab the sidebar currently shows."""
    import importlib
    ui = importlib.import_module(MODULE + '.ui')
    for cls in reversed(ui.classes):
        bpy.utils.unregister_class(cls)
    for cls in ui.classes:
        if getattr(cls, 'bl_category', None) == 'SDF Fusion':
            cls.bl_category = 'Tool'
        bpy.utils.register_class(cls)


state = {'i': 0}


def steps():
    i = state['i']
    state['i'] += 1
    try:
        if i == 0:
            if MODULE not in bpy.context.preferences.addons:
                bpy.ops.preferences.addon_enable(module=MODULE)
            say('addon enabled:', MODULE in bpy.context.preferences.addons)
            say('panel registered:', hasattr(bpy.types, 'SDFF_PT_main'))
            for ob in list(bpy.data.objects):
                bpy.data.objects.remove(ob, do_unlink=True)
            win, area, region = view3d()
            area.spaces.active.show_region_ui = True
            area.spaces.active.shading.type = 'SOLID'
            move_panel_into_visible_tab()
            return 0.5
        if i == 1:
            win, area, region = view3d()
            with bpy.context.temp_override(window=win, area=area, region=region):
                bpy.context.scene.cursor.location = (0, 0, 0)
                bpy.ops.sdf_fusion.add_shape(primitive='BOX')
                bpy.context.scene.cursor.location = (1.3, 0.0, 0.7)
                bpy.ops.sdf_fusion.add_shape(primitive='SPHERE')
                bpy.context.active_object.scale = (0.8, 0.8, 0.8)
            frame_view()
            say('objects:', sorted(o.name for o in bpy.data.objects))
            return 0.8
        if i == 2:
            screenshot('gui_01_box_sphere_blend_0.25.png')
            fusion = bpy.context.active_object.sdf_shape.fusion
            fusion.sdf_fusion.blend = 0.7
            return 0.8
        if i == 3:
            screenshot('gui_02_smooth_blend_0.7.png')
            fusion = bpy.context.active_object.sdf_shape.fusion
            fusion.sdf_fusion.blend_type = 'ROUND'
            fusion.sdf_fusion.blend = 0.4
            return 0.8
        if i == 4:
            screenshot('gui_03_round_blend.png')
            fusion = bpy.context.active_object.sdf_shape.fusion
            fusion.sdf_fusion.blend_type = 'SMOOTH'
            fusion.sdf_fusion.blend = 0.5
            shapes = [r.object for r in fusion.sdf_fusion.shapes]
            shapes[0].sdf_shape.color = (0.95, 0.30, 0.20, 1.0)
            shapes[1].sdf_shape.color = (0.20, 0.45, 0.95, 1.0)
            win, area, region = view3d()
            with bpy.context.temp_override(window=win, area=area, region=region, screen=win.screen):
                fusion.sdf_fusion.blend_colors = True
            say('blend colors on; viewport color type:', area.spaces.active.shading.color_type,
                'material:', fusion.active_material.name if fusion.active_material else None)
            return 0.8
        if i == 5:
            screenshot('gui_04_blend_colors.png')
            bpy.context.active_object.sdf_shape.operation = 'SUBTRACT'
            return 0.8
        if i == 6:
            screenshot('gui_05_smooth_subtract_colors.png')
            bpy.context.active_object.sdf_shape.operation = 'UNION'
            win, area, region = view3d()
            with bpy.context.temp_override(window=win, area=area, region=region):
                bpy.ops.sdf_fusion.convert()
            ob = bpy.context.active_object
            say('after convert active:', ob.name, 'modifiers:', len(ob.modifiers), 'faces:', len(ob.data.polygons),
                'color attr:', 'Color' in ob.data.color_attributes)
            return 0.8
        if i == 7:
            screenshot('gui_06_converted_mesh.png')
            say('DONE')
            bpy.ops.wm.quit_blender()
            return None
    except Exception:
        say('EXCEPTION\n' + traceback.format_exc())
        bpy.ops.wm.quit_blender()
        return None


bpy.app.timers.register(steps, first_interval=1.5)
