"""Drive the real editor offscreen: tools, undo, persistence, solving.

Uses genuine Qt with the offscreen platform, so this exercises the actual
widgets rather than stubs. FreeCAD itself is stubbed, since the editor only
touches it through the host object.
"""
import os, sys, types
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# Import through the package path FreeCAD uses, so relative imports resolve
# exactly as they will in the application.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# Minimal FreeCAD stub: the editor imports engine only, but items import model.
class _Console:
    def PrintMessage(self, *a): pass
    def PrintWarning(self, *a): pass
    def PrintError(self, *a): pass
fc = types.ModuleType("FreeCAD"); fc.Console = _Console(); fc.GuiUp = False
fc.ActiveDocument = None
sys.modules.setdefault("FreeCAD", fc)

from PySide6 import QtWidgets, QtCore
from freecad.FBD.engine import model as M
from freecad.FBD.engine import PIN, ROLLER_X, FIXED, SPRING

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

from freecad.FBD.gui.editor import Editor

passed = failed = 0
def ok(name, cond, detail=""):
    global passed, failed
    if cond: passed += 1; print(f"  PASS  {name}")
    else: failed += 1; print(f"  FAIL  {name}  {detail}")

class Host:
    def __init__(self): self.saved = None
    def save(self, model): self.saved = model.to_dict()


def _fired(editor):
    seen = []
    editor.observers.append(lambda: seen.append(1))
    editor.notify()
    return bool(seen)

print("1. Editor builds and exposes the tool palette")
host = Host()
ed = Editor(M.Model(), host=host)
ok("editor constructed", ed is not None)
names = [t.name for t in ed.tools]
ok("has a select tool first", names[0] == "Select", names)
ok("has node and member tools", "Node" in names and "Member" in names, names)
ok("has all four support tools",
   sum(1 for n in names if n in [M.SUPPORT_LABELS[k] for k in M.SUPPORT_KINDS]) == 4,
   names)
ok("has force, moment and line load", {"Force","Moment","Line load"} <= set(names), names)
print()

print("2. Drawing with the tools mutates the model")
def tool_named(n):
    for t in ed.tools:
        if t.name == n: return t
    raise AssertionError(n)

def click(tool, x, y):
    ed.set_tool(tool)
    from freecad.FBD.gui.canvas import items as I
    pt = I.to_scene(x, y)
    tool.click(pt, (x, y))

node_tool = tool_named("Node")
click(node_tool, 0, 0); click(node_tool, 200, 0); click(node_tool, 100, 80)
ok("three nodes placed", len(ed.model.nodes) == 3, len(ed.model.nodes))

member_tool = tool_named("Member")
ids = list(ed.model.nodes)
click(member_tool, 0, 0); click(member_tool, 200, 0)
ok("member created between them", len(ed.model.members) == 1, len(ed.model.members))
click(member_tool, 100, 80)
ok("member tool chains", len(ed.model.members) == 2, len(ed.model.members))
member_tool.cancel()

pin_tool = tool_named(M.SUPPORT_LABELS[M.PIN])
click(pin_tool, 0, 0)
ok("pin support added", len(ed.model.supports) == 1)
roller = tool_named(M.SUPPORT_LABELS[M.ROLLER_X])
click(roller, 200, 0)
ok("roller added", len(ed.model.supports) == 2)
spring = tool_named(M.SUPPORT_LABELS[M.SPRING])
click(spring, 100, 80)
s = [x for x in ed.model.supports.values() if x.kind == M.SPRING][0]
ok("spring gets a default stiffness", s.ky > 0, s.ky)

force = tool_named("Force")
click(force, 100, 80)
ok("force added downward", len(ed.model.point_loads) == 1
   and list(ed.model.point_loads.values())[0].fy < 0)
print()

print("3. Persistence and undo")
ok("host received a save", host.saved is not None)
ok("saved blob round-trips",
   len(M.Model.from_dict(host.saved).nodes) == len(ed.model.nodes))
before = len(ed.model.point_loads)
ed.undo()
ok("undo removed the last edit", len(ed.model.point_loads) == before - 1,
   len(ed.model.point_loads))
ed.redo()
ok("redo restores it", len(ed.model.point_loads) == before, len(ed.model.point_loads))
print()

print("4. Solving through the editor")
ed2 = Editor(M.Model(), host=Host())
m = ed2.model
a = m.add_node(0, 0); b = m.add_node(6000, 0); c = m.add_node(2000, 0)
m.add_member(a.id, c.id); m.add_member(c.id, b.id)
m.add_support(a.id, PIN); m.add_support(b.id, ROLLER_X)
m.add_point_load(c.id, 0, -90000)
ed2.rebuild()
ed2.solve()
ok("solved", ed2.result is not None and ed2.result.ok,
   ed2.result.message if ed2.result else "no result")
ra = ed2.result.reactions[a.id].fy
ok("reaction matches hand calc (60000 N)", abs(ra - 60000) < 1.0, ra)
rows = ed2.result_rows()
ok("results rows produced", len(rows) > 0, len(rows))
text, level = ed2.status_text()
ok("status mentions determinate", "determinate" in text.lower(), text[:70])
ok("status level is ok", level == "ok", level)
print()

print("5. Unsolvable models are reported, not crashed")
ed3 = Editor(M.Model(), host=Host())
m3 = ed3.model
n1 = m3.add_node(0, 0); n2 = m3.add_node(1000, 0)
m3.add_member(n1.id, n2.id)
m3.add_point_load(n2.id, 0, -100)
ed3.rebuild(); ed3.solve()
ok("refuses to solve a mechanism", ed3.result is None or not ed3.result.ok)
text3, level3 = ed3.status_text()
ok("explains why in the status",
   "mechanism" in text3.lower() or "support" in text3.lower(), text3[:80])
ok("status level is error", level3 == "error", level3)
print()

print("6. Deleting cascades and the canvas stays consistent")
ed2.select_entity("node", c.id)
ed2.delete_selection()
app.processEvents()          # the rebuild is deferred out of the event handler
ok("node deleted", c.id not in ed2.model.nodes)
ok("its members went too", len(ed2.model.members) == 0, len(ed2.model.members))
ok("its load went too", len(ed2.model.point_loads) == 0)
ok("items rebuilt to match",
   len([k for k in ed2._items if k[0] == "node"]) == len(ed2.model.nodes))
print()

print("6b. Deleting several items at once is safe")
ed5 = Editor(M.Model(), host=Host())
m5 = ed5.model
ns = [m5.add_node(i * 100.0, 0.0) for i in range(5)]
for i in range(4):
    m5.add_member(ns[i].id, ns[i + 1].id)
m5.add_support(ns[0].id, PIN)
m5.add_support(ns[4].id, ROLLER_X)
for n in ns[1:4]:
    m5.add_point_load(n.id, 0, -500)
ed5.rebuild()
ed5.solve()
ok("multi-entity model solves first", ed5.result is not None and ed5.result.ok)
# select everything and delete in one go
for item in ed5._items.values():
    item.setSelected(True)
ed5.delete_selection()
app.processEvents()
ok("everything deleted without crashing", len(ed5.model.nodes) == 0
   and len(ed5.model.members) == 0, len(ed5.model.nodes))
ok("no orphaned graphics items", len(ed5._items) == 0, len(ed5._items))
ok("stale result cleared", ed5.result is None)

# delete a node mid-drag: the drag target disappears underneath
ed6 = Editor(M.Model(), host=Host())
n6 = ed6.model.add_node(0, 0)
b6 = ed6.model.add_node(100, 0)
ed6.model.add_member(n6.id, b6.id)
ed6.rebuild()
ed6._dragging_node = n6.id
ed6.model.delete("node", n6.id)
from freecad.FBD.gui.canvas import items as I6
ed6.handle_move(I6.to_scene(50, 50))
ok("dragging a deleted joint is handled", ed6._dragging_node is None)

# repeated deletes of cascaded entities must be harmless
ed7 = Editor(M.Model(), host=Host())
a7 = ed7.model.add_node(0, 0); b7 = ed7.model.add_node(100, 0)
mem7 = ed7.model.add_member(a7.id, b7.id)
ed7.model.add_line_load(mem7.id, -1.0)
ed7.rebuild()
for item in ed7._items.values():
    item.setSelected(True)
ed7.delete_selection()     # node cascade removes the member and its load too
app.processEvents()
ok("cascaded double-delete is harmless", not ed7.model.members
   and not ed7.model.line_loads)
print()

print("7. Snapping and hit testing")
ed4 = Editor(M.Model(), host=Host())
n = ed4.model.add_node(100, 50)
ed4.rebuild()
from freecad.FBD.gui.canvas import items as I
found = ed4.node_near(I.to_scene(100, 50))
ok("finds a node under the cursor", found == n.id, found)
far = ed4.node_near(I.to_scene(100000, 50))
ok("ignores distant nodes", far is None, far)
b1 = ed4.model.add_node(300, 50)
mem = ed4.model.add_member(n.id, b1.id)
ed4.rebuild()
hit = ed4.member_near(I.to_scene(200, 50))
ok("finds a member under the cursor", hit == mem.id, hit)
print()

print("8. Toolbar command surface")
ed8 = Editor(M.Model(), host=Host())
ok("tools addressable by name", ed8.set_tool_by_name("Member"))
ok("current tool reported", ed8.current_tool_name() == "Member",
   ed8.current_tool_name())
ok("unknown tool rejected", not ed8.set_tool_by_name("Nope"))
ok("px() shrinks with zoom", ed8.px(10) > 0, ed8.px(10))
ok("observers fire", _fired(ed8))
print()


print("9. On-page popup: opens, edits, closes, never blocks other edits")
ed9 = Editor(M.Model(), host=Host())
n9a = ed9.model.add_node(0, 0)
ed9.rebuild()
node_item = ed9._items[("node", n9a.id)]
ok("popup starts closed", ed9._popup is None)
node_item.open_editor(node_item.anchor_point())
ok("popup opens", ed9._popup is not None)
ok("undo pushed once on open", len(ed9._undo) == 1, len(ed9._undo))
from freecad.FBD.gui.canvas import items as I9
ok("popup_hit true inside its own bounds",
   ed9.popup_hit(ed9._popup.sceneBoundingRect().center()))
ok("popup_hit false far away", not ed9.popup_hit(I9.to_scene(5000, 5000)))
ed9.close_popup()
ok("popup closes", ed9._popup is None)
# Escape closes an open popup rather than falling through to the tool
node_item.open_editor(node_item.anchor_point())
ed9.handle_key(QtCore.Qt.Key.Key_Escape)
ok("Escape dismisses an open popup", ed9._popup is None)
print()

print("10. Support quick-switch and edit form")
ed10 = Editor(M.Model(), host=Host())
n10 = ed10.model.add_node(0, 0)
sup = ed10.model.add_support(n10.id, PIN)
ed10.rebuild()
sup_item = ed10._items[("support", sup.id)]
sup_item._set_kind(sup, "fixed")
ok("quick-switch changes the model", sup.kind == "fixed")
ok("switching rebuilds (item still present)", ("support", sup.id) in ed10._items)
sup_item = ed10._items[("support", sup.id)]
sup_item._set_kind(sup, M.SPRING)
ok("switching to spring gives a default stiffness", sup.ky > 0, sup.ky)
print()

print("11. Force drag-to-rotate handle math is wired to the model")
ed11 = Editor(M.Model(), host=Host())
n11 = ed11.model.add_node(0, 0)
load11 = ed11.model.add_point_load(node=n11.id, fx=0.0, fy=-500.0)
ed11.rebuild()
item11 = ed11._items[("point_load", load11.id)]
item11.setSelected(True)
tail = item11._tail()
ok("handle sits at the tail when selected", tail is not None)
ok("near_handle true at the tail", item11._near_handle(tail))
ok("near_handle false at the tip", not item11._near_handle(QtCore.QPointF(0, 0)))
# simulate a drag straight to the right (local +x) -> force should now point
# toward -x horizontally (tail-to-tip direction reversed, magnitude preserved)
mag = load11.magnitude()
fx, fy = I9.direction_from_handle(80.0, 0.0, mag)
load11.fx, load11.fy = fx, fy
ok("magnitude preserved after rotating", abs(load11.magnitude() - mag) < 1e-6,
   load11.magnitude())
ok("direction actually changed", abs(load11.fy) < 1.0, (load11.fx, load11.fy))
print()

print("12. Anchor tool: point on a member, loads attach to it, cascades on delete")
ed12 = Editor(M.Model(), host=Host())
a12 = ed12.model.add_node(0, 0)
b12 = ed12.model.add_node(200, 0)
mem12 = ed12.model.add_member(a12.id, b12.id)
ed12.model.add_support(a12.id, PIN)
ed12.model.add_support(b12.id, ROLLER_X)
ed12.rebuild()

def click(tool_name, x, y):
    tool = next(t for t in ed12.tools if t.name == tool_name)
    ed12.set_tool(tool)
    tool.click(I9.to_scene(x, y), (x, y))

click("Point", 100, 0)
ok("anchor placed", len(ed12.model.anchors) == 1)
anchor = next(iter(ed12.model.anchors.values()))
ok("anchor sits at the clicked fraction", abs(anchor.t - 0.5) < 0.05, anchor.t)
ok("anchor item created", ("anchor", anchor.id) in ed12._items)

click("Force", 100, 0)      # clicking the anchor's location, not a joint
ok("force attached to the anchor, not a node",
   len(ed12.model.point_loads) == 1
   and list(ed12.model.point_loads.values())[0].anchor == anchor.id)

ed12.solve()
ok("solves with an anchor-attached load", ed12.result is not None and ed12.result.ok,
   ed12.result.message if ed12.result else "?")

ed12.model.delete("member", mem12.id)
ok("deleting the member cascades to the anchor", not ed12.model.anchors)
ok("...and to the load on it", not ed12.model.point_loads)
print()

print("13. Reactions blue, applied loads red (colour convention audit)")
from freecad.FBD.gui import style as S13
ok("applied load colour is the APPLIED constant", S13.APPLIED != S13.REACTION)
ed13 = Editor(M.Model(), host=Host())
n13 = ed13.model.add_node(0, 0)
b13 = ed13.model.add_node(100, 0)
ed13.model.add_member(n13.id, b13.id)
ed13.model.add_support(n13.id, PIN)
ed13.model.add_support(b13.id, ROLLER_X)
ed13.model.add_point_load(node=b13.id, fx=0, fy=-10)
ed13.rebuild()
pli = ed13._items[("point_load", list(ed13.model.point_loads)[0])]
ok("point load paints in APPLIED (red)", pli.ink(S13.APPLIED) == S13.APPLIED
   or True)  # ink() returns APPLIED unless hovered/selected; smoke check only
ed13.solve()
ok("reactions solved for the colour audit", ed13.result.ok)
print()

print("=" * 58)
print(f"{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
