"""Cursor previews what a click-drag would do: resize at a corner, move at
an edge or on a joint, arrow otherwise. Also fixes SelectTool's prompt text,
which still described dragging a member from before the box-edge mechanism.

Run after the two earlier scripts. Safe to run twice.
"""

import io, os, py_compile, sys

here = os.path.abspath(".")
GUI = None
for _ in range(5):
    for c in (os.path.join(here, "gui"), os.path.join(here, "freecad", "FBD", "gui")):
        if os.path.isfile(os.path.join(c, "editor.py")):
            GUI = c + os.sep
            break
    if GUI:
        break
    here = os.path.dirname(here)
if GUI is None:
    sys.exit("ABORT: cannot find freecad/FBD/gui from here")
print("patching", GUI)

edits = 0


def sub(path, old, new, label):
    global edits
    s = io.open(path, encoding="utf-8").read()
    if new in s and old not in s:
        print(f"  skip  {label} (already there)")
        return
    n = s.count(old)
    if n != 1:
        sys.exit(f"ABORT [{label}] in {os.path.basename(path)}: expected 1 match, found {n}")
    io.open(path, "w", encoding="utf-8").write(s.replace(old, new))
    edits += 1
    print(f"  ok    {label}")


# =====================================================================
# 1. gui/editor.py -- hovering a lone joint previews a drag too
# =====================================================================
p = GUI + "editor.py"
sub(
    p,
    """        # Update cursor for the box corner (resize) and edge (move)
        if self.tool.name == "Select" and comp and rect \\
                and self._is_near_box_corner(scene_pos, rect):
            self.view.setCursor(QtCore.Qt.CursorShape.SizeFDiagCursor)
        elif self.tool.name == "Select" and comp and rect \\
                and self._is_near_box_edge(scene_pos, rect):
            # Open hand means grabbable, closed means held, as everywhere else.
            self.view.setCursor(QtCore.Qt.CursorShape.OpenHandCursor)
        elif self._dragging_nodes:
            self.view.setCursor(QtCore.Qt.CursorShape.ClosedHandCursor)
        else:
            self.view.setCursor(
                QtCore.Qt.CursorShape.ArrowCursor
                if self.tool.name == "Select"
                else QtCore.Qt.CursorShape.CrossCursor
            )""",
    """        # Three states, so hovering always previews what a click-drag would
        # actually do here: resize at the corner, move at the edge or on a
        # joint of its own, arrow otherwise.
        if self.tool.name == "Select" and comp and rect \\
                and self._is_near_box_corner(scene_pos, rect):
            self.view.setCursor(QtCore.Qt.CursorShape.SizeFDiagCursor)
        elif self.tool.name == "Select" and comp and rect \\
                and self._is_near_box_edge(scene_pos, rect):
            # Open hand means grabbable, closed means held, as everywhere else.
            self.view.setCursor(QtCore.Qt.CursorShape.OpenHandCursor)
        elif self._dragging_nodes:
            self.view.setCursor(QtCore.Qt.CursorShape.ClosedHandCursor)
        elif self.tool.name == "Select" and self.node_near(scene_pos) is not None:
            self.view.setCursor(QtCore.Qt.CursorShape.OpenHandCursor)
        else:
            self.view.setCursor(
                QtCore.Qt.CursorShape.ArrowCursor
                if self.tool.name == "Select"
                else QtCore.Qt.CursorShape.CrossCursor
            )""",
    "1 hovering a lone joint previews the drag cursor too",
)

# =====================================================================
# 2. gui/canvas/tools.py -- SelectTool's prompt matches what it now does
# =====================================================================
p = GUI + "canvas/tools.py"
sub(
    p,
    """class SelectTool(Tool):
    name = "Select"
    prompt = "Click to select. Drag a member (or Shift+drag a joint) to move a structure. Drag a joint to adjust it."
    snaps_to_grid = False""",
    """class SelectTool(Tool):
    name = "Select"
    prompt = (
        "Click to select. Drag a joint to move it (Shift extends to its "
        "structure). Drag the edge of a structure's box to move it all, "
        "or its corner to resize it."
    )
    snaps_to_grid = False""",
    "2 SelectTool prompt matches the box-edge/corner mechanism",
)

for f in ("editor.py", "canvas/tools.py"):
    py_compile.compile(GUI + f, doraise=True)

print(f"\n{edits} edits applied, everything compiles.")
