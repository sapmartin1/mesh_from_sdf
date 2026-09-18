# SPDX-License-Identifier: MIT
"""
SDF Fusion -- smooth SDF modelling for Blender 5.x.

Built on the MIT licensed "Mesh from SDF" add-on by TLabAltoh
(https://github.com/TLabAltoh/mesh_from_sdf).  The GPU (ModernGL / OpenGL 4.3)
evaluation path of the original could not be ported to macOS / Metal, so the
distance field is now compiled into a Geometry Nodes tree; see
docs/PORTING_NOTES.md for the full record.
"""

from . import handlers, ops, props, ui

_modules = (props, ops, ui, handlers)


def register():
    for m in _modules:
        m.register()


def unregister():
    for m in reversed(_modules):
        m.unregister()
