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
                     'TORUS': 'MESH_TORUS', 'CONE': 'MESH_CONE', 'CAPSULE': 'MESH_CAPSULE',
                     'PYRAMID': 'CONE', 'PRISM': 'MESH_ICOSPHERE'}.get(st.primitive, 'MESH_DATA')
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
        row.operator('sdf_fusion.add_shape', text="Capsule", icon='MESH_CAPSULE').primitive = 'CAPSULE'
        row = col.row(align=True)
        row.operator('sdf_fusion.add_shape', text="Pyramid", icon='CONE').primitive = 'PYRAMID'
        row.operator('sdf_fusion.add_shape', text="Prism", icon='MESH_ICOSPHERE').primitive = 'PRISM'
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
            if st.operation == 'SUBTRACT':
                box.label(text="Carved out of the result", icon='SELECT_SUBTRACT')
            elif st.operation == 'INTERSECT':
                box.label(text="Only the part inside this shape is kept", icon='SELECT_INTERSECT')
            col = box.column(align=True)
            col.prop(st, 'radius', slider=True)
            col.prop(st, 'fill', slider=True)
            row = box.row(align=True)
            row.prop(st, 'color', text="Color")
            row.operator('sdf_fusion.color_from_material', text="", icon='MATERIAL')
            size_label = {'SPHERE': "Diameter", 'TORUS': "Outer Size"}.get(st.primitive, "Size")
            col = box.column(align=True)
            col.prop(shape, 'dimensions', text=size_label)
            if st.primitive == 'PRISM':
                box.prop(st, 'sides')
            if st.primitive in {'BOX', 'CYLINDER', 'PRISM'}:
                box.prop(st, 'rounding', slider=True)
            elif st.primitive == 'TORUS':
                box.prop(st, 'tube', slider=True)
            elif st.primitive == 'CONE':
                box.prop(st, 'top_radius', slider=True)

        fs = fusion.sdf_fusion
        included = [r.object for r in fs.shapes if r.object is not None and r.object.sdf_shape.include]
        if included and not any(o.sdf_shape.operation == 'UNION' for o in included):
            warn = layout.box()
            warn.alert = True
            warn.label(text="Nothing to cut: no Union shape", icon='ERROR')
            warn.label(text="Set at least one shape to Union")
        elif included and included[0].sdf_shape.operation != 'UNION':
            warn = layout.box()
            warn.alert = True
            warn.label(text="First shape is a cutter, it acts as the base", icon='ERROR')
            warn.label(text="Enable Cutters Last or reorder the shapes")
        box = layout.box()
        row = box.row()
        row.label(text=fusion.name, icon='MOD_SMOOTH')
        row.operator('sdf_fusion.select_fusion', text="", icon='RESTRICT_SELECT_OFF')
        row.operator('sdf_fusion.duplicate_fusion', text="", icon='DUPLICATE')
        col = box.column(align=True)
        col.prop(fs, 'radius_scale', slider=True)
        col.prop(fs, 'fill_scale', slider=True)
        col.prop(fs, 'mode', text="")
        if fs.mode == 'STEPS':
            col.prop(fs, 'steps')
        col = box.column(align=True)
        col.prop(fs, 'quality', text="Quality")
        if fs.quality == 'CUSTOM':
            col.prop(fs, 'resolution')
        col.prop(fs, 'adaptivity', slider=True)
        col.prop(fs, 'live', toggle=True, icon='PLAY' if fs.live else 'PAUSE')
        row = box.row(align=True)
        row.prop(fs, 'guide_display', expand=True)
        col = box.column(align=True)
        col.prop(fs, 'blend_colors', toggle=True, icon='COLOR')
        col.template_ID(fusion, 'active_material', new='material.new')
        if fs.blend_colors and not ops.material_reads_color_attribute(fusion.active_material):
            col.operator('sdf_fusion.setup_color_material', icon='NODE_MATERIAL')

        box = layout.box()
        box.prop(fs, 'final_resolution')
        row = box.row()
        row.scale_y = 1.4
        row.operator('sdf_fusion.convert', icon='MESH_DATA')
        if ops.setup_is_hidden(fusion):
            box.operator('sdf_fusion.show_setup', icon='HIDE_OFF')


class SDFF_PT_shape_material(Panel):
    bl_label = "Shape Material"
    bl_idname = "SDFF_PT_shape_material"
    bl_parent_id = "SDFF_PT_main"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "SDF Fusion"
    bl_options = {'DEFAULT_CLOSED'}

    @classmethod
    def poll(cls, context):
        return ops.active_shape(context) is not None

    def draw(self, context):
        layout = self.layout
        shape = ops.active_shape(context)
        st = shape.sdf_shape
        fusion = st.fusion
        if fusion is not None and not fusion.sdf_fusion.blend_colors:
            layout.label(text="Turn on Blend Materials on the fusion", icon='INFO')
        layout.prop(st, 'use_material', toggle=True, icon='MATERIAL')
        if st.use_material:
            layout.template_ID(shape, 'active_material', new='material.new')
            layout.label(text="Values follow this material's Principled BSDF", icon='LINKED')
        col = layout.column(align=True)
        col.enabled = not st.use_material
        col.prop(st, 'color', text="Color")
        col.prop(st, 'metallic', slider=True)
        col.prop(st, 'roughness', slider=True)
        col.prop(st, 'transmission', slider=True)
        col.prop(st, 'ior')
        col.prop(st, 'emission_color', text="Emission")
        col.prop(st, 'emission_strength')


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
        layout.prop(fs, 'seam_rule')
        layout.prop(fs, 'auto_order')
        if fs.auto_order:
            layout.label(text="Unions first, then Subtract, then Intersect", icon='INFO')
        else:
            layout.label(text="Shapes are combined strictly top to bottom", icon='INFO')


classes = (SDFF_UL_shapes, SDFF_PT_main, SDFF_PT_shape_material, SDFF_PT_shapes)


def register():
    for c in classes:
        bpy.utils.register_class(c)


def unregister():
    for c in reversed(classes):
        bpy.utils.unregister_class(c)
