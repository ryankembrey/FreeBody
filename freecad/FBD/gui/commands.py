import os

import FreeCAD as App  # type: ignore
import FreeCADGui as Gui  # type: ignore
from PySide6 import QtWidgets  # type: ignore

from . import document as doc_mod


ICON_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "resources", "icons"
)


def icon_path(name):
    return os.path.join(ICON_DIR, name)


class _Command:
    icon = "fbd_diagram.svg"
    text = ""
    tip = ""
    shortcut = ""

    def GetResources(self):
        res = {"Pixmap": icon_path(self.icon), "MenuText": self.text, "ToolTip": self.tip}
        if self.shortcut:
            res["Accel"] = self.shortcut
        return res

    def IsActive(self):
        return App.ActiveDocument is not None


def active_editor():
    from .editor_host import active_editor as _active

    return _active()


class _ToolCommand(_Command):
    """Selects a drawing tool in the open diagram.

    Tools live on the workbench toolbar rather than inside the view, so there
    is one toolbar instead of two and the shortcuts behave like every other
    FreeCAD command.
    """

    tool_name = ""

    def IsActive(self):
        return active_editor() is not None

    def GetResources(self):
        res = super().GetResources()
        res["Checkable"] = True
        return res

    def Activated(self, checked=False):
        # Checkable commands are invoked with the button's toggle state as
        # an argument (like QAction.toggled), unlike plain commands, so this
        # must accept it even though the tool logic doesn't need the value.
        del checked
        editor = active_editor()
        if editor is not None:
            editor.set_tool_by_name(self.tool_name)


class ToolSelect(_ToolCommand):
    icon = "tool_select.svg"
    text = "Select"
    tip = "Select and move. Drag a joint to reposition it, Delete to remove."
    shortcut = "S"
    tool_name = "Select"


class ToolNode(_ToolCommand):
    icon = "tool_node.svg"
    text = "Joint"
    tip = "Place a joint. Joints are where members, supports and loads meet."
    shortcut = "N"
    tool_name = "Node"


class ToolMember(_ToolCommand):
    icon = "tool_member.svg"
    text = "Member"
    tip = (
        "Draw a member between two joints. Keeps chaining from the last joint; Escape ends the run."
    )
    shortcut = "M"
    tool_name = "Member"


class ToolAnchor(_ToolCommand):
    icon = "tool_anchor.svg"
    text = "Point"
    tip = (
        "Add a point along a member, where a load can attach anywhere "
        "along its length, not just at the ends."
    )
    shortcut = "K"
    tool_name = "Point"


class ToolPin(_ToolCommand):
    icon = "tool_pin.svg"
    text = "Pin"
    tip = "Pin support: holds horizontally and vertically, free to rotate."
    shortcut = "P"
    tool_name = "Pin"


class ToolRoller(_ToolCommand):
    icon = "tool_roller.svg"
    text = "Roller"
    tip = "Roller support: one reaction only, free to slide along the surface."
    shortcut = "R"
    tool_name = "Roller (rolls horizontally)"


class ToolFixed(_ToolCommand):
    icon = "tool_fixed.svg"
    text = "Fixed"
    tip = "Fixed support: holds translation and rotation, so it carries moment."
    shortcut = "F"
    tool_name = "Fixed"


class ToolSpring(_ToolCommand):
    icon = "tool_spring.svg"
    text = "Spring"
    tip = "Spring support: elastic restraint, set its stiffness in the panel."
    tool_name = "Spring"


class ToolForce(_ToolCommand):
    icon = "tool_force.svg"
    text = "Force"
    tip = "Apply a point force at a joint, downward by default."
    shortcut = "L"
    tool_name = "Force"


class ToolMoment(_ToolCommand):
    icon = "tool_moment.svg"
    text = "Moment"
    tip = "Apply a couple at a joint, counter-clockwise positive."
    shortcut = "T"
    tool_name = "Moment"


class ToolLineLoad(_ToolCommand):
    icon = "tool_lineload.svg"
    text = "Line load"
    tip = "Apply a uniform distributed load along a member."
    shortcut = "Q"
    tool_name = "Line load"


class ToolLever(_ToolCommand):
    icon = "tool_lever.svg"
    text = "Lever"
    shortcut = "V"
    tip = "Place a first class lever: a bar on a pivot in its centre."
    tool_name = "Lever"


class ToolMotor(_ToolCommand):
    icon = "tool_motor.svg"
    text = "Motor"
    shortcut = "O"
    tip = "Drive a link about its pinned joint."
    tool_name = "Motor"


class ToolActuator(_ToolCommand):
    icon = "tool_actuator.svg"
    text = "Actuator"
    shortcut = "A"
    tip = "Turn a member into a linear actuator with a stroke and a speed."
    tool_name = "Actuator"


class SyncSketch(_Command):
    icon = "fbd_import_sketch.svg"
    text = "Sync Sketch"
    tip = "Re-read the linked sketch, keeping supports and loads."

    def IsActive(self):
        editor = active_editor()
        return editor is not None and editor.model.sketch_link is not None

    def Activated(self):
        editor = active_editor()
        from . import sketch_import

        sketch = sketch_import.find_linked_sketch(editor.model, editor.host.obj.Document)
        if sketch is None:
            App.Console.PrintWarning("FBD: the linked sketch is gone.\n")
            return
        editor.push_undo("Sync sketch")
        report = sketch_import.resync(editor.model, sketch)
        editor.model_changed()
        editor.set_prompt(report.summary())


class RunMotion(_Command):
    icon = "fbd_motion.svg"
    text = "Run Motion"
    shortcut = "Space"
    tip = "Run the mechanism and watch it move."

    def IsActive(self):
        editor = active_editor()
        return editor is not None and editor.model.has_drivers()

    def Activated(self):
        editor = active_editor()
        if editor is not None:
            editor.run_motion()


class FitView(_Command):
    icon = "tool_fit.svg"
    text = "Fit"
    tip = "Zoom to fit the whole sheet."
    shortcut = "Ctrl+0"

    def IsActive(self):
        return active_editor() is not None

    def Activated(self):
        editor = active_editor()
        if editor is not None:
            editor.fit()


class ToggleSnap(_Command):
    icon = "tool_snap.svg"
    text = "Snap"
    tip = "Snap to the grid and to existing joints."

    def IsActive(self):
        return active_editor() is not None

    def Activated(self):
        editor = active_editor()
        if editor is not None:
            editor.snap_enabled = not editor.snap_enabled


class NewDiagram(_Command):
    icon = "fbd_new.svg"
    text = "New Diagram"
    tip = (
        "Create a free body diagram and open the drawing canvas.\n"
        "Draw members, add supports and loads, then solve."
    )

    def IsActive(self):
        return True

    def Activated(self):
        if App.ActiveDocument is None:
            App.newDocument("FBD")
        obj = doc_mod.create(App.ActiveDocument)
        from .editor_host import open_editor

        open_editor(obj)


class OpenDiagram(_Command):
    icon = "fbd_open.svg"
    text = "Open Diagram"
    tip = "Open the drawing canvas for the selected diagram."

    def IsActive(self):
        return bool(doc_mod.find_diagrams())

    def Activated(self):
        selected = [o for o in Gui.Selection.getSelection() if doc_mod.is_diagram(o)]
        diagrams = selected or doc_mod.find_diagrams()
        if not diagrams:
            App.Console.PrintWarning("FBD: no diagram in this document.\n")
            return
        from .editor_host import open_editor

        open_editor(diagrams[0])


class ImportSketch(_Command):
    icon = "fbd_import_sketch.svg"
    text = "Import Sketch"
    tip = (
        "Import a Sketcher sketch into the open diagram.\n"
        "Lines become members, points become joints. Coincident "
        "endpoints share one joint. Dimensions are taken as drawn, "
        "with no scaling."
    )
    shortcut = "Ctrl+I"

    def _pick_sketch(self):
        selected = [
            o
            for o in Gui.Selection.getSelection()
            if getattr(o, "TypeId", "") == "Sketcher::SketchObject"
        ]
        if selected:
            return selected[0]

        doc = App.ActiveDocument
        sketches = [
            o
            for o in (doc.Objects if doc else [])
            if getattr(o, "TypeId", "") == "Sketcher::SketchObject"
        ]
        if not sketches:
            App.Console.PrintWarning("FBD: no sketch found. Select a Sketch first.\n")
            return None
        if len(sketches) == 1:
            return sketches[0]

        names = [s.Label for s in sketches]
        choice, accepted = QtWidgets.QInputDialog.getItem(
            Gui.getMainWindow(), "Import Sketch", "Choose a sketch to import:", names, 0, False
        )
        if not accepted:
            return None
        return sketches[names.index(choice)]

    def Activated(self):
        editor = active_editor()
        if editor is None:
            diagrams = doc_mod.find_diagrams()
            from .editor_host import open_editor

            if diagrams:
                editor = open_editor(diagrams[0])
            else:
                if App.ActiveDocument is None:
                    App.Console.PrintWarning("FBD: open or create a document first.\n")
                    return
                editor = open_editor(doc_mod.create(App.ActiveDocument))
        if editor is None:
            return

        sketch = self._pick_sketch()
        if sketch is None:
            return

        from . import sketch_import

        editor.push_undo(f"Import sketch {sketch.Label}")
        try:
            nodes, members = sketch_import.import_sketch(editor.model, sketch)
        except Exception as exc:
            editor._undo.pop()
            App.Console.PrintError(f"FBD: could not import sketch. {exc}\n")
            return
        if editor.host is not None and getattr(editor.host, "obj", None):
            editor.host.obj.Sketch = sketch
        if not nodes and not members:
            editor._undo.pop()
            App.Console.PrintWarning(f"FBD: '{sketch.Label}' has no lines or points to import.\n")
            return

        editor.model_changed()
        editor.fit()
        App.Console.PrintMessage(
            f"FBD: imported {len(members)} member(s) and {len(nodes)} joint(s) "
            f"from '{sketch.Label}'.\n"
        )


class SolveDiagram(_Command):
    icon = "fbd_solve.svg"
    text = "Solve"
    tip = "Solve the open diagram and show the reactions."

    def IsActive(self):
        return active_editor() is not None or bool(doc_mod.find_diagrams())

    def Activated(self):
        editor = active_editor()
        if editor is None:
            diagrams = doc_mod.find_diagrams()
            if not diagrams:
                return
            from .editor_host import open_editor

            editor = open_editor(diagrams[0])
        editor.solve()


class ExportPDF(_Command):
    icon = "fbd_new.svg"
    text = "Export PDF"
    tip = "Export diagram sheet to a PDF file."

    def IsActive(self):
        return active_editor() is not None

    def Activated(self):
        editor = active_editor()
        if editor is not None:
            editor.export_pdf_prompt()


COMMANDS = {
    "FBD_NewDiagram": NewDiagram,
    "FBD_OpenDiagram": OpenDiagram,
    "FBD_ImportSketch": ImportSketch,
    "FBD_ToolSelect": ToolSelect,
    "FBD_ToolNode": ToolNode,
    "FBD_ToolMember": ToolMember,
    "FBD_ToolAnchor": ToolAnchor,
    "FBD_ToolPin": ToolPin,
    "FBD_ToolRoller": ToolRoller,
    "FBD_ToolFixed": ToolFixed,
    "FBD_ToolSpring": ToolSpring,
    "FBD_ToolForce": ToolForce,
    "FBD_ToolMoment": ToolMoment,
    "FBD_ToolLineLoad": ToolLineLoad,
    "FBD_ToolLever": ToolLever,
    "FBD_ToolMotor": ToolMotor,
    "FBD_ToolActuator": ToolActuator,
    "FBD_SyncSketch": SyncSketch,
    "FBD_RunMotion": RunMotion,
    "FBD_ToggleSnap": ToggleSnap,
    "FBD_FitView": FitView,
    "FBD_Solve": SolveDiagram,
    "FBD_ExportPDF": ExportPDF,
}


TOOL_ICONS = {
    cls.tool_name: cls.icon
    for cls in COMMANDS.values()
    if issubclass(cls, _ToolCommand) and getattr(cls, "tool_name", "")
}


def sync_tool_actions(active_name):
    """Check the toolbar button for the active drawing tool and uncheck
    every other one, the way Sketcher highlights whichever tool is running."""
    if not App.GuiUp:
        return
    for name, cls in COMMANDS.items():
        if not issubclass(cls, _ToolCommand):
            continue
        try:
            action = Gui.Command.get(name).getAction()
        except Exception:
            continue
        actions = action if isinstance(action, (list, tuple)) else [action]
        for act in actions:
            if act is None:
                continue
            try:
                act.blockSignals(True)
                act.setChecked(cls.tool_name == active_name)
            finally:
                act.blockSignals(False)


def register():
    if not App.GuiUp:
        return
    for name, cls in COMMANDS.items():
        Gui.addCommand(name, cls())
