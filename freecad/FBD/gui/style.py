# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Ryan Kembrey <ryan.FreeCAD@gmail.com>
# SPDX-FileNotice: Part of the FBD addon.

"""The drawing's visual language, in one place.

A free body diagram is a document, so this is set up like drafting stationery
rather than a UI theme: paper white, ink near-black, and a small set of
deliberate accent colours that each mean one thing. Nothing is fully saturated,
which keeps it readable on screen and sane when printed.

Sizes are in millimetres of paper, so a symbol is the same physical size on the
sheet regardless of zoom, exactly like a real drawing.
"""

from PySide6 import QtGui, QtCore


# ---- colour ---------------------------------------------------------------

INK = QtGui.QColor("#1d2025")          # members, primary linework
INK_LIGHT = QtGui.QColor("#5b6270")    # secondary linework, dimensions
PAPER = QtGui.QColor("#ffffff")
DESK = QtGui.QColor("#e9ebef")         # around the sheet
SHEET_EDGE = QtGui.QColor("#c3c8d2")
SHEET_SHADOW = QtGui.QColor(0, 0, 0, 28)

GRID = QtGui.QColor("#eef0f4")
GRID_MAJOR = QtGui.QColor("#dfe3ea")

APPLIED = QtGui.QColor("#c62828")      # applied loads
REACTION = QtGui.QColor("#1565c0")     # solved reactions
INTERNAL = QtGui.QColor("#2e7d32")     # internal / axial annotation
SUPPORT = QtGui.QColor("#37474f")      # supports and ground hatching
SPRING_COL = QtGui.QColor("#6a4bab")

SELECT = QtGui.QColor("#0a84ff")
HOVER = QtGui.QColor("#34c759")  # green: kept clearly apart from SELECT's blue
PREVIEW = QtGui.QColor("#8a94a6")
SNAP = QtGui.QColor("#ff8f00")

MOMENT_FILL = QtGui.QColor(21, 101, 192, 46)
SHEAR_FILL = QtGui.QColor(46, 125, 50, 46)
AXIAL_FILL = QtGui.QColor(198, 40, 40, 46)


# ---- geometry, in mm of paper --------------------------------------------

MEMBER_W = 0.9
THIN_W = 0.35
NODE_R = 1.1
SUPPORT_SIZE = 5.0
ARROW_LEN = 16.0            # nominal load arrow length
ARROW_HEAD = 3.4
ARROW_MIN = 6.0
MOMENT_R = 5.5
HATCH_COUNT = 7
LABEL_GAP = 1.6

SNAP_PIXELS = 10.0          # snapping tolerance, in screen pixels
HANDLE_PIXELS = 9.0


# ---- text -----------------------------------------------------------------

def font(size_pt=7.5, bold=False):
    f = QtGui.QFont("DejaVu Sans")
    if not f.exactMatch():
        f = QtGui.QFont()
    f.setPointSizeF(size_pt)
    f.setBold(bold)
    return f


def pen(color, width_mm=MEMBER_W, style=QtCore.Qt.PenStyle.SolidLine,
        cap=QtCore.Qt.PenCapStyle.RoundCap):
    p = QtGui.QPen(color, width_mm)
    p.setStyle(style)
    p.setCapStyle(cap)
    p.setJoinStyle(QtCore.Qt.PenJoinStyle.RoundJoin)
    p.setCosmetic(False)
    return p


def thin_pen(color, style=QtCore.Qt.PenStyle.SolidLine):
    return pen(color, THIN_W, style)
