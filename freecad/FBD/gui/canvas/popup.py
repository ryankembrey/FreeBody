# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Ryan Kembrey <ryan.FreeCAD@gmail.com>
# SPDX-FileNotice: Part of the FBD addon.

"""On-page editing widgets: a small form that appears next to whatever you
double-click, and a quick menu for one-click choices like a support type.

Neither is a FreeCAD task dialog. They are ordinary widgets embedded in the
scene via QGraphicsProxyWidget, so opening one never disables the workbench
toolbar the way Gui.Control.showDialog does, and exactly one is ever open at a
time, dismissed by clicking elsewhere or pressing Escape.
"""

from PySide6 import QtCore, QtGui, QtWidgets


class PopupForm(QtWidgets.QFrame):
    """A small bordered form styled to sit on the page rather than look like
    an application dialog.

    Every row shares the same label and field column widths, FreeCAD
    property-editor style, so values line up between rows and between
    different entities' popups instead of each control sizing itself to
    its own content.
    """

    LABEL_W = 84   # px: shared label-cell width, every popup lines up
    FIELD_W = 150  # px: shared value-field width, room for "630000000 N"

    def __init__(self):
        super().__init__()
        self.setFrameShape(QtWidgets.QFrame.Shape.StyledPanel)
        self.setAutoFillBackground(True)
        self.setFixedWidth(self.LABEL_W + self.FIELD_W + 16)
        self.setStyleSheet(
            "QFrame { background: #fdfdfc; border: 1px solid #a9afba; }"
            "QLabel#RowLabel {"
            "    color: #37474f; font-size: 12px; background: #eef0f3;"
            "    border-right: 1px solid #d7dbe1; padding: 3px 6px; }"
            "QLabel#Note { color: #8a94a6; font-size: 11px; padding: 5px 8px 3px 8px; }"
            "QDoubleSpinBox, QComboBox, QLineEdit {"
            "    font-size: 12px; min-height: 20px; padding: 1px 4px; }"
            "QLineEdit:read-only { background: #f1f2f4; color: #5b6270; }"
        )
        self._form = QtWidgets.QFormLayout(self)
        self._form.setContentsMargins(0, 6, 6, 6)
        self._form.setHorizontalSpacing(0)
        self._form.setVerticalSpacing(2)
        self._form.setLabelAlignment(
            QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter
        )
        self._form.setFieldGrowthPolicy(
            QtWidgets.QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow
        )
        self._form.setFormAlignment(
            QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignTop
        )

    def _row_label(self, text):
        lbl = QtWidgets.QLabel(text)
        lbl.setObjectName("RowLabel")
        lbl.setFixedWidth(self.LABEL_W)
        return lbl

    def _stretch(self, widget):
        widget.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Fixed
        )
        return widget

    def add_spin(self, label, value, setter, lo=-1e12, hi=1e12, decimals=3,
                suffix="", tooltip=""):
        box = QtWidgets.QDoubleSpinBox()
        box.setRange(lo, hi)
        box.setDecimals(decimals)
        box.setValue(float(value))
        box.setKeyboardTracking(False)
        self._stretch(box)
        if suffix:
            box.setSuffix(" " + suffix)
        if tooltip:
            box.setToolTip(tooltip)
        box.valueChanged.connect(setter)
        self._form.addRow(self._row_label(label), box)
        return box

    def add_combo(self, label, options, current, setter, tooltip=""):
        combo = QtWidgets.QComboBox()
        self._stretch(combo)
        for key, text in options:
            combo.addItem(text, key)
        keys = [k for k, _ in options]
        combo.setCurrentIndex(keys.index(current) if current in keys else 0)
        if tooltip:
            combo.setToolTip(tooltip)
        combo.currentIndexChanged.connect(lambda i: setter(combo.itemData(i)))
        self._form.addRow(self._row_label(label), combo)
        return combo

    def add_text(self, label, value, setter):
        edit = QtWidgets.QLineEdit(value)
        self._stretch(edit)
        edit.editingFinished.connect(lambda: setter(edit.text()))
        self._form.addRow(self._row_label(label), edit)
        return edit

    def add_readonly(self, label, text):
        edit = QtWidgets.QLineEdit(str(text))
        edit.setReadOnly(True)
        edit.setCursorPosition(0)
        self._stretch(edit)
        self._form.addRow(self._row_label(label), edit)
        return edit

    def add_note(self, text):
        note = QtWidgets.QLabel(text)
        note.setObjectName("Note")
        note.setWordWrap(True)
        note.setMaximumWidth(self.LABEL_W + self.FIELD_W)
        self._form.addRow(note)
        return note

def quick_menu(view: QtWidgets.QGraphicsView, global_pos, options, on_pick):
    """A small QMenu at a global screen position: for a fast one-click choice,
    such as switching a support's type, without opening a form."""
    menu = QtWidgets.QMenu(view)
    for key, text in options:
        action = menu.addAction(text)
        action.triggered.connect(lambda _c=False, k=key: on_pick(k))
    menu.exec(global_pos)
