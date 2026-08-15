# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Ryan Kembrey <ryan.FreeCAD@gmail.com>
# SPDX-FileNotice: Part of the FBD addon.

"""Hosting the editor as an MDI view, the way Spreadsheet and TechDraw do.

The host owns the link back to the document object: the editor calls save() on
every change and the host writes the blob, so nothing is lost if FreeCAD closes
unexpectedly.
"""

from PySide6 import QtWidgets

import FreeCAD as App  # type: ignore
import FreeCADGui as Gui  # type: ignore

from . import document as doc_mod
from .editor import Editor


_OPEN = {}  # object Name -> (subwindow, editor)
_ACTIVE = {"editor": None}


def active_editor():
    """The editor the user is working in, or None.

    Tool commands ask for this, so they light up only while a diagram is open.
    """
    editor = _ACTIVE.get("editor")
    if editor is None:
        return None
    try:
        editor.isVisible()  # raises if the C++ side has gone
    except RuntimeError:
        _ACTIVE["editor"] = None
        return None
    return editor


def set_active(editor):
    """Track which diagram the toolbar commands act on.

    There is no task dialog to open: everything the panel used to hold
    (properties, results, display options) now lives on the page itself, so
    opening a diagram never disables the rest of the workbench toolbar the way
    a modal task dialog does.
    """
    _ACTIVE["editor"] = editor


def set_editor_visible(name, visible):
    """Show or hide the diagram's editor page without closing it.

    Unlike close_editor (used when the diagram itself is deleted), the
    window and everything in it -- the current tool, the selection, the
    undo stack -- survives a hide the same way any other Visibility
    toggle is reversible: toggling it back on restores the same editor,
    not a fresh one.
    """
    entry = _OPEN.get(name)
    if not entry:
        return
    sub_window, _editor = entry
    try:
        sub_window.setVisible(bool(visible))
    except RuntimeError:
        pass


def close_editor(name):
    """Close the editor for a diagram object, if one is open.

    Used when the diagram itself is deleted, so its page doesn't linger
    open on a document object that no longer exists.
    """
    entry = _OPEN.get(name)
    if not entry:
        return
    sub_window, _editor = entry
    try:
        sub_window.close()
    except RuntimeError:
        pass
    _forget(name)


def _forget(name):
    _OPEN.pop(name, None)
    if not _OPEN:
        _ACTIVE["editor"] = None


class _Host:
    def __init__(self, obj):
        self.obj = obj

    def save(self, model):
        try:
            doc_mod.store_model(self.obj, model)
        except Exception as exc:
            App.Console.PrintWarning(f"FBD: could not store the diagram. {exc}\n")


def open_editor(obj):
    """Open (or raise) the editor for a diagram object."""
    name = obj.Name
    existing = _OPEN.get(name)
    if existing is not None:
        sub, editor = existing
        try:
            sub.show()
            sub.raise_()
            mdi = _mdi_area()
            if mdi is not None:
                mdi.setActiveSubWindow(sub)
            set_active(editor)
            return editor
        except Exception:
            _OPEN.pop(name, None)

    model = doc_mod.load_model(obj)
    editor = Editor(model, host=_Host(obj))
    editor.setWindowTitle(f"FBD: {obj.Label}")

    mdi = _mdi_area()
    if mdi is None:
        editor.show()
        _OPEN[name] = (editor, editor)
        set_active(editor)
        return editor

    sub = mdi.addSubWindow(editor)
    sub.setWindowTitle(editor.windowTitle())
    sub.setAttribute(_delete_on_close(), True)
    sub.destroyed.connect(lambda *_a, n=name: _forget(n))
    sub.show()
    try:
        sub.showMaximized()
    except Exception:
        pass
    mdi.setActiveSubWindow(sub)
    _OPEN[name] = (sub, editor)
    set_active(editor)
    return editor


def refresh_editor(obj):
    entry = _OPEN.get(obj.Name)
    if not entry:
        return
    _sub, editor = entry
    try:
        fresh = doc_mod.load_model(obj)
        if fresh.to_dict() == editor.model.to_dict():
            # Already caught up: this call is the editor's own save
            # reaching back around through onChanged, not something
            # external like undo. A rebuild here would be a no-op except
            # for silently dropping the current selection, so skip it
            # rather than pay that cost on every single edit.
            return
        editor.model.__dict__.update(fresh.__dict__)
        editor.invalidate_result()
        editor.rebuild()
    except RuntimeError:
        _forget(obj.Name)


def _mdi_area():
    try:
        return Gui.getMainWindow().findChild(QtWidgets.QMdiArea)
    except Exception:
        return None


def _delete_on_close():
    from PySide6 import QtCore

    return QtCore.Qt.WidgetAttribute.WA_DeleteOnClose
