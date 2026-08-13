# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Ryan Kembrey <ryan.FreeCAD@gmail.com>
# SPDX-FileNotice: Part of the FBD addon.

"""The editor: toolbar, paper, properties, results.

The model is the single source of truth. Graphics items are views onto it and
are rebuilt on change, which is fast enough for diagram-sized problems and
removes a whole class of synchronisation bugs.

Selecting anything shows its properties inline on the right, so nothing needs a
modal dialog while drawing.
"""

import math
import FreeCAD as App  # type: ignore

from PySide6 import QtCore, QtGui, QtWidgets

from . import style as S
from .canvas.scene import Scene, View
from .canvas import items as I
from .canvas import popup as P
from .canvas.tools import build_tools
from ..engine import model as M
from ..engine import checks, statics
from .motion import MotionController, MotionBar, MotorItem, ActuatorItem


DIAGRAM_MODES = [
    ("none", "No diagram"),
    ("moment", "Bending moment"),
    ("shear", "Shear force"),
    ("axial", "Axial force"),
]


class TreeSelectionObserver:
    """Synchronizes FreeCAD's native Tree View selection with the FBD Canvas."""

    def __init__(self, editor):
        self.editor = editor

    def addSelection(self, doc_name, obj_name, sub_name, pnt):
        if not self.editor or getattr(self.editor, "_suspend_selection", False):
            return
        try:
            if self.editor.host and self.editor.host.obj and self.editor.host.obj.Document:
                if self.editor.host.obj.Document.Name != doc_name:
                    return
        except Exception:
            pass

        if obj_name.startswith("Structure_"):
            try:
                doc = self.editor.host.obj.Document
                struct_obj = doc.getObject(obj_name)
                if struct_obj and hasattr(struct_obj, "Group"):
                    self.editor._suspend_selection = True
                    try:
                        self.editor.scene.clearSelection()
                        for child in struct_obj.Group:
                            kind, ident = self._parse_obj_name(child.Name)
                            if kind and ident is not None:
                                item = self.editor._items.get((kind, ident))
                                if item:
                                    item.setSelected(True)
                    finally:
                        self.editor._suspend_selection = False
            except Exception:
                pass
            return

        kind, ident = self._parse_obj_name(obj_name)
        if kind and ident is not None:
            self.editor._suspend_selection = True
            try:
                self.editor.select_entity(kind, ident)
            finally:
                self.editor._suspend_selection = False

    def clearSelection(self, doc_name):
        if not self.editor or getattr(self.editor, "_suspend_selection", False):
            return
        try:
            if self.editor.host and self.editor.host.obj and self.editor.host.obj.Document:
                if self.editor.host.obj.Document.Name != doc_name:
                    return
        except Exception:
            pass
        self.editor._suspend_selection = True
        try:
            self.editor.scene.clearSelection()
        finally:
            self.editor._suspend_selection = False

    def _parse_obj_name(self, name):
        if name.startswith("Node_"):
            return "node", int(name.split("_")[1])
        elif name.startswith("Member_"):
            return "member", int(name.split("_")[1])
        elif name.startswith("Support_"):
            return "support", int(name.split("_")[1])
        elif name.startswith("Force_"):
            return "point_load", int(name.split("_")[1])
        elif name.startswith("Motor_"):
            return "motor", int(name.split("_")[1])
        elif name.startswith("Actuator_"):
            return "actuator", int(name.split("_")[1])
        return None, None


class DisplayHUD(QtWidgets.QFrame):
    """Sleek, floating interactive UI palette overlaid on the canvas."""

    def __init__(self, editor):
        super().__init__()
        self.editor = editor
        self.setObjectName("HUD")
        self.setStyleSheet("""
            QFrame#HUD {
                background-color: rgba(255, 255, 255, 240);
                border: 1px solid #c3c8d2;
                border-radius: 8px;
            }
            QPushButton {
                background: transparent;
                border: none;
                border-radius: 5px;
                padding: 6px 12px;
                color: #5b6270;
                font-weight: bold;
                font-family: "DejaVu Sans", sans-serif;
                font-size: 11px;
            }
            QPushButton:hover {
                background: rgba(0, 0, 0, 12);
                color: #1d2025;
            }
            QPushButton:checked {
                background: #e3f2fd;
                color: #1565c0;
            }
            QPushButton:disabled {
                color: #b8bec9;
            }
            QFrame#separator {
                background: #c3c8d2;
                min-width: 1px;
                max-width: 1px;
                margin: 6px 4px;
            }
        """)

        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(2)

        def make_btn(text, tooltip, callback):
            btn = QtWidgets.QPushButton(text)
            btn.setToolTip(tooltip)
            btn.setCheckable(True)
            btn.clicked.connect(lambda _checked=False, cb=callback: cb())
            layout.addWidget(btn)
            return btn

        self.btn_lbl = make_btn("Labels", "Toggle Joint Labels", editor.toggle_labels)
        self.btn_rxn = make_btn("Reactions", "Toggle Reactions", editor.toggle_reactions)
        self.btn_tbl = make_btn("Table", "Toggle Results Table", editor.toggle_results_table)
        self.btn_comp = make_btn(
            "Components", "Show forces as separate X and Y arrows",
            editor.toggle_components)

        sep1 = QtWidgets.QFrame()
        sep1.setObjectName("separator")
        layout.addWidget(sep1)

        self.btn_ax = make_btn("Axial", "Axial Force Diagram", editor.toggle_axial)
        self.btn_sh = make_btn("Shear", "Shear Force Diagram", editor.toggle_shear)
        self.btn_mo = make_btn("Moment", "Bending Moment Diagram", editor.toggle_moment)
        self.btn_ov = make_btn(
            "Overlay", "Overlay Diagrams on Structure", editor.toggle_diagrams_overlay
        )

        sep2 = QtWidgets.QFrame()
        sep2.setObjectName("separator")
        layout.addWidget(sep2)

        self.btn_sheet = make_btn("Sheet", "Toggle Paper Sheet Boundary", editor.toggle_sheet)

    def sync_state(self):
        # A motion frame carries axial force, which the whole toolbar can
        # already show; it never carries shear or bending, since a
        # mechanism member is a rigid pin-jointed link rather than an
        # elastic beam, so those two stay tied to a real static solve.
        display = getattr(self.editor, "display_result", self.editor.result)
        has_res = display is not None and getattr(display, "ok", False)
        has_bending = self.editor.result is not None and getattr(self.editor.result, "ok", False)

        buttons_state = [
            (self.btn_lbl, self.editor.show_labels, True),
            (self.btn_rxn, self.editor.show_reactions, has_res),
            (self.btn_tbl, getattr(self.editor, "show_results_table", True), has_res),
            (self.btn_comp, getattr(self.editor, "show_components", False), True),
            (self.btn_ax, self.editor.show_axial, has_res),
            (self.btn_sh, self.editor.show_shear, has_bending),
            (self.btn_mo, self.editor.show_moment, has_bending),
            (
                self.btn_ov,
                self.editor.diagrams_overlay,
                has_res
                and (self.editor.show_axial or self.editor.show_shear or self.editor.show_moment),
            ),
            (self.btn_sheet, getattr(self.editor, "show_sheet", True), True),
        ]

        for btn, checked, enabled in buttons_state:
            btn.blockSignals(True)
            btn.setChecked(checked)
            btn.setEnabled(enabled)
            btn.blockSignals(False)


class Editor(QtWidgets.QWidget, MotionController):
    # Panning should feel unrestricted in every direction, like Sketcher's
    # 2D editor, regardless of sheet size or current zoom. Kept as a
    # constant so _infinite_scene_rect() and any future readers agree.
    INFINITE_EXTENT = 2_000_000.0  # mm: half-extent of the pannable area

    def __init__(self, model: M.Model, host=None, parent=None):
        super().__init__(parent)
        self.model = model
        self.host = host  # object with save(model), may be None
        self.result = None
        self.diagnosis = None
        self._items = {}  # (kind, id) -> item
        self._undo = []
        self._redo = []
        self._preview = None
        self._snap_marker = None
        self._popup = None  # the one open on-page form or None
        self._popup_widget = None  # keeps its Python wrapper alive too
        self._suspend_selection = False
        self._dragging_nodes = []
        self._drag_ref_node = None
        self._drag_start_mouse_pos = (0.0, 0.0)
        self._drag_initial_positions = {}
        self._drag_started = False

        self.show_labels = False  # joint names off by default: less clutter
        self.label_loads = True
        self.label_reactions = True
        self.show_reactions = True
        self.show_results_table = True
        self.show_moment = False
        self.show_shear = False
        self.show_axial = False
        self.diagrams_overlay = False
        self.infinite_canvas = True
        self.show_sheet = True
        self.diagram_user_dragged = {"axial": False, "shear": False, "moment": False}
        self.diagram_activation_order = []
        self.diagram_activation_order = []
        self.results_table_pos = None
        self.results_table_scale = 1.0
        self.show_components = False   # forces as separate X/Y arrows, not one angled arrow
        self.observers = []  # callables notified when anything changes
        self.snap_enabled = True
        self.default_force = 1000.0  # N
        self.default_moment = 1.0e5  # N.mm
        self.default_line_load = 1.0  # N/mm

        self.scene = Scene(self)
        self.view = View(self.scene, self)
        self.diagram_offsets = {"axial": None, "shear": None, "moment": None}
        self.structure_overlay = I.StructureBoundsOverlay(self)
        self.scene.addItem(self.structure_overlay)
        self.projection_overlay = I.ProjectionLinesOverlay(self)
        self.scene.addItem(self.projection_overlay)

        self.diagram_overlays = {
            k: I.SingleDiagramOverlay(self, k) for k in ("axial", "shear", "moment")
        }
        for diag in self.diagram_overlays.values():
            self.scene.addItem(diag)

        self.result_overlay = I.ResultOverlay(self)
        self.scene.addItem(self.result_overlay)
        self.results_overlay = I.ResultsTableOverlay(self)
        self.scene.addItem(self.results_overlay)

        self.tools = build_tools(self)
        self.tool = self.tools[0]

        self.init_motion()

        self._build_ui()
        self.rebuild()
        QtCore.QTimer.singleShot(0, self.fit)
        if App.GuiUp:
            try:
                import FreeCADGui as Gui

                self._tree_observer = TreeSelectionObserver(self)
                Gui.Selection.addObserver(self._tree_observer)
            except Exception:
                self._tree_observer = None

    # ---------------------------------------------------------------- ui

    def _build_ui(self):
        """Drawing surface + modern floating UI Overlay."""
        # Use QGridLayout to effortlessly float widgets over the QGraphicsView
        outer = QtWidgets.QGridLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self.view, 0, 0)

        # Instantiate HUD
        self.hud = DisplayHUD(self)

        self.motion_bar = MotionBar(self)
        bar_row = QtWidgets.QHBoxLayout()
        bar_row.addStretch()
        bar_row.addWidget(self.motion_bar)
        bar_row.addStretch()

        # Center horizontally
        hud_h_layout = QtWidgets.QHBoxLayout()
        hud_h_layout.addStretch()
        hud_h_layout.addWidget(self.hud)
        hud_h_layout.addStretch()

        # Push to the top
        hud_v_layout = QtWidgets.QVBoxLayout()
        hud_v_layout.setContentsMargins(20, 20, 20, 20)
        hud_v_layout.addLayout(hud_h_layout)
        hud_v_layout.addStretch()
        hud_v_layout.addLayout(bar_row)

        outer.addLayout(hud_v_layout, 0, 0)

        self.scene.selectionChanged.connect(self._on_selection)
        self.set_tool(self.tools[0])

    # ---- geometry helpers ------------------------------------------------

    def px(self, pixels: float) -> float:
        """Scene units for a screen distance, so drawing stays screen-constant."""
        return self.view.pixels_to_scene(pixels)

    def tool_names(self):
        return [t.name for t in self.tools]

    def set_tool_by_name(self, name):
        for tool in self.tools:
            if tool.name == name:
                self.set_tool(tool)
                return True
        return False

    def current_tool_name(self):
        return self.tool.name if self.tool else ""

    # ------------------------------------------------------------ tools

    def set_tool(self, tool):
        if getattr(self, "tool", None) is tool:
            tool.activate()
            self._sync_toolbar_highlight()
            return
        if getattr(self, "tool", None):
            self.tool.deactivate()
        self.tool = tool
        select_mode = tool is self.tools[0]
        self.view.setDragMode(
            QtWidgets.QGraphicsView.DragMode.RubberBandDrag
            if select_mode
            else QtWidgets.QGraphicsView.DragMode.NoDrag
        )
        self._apply_tool_cursor(tool, select_mode)
        tool.activate()
        self._sync_toolbar_highlight()

    def _apply_tool_cursor(self, tool, select_mode):
        """Crosshair with the active tool's icon riding beside it, so a
        placement tool always shows what it's about to place."""
        if select_mode:
            self.view.setCursor(QtCore.Qt.CursorShape.ArrowCursor)
            return
        pix = self._tool_cursor_pixmap(tool.name)
        if pix is not None:
            self.view.setCursor(QtGui.QCursor(pix, 4, 4))
        else:
            self.view.setCursor(QtCore.Qt.CursorShape.CrossCursor)

    def _tool_cursor_pixmap(self, tool_name):
        cache = self.__dict__.setdefault("_cursor_cache", {})
        if tool_name in cache:
            return cache[tool_name]
        pix = None
        try:
            from .commands import icon_path, TOOL_ICONS

            icon_file = TOOL_ICONS.get(tool_name)
            badge = QtGui.QPixmap(icon_path(icon_file)) if icon_file else QtGui.QPixmap()
            if not badge.isNull():
                badge = badge.scaled(
                    16,
                    16,
                    QtCore.Qt.AspectRatioMode.KeepAspectRatio,
                    QtCore.Qt.TransformationMode.SmoothTransformation,
                )
                canvas = QtGui.QPixmap(26, 26)
                canvas.fill(QtCore.Qt.GlobalColor.transparent)
                painter = QtGui.QPainter(canvas)
                painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
                painter.setPen(QtGui.QPen(QtGui.QColor(30, 30, 30), 1.3))
                painter.drawLine(QtCore.QPointF(4, 1), QtCore.QPointF(4, 7))
                painter.drawLine(QtCore.QPointF(1, 4), QtCore.QPointF(7, 4))
                painter.drawPixmap(9, 9, badge)
                painter.end()
                pix = canvas
        except Exception:
            pix = None
        cache[tool_name] = pix
        return pix

    def _sync_toolbar_highlight(self):
        """Mirror the active drawing tool onto the toolbar buttons, the way
        Sketcher highlights whichever tool is running."""
        try:
            from .commands import sync_tool_actions

            sync_tool_actions(self.tool.name if self.tool else None)
        except Exception:
            pass

    def set_prompt(self, text):
        """Tool hints belong in the status bar, not printed on the page."""
        self._prompt = text
        try:
            import FreeCADGui as Gui  # type: ignore

            Gui.getMainWindow().statusBar().showMessage(text, 6000)
        except Exception:
            pass
        self.notify()

    def clear_preview(self):
        if self._preview is not None:
            self.scene.removeItem(self._preview)
            self._preview = None

    def set_preview_line(self, model_a, model_b):
        a = I.to_scene(*model_a, scale=self.global_scale)
        b = I.to_scene(*model_b, scale=self.global_scale)
        if self._preview is None:
            self._preview = self.scene.addLine(
                QtCore.QLineF(a, b), S.pen(S.PREVIEW, S.THIN_W * 2, QtCore.Qt.PenStyle.DashLine)
            )
            self._preview.setZValue(60)
        else:
            self._preview.setLine(QtCore.QLineF(a, b))

    # ------------------------------------------------------- interaction

    @property
    def _dragging_node(self):
        return self._dragging_nodes[0] if self._dragging_nodes else None

    @_dragging_node.setter
    def _dragging_node(self, val):
        if val is None:
            self._dragging_nodes = []
            self._drag_ref_node = None
        else:
            self._dragging_nodes = [val]
            self._drag_ref_node = val
            if val in self.model.nodes:
                self._drag_initial_positions = {
                    val: (self.model.nodes[val].x, self.model.nodes[val].y)
                }

    def _component_bounds(self, comp_nodes) -> QtCore.QRectF:
        # Real scene position, the same as everything drawn. This used to
        # multiply by scale instead of dividing, which only ever matched
        # reality while unit_scale sat at 1 -- true for a freehand diagram
        # that never touched it, but every sketch import now sets a real
        # scale, and the box would land far from wherever the structure
        # actually is: invisible in practice, not merely misplaced.
        pts = [I.to_scene(self.model.nodes[n].x, self.model.nodes[n].y, self.global_scale)
               for n in comp_nodes if n in self.model.nodes]
        if not pts:
            return QtCore.QRectF()
        xs = [p.x() for p in pts]
        ys = [p.y() for p in pts]
        pad = 18.0
        return QtCore.QRectF(
            min(xs) - pad,
            min(ys) - pad,
            (max(xs) - min(xs)) + 2 * pad,
            (max(ys) - min(ys)) + 2 * pad,
        )

    def _is_near_box_corner(self, scene_pos, rect, tol_pixels=14.0) -> bool:
        tol = self.px(tol_pixels)
        corner = QtCore.QPointF(rect.right(), rect.bottom())
        return math.hypot(scene_pos.x() - corner.x(), scene_pos.y() - corner.y()) <= tol

    def _is_near_box_edge(self, scene_pos, rect, tol_pixels=8.0) -> bool:
        tol = self.px(tol_pixels)
        outer = rect.adjusted(-tol, -tol, tol, tol)
        inner = rect.adjusted(tol, tol, -tol, -tol)
        return outer.contains(scene_pos) and not inner.contains(scene_pos)

    def _find_component_at(self, scene_pos):
        from ..engine.checks import _components

        # Padded by the corner hit-zone: that hit-zone is a circle centred
        # on the rect's own corner, so half of it sits just outside the
        # strict rect -- exactly where hovering to grab a corner naturally
        # lands. Without this, that whole outer half was rejected before
        # the corner check ever ran, which is why the cursor never changed.
        tol = self.px(14.0)
        components = _components(self.model)
        for comp in components:
            rect = self._component_bounds(comp)
            if rect.adjusted(-tol, -tol, tol, tol).contains(scene_pos):
                return comp, rect
        return None, None

    def _connected_component(self, start_node_id: int) -> set:
        model = self.model
        if start_node_id not in model.nodes:
            return set()
        adjacency = {nid: set() for nid in model.nodes}
        for m in model.members.values():
            if m.start in adjacency and m.end in adjacency:
                adjacency[m.start].add(m.end)
                adjacency[m.end].add(m.start)
        visited = set()
        stack = [start_node_id]
        while stack:
            cur = stack.pop()
            if cur in visited:
                continue
            visited.add(cur)
            stack.extend(adjacency[cur] - visited)
        return visited

    def _start_node_drag(self, node_ids, ref_node_id, start_model_pos):
        self._dragging_nodes = list(node_ids)
        self._drag_ref_node = ref_node_id
        self._drag_start_mouse_pos = start_model_pos
        self._drag_initial_positions = {
            nid: (self.model.nodes[nid].x, self.model.nodes[nid].y)
            for nid in node_ids
            if nid in self.model.nodes
        }
        self._drag_started = False

    def _start_component_resize(self, comp_nodes, rect, start_model_pos):
        """Grab the corner of one structure's own bounding box.

        Unlike the whole-diagram handle, this rescales that structure's
        real x, y directly: the same kind of operation as calibrating a
        member, just aimed at one piece of a multi-structure sheet rather
        than the whole page's display scale.
        """
        del rect, start_model_pos
        nodes = {n: (self.model.nodes[n].x, self.model.nodes[n].y)
                 for n in comp_nodes if n in self.model.nodes}
        if not nodes:
            return
        xs = [xy[0] for xy in nodes.values()]
        ys = [xy[1] for xy in nodes.values()]
        self._resizing_component = set(nodes)
        self._resize_comp_initial = nodes
        # Anchor the opposite corner so it stays put while the dragged
        # corner follows the cursor: the ordinary meaning of a resize.
        self._resize_comp_anchor = (min(xs), max(ys))
        self._resize_comp_corner0 = (max(xs), min(ys))
        self.push_undo("Resize structure")

    def _update_component_resize(self, scene_pos):
        ax, ay = self._resize_comp_anchor
        cx0, cy0 = self._resize_comp_corner0
        start_len = math.hypot(cx0 - ax, cy0 - ay)
        if start_len < 1e-6:
            return
        mx, my = I.to_model(scene_pos, scale=self.global_scale)
        new_len = math.hypot(mx - ax, my - ay)
        ratio = max(0.05, min(20.0, new_len / start_len))
        for nid, (ix, iy) in self._resize_comp_initial.items():
            node = self.model.nodes.get(nid)
            if node:
                node.x = ax + (ix - ax) * ratio
                node.y = ay + (iy - ay) * ratio
        self.invalidate_result()
        self.refresh_geometry()

    def snap(self, scene_pos: QtCore.QPointF) -> QtCore.QPointF:
        if not self.snap_enabled:
            return scene_pos
        tol = self.view.pixels_to_scene(S.SNAP_PIXELS)
        best, best_d = None, tol
        for node in self.model.nodes.values():
            p = I.to_scene(node.x, node.y, scale=self.global_scale)
            d = math.hypot(p.x() - scene_pos.x(), p.y() - scene_pos.y())
            if d < best_d:
                best, best_d = p, d
        if best is not None:
            return QtCore.QPointF(best)
        step = self.model.sheet.grid or 10.0
        return QtCore.QPointF(
            round(scene_pos.x() / step) * step, round(scene_pos.y() / step) * step
        )

    def node_near(self, scene_pos):
        tol = self.view.pixels_to_scene(S.SNAP_PIXELS)
        best, best_d = None, tol
        sc = self.global_scale
        for node in self.model.nodes.values():
            p = I.to_scene(node.x, node.y, scale=sc)
            d = math.hypot(p.x() - scene_pos.x(), p.y() - scene_pos.y())
            if d < best_d:
                best, best_d = node.id, d
        return best

    def member_near(self, scene_pos):
        tol = self.view.pixels_to_scene(S.SNAP_PIXELS)
        best, best_d = None, tol
        for member in self.model.members.values():
            a = self.model.nodes.get(member.start)
            b = self.model.nodes.get(member.end)
            if not a or not b:
                continue
            pa, pb = (I.to_scene(a.x, a.y, self.global_scale),
                      I.to_scene(b.x, b.y, self.global_scale))
            d = _point_segment_distance(scene_pos, pa, pb)
            if d < best_d:
                best, best_d = member.id, d
        return best

    def anchor_near(self, scene_pos):
        tol = self.view.pixels_to_scene(S.SNAP_PIXELS)
        best, best_d = None, tol
        for anchor in self.model.anchors.values():
            xy = self.model.anchor_xy(anchor)
            if xy is None:
                continue
            p = I.to_scene(*xy, scale=self.global_scale)
            d = math.hypot(p.x() - scene_pos.x(), p.y() - scene_pos.y())
            if d < best_d:
                best, best_d = anchor.id, d
        return best

    def member_point_at(self, scene_pos, member_id):
        """Fraction t (0..1) of the member nearest scene_pos, for placing a
        point on it where the user actually clicked."""
        member = self.model.members.get(member_id)
        if member is None:
            return 0.5
        a = self.model.nodes.get(member.start)
        b = self.model.nodes.get(member.end)
        if not a or not b:
            return 0.5
        pa, pb = (I.to_scene(a.x, a.y, self.global_scale),
                  I.to_scene(b.x, b.y, self.global_scale))
        dx, dy = pb.x() - pa.x(), pb.y() - pa.y()
        length_sq = dx * dx + dy * dy
        if length_sq < 1e-12:
            return 0.5
        t = ((scene_pos.x() - pa.x()) * dx + (scene_pos.y() - pa.y()) * dy) / length_sq
        return max(0.0, min(1.0, t))

    def attachment_scene_pos(self, node_id, anchor_id):
        """Scene position for a load, whichever kind of attachment it has."""
        xy = self.model.attachment_xy(node_id, anchor_id)
        return I.to_scene(*xy, scale=self.global_scale) if xy else QtCore.QPointF()

    def handle_click(self, scene_pos):
        if self._popup is not None:
            self.close_popup()
            return True
        snapped = self.snap(scene_pos) if self.tool.snaps_to_grid else scene_pos
        model_pos = I.to_model(snapped, scale=self.global_scale)
        if self.tool.click(snapped, model_pos):
            return True

        if self.tool.name == "Select":
            modifiers = QtWidgets.QApplication.keyboardModifiers()
            shift_held = bool(modifiers & QtCore.Qt.KeyboardModifier.ShiftModifier)

            # 1. Check the bounding box: corner resizes the structure,
            # edge drags the whole thing.
            comp, rect = self._find_component_at(scene_pos)
            if comp and rect:
                if self._is_near_box_corner(scene_pos, rect):
                    self._start_component_resize(comp, rect, model_pos)
                    return True
                if self._is_near_box_edge(scene_pos, rect):
                    ref_node = next(iter(comp))
                    self._start_node_drag(comp, ref_node, model_pos)
                    return True

            # 2. Check joint
            node_id = self.node_near(scene_pos)
            if node_id is not None:
                if shift_held:
                    comp_nodes = self._connected_component(node_id)
                else:
                    comp_nodes = {node_id}
                self._start_node_drag(comp_nodes, node_id, model_pos)
                return False

        return False

    def handle_move(self, scene_pos):
        if getattr(self, "_resizing_component", None):
            self._update_component_resize(scene_pos)
            self.view.setCursor(QtCore.Qt.CursorShape.SizeFDiagCursor)
            return

        if self._dragging_nodes:
            if not any(nid in self.model.nodes for nid in self._dragging_nodes):
                self._dragging_nodes = []
                self._drag_ref_node = None
                self._drag_started = False
                return

        # Update hovered structure for blue dashed box
        comp, rect = self._find_component_at(scene_pos)
        self._hovered_component = comp
        if hasattr(self, "structure_overlay"):
            try:
                self.structure_overlay.update()
            except RuntimeError:
                pass

        print("DEBUG tool=", self.tool.name, "comp=", bool(comp), "rect=", rect,
              "corner=", bool(comp and rect and self._is_near_box_corner(scene_pos, rect)))
        # Three states, so hovering always previews what a click-drag would
        # actually do here: resize at the corner, move at the edge or on a
        # joint of its own, arrow otherwise.
        if self.tool.name == "Select" and comp and rect \
                and self._is_near_box_corner(scene_pos, rect):
            self.view.setCursor(QtCore.Qt.CursorShape.SizeFDiagCursor)
        elif self.tool.name == "Select" and comp and rect \
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
            )

        if self._dragging_nodes:
            raw_x, raw_y = I.to_model(scene_pos, scale=self.global_scale)
            start_x, start_y = self._drag_start_mouse_pos
            dx = raw_x - start_x
            dy = raw_y - start_y

            if not self._drag_started:
                if math.hypot(dx, dy) < self.view.pixels_to_scene(2.0):
                    return
                desc = "Move structure" if len(self._dragging_nodes) > 1 else "Move joint"
                self.push_undo(desc)
                self._drag_started = True

            if self.snap_enabled and self._drag_ref_node in self._drag_initial_positions:
                ref_init_x, ref_init_y = self._drag_initial_positions[self._drag_ref_node]
                ref_target_scene = I.to_scene(
                    ref_init_x + dx, ref_init_y + dy, scale=self.global_scale
                )
                snapped_scene = self.snap(ref_target_scene)
                snap_x, snap_y = I.to_model(snapped_scene, scale=self.global_scale)
                final_dx = snap_x - ref_init_x
                final_dy = snap_y - ref_init_y
            else:
                final_dx = dx
                final_dy = dy

            for nid in self._dragging_nodes:
                node = self.model.nodes.get(nid)
                if node and nid in self._drag_initial_positions:
                    init_x, init_y = self._drag_initial_positions[nid]
                    node.x = init_x + final_dx
                    node.y = init_y + final_dy

            self.invalidate_result()
            self.refresh_geometry()
            return

        snapped = self.snap(scene_pos) if self.tool.snaps_to_grid else scene_pos
        self.tool.move(snapped, I.to_model(snapped, scale=self.global_scale))
        self._update_snap_marker(scene_pos)

    def handle_release(self):
        if getattr(self, "_resizing_component", None):
            self._resizing_component = None
            self._resize_comp_initial = {}
            self.view.unsetCursor()
            self.model_changed()
            return
        if self._dragging_node is not None and self._drag_started:
            self.model_changed()
        self._dragging_nodes = []
        self._drag_ref_node = None
        self._drag_start_mouse_pos = (0.0, 0.0)
        self._drag_initial_positions = {}
        self._drag_started = False

    def item_at(self, scene_pos):
        """Find entity item under scene_pos using screen-pixel tolerance."""
        # 1. Check motor
        for (kind, ident), item in self._items.items():
            if kind == "motor":
                try:
                    if item.contains(item.mapFromScene(scene_pos)):
                        return item
                except Exception:
                    pass

        # 2. Check joint
        nid = self.node_near(scene_pos)
        if nid is not None and ("node", nid) in self._items:
            return self._items[("node", nid)]

        # 3. Check anchor
        aid = self.anchor_near(scene_pos)
        if aid is not None and ("anchor", aid) in self._items:
            return self._items[("anchor", aid)]

        # 4. Check supports
        for (kind, ident), item in self._items.items():
            if kind == "support":
                try:
                    if item.contains(item.mapFromScene(scene_pos)):
                        return item
                except Exception:
                    pass

        # 5. Check loads
        for (kind, ident), item in self._items.items():
            if kind in ("point_load", "moment_load"):
                try:
                    if item.contains(item.mapFromScene(scene_pos)):
                        return item
                except Exception:
                    pass

        # 6. Check actuator or member
        mid = self.member_near(scene_pos)
        if mid is not None:
            ac = self.model.actuator_on(mid)
            if ac and ("actuator", ac.id) in self._items:
                return self._items[("actuator", ac.id)]
            if ("member", mid) in self._items:
                return self._items[("member", mid)]

        return None

    def handle_double_click(self, scene_pos):
        item = self.item_at(scene_pos)
        if item is None:
            return False
        self.scene.clearSelection()
        item.setSelected(True)
        item.open_editor(scene_pos)
        return True

    def handle_key(self, key, modifiers=None):
        # The diagram keeps its own snapshot stack, because the whole model
        # serializes to one blob. FreeCAD's document undo does not reach into
        # it, so the shortcuts have to be handled here or Ctrl+Z silently
        # undoes something else entirely.
        control = bool(modifiers) and bool(
            modifiers & QtCore.Qt.KeyboardModifier.ControlModifier)
        if control:
            shift = bool(modifiers & QtCore.Qt.KeyboardModifier.ShiftModifier)
            if key == QtCore.Qt.Key.Key_Z and not shift:
                self.undo()
                return True
            if key == QtCore.Qt.Key.Key_Y or (key == QtCore.Qt.Key.Key_Z and shift):
                self.redo()
                return True
        if key == QtCore.Qt.Key.Key_Escape:
            if self._popup is not None:
                self.close_popup()
                return True
            self.tool.cancel()
            if self.tool is not self.tools[0]:
                self.set_tool(self.tools[0])
            return True
        if key in (QtCore.Qt.Key.Key_Delete, QtCore.Qt.Key.Key_Backspace):
            self.delete_selection()
            return True
        if self.tool.key(key):
            return True
        return False

    def _update_snap_marker(self, scene_pos):
        if not self.snap_enabled or self.tool is self.tools[0]:
            if self._snap_marker is not None:
                self.scene.removeItem(self._snap_marker)
                self._snap_marker = None
            return
        p = self.snap(scene_pos)
        r = self.view.pixels_to_scene(4.0)
        if self._snap_marker is None:
            self._snap_marker = self.scene.addEllipse(
                QtCore.QRectF(p.x() - r, p.y() - r, 2 * r, 2 * r),
                S.pen(S.SNAP, S.THIN_W * 1.5),
                QtGui.QBrush(QtCore.Qt.BrushStyle.NoBrush),
            )
            self._snap_marker.setZValue(70)
        else:
            self._snap_marker.setRect(QtCore.QRectF(p.x() - r, p.y() - r, 2 * r, 2 * r))

    def zoom_changed(self):
        if self._snap_marker is not None:
            self.scene.removeItem(self._snap_marker)
            self._snap_marker = None
        # Geometric items size themselves from px(), so they must repaint.
        self.refresh_geometry()

    # ---------------------------------------------------------- rebuild

    def _discard_items(self):
        """Detach every item safely.

        Items must stop accepting hover and selection before they leave the
        scene: Qt can otherwise deliver an event to an item that is already
        being destroyed, which crashes rather than raising.
        """
        for item in list(self._items.values()):
            try:
                item.setAcceptHoverEvents(False)
                item.setSelected(False)
                self.scene.removeItem(item)
            except RuntimeError:
                pass  # already gone
        self._items.clear()

    def rebuild(self):
        """Rebuild every graphics item from the model."""
        self._suspend_selection = True
        self._discard_items()

        for nid in self.model.nodes:
            self._add(I.NodeItem(nid, self), "node", nid)
        for mid in self.model.members:
            self._add(I.MemberItem(mid, self), "member", mid)
        for aid in self.model.anchors:
            self._add(I.AnchorItem(aid, self), "anchor", aid)
        for sid in self.model.supports:
            self._add(I.SupportItem(sid, self), "support", sid)
        for lid in self.model.point_loads:
            self._add(I.PointLoadItem(lid, self), "point_load", lid)
        for lid in self.model.moment_loads:
            self._add(I.MomentLoadItem(lid, self), "moment_load", lid)
        for lid in self.model.line_loads:
            self._add(I.LineLoadItem(lid, self), "line_load", lid)
        for mid in self.model.motors:
            self._add(MotorItem(mid, self), "motor", mid)
        for aid in self.model.actuators:
            self._add(ActuatorItem(aid, self), "actuator", aid)

        sheet = self.scene.sheet_rect()
        self.scene.setSceneRect(sheet.adjusted(-60, -60, 60, 60))
        self._suspend_selection = False
        self.refresh_geometry()
        self.notify()

    def _add(self, item, kind, ident):
        self.scene.addItem(item)
        self._items[(kind, ident)] = item

    def refresh_geometry(self):
        for item in list(self._items.values()):
            try:
                if getattr(item, "anchored", False):
                    item.sync()
                item.prepareGeometryChange()
                item.update()
            except RuntimeError:
                pass

        if hasattr(self, "structure_overlay"):
            try:
                self.structure_overlay.prepareGeometryChange()
                self.structure_overlay.update()
            except RuntimeError:
                pass

        if hasattr(self, "projection_overlay"):
            try:
                self.projection_overlay.prepareGeometryChange()
                self.projection_overlay.update()
            except RuntimeError:
                pass

        if hasattr(self, "diagram_overlays"):
            for diag in self.diagram_overlays.values():
                try:
                    diag.sync_pos()
                    diag.prepareGeometryChange()
                    diag.update()
                except RuntimeError:
                    pass

        try:
            self.result_overlay.prepareGeometryChange()
            self.result_overlay.update()
        except RuntimeError:
            pass

        try:
            self.motion_overlay.prepareGeometryChange()
            self.motion_overlay.update()
            self.motion_bar.sync_state()
        except RuntimeError:
            pass

        if hasattr(self, "results_overlay"):
            try:
                self.results_overlay.prepareGeometryChange()
                self.results_overlay.update()
            except RuntimeError:
                pass

        if hasattr(self, "hud"):
            try:
                self.hud.sync_state()
            except RuntimeError:
                pass

        self.scene.update()

    def model_changed(self):
        self.invalidate_result()
        self.rebuild()
        self.save()
        self.notify()

    def notify(self):
        for callback in list(self.observers):
            try:
                callback()
            except Exception:
                pass

    def invalidate_result(self):
        self.invalidate_motion()
        if self.result is not None:
            self.result = None
            self.notify()

    def fit(self):
        rect = self.scene.sheet_rect()
        if self.model.nodes:
            # Scene coordinates, not raw model ones: a sketch-imported
            # diagram's unit_scale can be large, so a joint's real x, y is
            # real engineering mm while the page itself is drawn in much
            # smaller paper mm. Comparing the two directly is exactly what
            # made fit() zoom out to the size of the steel instead of the
            # size of the page.
            sc = self.unit_scale
            pts = [I.to_scene(n.x, n.y, sc) for n in self.model.nodes.values()]
            xs = [p.x() for p in pts]
            ys = [p.y() for p in pts]
            drawn = QtCore.QRectF(
                QtCore.QPointF(min(xs), min(ys)), QtCore.QPointF(max(xs), max(ys))
            )
            drawn = drawn.adjusted(-40, -40, 40, 40)
            rect = rect.united(drawn) if drawn.width() < rect.width() * 3 else drawn
        self.view.fitInView(rect, QtCore.Qt.AspectRatioMode.KeepAspectRatio)
        self.zoom_changed()

    def arrow_length(self, magnitude):
        """Arrow length in paper mm. Scaled against the largest load so the
        diagram stays legible, with a floor so small loads stay visible."""
        biggest = self._biggest_force()
        if biggest <= 0:
            return S.ARROW_LEN
        ratio = abs(magnitude) / biggest
        return max(S.ARROW_MIN, S.ARROW_LEN * (0.45 + 0.55 * ratio))

    def _biggest_force(self):
        values = [p.magnitude() for p in self.model.point_loads.values()]
        if self.display_result and self.display_result.ok:
            values += [r.magnitude() for r in self.display_result.reactions.values()]
        return max(values) if values else 0.0

    # ------------------------------------------------------------ edits

    def push_undo(self, description):
        self._undo.append((description, self.model.to_dict()))
        del self._undo[:-60]
        self._redo.clear()

    def undo(self):
        if not self._undo:
            self.set_prompt("Nothing left to undo.")
            return
        description, snapshot = self._undo.pop()
        self._redo.append((description, self.model.to_dict()))
        self._apply_snapshot(snapshot)
        self.set_prompt(f"Undo: {description}")

    def redo(self):
        if not self._redo:
            self.set_prompt("Nothing to redo.")
            return
        description, snapshot = self._redo.pop()
        self._undo.append((description, self.model.to_dict()))
        self._apply_snapshot(snapshot)
        self.set_prompt(f"Redo: {description}")

    def _apply_snapshot(self, snapshot):
        restored = M.Model.from_dict(snapshot)
        self._suspend_selection = True
        self.model.__dict__.update(restored.__dict__)
        self._dragging_node = None
        self._hovered_component = None
        self.invalidate_result()
        self.rebuild()
        self.save()
        self.notify()

    def delete_selection(self):
        """Delete everything selected.

        Nothing touches the model or the scene synchronously: a rubber-band
        select-all followed by Delete used to mutate the model immediately and
        only defer the item teardown, leaving a window where the graphics
        items still sat in the scene pointing at model objects that no longer
        existed. Any repaint in that window (and Qt's own rubber-band and
        selection bookkeeping can trigger one) crashed rather than raised,
        because it's a Qt-internal spatial structure holding a stale pointer,
        not a Python-level null check. The whole operation, model mutation and
        scene rebuild together, now happens in a single deferred call with the
        viewport frozen, so nothing can paint mid-mutation.
        """
        targets = [
            (kind, ident) for (kind, ident), item in self._items.items() if item.isSelected()
        ]
        if not targets:
            return
        self.tool.cancel()
        QtCore.QTimer.singleShot(0, lambda: self._do_delete(targets))

    def _do_delete(self, targets):
        self.close_popup()  # it may hold a reference to a deleted object
        self.view.setUpdatesEnabled(False)
        try:
            self._suspend_selection = True
            try:
                self.scene.clearSelection()
            except RuntimeError:
                pass
            self._dragging_nodes = []
            self._drag_started = False
            self.push_undo("Delete")
            for kind, ident in targets:
                self.model.delete(kind, ident)  # cascades; repeats are harmless
            self._suspend_selection = False
            self.model_changed()
        finally:
            self.view.setUpdatesEnabled(True)
        self.view.viewport().update()

    def select_entity(self, kind, ident):
        item = self._items.get((kind, ident))
        if item is None:
            return
        self.scene.clearSelection()
        item.setSelected(True)

    def save(self):
        if self.host is not None:
            try:
                self.host.save(self.model)
            except Exception:
                pass

    # ---- on-page popups ---------------------------------------------------

    def open_popup(self, scene_pos, widget):
        """Show an on-page form near scene_pos. Exactly one is ever open.

        PySide6's QGraphicsScene.addWidget() does not reliably keep the
        embedded widget's own Python wrapper alive: if the caller's only
        reference to it goes out of scope (as it does when the widget is
        built inside a method and the popup opened from a different call
        frame, e.g. an item's own open_editor), Python can garbage-collect
        the wrapper while the proxy is still using it in C++, leaving the
        proxy holding a dangling pointer that segfaults on the next removal
        or repaint. Holding our own reference here for as long as the popup
        is open avoids that.
        """
        self.close_popup()
        self.push_undo("Edit")  # one undo step for the whole editing session
        proxy = self.scene.addWidget(widget)
        proxy.setZValue(500)
        proxy.setFlag(QtWidgets.QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations, True)
        offset = self.px(28.0)
        proxy.setPos(scene_pos.x() + offset, scene_pos.y() + offset)
        self._popup = proxy
        self._popup_widget = widget
        return proxy

    def close_popup(self):
        if self._popup is not None:
            proxy = self._popup
            self._popup = None
            self._popup_widget = None
            try:
                proxy.setVisible(False)
                proxy.deleteLater()
            except (RuntimeError, AttributeError):
                pass

    def popup_hit(self, scene_pos) -> bool:
        return self._popup is not None and self._popup.sceneBoundingRect().contains(scene_pos)

    def edit(self, apply, rebuild=False):
        """Apply a mutation made from an on-page form or handle: repaint (or
        fully rebuild if the change affects which symbol is drawn) and
        persist. Undo was already pushed once when the popup opened."""
        apply()
        self.invalidate_result()
        if rebuild:
            self.rebuild()
        else:
            self.refresh_geometry()
        self.save()
        self.notify()

    # ------------------------------------------------------------ solve

    @property
    def unit_scale(self):
        return getattr(self.model.sheet, "unit_scale", 1.0)

    @unit_scale.setter
    def unit_scale(self, val):
        self.model.sheet.unit_scale = max(1e-6, float(val))
        self.refresh_geometry()

    @property
    def global_scale(self):
        return self.unit_scale

    def prompt_first_member_calibration(self, member):
        drawn_len = self.model.member_length(member)
        if drawn_len < 1e-3:
            return

        form = P.PopupForm()
        form.add_note(
            "<b>Calibrate Diagram Scale</b><br>Set physical length for this first member:"
        )

        default_val = 1000.0 if drawn_len < 500 else round(drawn_len, -1)
        spin = form.add_spin(
            "Length", default_val, lambda v: None, lo=1.0, hi=1e9, decimals=1, suffix="mm"
        )

        def apply_calibration():
            target_len = spin.value()
            if target_len <= 0 or drawn_len <= 0:
                return

            S = target_len / drawn_len

            for n in self.model.nodes.values():
                n.x *= S
                n.y *= S

            self.model.sheet.unit_scale *= S
            self.model.sheet.calibrated = True

            self.close_popup()
            self.invalidate_result()
            self.rebuild()
            self.save()
            self.set_prompt(f"Diagram calibrated: Member length = {target_len:,.1f} mm")

        btn = QtWidgets.QPushButton("Set Dimension")
        btn.setStyleSheet("""
            QPushButton {
                background: #1565c0; color: white; font-weight: bold;
                border-radius: 4px; padding: 6px 12px; font-size: 11px;
            }
            QPushButton:hover { background: #0d47a1; }
        """)
        btn.clicked.connect(apply_calibration)
        form._form.addRow(btn)

        a = self.model.nodes.get(member.start)
        b = self.model.nodes.get(member.end)
        if a and b:
            sc = self.unit_scale
            mid_scene = I.to_scene((a.x + b.x) / 2.0, (a.y + b.y) / 2.0, sc)
            self.open_popup(mid_scene, form)

    def toggle_sheet(self):
        self.show_sheet = not getattr(self, "show_sheet", True)
        self.refresh_geometry()

    def toggle_components(self):
        self.show_components = not self.show_components
        self.refresh_geometry()

    def toggle_labels(self):
        self.show_labels = not self.show_labels
        self.refresh_geometry()

    def toggle_reactions(self):
        self.show_reactions = not self.show_reactions
        self.refresh_geometry()

    def toggle_results_table(self):
        self.show_results_table = not getattr(self, "show_results_table", True)
        self.refresh_geometry()

    def toggle_moment(self):
        self.show_moment = not self.show_moment
        if self.show_moment:
            if "moment" not in self.diagram_activation_order:
                self.diagram_activation_order.append("moment")
        else:
            if "moment" in self.diagram_activation_order:
                self.diagram_activation_order.remove("moment")
            self.diagram_user_dragged["moment"] = False
            self.diagram_offsets["moment"] = None
        self.refresh_geometry()

    def toggle_shear(self):
        self.show_shear = not self.show_shear
        if self.show_shear:
            if "shear" not in self.diagram_activation_order:
                self.diagram_activation_order.append("shear")
        else:
            if "shear" in self.diagram_activation_order:
                self.diagram_activation_order.remove("shear")
            self.diagram_user_dragged["shear"] = False
            self.diagram_offsets["shear"] = None
        self.refresh_geometry()

    def toggle_axial(self):
        self.show_axial = not self.show_axial
        if self.show_axial:
            if "axial" not in self.diagram_activation_order:
                self.diagram_activation_order.append("axial")
        else:
            if "axial" in self.diagram_activation_order:
                self.diagram_activation_order.remove("axial")
            self.diagram_user_dragged["axial"] = False
            self.diagram_offsets["axial"] = None
        self.refresh_geometry()

    def toggle_diagrams_overlay(self):
        self.diagrams_overlay = not self.diagrams_overlay
        self.refresh_geometry()

    def export_pdf_prompt(self):
        import PySide6.QtWidgets as QtWidgets

        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Export PDF", "diagram.pdf", "PDF Files (*.pdf)"
        )
        if path:
            self.export_pdf(path)

    def show_context_menu(self, global_pos):
        import PySide6.QtWidgets as QtWidgets

        menu = QtWidgets.QMenu(self)

        menu.addAction("Export PDF...").triggered.connect(self.export_pdf_prompt)
        menu.addSeparator()

        lbl_act = menu.addAction("Show Joint Labels")
        lbl_act.setCheckable(True)
        lbl_act.setChecked(self.show_labels)
        lbl_act.triggered.connect(self.toggle_labels)

        rxn_act = menu.addAction("Show Reactions")
        rxn_act.setCheckable(True)
        rxn_act.setChecked(self.show_reactions)
        rxn_act.triggered.connect(self.toggle_reactions)

        tbl_act = menu.addAction("Show Results Table")
        tbl_act.setCheckable(True)
        tbl_act.setChecked(getattr(self, "show_results_table", True))
        tbl_act.triggered.connect(self.toggle_results_table)

        menu.addSeparator()
        diag_menu = menu.addMenu("Internal Diagrams")

        act = diag_menu.addAction("Axial Force")
        act.setCheckable(True)
        act.setChecked(self.show_axial)
        act.triggered.connect(self.toggle_axial)

        act = diag_menu.addAction("Shear Force")
        act.setCheckable(True)
        act.setChecked(self.show_shear)
        act.triggered.connect(self.toggle_shear)

        act = diag_menu.addAction("Bending Moment")
        act.setCheckable(True)
        act.setChecked(self.show_moment)
        act.triggered.connect(self.toggle_moment)

        diag_menu.addSeparator()
        act = diag_menu.addAction("Overlay on Structure")
        act.setCheckable(True)
        act.setChecked(self.diagrams_overlay)
        act.triggered.connect(self.toggle_diagrams_overlay)

        menu.exec(global_pos)

    def export_pdf(self, path):
        from PySide6 import QtGui, QtCore

        printer = QtGui.QPdfWriter(path)
        w = self.model.sheet.width
        h = self.model.sheet.height
        printer.setPageSize(QtGui.QPageSize(QtCore.QSizeF(w, h), QtGui.QPageSize.Unit.Millimeter))
        printer.setResolution(300)
        painter = QtGui.QPainter(printer)
        target = QtCore.QRectF(0, 0, printer.width(), printer.height())
        self.scene.render(painter, target, self.scene.sheet_rect())
        painter.end()
        self.set_prompt(f"Exported to {path}")

    def solve(self):
        """Run the analysis. Returns the diagnosis so callers can report it."""
        self.diagnosis = checks.check(self.model)
        if not self.diagnosis.solvable:
            self.result = None
        else:
            self.result = statics.solve(self.model, run_checks=False)
        self.refresh_geometry()
        self.notify()
        return self.diagnosis

    def status_text(self):
        """One line describing the model, for the task panel."""
        diagnosis = self.diagnosis or checks.check(self.model)
        self.diagnosis = diagnosis
        if diagnosis.errors:
            return diagnosis.errors[0].message, "error"
        if self.result and self.result.ok:
            if self.result.equilibrium_error < 1e-6:
                return diagnosis.summary(), "ok"
            return (f"Equilibrium residual {self.result.equilibrium_error:.2e}", "error")
        if diagnosis.warnings:
            return diagnosis.warnings[0].message, "warning"
        return diagnosis.summary(), "ok"

    def result_rows(self):
        """Result table contents, as (item, value, note) tuples."""
        rows = []
        res = self.display_result
        if not (res and res.ok):
            return rows
        for nid, reaction in sorted(res.reactions.items()):
            name = self.model.entity_label(nid)
            if reaction.magnitude() > 1e-6:
                rows.append(
                    (
                        f"Reaction {name}",
                        I.fmt(reaction.magnitude(), "N"),
                        f"Fx {reaction.fx:,.1f}, Fy {reaction.fy:,.1f}",
                    )
                )
            if abs(reaction.m) > 1e-6:
                rows.append(
                    (
                        f"Fixing moment {name}",
                        I.fmt(reaction.m, "N.mm"),
                        "counter-clockwise positive",
                    )
                )
        for mid, forces in sorted(res.members.items()):
            member = self.model.members.get(mid)
            if member is None:
                continue
            if abs(forces.axial_max) > 1e-6:
                rows.append(
                    (
                        f"Axial {member.label}",
                        I.fmt(forces.axial_max, "N"),
                        "tension" if forces.axial_max > 0 else "compression",
                    )
                )
            if abs(forces.shear_max) > 1e-6:
                rows.append((f"Shear {member.label}", I.fmt(forces.shear_max, "N"), "maximum"))
            if abs(forces.moment_max) > 1e-6:
                rows.append((f"Moment {member.label}", I.fmt(forces.moment_max, "N.mm"), "maximum"))
        return rows

    def selected_entity(self):
        for (kind, ident), item in self._items.items():
            if item.isSelected():
                return kind, ident
        return None, None

    def closeEvent(self, event):
        if hasattr(self, "_tree_observer") and self._tree_observer:
            try:
                import FreeCADGui as Gui

                Gui.Selection.removeObserver(self._tree_observer)
            except Exception:
                pass
        super().closeEvent(event)

    def _on_selection(self):
        if self._suspend_selection:
            return
        self._suspend_selection = True
        try:
            self.refresh_geometry()
            self.notify()
            self._sync_to_tree_view()
        finally:
            self._suspend_selection = False

    def _sync_to_tree_view(self):
        if not App.GuiUp or self.host is None or not hasattr(self.host, "obj"):
            return
        try:
            diagram_obj = self.host.obj
            if diagram_obj is None or diagram_obj.Document is None:
                return

            doc_name = diagram_obj.Document.Name

            selected_entities = [
                (kind, ident) for (kind, ident), item in self._items.items() if item.isSelected()
            ]

            kind_map = {
                "node": "Node",
                "member": "Member",
                "support": "Support",
                "point_load": "Force",
                "motor": "Motor",
                "actuator": "Actuator",
            }

            import FreeCADGui as Gui

            Gui.Selection.clearSelection(doc_name)

            for kind, ident in selected_entities:
                prefix = kind_map.get(kind)
                if prefix:
                    obj_name = f"{prefix}_{ident}"
                    obj = diagram_obj.Document.getObject(obj_name)
                    if obj:
                        Gui.Selection.addSelection(doc_name, obj.Name)
        except Exception:
            pass

    def _spin(self, value, setter, lo=-1e12, hi=1e12, decimals=3, suffix=""):
        box = QtWidgets.QDoubleSpinBox()
        box.setRange(lo, hi)
        box.setDecimals(decimals)
        box.setValue(float(value))
        box.setKeyboardTracking(False)
        if suffix:
            box.setSuffix(" " + suffix)

        def changed(v):
            self.push_undo("Edit")
            setter(v)
            self.invalidate_result()
            self.rebuild()
            self.save()

        box.valueChanged.connect(changed)
        return box

    def _props_node(self, ident):
        node = self.model.nodes.get(ident)
        if not node:
            return
        name = QtWidgets.QLineEdit(node.label)
        name.editingFinished.connect(
            lambda: (setattr(node, "label", name.text()), self.refresh_geometry())
        )
        self.props_form.addRow("Name", name)
        self.props_form.addRow(
            "X", self._spin(node.x, lambda v: setattr(node, "x", v), suffix="mm")
        )
        self.props_form.addRow(
            "Y", self._spin(node.y, lambda v: setattr(node, "y", v), suffix="mm")
        )

    def _props_member(self, ident):
        member = self.model.members.get(ident)
        if not member:
            return
        name = QtWidgets.QLineEdit(member.label)
        name.editingFinished.connect(
            lambda: (setattr(member, "label", name.text()), self.refresh_geometry())
        )
        self.props_form.addRow("Name", name)
        length = self.model.member_length(member)
        self.props_form.addRow("Length", QtWidgets.QLabel(f"{length:.4g} mm"))
        self.props_form.addRow(
            "EA",
            self._spin(
                member.EA, lambda v: setattr(member, "EA", v), lo=1.0, decimals=1, suffix="N"
            ),
        )
        self.props_form.addRow(
            "EI",
            self._spin(
                member.EI, lambda v: setattr(member, "EI", v), lo=1.0, decimals=1, suffix="N.mm2"
            ),
        )
        note = QtWidgets.QLabel("Stiffness only affects indeterminate structures.")
        note.setWordWrap(True)
        note.setStyleSheet("color:#5b6270")
        self.props_form.addRow(note)

    def _props_support(self, ident):
        support = self.model.supports.get(ident)
        if not support:
            return
        combo = QtWidgets.QComboBox()
        for kind in M.SUPPORT_KINDS:
            combo.addItem(M.SUPPORT_LABELS[kind], kind)
        combo.setCurrentIndex(M.SUPPORT_KINDS.index(support.kind))

        def kind_changed(index):
            self.push_undo("Change support")
            support.kind = combo.itemData(index)
            if support.kind == M.SPRING and not support.reaction_components():
                support.ky = 1000.0
            self.invalidate_result()
            self.rebuild()
            self.save()
            self.select_entity("support", ident)

        combo.currentIndexChanged.connect(kind_changed)
        self.props_form.addRow("Type", combo)
        self.props_form.addRow(
            "Angle",
            self._spin(
                support.angle,
                lambda v: setattr(support, "angle", v),
                lo=-360,
                hi=360,
                decimals=1,
                suffix="deg",
            ),
        )
        if support.kind == M.SPRING:
            self.props_form.addRow(
                "kx",
                self._spin(
                    support.kx,
                    lambda v: setattr(support, "kx", v),
                    lo=0.0,
                    decimals=2,
                    suffix="N/mm",
                ),
            )
            self.props_form.addRow(
                "ky",
                self._spin(
                    support.ky,
                    lambda v: setattr(support, "ky", v),
                    lo=0.0,
                    decimals=2,
                    suffix="N/mm",
                ),
            )
            self.props_form.addRow(
                "kr",
                self._spin(
                    support.kr,
                    lambda v: setattr(support, "kr", v),
                    lo=0.0,
                    decimals=2,
                    suffix="N.mm/rad",
                ),
            )
        if self.result and self.result.ok:
            reaction = self.result.reactions.get(support.holds)
            if reaction:
                self.props_form.addRow(
                    "Reaction", QtWidgets.QLabel(f"{reaction.fx:.4g}, {reaction.fy:.4g} N")
                )

    def _props_point_load(self, ident):
        load = self.model.point_loads.get(ident)
        if not load:
            return
        self.props_form.addRow(
            "Fx", self._spin(load.fx, lambda v: setattr(load, "fx", v), decimals=2, suffix="N")
        )
        self.props_form.addRow(
            "Fy", self._spin(load.fy, lambda v: setattr(load, "fy", v), decimals=2, suffix="N")
        )
        magnitude = QtWidgets.QLabel(f"{load.magnitude():.4g} N")
        self.props_form.addRow("Magnitude", magnitude)
        angle = math.degrees(math.atan2(load.fy, load.fx)) if load.magnitude() else 0.0
        self.props_form.addRow("Direction", QtWidgets.QLabel(f"{angle:.1f} deg"))

    def _props_moment_load(self, ident):
        load = self.model.moment_loads.get(ident)
        if not load:
            return
        self.props_form.addRow(
            "M", self._spin(load.m, lambda v: setattr(load, "m", v), decimals=2, suffix="N.mm")
        )
        note = QtWidgets.QLabel("Counter-clockwise is positive.")
        note.setStyleSheet("color:#5b6270")
        self.props_form.addRow(note)

    def _props_line_load(self, ident):
        load = self.model.line_loads.get(ident)
        if not load:
            return
        self.props_form.addRow(
            "q", self._spin(load.q, lambda v: setattr(load, "q", v), decimals=4, suffix="N/mm")
        )
        combo = QtWidgets.QComboBox()
        for key, label in (
            ("y", "Global vertical"),
            ("x", "Global horizontal"),
            ("perp", "Perpendicular to member"),
        ):
            combo.addItem(label, key)
        index = max(
            0,
            [c[0] for c in (("y", 0), ("x", 1), ("perp", 2))].index(load.direction)
            if load.direction in ("y", "x", "perp")
            else 0,
        )
        combo.setCurrentIndex(index)

        def changed(i):
            self.push_undo("Edit line load")
            load.direction = combo.itemData(i)
            self.invalidate_result()
            self.rebuild()
            self.save()

        combo.currentIndexChanged.connect(changed)
        self.props_form.addRow("Direction", combo)
        member = self.model.members.get(load.member)
        if member:
            total = abs(load.q) * self.model.member_length(member)
            self.props_form.addRow("Total", QtWidgets.QLabel(f"{total:.4g} N"))

    # ----------------------------------------------------------- display

    def _on_snap(self, on):
        self.snap_enabled = on


def _point_segment_distance(p, a, b):
    ax, ay = a.x(), a.y()
    bx, by = b.x(), b.y()
    dx, dy = bx - ax, by - ay
    length_sq = dx * dx + dy * dy
    if length_sq < 1e-12:
        return math.hypot(p.x() - ax, p.y() - ay)
    t = max(0.0, min(1.0, ((p.x() - ax) * dx + (p.y() - ay) * dy) / length_sq))
    return math.hypot(p.x() - (ax + t * dx), p.y() - (ay + t * dy))
