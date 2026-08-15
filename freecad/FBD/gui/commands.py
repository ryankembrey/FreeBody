import os

import FreeCAD as App  # type: ignore
import FreeCADGui as Gui  # type: ignore
from PySide6 import QtCore, QtGui, QtWidgets  # type: ignore

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
        res: dict = {"Pixmap": icon_path(self.icon), "MenuText": self.text, "ToolTip": self.tip}
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


class ToolPivot(_ToolCommand):
    icon = "tool_pivot.svg"
    text = "Pivot"
    shortcut = "V"
    tip = "Pivot a point on an existing member, turning it into a lever."
    tool_name = "Pivot"


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


class _DiagramToggleCommand(_Command):
    """A toolbar toggle for one internal-force diagram.

    Checked exactly when that diagram is currently shown on the open
    diagram's page, kept in sync via sync_diagram_actions() the same way
    the drawing tools are kept in sync via sync_tool_actions().
    """

    kind = ""

    def IsActive(self):
        editor = active_editor()
        return editor is not None and bool(getattr(editor, "display_result", None))

    def GetResources(self):
        res = super().GetResources()
        res["Checkable"] = True
        return res

    def Activated(self, checked=False):
        del checked
        editor = active_editor()
        if editor is None:
            return
        toggle = getattr(editor, f"toggle_{self.kind}", None)
        if callable(toggle):
            toggle()
        sync_diagram_actions()


class DiagramAxial(_DiagramToggleCommand):
    icon = "tool_diagram_axial.svg"
    text = "Axial"
    tip = "Show or hide the axial force diagram."
    kind = "axial"


class DiagramShear(_DiagramToggleCommand):
    icon = "tool_diagram_shear.svg"
    text = "Shear"
    tip = "Show or hide the shear force diagram."
    kind = "shear"


class DiagramMoment(_DiagramToggleCommand):
    icon = "tool_diagram_moment.svg"
    text = "Moment"
    tip = "Show or hide the bending moment diagram."
    kind = "moment"


class DiagramDeflection(_DiagramToggleCommand):
    icon = "tool_diagram_deflection.svg"
    text = "Deflect"
    tip = "Show or hide the deflected shape."
    kind = "deflection"


class _HUDToggleCommand(_Command):
    """A toolbar toggle mirroring one of the editor's plain on/off
    display settings -- Labels, Components, Reactions, and so on -- kept
    in sync via sync_hud_actions() the same way the drawing tools are
    kept in sync via sync_tool_actions().
    """

    attr = ""     # editor attribute read for the checked state
    default = False
    toggle = ""   # editor method name this button calls

    def IsActive(self):
        return active_editor() is not None

    def GetResources(self):
        res = super().GetResources()
        res["Checkable"] = True
        return res

    def Activated(self, checked=False):
        del checked
        editor = active_editor()
        if editor is None:
            return
        method = getattr(editor, self.toggle, None)
        if callable(method):
            method()
        sync_hud_actions()


class ToggleLabels(_HUDToggleCommand):
    icon = "tool_hud_labels.svg"
    text = "Labels"
    tip = "Show or hide joint labels."
    attr = "show_labels"
    toggle = "toggle_labels"


class ToggleComponents(_HUDToggleCommand):
    icon = "tool_hud_components.svg"
    text = "Components"
    tip = "Show forces as separate Fx/Fy arrows instead of one angled arrow."
    attr = "show_components"
    toggle = "toggle_components"


class ToggleReactions(_HUDToggleCommand):
    icon = "tool_hud_reactions.svg"
    text = "Reactions"
    tip = "Show or hide solved reactions."
    attr = "show_reactions"
    toggle = "toggle_reactions"


class ToggleSchedule(_HUDToggleCommand):
    icon = "tool_hud_schedule.svg"
    text = "Schedule"
    tip = "Choreograph when each driver runs."
    attr = "show_schedule"
    toggle = "toggle_schedule"


class ToggleGraph(_HUDToggleCommand):
    icon = "tool_hud_graph.svg"
    text = "Graph"
    tip = "Effort against travel, for every driver at once."
    attr = "show_graph"
    toggle = "toggle_graph"


class ToggleResultsTable(_HUDToggleCommand):
    icon = "tool_hud_table.svg"
    text = "Table"
    tip = "Show or hide the results table."
    attr = "show_results_table"
    default = True
    toggle = "toggle_results_table"


class ToggleSheetBoundary(_HUDToggleCommand):
    icon = "tool_hud_sheet.svg"
    text = "Sheet"
    tip = "Show or hide the paper sheet boundary."
    attr = "show_sheet"
    default = True
    toggle = "toggle_sheet"


class SyncSketch(_Command):
    icon = "fbd_sync_sketch.svg"
    text = "Sync Sketch"
    tip = "Re-read the linked sketch, keeping supports and loads."

    def IsActive(self):
        editor = active_editor()
        return editor is not None and editor.model.sketch_link is not None

    def Activated(self):
        editor = active_editor()
        if editor is None:
            return
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
    tip = "Run the mechanism and watch it move. Click again to clear."

    def IsActive(self):
        editor = active_editor()
        return editor is not None and (editor.model.has_drivers()
                                       or bool(getattr(editor, "motion_result", None)))

    def GetResources(self):
        res = super().GetResources()
        res["Checkable"] = True
        return res

    def Activated(self, checked=False):
        editor = active_editor()
        if editor is None:
            return
        if checked:
            if getattr(editor, "result", None):
                editor.invalidate_result()
            editor.run_motion()
        else:
            editor.clear_motion()
        sync_hud_actions()


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

        first_import = editor.model.sketch_link is None
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

        if first_import:
            # Sketch coordinates are already true engineering mm, so this
            # only ever changes the display scale and where the origin sits
            # on the sheet, never a joint's real x, y.
            sketch_import.fit_to_sheet(editor.model)

        editor.model_changed()
        editor.fit()
        App.Console.PrintMessage(
            f"FBD: imported {len(members)} member(s) and {len(nodes)} joint(s) "
            f"from '{sketch.Label}'.\n"
        )


class SolveDiagram(_Command):
    icon = "fbd_solve.svg"
    text = "Solve"
    tip = "Solve the open diagram and show the reactions. Click again to clear."

    def IsActive(self):
        return active_editor() is not None or bool(doc_mod.find_diagrams())

    def GetResources(self):
        res = super().GetResources()
        res["Checkable"] = True
        return res

    def Activated(self, checked=False):
        editor = active_editor()
        if editor is None:
            diagrams = doc_mod.find_diagrams()
            if not diagrams:
                return
            from .editor_host import open_editor

            editor = open_editor(diagrams[0])
        if checked:
            if getattr(editor, "motion_result", None):
                editor.clear_motion()
            editor.solve()
        else:
            editor.clear_all()
        sync_hud_actions()


class ExportPDF(_Command):
    text = "Export PDF"
    tip = "Export diagram sheet to a PDF file."

    def GetResources(self):
        # FreeCAD's own export icon, not a hand-drawn one: this is
        # the same generic "export a file" action already on File >
        # Export (Ctrl+E), and Std_Export is the icon FreeCAD itself
        # uses there.
        res = super().GetResources()
        res["Pixmap"] = "Std_Export"
        return res

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
    "FBD_ToolAnchor": ToolAnchor,
    "FBD_ToolPin": ToolPin,
    "FBD_ToolRoller": ToolRoller,
    "FBD_ToolFixed": ToolFixed,
    "FBD_ToolSpring": ToolSpring,
    "FBD_ToolForce": ToolForce,
    "FBD_ToolMoment": ToolMoment,
    "FBD_ToolLineLoad": ToolLineLoad,
    "FBD_ToolPivot": ToolPivot,
    "FBD_ToolMotor": ToolMotor,
    "FBD_ToolActuator": ToolActuator,
    "FBD_DiagramAxial": DiagramAxial,
    "FBD_DiagramShear": DiagramShear,
    "FBD_DiagramMoment": DiagramMoment,
    "FBD_DiagramDeflection": DiagramDeflection,
    "FBD_ToggleLabels": ToggleLabels,
    "FBD_ToggleComponents": ToggleComponents,
    "FBD_ToggleReactions": ToggleReactions,
    "FBD_ToggleSchedule": ToggleSchedule,
    "FBD_ToggleGraph": ToggleGraph,
    "FBD_ToggleResultsTable": ToggleResultsTable,
    "FBD_ToggleSheet": ToggleSheetBoundary,
    "FBD_SyncSketch": SyncSketch,
    "FBD_RunMotion": RunMotion,
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


_DIAGRAM_COMMAND_NAMES = {
    "axial": "FBD_DiagramAxial",
    "shear": "FBD_DiagramShear",
    "moment": "FBD_DiagramMoment",
    "deflection": "FBD_DiagramDeflection",
}


def sync_diagram_actions():
    """Check each diagram toolbar button against whether that diagram is
    actually showing right now, the same way sync_tool_actions() mirrors
    the active drawing tool."""
    if not App.GuiUp:
        return
    editor = active_editor()
    for kind, name in _DIAGRAM_COMMAND_NAMES.items():
        try:
            action = Gui.Command.get(name).getAction()
        except Exception:
            continue
        actions = action if isinstance(action, (list, tuple)) else [action]
        checked = bool(editor is not None and getattr(editor, f"show_{kind}", False))
        for act in actions:
            if act is None:
                continue
            try:
                act.blockSignals(True)
                act.setChecked(checked)
            finally:
                act.blockSignals(False)


# Play plus the time scrubber don't fit the plain-icon-button toolbar
# pattern, so they're a small hand-built widget embedded directly in the
# FBD toolbar instead of a registered Gui.Command. Every entry stays None
# until ensure_motion_widget() has actually inserted it.
_MOTION_WIDGET: dict = {"toolbar": None, "play_btn": None, "slider": None, "label": None}


def _set_checked(name, checked, enabled=None):
    try:
        action = Gui.Command.get(name).getAction()
    except Exception:
        return
    actions = action if isinstance(action, (list, tuple)) else [action]
    for act in actions:
        if act is None:
            continue
        try:
            act.blockSignals(True)
            act.setChecked(bool(checked))
            if enabled is not None:
                act.setEnabled(bool(enabled))
        finally:
            act.blockSignals(False)


def sync_hud_actions():
    """Mirror the editor's current run/display state onto the toolbar:
    Solve and Run Motion light up while their result is on screen, the
    plain display toggles match what's actually showing.
    """
    if not App.GuiUp:
        return
    editor = active_editor()

    has_statics = bool(editor is not None and getattr(editor, "result", None)
                       and editor.result.ok)
    mot_res = getattr(editor, "motion_result", None) if editor is not None else None
    has_mot = bool(mot_res and getattr(mot_res, "ok", False) and getattr(mot_res, "frames", None))
    disp_res = getattr(editor, "display_result", None) if editor is not None else None
    has_any_res = bool(disp_res is not None and getattr(disp_res, "ok", False))
    has_drivers = bool(editor is not None and editor.model.has_drivers())

    _set_checked("FBD_Solve", has_statics)
    _set_checked("FBD_RunMotion", has_mot, enabled=(has_drivers or has_mot))

    _set_checked("FBD_ToggleReactions",
                bool(editor is not None and getattr(editor, "show_reactions", False)),
                enabled=has_any_res)
    _set_checked("FBD_ToggleResultsTable",
                bool(getattr(editor, "show_results_table", True)) if editor is not None else True,
                enabled=has_any_res)
    _set_checked("FBD_ToggleGraph",
                bool(editor is not None and getattr(editor, "show_graph", False)),
                enabled=has_mot)
    _set_checked("FBD_ToggleSchedule",
                bool(editor is not None and getattr(editor, "show_schedule", False)),
                enabled=has_drivers)
    _set_checked("FBD_ToggleLabels",
                bool(editor is not None and getattr(editor, "show_labels", False)))
    _set_checked("FBD_ToggleComponents",
                bool(editor is not None and getattr(editor, "show_components", False)))
    _set_checked("FBD_ToggleSheet",
                bool(getattr(editor, "show_sheet", True)) if editor is not None else True)



def register():
    if not App.GuiUp:
        return
    for name, cls in COMMANDS.items():
        Gui.addCommand(name, cls())
