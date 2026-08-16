# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Ryan Kembrey <ryan.FreeCAD@gmail.com>
# SPDX-FileNotice: Part of the FBD addon.

import os
import FreeCADGui as Gui  # type: ignore
from .gui import commands as _fbd_commands
from .gui import preferences as _fbd_preferences

_ICONS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "resources", "icons")

_fbd_commands.register()
_fbd_preferences.register()


class FBDWorkbench(Gui.Workbench):
    MenuText = "Free Body"
    ToolTip = "Free body diagrams: draw structures, solve reactions and internal forces"
    Icon = os.path.join(_ICONS, "fbd_diagram.svg")

    def Initialize(self):
        self.document = ["FBD_NewDiagram", "FBD_OpenDiagram", "FBD_ImportSketch", "FBD_SyncSketch"]
        self.draw = ["FBD_ToolSelect", "FBD_ToolAnchor"]
        # Pivot lives here, not with Motor/Actuator: it constrains a point,
        # the same job as every other support, just at a point on a member
        # rather than at a joint.
        self.supports = [
            "FBD_ToolPin",
            "FBD_ToolRoller",
            "FBD_ToolFixed",
            "FBD_ToolSpring",
            "FBD_ToolPivot",
        ]
        self.loads = ["FBD_ToolForce", "FBD_ToolMoment", "FBD_ToolLineLoad"]
        self.mechanism = ["FBD_ToolMotor", "FBD_ToolActuator", "FBD_RunMotion"]
        self.analysis = ["FBD_Solve"]
        self.diagrams = [
            "FBD_DiagramAxial",
            "FBD_DiagramShear",
            "FBD_DiagramMoment",
            "FBD_DiagramDeflection",
        ]
        self.display = [
            "FBD_ToggleLabels",
            "FBD_ToggleComponents",
            "FBD_ToggleReactions",
            "FBD_ToggleSchedule",
            "FBD_ToggleGraph",
            "FBD_ToggleResultsTable",
            "FBD_ToggleSheet",
        ]
        self.view = ["FBD_FitView"]
        self.output = ["FBD_ExportPDF"]

        # Main Toolbar: build geometry, constrain it, load it, optionally
        # animate it, solve it, then adjust the view or ship it out --
        # grouped with separators the same way the menu is grouped into
        # submenus, so one long toolbar still reads as distinct sections.
        self.appendToolbar(
            "FBD",
            self.document
            + ["Separator"] + self.draw
            + ["Separator"] + self.supports
            + ["Separator"] + self.loads
            + ["Separator"] + self.mechanism
            + ["Separator"] + self.analysis
            + ["Separator"] + self.diagrams
            + ["Separator"] + self.display
            + ["Separator"] + self.view
            + ["Separator"] + self.output,
        )

        self.appendMenu("FBD", self.document + self.output)
        self.appendMenu(["FBD", "Draw"], self.draw)
        self.appendMenu(["FBD", "Supports"], self.supports)
        self.appendMenu(["FBD", "Loads"], self.loads)
        self.appendMenu(["FBD", "Mechanism"], self.mechanism)
        self.appendMenu(["FBD", "Analysis"], self.analysis)
        self.appendMenu(["FBD", "Diagrams"], self.diagrams)
        self.appendMenu(["FBD", "Display"], self.display)
        self.appendMenu(["FBD", "View"], self.view)

    def Activated(self):
        """Make the toolbar tell the truth the moment it appears.

        The tool commands are checkable, so until something syncs them they
        come up in whatever state Qt left them in, which looks like every tool
        is running at once. Nothing has called sync_tool_actions yet because
        there is no editor, so do it here, and again on every activation so
        switching away and back cannot leave a stale highlight.

        Deferred by one turn of the event loop because the toolbar is built
        during Initialize, and the actions do not exist before that finishes.
        """
        from PySide6 import QtCore
        from .gui.commands import (
            sync_tool_actions,
            sync_diagram_actions,
            sync_hud_actions,
            
        )
        from .gui.editor_host import active_editor

        def sync():
            ()
            editor = active_editor()
            sync_tool_actions(editor.current_tool_name() if editor else None)
            sync_diagram_actions()
            sync_hud_actions()

        QtCore.QTimer.singleShot(220, sync)

    def Deactivated(self):
        pass

    def ContextMenu(self, recipient):
        pass

    def GetClassName(self):
        return "Gui::PythonWorkbench"


Gui.addWorkbench(FBDWorkbench())
