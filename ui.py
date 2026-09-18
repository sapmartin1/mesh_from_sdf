# SPDX-License-Identifier: MIT
"""Sidebar UI: a single, modelling-tool style panel."""

import bpy
from bpy.types import Panel, UIList

from . import ops


class SDFF_UL_shapes(UIList):
    bl_idname = "SDFF_UL_shapes"

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        ob = item.object
        if ob is None:
            layout.label(text="<missing>", icon='ERROR')
            return
        st = ob.sdf_shape
        row = layout.row(align=True)
        icon_name = {'BOX': 'MESH_CUBE', 'SPHERE': 'MESH_UVSPHERE', 'CYLINDER': 'MESH_CYLINDER',
                     'TORUS': 'MESH_TORUS', 'CONE': 'MESH_CONE'}.get(st.primitive, 'MESH_DATA')
        row.prop(st, 'include', text="")
        row.label(text=ob.name, icon=icon_name)
        row.prop(st, 'operation', text="", emboss=False, icon_only=True)


class SDFF_PT_main(Panel):
    bl_label = "SDF Fusion"
    bl_idname = "SDFF_PT_main"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "SDF Fusion"

    def draw(self, context):
        layout = self.layout
        fusion = ops.find_fusion(context)
        shape = ops.active_shape(context)

        col = layout.column(align=True)
        row = col.row(align=True)
        row.scale_y = 1.4
        row.operator('sdf_fusion.add_shape', text="Box", icon='MESH_CUBE').primitive = 'BOX'
        row.operator('sdf_fusion.add_shape', text="Sphere", icon='MESH_UVSPHERE').primitive = 'SPHERE'
        row.operator('sdf_fusion.add_shape', text="Cylinder", icon='MESH_CYLINDER').primitive = 'CYLINDER'
        row = col.row(align=True)
        row.operator('sdf_fusion.add_shape', text="Torus", icon='MESH_TORUS').primitive = 'TORUS'
        row.operator('sdf_fusion.add_shape', text="Cone", icon='MESH_CONE').primitive = 'CONE'
        row.operator('sdf_fusion.new_fusion', text="", icon='ADD')

        if fusion is None:
            box = layout.box()
            box.label(text="Add a shape to start fusing", icon='INFO')
            return

        if shape is not None:
            st = shape.sdf_shape
            box = layout.box()
            box.label(text=shape.name, icon='OBJECT_DATA')
            box.prop(st, 'primitive', text="")
            row = box.row(align=True)
            row.prop(st, 'operation', expand=True)
            if st.primitive in {'BOX', 'CYLINDER'}:
                box.prop(st, 'rounding', slider=True)
            elif st.primitive == 'TORUS':
                box.prop(st, 'tube', slider=True)
            elif st.primitive == 'CONE':
                box.prop(st, 'top_radius', slider=True)
            row = box.row()
            row.prop(st, 'use_custom_blend')
            if st.use_custom_blend:
                sub = box.column(align=True)
                sub.prop(st, 'blend', slider=True)
                sub.prop(st, 'blend_type', text="")
                if st.blend_type == 'STEPS':
                    sub.prop(st, 'steps')

        fs = fusion.sdf_fusion
        box = layout.box()
        row = box.row()
        row.label(text=fusion.name, icon='MOD_SMOOTH')
        row.operator('sdf_fusion.select_fusion', text="", icon='RESTRICT_SELECT_OFF')
        col = box.column(align=True)
        col.prop(fs, 'blend', slider=True)
        col.prop(fs, 'blend_type', text="")
        if fs.blend_type == 'STEPS':
            col.prop(fs, 'steps')
        col = box.column(align=True)
        col.prop(fs, 'quality', text="Quality")
        if fs.quality == 'CUSTOM':
            col.prop(fs, 'resolution')
        col.prop(fs, 'adaptivity', slider=True)
        col.prop(fs, 'live', toggle=True, icon='PLAY' if fs.live else 'PAUSE')
        box.template_ID(fusion, 'active_material', new='material.new')

        box = layout.box()
        box.prop(fs, 'final_resolution')
        row = box.row()
        row.scale_y = 1.4
        row.operator('sdf_fusion.convert', icon='MESH_DATA')


class SDFF_PT_shapes(Panel):
    bl_label = "Shapes"
    bl_idname = "SDFF_PT_shapes"
    bl_parent_id = "SDFF_PT_main"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "SDF Fusion"
    bl_options = {'DEFAULT_CLOSED'}

    @classmethod
    def poll(cls, context):
        return ops.find_fusion(context) is not None

    def draw(self, context):
        layout = self.layout
        fusion = ops.find_fusion(context)
        fs = fusion.sdf_fusion
        row = layout.row()
        row.template_list('SDFF_UL_shapes', '', fs, 'shapes', fs, 'active_shape_index', rows=4)
        col = row.column(align=True)
        col.operator('sdf_fusion.move_shape', text="", icon='TRIA_UP').direction = 'UP'
        col.operator('sdf_fusion.move_shape', text="", icon='TRIA_DOWN').direction = 'DOWN'
        col.separator()
        col.operator('sdf_fusion.remove_shape', text="", icon='X')
        col.separator()
        col.operator('sdf_fusion.rebuild', text="", icon='FILE_REFRESH')
        layout.label(text="Shapes are combined top to bottom", icon='INFO')


classes = (SDFF_UL_shapes, SDFF_PT_main, SDFF_PT_shapes)


def register():
    for c in classes:
        bpy.utils.register_class(c)


def unregister():
    for c in reversed(classes):
        bpy.utils.unregister_class(c)
