"""Import the whole addon with FreeCAD and Qt stubbed out.

Catches relative-import depth errors, bad resource paths and missing icons in
milliseconds, instead of via a FreeCAD restart cycle where the error is silently
swallowed and the workbench simply never appears.
"""
import os, sys, types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class _Any:
    def __init__(self, *a, **k): pass
    def __getattr__(self, item): return _Any()
    def __call__(self, *a, **k): return _Any()


def _module(name, **attrs):
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m
    return m


class _Vector:
    def __init__(self, x=0, y=0, z=0):
        self.x, self.y, self.z = float(x), float(y), float(z)


class _Console:
    def PrintMessage(self, *a): pass
    def PrintWarning(self, *a): pass
    def PrintError(self, *a): pass


_module("FreeCAD", Vector=_Vector, Console=_Console(), ActiveDocument=None,
        GuiUp=True, newDocument=lambda *a, **k: None)

_registered = {}


class _Workbench:
    def appendToolbar(self, *a): pass
    def appendMenu(self, *a): pass


_module("FreeCADGui", Workbench=_Workbench,
        addCommand=lambda n, o: _registered.__setitem__(n, o),
        addWorkbench=lambda wb: _registered.__setitem__("__wb__", wb),
        addPreferencePage=lambda cls, group: _registered.setdefault(
            "__prefs__", []).append((cls, group)),
        getMainWindow=lambda: _Any(), ActiveDocument=None,
        Selection=types.SimpleNamespace(getSelection=lambda: [],
                                        clearSelection=lambda: None),
        Control=types.SimpleNamespace(showDialog=lambda d: None,
                                      closeDialog=lambda: None))

# --- PySide6 -----------------------------------------------------------
qtc = types.ModuleType("PySide6.QtCore")
qtg = types.ModuleType("PySide6.QtGui")
qtw = types.ModuleType("PySide6.QtWidgets")
for name in ("QPointF", "QRectF", "QLineF", "QPoint", "QRect", "QSize", "QTimer",
             "QObject", "Signal", "QEvent"):
    setattr(qtc, name, _Any)
qtc.Qt = _Any()
for name in ("QColor", "QPen", "QBrush", "QPainter", "QPainterPath", "QPolygonF",
             "QPainterPathStroker", "QFont", "QFontMetricsF", "QAction",
             "QActionGroup", "QKeySequence", "QTransform"):
    setattr(qtg, name, _Any)
for name in ("QWidget", "QVBoxLayout", "QHBoxLayout", "QGridLayout", "QFormLayout",
             "QGroupBox", "QLabel", "QPushButton", "QDoubleSpinBox", "QSpinBox",
             "QCheckBox", "QComboBox", "QLineEdit", "QSlider", "QTableWidget",
             "QTableWidgetItem", "QAbstractItemView", "QSplitter", "QToolBar",
             "QGraphicsScene", "QGraphicsView", "QGraphicsItem", "QGraphicsObject",
             "QMdiArea", "QFrame", "QDialogButtonBox", "QInputDialog",
             "QApplication", "QStyle"):
    setattr(qtw, name, _Any)
ps = _module("PySide6")
ps.QtCore, ps.QtGui, ps.QtWidgets = qtc, qtg, qtw
sys.modules["PySide6.QtCore"] = qtc
sys.modules["PySide6.QtGui"] = qtg
sys.modules["PySide6.QtWidgets"] = qtw

sys.path.insert(0, ROOT)

failures = []

import freecad.FBD.init_gui                      # noqa: E402
from freecad.FBD.gui import commands             # noqa: E402

print("addon imported cleanly")

missing = [n for n in commands.COMMANDS if n not in _registered]
if missing:
    failures.append(f"commands not registered: {missing}")
else:
    print(f"registered {len(commands.COMMANDS)} commands: "
          f"{', '.join(sorted(commands.COMMANDS))}")

from freecad.FBD.gui import preferences as _fbd_prefs_mod  # noqa: E402
pages = _registered.get("__prefs__", [])
if len(pages) != len(_fbd_prefs_mod.PAGES):
    failures.append(
        f"preference pages not all registered: {len(pages)} of "
        f"{len(_fbd_prefs_mod.PAGES)}"
    )
else:
    print(f"registered {len(pages)} preference pages")

if "__wb__" not in _registered:
    failures.append("workbench never added")
else:
    wb = _registered["__wb__"]
    wb.Initialize()
    print(f"workbench '{wb.MenuText}' initialised")

for name, cls in commands.COMMANDS.items():
    path = cls().GetResources()["Pixmap"]
    if not os.path.isfile(path):
        failures.append(f"missing icon for {name}: {path}")
else:
    print(f"all {len(commands.COMMANDS)} command icons present")

import importlib                                  # noqa: E402
for mod in ("freecad.FBD.engine.model", "freecad.FBD.engine.checks",
            "freecad.FBD.engine.statics", "freecad.FBD.engine.results",
            "freecad.FBD.gui.style", "freecad.FBD.gui.editor",
            "freecad.FBD.gui.document", "freecad.FBD.gui.editor_host",
            "freecad.FBD.gui.engine_bridge",
            "freecad.FBD.gui.canvas.scene", "freecad.FBD.gui.canvas.items",
            "freecad.FBD.gui.canvas.tools"):
    try:
        importlib.import_module(mod)
    except Exception as exc:
        failures.append(f"{mod}: {type(exc).__name__}: {exc}")
print("all modules imported")

# the engine must stay free of FreeCAD and Qt
import subprocess                                 # noqa: E402
probe = subprocess.run(
    [sys.executable, "-c",
     "import sys; sys.path.insert(0, %r);"
     "import engine;"
     "assert 'FreeCAD' not in sys.modules, 'engine imported FreeCAD';"
     "assert 'PySide6' not in sys.modules, 'engine imported Qt';"
     "print('engine is standalone')" % os.path.join(ROOT, "freecad", "FBD")],
    capture_output=True, text=True)
if probe.returncode != 0:
    failures.append("engine purity: " + (probe.stderr.strip().splitlines() or [""])[-1])
else:
    print(probe.stdout.strip())

print()
if failures:
    print("FAILURES:")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("IMPORT TESTS PASSED")
