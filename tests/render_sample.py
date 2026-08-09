"""Render sample diagrams to PNG so the drawing quality can be eyeballed."""
import os, sys, types
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

class _C:
    def PrintMessage(self,*a): pass
    def PrintWarning(self,*a): pass
    def PrintError(self,*a): pass
fc = types.ModuleType("FreeCAD"); fc.Console=_C(); fc.GuiUp=False; fc.ActiveDocument=None
sys.modules.setdefault("FreeCAD", fc)

from PySide6 import QtWidgets, QtGui, QtCore
from freecad.FBD.engine import model as M
from freecad.FBD.engine import PIN, ROLLER_X, FIXED, SPRING
from freecad.FBD.gui.editor import Editor

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

def render(editor, path, w=1400, h=850, zoom=None):
    """Grab the real view widget, so anchored (screen-constant) items and
    scene-space items are drawn through the same transform the user sees."""
    editor.resize(w, h)
    editor.view.resize(w, h)
    editor.show()
    app.processEvents()
    if zoom is None:
        editor.fit()
    else:
        editor.view.resetTransform()
        editor.view.scale(zoom, zoom)
        rect = editor.scene.sheet_rect()
        editor.view.centerOn(rect.center())
    editor.refresh_geometry()
    app.processEvents()
    pixmap = editor.view.grab()
    pixmap.save(path)
    print("wrote", path)


# --- 1. simply supported beam with a UDL and a point load -----------------
ed = Editor(M.Model(), host=None)
m = ed.model
m.sheet.title = "Simply supported beam"
a = m.add_node(60, 180, "A"); c = m.add_node(200, 180, "C"); b = m.add_node(360, 180, "B")
m1 = m.add_member(a.id, c.id); m2 = m.add_member(c.id, b.id)
m.add_support(a.id, PIN); m.add_support(b.id, ROLLER_X)
m.add_point_load(c.id, 0, -5000)
m.add_line_load(m2.id, -20.0, "y")
ed.rebuild(); ed.solve()
render(ed, "/mnt/user-data/outputs/sample_beam.png")

# --- 2. portal frame, fixed and spring supports, moment diagram -----------
ed2 = Editor(M.Model(), host=None)
m = ed2.model
m.sheet.title = "Portal frame"
bl = m.add_node(90, 60, "A"); tl = m.add_node(90, 200, "B")
tr = m.add_node(330, 200, "C"); br = m.add_node(330, 60, "D")
m.add_member(bl.id, tl.id); top = m.add_member(tl.id, tr.id); m.add_member(tr.id, br.id)
m.add_support(bl.id, FIXED)
m.add_support(br.id, PIN)
m.add_line_load(top.id, -15.0, "y")
m.add_point_load(tl.id, 3000, 0)
m.add_moment_load(tr.id, 2.0e5)
ed2.rebuild(); ed2.solve()
ed2.diagram_mode = "moment"; ed2.refresh_geometry()
render(ed2, "/mnt/user-data/outputs/sample_frame.png")
# same diagram at 3x zoom: symbols and text must not grow with it
ed2.view.resetTransform(); ed2.view.scale(3.0, 3.0); ed2.refresh_geometry()
render(ed2, "/mnt/user-data/outputs/sample_frame_zoom.png", zoom=3.0)

# --- 3. cantilever with a spring prop --------------------------------------
ed3 = Editor(M.Model(), host=None)
m = ed3.model
m.sheet.title = "Propped cantilever"
w = m.add_node(70, 170, "W"); mid = m.add_node(230, 170, "M"); t = m.add_node(370, 170, "T")
m.add_member(w.id, mid.id); m.add_member(mid.id, t.id)
m.add_support(w.id, FIXED)
sp = m.add_support(t.id, SPRING); sp.ky = 500.0
m.add_point_load(mid.id, 0, -4000)
ed3.rebuild(); ed3.solve()
ed3.diagram_mode = "shear"; ed3.refresh_geometry()
render(ed3, "/mnt/user-data/outputs/sample_cantilever.png")

for ed_, name in ((ed,"beam"),(ed2,"frame"),(ed3,"cantilever")):
    r = ed_.result
    print(f"{name}: ok={r.ok if r else None} "
          f"reactions={ {k: (round(v.fx,1), round(v.fy,1)) for k,v in (r.reactions.items() if r else [])} }")
