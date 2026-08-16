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


class PopupForm(QtWidgets.QWidget):
    """A form styled to sit in a FreeCAD Task Panel."""

    def __init__(self, title="", icon_path=""):
        super().__init__()
        if title:
            self.setWindowTitle(title)
        if icon_path:
            self.setWindowIcon(QtGui.QIcon(icon_path))

        self._main_layout = QtWidgets.QVBoxLayout(self)
        self._main_layout.setContentsMargins(4, 6, 4, 6)
        self._main_layout.setSpacing(6)

        self._current_layout = self._create_grid_layout()
        self._main_layout.addLayout(self._current_layout)
        self._row_index = 0

    def _create_grid_layout(self):
        layout = QtWidgets.QGridLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setHorizontalSpacing(8)
        layout.setVerticalSpacing(4)
        layout.setColumnStretch(0, 1)
        layout.setColumnStretch(1, 1)
        return layout

    def _stretch(self, widget):
        widget.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Fixed
        )
        return widget

    def _add_row(self, label_text, widget):
        lbl = QtWidgets.QLabel(label_text)
        self._current_layout.addWidget(
            lbl,
            self._row_index,
            0,
            QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter,
        )
        self._current_layout.addWidget(widget, self._row_index, 1)
        self._row_index += 1

    def add_spin(self, label, value, setter, lo=-1e12, hi=1e12, decimals=3, suffix="", tooltip=""):
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
        self._add_row(label, box)
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
        self._add_row(label, combo)
        return combo

    def add_text(self, label, value, setter, tooltip=""):
        edit = QtWidgets.QLineEdit(value)
        self._stretch(edit)
        if tooltip:
            edit.setToolTip(tooltip)
        edit.editingFinished.connect(lambda: setter(edit.text()))
        self._add_row(label, edit)
        return edit

    def add_readonly(self, label, text, tooltip=""):
        edit = QtWidgets.QLineEdit(str(text))
        edit.setReadOnly(True)
        edit.setCursorPosition(0)
        self._stretch(edit)
        if tooltip:
            edit.setToolTip(tooltip)
        self._add_row(label, edit)
        return edit

    def add_section(self, title):
        group_box = QtWidgets.QGroupBox(title)
        group_layout = self._create_grid_layout()
        group_layout.setContentsMargins(4, 8, 4, 4)
        group_box.setLayout(group_layout)
        self._main_layout.addWidget(group_box)

        self._current_layout = group_layout
        self._row_index = 0
        return group_box

    def add_button(self, text, callback, tooltip=""):
        btn = QtWidgets.QPushButton(text)
        if tooltip:
            btn.setToolTip(tooltip)
        btn.clicked.connect(callback)
        self._current_layout.addWidget(btn, self._row_index, 0, 1, 2)
        self._row_index += 1
        return btn


def quick_menu(view: QtWidgets.QGraphicsView, global_pos, options, on_pick):
    """A small QMenu at a global screen position: for a fast one-click choice,
    such as switching a support's type, without opening a form."""
    menu = QtWidgets.QMenu(view)
    for key, text in options:
        action = menu.addAction(text)
        action.triggered.connect(lambda _c=False, k=key: on_pick(k))
    menu.exec(global_pos)
