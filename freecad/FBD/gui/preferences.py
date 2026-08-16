# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Ryan Kembrey <ryan.FreeCAD@gmail.com>
# SPDX-FileNotice: Part of the FBD addon.

"""Preferences for the Free Body workbench.

Everything a diagram is drawn or solved with by default lives here: the
sheet a new diagram starts on, the load a placed force arrives with, the
stiffness a new member is given, how a mechanism is simulated, and the
colours and sizes the canvas draws with. Nothing about an existing diagram
changes when a preference changes; these are only ever read when something
new is created. The one exception is Appearance, since colours and sizes
are read fresh by every paint call, so a change there applies immediately.

Follows the same recipe as every other Free Body task panel: a dataclass
per field type, a FieldGroup to collect them under one heading, and a
PreferencePanel subclass that turns the two into a full page with zero
layout code. FreeCAD only asks a preference page for a `.form` widget plus
`loadSettings()` and `saveSettings()`, so `_PreferencePage` is the thin
adapter between that contract and the reusable panels below.

Other modules read the current value through the `prefs` object at the
bottom of this file, e.g. `from .preferences import prefs` then
`prefs.default_force()`. Reading is defensive: if FreeCAD's own parameter
store cannot be reached (a bare Python test harness, for instance) every
getter simply returns the same default shown in the dialog, so nothing
that already worked stops working.
"""

import os
from dataclasses import dataclass, field
from typing import TypeVar

from PySide6 import QtCore, QtGui, QtWidgets

import FreeCAD as App  # type: ignore
import FreeCADGui as Gui  # type: ignore

from ..engine import model as M

_PREF_PATH = "Mod/FBD"

_ICON_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "resources", "icons"
)

_W = TypeVar("_W", bound=QtWidgets.QWidget)


def _icon_path(name: str) -> str:
    return os.path.join(_ICON_DIR, name)


def _pref(widget: _W, entry: str) -> _W:
    """Sets FreeCAD preference properties on a widget so its own reset
    machinery can find this widget the same way it would a Designer built
    page."""
    widget.setProperty("prefPath", _PREF_PATH)
    widget.setProperty("prefEntry", entry)
    return widget


# =============================================================================
# Field types


@dataclass
class IntField:
    key: str
    label: str
    default: int
    min: int = 0
    max: int = 100
    suffix: str = ""
    tooltip: str = ""


@dataclass
class FloatField:
    key: str
    label: str
    default: float
    min: float = 0.0
    max: float = 1.0
    step: float = 0.01
    decimals: int = 3
    suffix: str = ""
    tooltip: str = ""


@dataclass
class BoolField:
    key: str
    label: str
    default: bool = False
    tooltip: str = ""


@dataclass
class StringField:
    key: str
    label: str
    default: str = ""
    tooltip: str = ""


@dataclass
class ChoiceField:
    key: str
    label: str
    options: list  # [(value, display text), ...]
    default: str = ""
    tooltip: str = ""


@dataclass
class ColorField:
    key: str
    label: str
    default: str = "#000000"  # hex, e.g. "#c62828"
    tooltip: str = ""


@dataclass
class FieldGroup:
    title: str
    fields: list = field(default_factory=list)


class ColorButton(QtWidgets.QPushButton):
    """A flat swatch button that opens FreeCAD's own colour picker, for a
    ColorField. Spans its whole grid column, the same as the spin boxes
    next to it, so the colour is painted directly onto the button rather
    than fixed to a small width. Painted by hand rather than through a
    stylesheet: an unscoped stylesheet rule cascades to every child
    widget, including a colour dialog opened with this button as its
    parent, which is exactly what turned a whole preference page red
    rather than just the button.
    """

    def __init__(self, color: str = "#000000"):
        super().__init__()
        self._color = color
        self.setFixedHeight(24)
        self.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.setToolTip(color)
        self.clicked.connect(self._pick)

    def paintEvent(self, event):
        del event
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        rect = self.rect().adjusted(1, 1, -1, -1)
        painter.setPen(QtGui.QPen(QtGui.QColor("#888888"), 1))
        painter.setBrush(QtGui.QColor(self._color))
        painter.drawRoundedRect(rect, 3, 3)
        painter.end()

    def _pick(self):
        chosen = QtWidgets.QColorDialog.getColor(QtGui.QColor(self._color), self, "Choose Colour")
        if chosen.isValid():
            self.setColor(chosen.name())

    def color(self) -> str:
        return self._color

    def setColor(self, hex_color: str):
        self._color = hex_color
        self.setToolTip(hex_color)
        self.update()


# =============================================================================
# Panel base


class PreferencePanel(QtWidgets.QWidget):
    """
    Subclass and set `title` and `groups` to get a full preference panel
    with zero widget or layout code.
    """

    title: str = ""
    groups: list = []

    def __init__(self):
        super().__init__()
        self._widgets: dict = {}
        self._defaults: dict = {}
        self._build_ui()

    def widget(self, key: str) -> QtWidgets.QWidget:
        return self._widgets[key]

    def _build_ui(self):
        layout = QtWidgets.QVBoxLayout(self)

        # FreeCAD already prints the page title at the top of the dialog
        # (and again as a breadcrumb above that), so repeating it here
        # would just be a third copy of the same words.
        for group in self.groups:
            layout.addWidget(self._build_group(group))

        layout.addStretch()

    def _build_group(self, group: FieldGroup) -> QtWidgets.QGroupBox:
        box = QtWidgets.QGroupBox(group.title)
        grid = QtWidgets.QGridLayout(box)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)

        for row, f in enumerate(group.fields):
            w = self._create_widget(f)
            self._widgets[f.key] = w
            self._defaults[f.key] = f.default

            if isinstance(f, BoolField):
                grid.addWidget(w, row, 0, 1, 2)
            else:
                label = QtWidgets.QLabel(f.label)
                grid.addWidget(label, row, 0)
                grid.addWidget(w, row, 1)

        return box

    def _create_widget(self, f) -> QtWidgets.QWidget:
        if isinstance(f, IntField):
            w = _pref(QtWidgets.QSpinBox(), f.key)
            w.setRange(f.min, f.max)
            if f.suffix:
                w.setSuffix(f.suffix)
            if f.tooltip:
                w.setToolTip(f.tooltip)
            return w

        if isinstance(f, FloatField):
            w = _pref(QtWidgets.QDoubleSpinBox(), f.key)
            w.setRange(f.min, f.max)
            w.setSingleStep(f.step)
            w.setDecimals(f.decimals)
            if f.suffix:
                w.setSuffix(f.suffix)
            if f.tooltip:
                w.setToolTip(f.tooltip)
            return w

        if isinstance(f, BoolField):
            w = _pref(QtWidgets.QCheckBox(f.label), f.key)
            if f.tooltip:
                w.setToolTip(f.tooltip)
            return w

        if isinstance(f, StringField):
            w = _pref(QtWidgets.QLineEdit(), f.key)
            if f.tooltip:
                w.setToolTip(f.tooltip)
            return w

        if isinstance(f, ChoiceField):
            w = _pref(QtWidgets.QComboBox(), f.key)
            for value, text in f.options:
                w.addItem(text, value)
            if f.tooltip:
                w.setToolTip(f.tooltip)
            return w

        if isinstance(f, ColorField):
            w = _pref(ColorButton(f.default), f.key)
            if f.tooltip:
                w.setToolTip(f.tooltip)
            return w

        raise TypeError(f"Unknown preference field type: {type(f)}")

    def load(self, params):
        for key, widget in self._widgets.items():
            default = self._defaults[key]
            if isinstance(widget, QtWidgets.QCheckBox):
                widget.setChecked(params.GetBool(key, default))
            elif isinstance(widget, ColorButton):
                widget.setColor(params.GetString(key, default))
            elif isinstance(widget, QtWidgets.QComboBox):
                value = params.GetString(key, default)
                index = widget.findData(value)
                widget.setCurrentIndex(index if index >= 0 else 0)
            elif isinstance(widget, QtWidgets.QDoubleSpinBox):
                widget.setValue(params.GetFloat(key, default))
            elif isinstance(widget, QtWidgets.QSpinBox):
                widget.setValue(params.GetInt(key, default))
            elif isinstance(widget, QtWidgets.QLineEdit):
                widget.setText(params.GetString(key, default))

    def save(self, params):
        for key, widget in self._widgets.items():
            if isinstance(widget, QtWidgets.QCheckBox):
                params.SetBool(key, widget.isChecked())
            elif isinstance(widget, ColorButton):
                params.SetString(key, widget.color())
            elif isinstance(widget, QtWidgets.QComboBox):
                params.SetString(key, widget.currentData())
            elif isinstance(widget, QtWidgets.QDoubleSpinBox):
                params.SetFloat(key, widget.value())
            elif isinstance(widget, QtWidgets.QSpinBox):
                params.SetInt(key, widget.value())
            elif isinstance(widget, QtWidgets.QLineEdit):
                params.SetString(key, widget.text())


# =============================================================================
# Pages


class GeneralPreferencesPanel(PreferencePanel):
    title = "General"
    groups = [
        FieldGroup(
            "New Diagram",
            [
                ChoiceField(
                    "SheetPreset",
                    "Sheet size",
                    options=[(name, name) for name in M.SHEET_PRESETS],
                    default="A3 landscape",
                    tooltip="Paper size given to a newly created diagram.",
                ),
                FloatField(
                    "SheetGrid",
                    "Grid spacing",
                    default=10.0,
                    max=1000.0,
                    step=1.0,
                    decimals=1,
                    suffix=" mm",
                    tooltip="Spacing of the grid lines drawn on the sheet.",
                ),
                StringField(
                    "SheetTitle",
                    "Title",
                    default="Free Body Diagram",
                    tooltip="Title printed in the corner of a newly created diagram.",
                ),
            ],
        ),
        FieldGroup(
            "Snapping",
            [
                BoolField(
                    "SnapEnabled",
                    "Snap to joints by default",
                    default=True,
                    tooltip="Whether snapping is turned on when a diagram is first opened.",
                ),
                FloatField(
                    "SnapPixels",
                    "Snap radius",
                    default=10.0,
                    max=60.0,
                    step=1.0,
                    decimals=0,
                    suffix=" px",
                    tooltip="How close the cursor must be to a joint, in screen pixels, "
                    "before it snaps.",
                ),
                FloatField(
                    "HandlePixels",
                    "Handle grab radius",
                    default=9.0,
                    max=60.0,
                    step=1.0,
                    decimals=0,
                    suffix=" px",
                    tooltip="Grab radius for draggable handles, such as a load's rotate "
                    "handle, in screen pixels.",
                ),
            ],
        ),
    ]


class LoadsPreferencesPanel(PreferencePanel):
    title = "Loads"
    groups = [
        FieldGroup(
            "Default Magnitudes",
            [
                FloatField(
                    "DefaultForce",
                    "Point force",
                    default=1000.0,
                    max=1.0e9,
                    step=100.0,
                    decimals=1,
                    suffix=" N",
                    tooltip="Magnitude given to a new point force placed with the Force tool.",
                ),
                FloatField(
                    "DefaultMoment",
                    "Moment",
                    default=1.0e5,
                    max=1.0e12,
                    step=1000.0,
                    decimals=1,
                    suffix=" N.mm",
                    tooltip="Magnitude given to a new couple placed with the Moment tool.",
                ),
                FloatField(
                    "DefaultLineLoad",
                    "Line load",
                    default=1.0,
                    max=1.0e6,
                    step=0.1,
                    decimals=3,
                    suffix=" N/mm",
                    tooltip="Intensity given to a new distributed load placed with the "
                    "Line load tool.",
                ),
            ],
        ),
        FieldGroup(
            "Labels",
            [
                BoolField(
                    "ShowJointLabels",
                    "Show joint labels",
                    default=False,
                    tooltip="Show joint names (A, B, C...) on the diagram.",
                ),
                BoolField(
                    "LabelLoads",
                    "Show load magnitudes",
                    default=True,
                    tooltip="Show the magnitude next to applied loads.",
                ),
                BoolField(
                    "LabelReactions",
                    "Show reaction magnitudes",
                    default=True,
                    tooltip="Show the magnitude next to solved reactions.",
                ),
            ],
        ),
    ]


class MembersPreferencesPanel(PreferencePanel):
    title = "Members"
    groups = [
        FieldGroup(
            "Default Section Stiffness",
            [
                FloatField(
                    "DefaultEA",
                    "Axial stiffness (EA)",
                    default=2.1e5 * 3.0e3,
                    max=1.0e13,
                    step=1.0e6,
                    decimals=0,
                    suffix=" N",
                    tooltip="Default axial stiffness given to a new member, a nominal "
                    "steel section. Only changes the answer for statically indeterminate "
                    "structures.",
                ),
                FloatField(
                    "DefaultEI",
                    "Bending stiffness (EI)",
                    default=2.1e5 * 2.0e6,
                    max=1.0e16,
                    step=1.0e8,
                    decimals=0,
                    suffix=" N.mm2",
                    tooltip="Default bending stiffness given to a new member, a nominal "
                    "steel section. Only changes the answer for statically indeterminate "
                    "structures.",
                ),
                FloatField(
                    "LeverDefaultLength",
                    "Lever length",
                    default=200.0,
                    max=1.0e6,
                    step=10.0,
                    decimals=1,
                    suffix=" mm",
                    tooltip="Bar length given to a newly placed first class lever.",
                ),
            ],
        ),
    ]


class MotionPreferencesPanel(PreferencePanel):
    title = "Drivers and Motion"
    groups = [
        FieldGroup(
            "New Motor",
            [
                FloatField(
                    "MotorSpeed",
                    "Speed",
                    default=60.0,
                    min=-1.0e6,
                    max=1.0e6,
                    step=5.0,
                    decimals=1,
                    suffix=" deg/s",
                    tooltip="Speed given to a newly placed motor. Counter-clockwise is positive.",
                ),
                FloatField(
                    "MotorSweep",
                    "Sweep",
                    default=90.0,
                    min=1.0,
                    max=360.0,
                    step=5.0,
                    decimals=1,
                    suffix=" deg",
                    tooltip="Sweep angle given to a newly placed motor set to rock back "
                    "and forth, either side of its starting angle.",
                ),
            ],
        ),
        FieldGroup(
            "New Actuator",
            [
                FloatField(
                    "ActuatorStrokeFraction",
                    "Stroke fraction (0 to 1)",
                    default=0.3,
                    max=1.0,
                    step=0.05,
                    decimals=2,
                    tooltip="A newly placed actuator's stroke is set to this fraction of "
                    "the member's own drawn length.",
                ),
                FloatField(
                    "ActuatorMinStroke",
                    "Minimum stroke",
                    default=10.0,
                    max=1.0e5,
                    step=1.0,
                    decimals=1,
                    suffix=" mm",
                    tooltip="A newly placed actuator's stroke is never set shorter than "
                    "this, even on a very short member.",
                ),
                FloatField(
                    "ActuatorSpeed",
                    "Speed",
                    default=50.0,
                    max=1.0e5,
                    step=5.0,
                    decimals=1,
                    suffix=" mm/s",
                    tooltip="Speed given to a newly placed actuator.",
                ),
            ],
        ),
        FieldGroup(
            "Playback",
            [
                FloatField(
                    "MotionDuration",
                    "Duration",
                    default=4.0,
                    max=3600.0,
                    step=0.5,
                    decimals=2,
                    suffix=" s",
                    tooltip="How long a newly created diagram simulates for, before its "
                    "own natural cycle length rounds it up, or a scheduled sequence "
                    "overrides it.",
                ),
                IntField(
                    "MotionFPS",
                    "Frame rate",
                    default=30,
                    min=1,
                    max=240,
                    suffix=" fps",
                    tooltip="How many frames per second a newly created diagram simulates.",
                ),
                IntField(
                    "MotionGhosts",
                    "Ghost frames",
                    default=0,
                    min=0,
                    max=20,
                    tooltip="Faint copies of earlier frames shown during playback, for a "
                    "newly created diagram.",
                ),
                BoolField(
                    "MotionTrace",
                    "Draw joint traces",
                    default=True,
                    tooltip="Draw the path swept by moving joints, for a newly created diagram.",
                ),
                BoolField(
                    "MotionRepeat",
                    "Repeat automatically",
                    default=False,
                    tooltip="Restart the animation from the top once it finishes, for a "
                    "newly created diagram. An ordinary always-on driver already loops "
                    "on its own natural period regardless of this setting.",
                ),
                IntField(
                    "PlaybackRefreshMs",
                    "Refresh interval",
                    default=33,
                    min=8,
                    max=250,
                    suffix=" ms",
                    tooltip="How often the running pose repaints while playing. 33 ms is "
                    "about 30 updates a second.",
                ),
            ],
        ),
    ]


class AnalysisPreferencesPanel(PreferencePanel):
    title = "Analysis"
    groups = [
        FieldGroup(
            "Static Solver",
            [
                BoolField(
                    "GeometricNonlinear",
                    "Geometric non-linear by default",
                    default=False,
                    tooltip="Include second order (P-delta) effects for a newly created "
                    "diagram's analysis.",
                ),
                IntField(
                    "MaxIterations",
                    "Max iterations",
                    default=200,
                    min=1,
                    max=100000,
                    tooltip="Maximum solver iterations for a newly created diagram.",
                ),
                IntField(
                    "Discretisation",
                    "Diagram sampling",
                    default=50,
                    min=2,
                    max=1000,
                    tooltip="Sampling points per member used to draw the internal force "
                    "diagrams, for a newly created diagram.",
                ),
                IntField(
                    "MaxSlackPasses",
                    "Max slack passes",
                    default=12,
                    min=1,
                    max=200,
                    tooltip="Maximum passes allowed when resolving which tension only "
                    "cables or compression only struts have gone slack.",
                ),
            ],
        ),
        FieldGroup(
            "Mechanism Solver",
            [
                FloatField(
                    "KinematicsTolerance",
                    "Convergence tolerance",
                    default=1e-7,
                    min=1e-9,
                    max=1e-3,
                    step=1e-7,
                    decimals=9,
                    tooltip="How close the Newton-Raphson mechanism solver must get to a "
                    "valid pose before it is accepted.",
                ),
                IntField(
                    "KinematicsMaxIterations",
                    "Max Newton iterations",
                    default=60,
                    min=1,
                    max=1000,
                    tooltip="Maximum Newton iterations per frame before a mechanism pose "
                    "is treated as unreached and the linkage is held at its limit.",
                ),
            ],
        ),
    ]


class DisplayPreferencesPanel(PreferencePanel):
    title = "Display"
    groups = [
        FieldGroup(
            "Initial Display State",
            [
                BoolField(
                    "ShowSheet",
                    "Show the paper sheet boundary",
                    default=True,
                    tooltip="Show the paper sheet boundary and title block when a "
                    "diagram is opened.",
                ),
                BoolField(
                    "InfiniteCanvas",
                    "Allow panning past the sheet edge",
                    default=True,
                    tooltip="Whether the canvas can be panned freely beyond the edges "
                    "of the sheet.",
                ),
                BoolField(
                    "ShowReactions",
                    "Show reactions",
                    default=True,
                    tooltip="Show solved reaction arrows after a diagram is solved.",
                ),
                BoolField(
                    "ShowResultsTable",
                    "Show results table",
                    default=True,
                    tooltip="Show the results table after solving, when a diagram is opened.",
                ),
                BoolField(
                    "ShowComponents",
                    "Show forces as Fx/Fy components",
                    default=False,
                    tooltip="Show loads and reactions as separate horizontal and "
                    "vertical arrows instead of one angled arrow.",
                ),
            ],
        ),
        FieldGroup(
            "Initial Diagram Display",
            [
                BoolField(
                    "ShowMomentDiagram",
                    "Bending moment",
                    default=False,
                    tooltip="Show the bending moment diagram when a diagram is opened.",
                ),
                BoolField(
                    "ShowShearDiagram",
                    "Shear force",
                    default=False,
                    tooltip="Show the shear force diagram when a diagram is opened.",
                ),
                BoolField(
                    "ShowAxialDiagram",
                    "Axial force",
                    default=False,
                    tooltip="Show the axial force diagram when a diagram is opened.",
                ),
                BoolField(
                    "ShowDeflectionDiagram",
                    "Deflected shape",
                    default=False,
                    tooltip="Show the deflected shape when a diagram is opened.",
                ),
            ],
        ),
        FieldGroup(
            "Motion",
            [
                BoolField(
                    "ShowMotion",
                    "Show the running pose",
                    default=True,
                    tooltip="Draw the moving mechanism over the drawn diagram while it runs.",
                ),
                BoolField(
                    "LabelMotion",
                    "Label peak speed",
                    default=True,
                    tooltip="Show the speed of the fastest moving joint during playback.",
                ),
                BoolField(
                    "ShowGraph",
                    "Show the effort graph",
                    default=True,
                    tooltip="Show the driver effort against travel graph after running "
                    "a mechanism.",
                ),
                BoolField(
                    "ShowSchedule",
                    "Show the schedule panel",
                    default=False,
                    tooltip="Show the driver choreography timeline when a diagram with "
                    "drivers is opened.",
                ),
            ],
        ),
    ]


class AppearancePreferencesPanel(PreferencePanel):
    title = "Appearance"
    groups = [
        FieldGroup(
            "Arrows and Symbols",
            [
                FloatField(
                    "ArrowLength",
                    "Load arrow length",
                    default=10.0,
                    max=200.0,
                    step=0.5,
                    decimals=1,
                    suffix=" mm",
                    tooltip="Nominal length of a load arrow, before it is scaled "
                    "against the largest load on the sheet.",
                ),
                FloatField(
                    "ArrowMinLength",
                    "Minimum arrow length",
                    default=6.0,
                    max=200.0,
                    step=0.5,
                    decimals=1,
                    suffix=" mm",
                    tooltip="Shortest an arrow is ever drawn, even for a very small load.",
                ),
                IntField(
                    "HatchCount",
                    "Ground hatch ticks",
                    default=7,
                    min=2,
                    max=30,
                    tooltip="Number of hatch ticks drawn under a pin or fixed support.",
                ),
            ],
        ),
        FieldGroup(
            "Colours",
            [
                ColorField(
                    "ColorApplied",
                    "Applied loads",
                    default="#c62828",
                    tooltip="Colour used for applied point loads, moments and line loads.",
                ),
                ColorField(
                    "ColorReaction",
                    "Reactions",
                    default="#1565c0",
                    tooltip="Colour used for solved reactions.",
                ),
                ColorField(
                    "ColorInternal",
                    "Internal forces",
                    default="#2e7d32",
                    tooltip="Colour used for axial, shear and moment annotations.",
                ),
                ColorField(
                    "ColorSelect",
                    "Selection",
                    default="#0a84ff",
                    tooltip="Colour used to highlight a selected item.",
                ),
                ColorField(
                    "ColorSnap",
                    "Snap indicator",
                    default="#ff8f00",
                    tooltip="Colour used for the snap indicator ring.",
                ),
            ],
        ),
    ]


# =============================================================================
# FreeCAD adapter and registration


def _refresh_style():
    """Live-reload the canvas colours and sizes after Appearance is saved,
    so a change is visible the next time anything is drawn, with no
    restart needed."""
    try:
        from . import style

        style.reload_from_preferences()
    except Exception:
        pass


class _PreferencePage:
    """Wraps one PreferencePanel as a native FreeCAD preference page.

    FreeCAD's preference dialog looks for a `.form` widget plus
    `loadSettings()` and `saveSettings()` methods on the class it is
    given; this is the whole adapter between that contract and the
    reusable panels defined above.
    """

    panel_cls: type = PreferencePanel

    def __init__(self):
        self.panel = self.panel_cls()
        self.form = self.panel
        self.form.setWindowTitle(self.panel.title)
        self.form.setWindowIcon(QtGui.QIcon(_icon_path("fbd_diagram.svg")))

    def loadSettings(self):
        self.panel.load(prefs.params())

    def saveSettings(self):
        self.panel.save(prefs.params())
        _refresh_style()


class FBDPreferencesGeneral(_PreferencePage):
    panel_cls = GeneralPreferencesPanel


class FBDPreferencesLoads(_PreferencePage):
    panel_cls = LoadsPreferencesPanel


class FBDPreferencesMembers(_PreferencePage):
    panel_cls = MembersPreferencesPanel


class FBDPreferencesMotion(_PreferencePage):
    panel_cls = MotionPreferencesPanel


class FBDPreferencesAnalysis(_PreferencePage):
    panel_cls = AnalysisPreferencesPanel


class FBDPreferencesDisplay(_PreferencePage):
    panel_cls = DisplayPreferencesPanel


class FBDPreferencesAppearance(_PreferencePage):
    panel_cls = AppearancePreferencesPanel


PAGES = [
    FBDPreferencesGeneral,
    FBDPreferencesLoads,
    FBDPreferencesMembers,
    FBDPreferencesMotion,
    FBDPreferencesAnalysis,
    FBDPreferencesDisplay,
    FBDPreferencesAppearance,
]

_GROUP = "Free Body"


def _group_icon() -> str:
    """FreeCAD looks for an icon named preferences-<group>, lowercased
    with spaces turned to underscores, whenever it draws the sidebar
    entry for a preference group it does not already know about. Nothing
    ships that exact file, so make one now: a copy of the workbench's
    own icon, under the name FreeCAD is actually looking for. Purely
    cosmetic, so any failure here is silently ignored; the dialog still
    works either way, it just falls back to a generic icon.
    """
    wanted = _icon_path("preferences-" + _GROUP.lower().replace(" ", "_") + ".svg")
    if not os.path.isfile(wanted):
        try:
            import shutil

            shutil.copyfile(_icon_path("fbd_diagram.svg"), wanted)
        except Exception:
            pass
    return wanted


def register():
    if not App.GuiUp:
        return
    _group_icon()
    try:
        Gui.addIconPath(_ICON_DIR)
    except Exception:
        pass
    for cls in PAGES:
        Gui.addPreferencePage(cls, _GROUP)


# =============================================================================
# Runtime access, for every other module that needs a default


class _FallbackParams:
    """Stand in for FreeCAD's parameter store when it cannot be reached,
    such as under a test harness with a stubbed FreeCAD module. Every
    getter simply returns the default it was asked for, so behaviour
    matches exactly what this addon did before preferences existed.
    """

    def GetBool(self, key, default=False):
        return default

    def GetFloat(self, key, default=0.0):
        return default

    def GetInt(self, key, default=0):
        return default

    def GetString(self, key, default=""):
        return default


class _Prefs:
    """Read access to Free Body's stored preferences, with the same
    defaults the preference pages themselves show."""

    def params(self):
        try:
            return App.ParamGet(f"User parameter:BaseApp/Preferences/{_PREF_PATH}")
        except Exception:
            return _FallbackParams()

    # -- General ----------------------------------------------------------
    def sheet_preset(self):
        return self.params().GetString("SheetPreset", "A3 landscape")

    def sheet_grid(self):
        return self.params().GetFloat("SheetGrid", 10.0)

    def sheet_title(self):
        return self.params().GetString("SheetTitle", "Free Body Diagram")

    def snap_enabled(self):
        return self.params().GetBool("SnapEnabled", True)

    def snap_pixels(self):
        return self.params().GetFloat("SnapPixels", 10.0)

    def handle_pixels(self):
        return self.params().GetFloat("HandlePixels", 9.0)

    # -- Loads --------------------------------------------------------------
    def default_force(self):
        return self.params().GetFloat("DefaultForce", 1000.0)

    def default_moment(self):
        return self.params().GetFloat("DefaultMoment", 1.0e5)

    def default_line_load(self):
        return self.params().GetFloat("DefaultLineLoad", 1.0)

    def show_joint_labels(self):
        return self.params().GetBool("ShowJointLabels", False)

    def label_loads(self):
        return self.params().GetBool("LabelLoads", True)

    def label_reactions(self):
        return self.params().GetBool("LabelReactions", True)

    # -- Members ------------------------------------------------------------
    def default_ea(self):
        return self.params().GetFloat("DefaultEA", 2.1e5 * 3.0e3)

    def default_ei(self):
        return self.params().GetFloat("DefaultEI", 2.1e5 * 2.0e6)

    def lever_default_length(self):
        return self.params().GetFloat("LeverDefaultLength", 200.0)

    # -- Drivers and motion ---------------------------------------------
    def motor_speed(self):
        return self.params().GetFloat("MotorSpeed", 60.0)

    def motor_sweep(self):
        return self.params().GetFloat("MotorSweep", 90.0)

    def actuator_stroke_fraction(self):
        return self.params().GetFloat("ActuatorStrokeFraction", 0.3)

    def actuator_min_stroke(self):
        return self.params().GetFloat("ActuatorMinStroke", 10.0)

    def actuator_speed(self):
        return self.params().GetFloat("ActuatorSpeed", 50.0)

    def motion_duration(self):
        return self.params().GetFloat("MotionDuration", 4.0)

    def motion_fps(self):
        return self.params().GetInt("MotionFPS", 30)

    def motion_trace(self):
        return self.params().GetBool("MotionTrace", True)

    def motion_ghosts(self):
        return self.params().GetInt("MotionGhosts", 0)

    def motion_repeat(self):
        return self.params().GetBool("MotionRepeat", False)

    def playback_refresh_ms(self):
        return self.params().GetInt("PlaybackRefreshMs", 33)

    def show_motion(self):
        return self.params().GetBool("ShowMotion", True)

    def label_motion(self):
        return self.params().GetBool("LabelMotion", True)

    def show_graph(self):
        return self.params().GetBool("ShowGraph", True)

    def show_schedule(self):
        return self.params().GetBool("ShowSchedule", False)

    # -- Analysis -----------------------------------------------------------
    def geometric_nonlinear(self):
        return self.params().GetBool("GeometricNonlinear", False)

    def max_iterations(self):
        return self.params().GetInt("MaxIterations", 200)

    def discretisation(self):
        return self.params().GetInt("Discretisation", 50)

    def max_slack_passes(self):
        return self.params().GetInt("MaxSlackPasses", 12)

    def kinematics_tolerance(self):
        return self.params().GetFloat("KinematicsTolerance", 1e-7)

    def kinematics_max_iterations(self):
        return self.params().GetInt("KinematicsMaxIterations", 60)

    # -- Display --------------------------------------------------------
    def show_sheet(self):
        return self.params().GetBool("ShowSheet", True)

    def infinite_canvas(self):
        return self.params().GetBool("InfiniteCanvas", True)

    def show_reactions(self):
        return self.params().GetBool("ShowReactions", True)

    def show_results_table(self):
        return self.params().GetBool("ShowResultsTable", True)

    def show_components(self):
        return self.params().GetBool("ShowComponents", False)

    def show_moment_diagram(self):
        return self.params().GetBool("ShowMomentDiagram", False)

    def show_shear_diagram(self):
        return self.params().GetBool("ShowShearDiagram", False)

    def show_axial_diagram(self):
        return self.params().GetBool("ShowAxialDiagram", False)

    def show_deflection_diagram(self):
        return self.params().GetBool("ShowDeflectionDiagram", False)

    # -- Appearance -----------------------------------------------------
    # style.py reads these same keys directly (it cannot import this
    # module without importing FreeCADGui into a file every other module
    # counts on staying light), so the two are kept in step by hand: any
    # key changed above must also be changed in style.reload_from_preferences.
    def arrow_length(self):
        return self.params().GetFloat("ArrowLength", 10.0)

    def arrow_min_length(self):
        return self.params().GetFloat("ArrowMinLength", 6.0)

    def hatch_count(self):
        return self.params().GetInt("HatchCount", 7)

    def color_applied(self):
        return self.params().GetString("ColorApplied", "#c62828")

    def color_reaction(self):
        return self.params().GetString("ColorReaction", "#1565c0")

    def color_internal(self):
        return self.params().GetString("ColorInternal", "#2e7d32")

    def color_select(self):
        return self.params().GetString("ColorSelect", "#0a84ff")

    def color_snap(self):
        return self.params().GetString("ColorSnap", "#ff8f00")


prefs = _Prefs()
