# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Ryan Kembrey <ryan.FreeCAD@gmail.com>
# SPDX-FileNotice: Part of the FBD addon.

import math
from PySide6 import QtCore, QtGui, QtWidgets
from .. import style as S


class Scene(QtWidgets.QGraphicsScene):
    def __init__(self, canvas):
        super().__init__()
        self.canvas = canvas
        self.setBackgroundBrush(QtGui.QBrush(S.DESK))
        self.setItemIndexMethod(QtWidgets.QGraphicsScene.ItemIndexMethod.NoIndex)

    def sheet_rect(self) -> QtCore.QRectF:
        sheet = self.canvas.model.sheet
        return QtCore.QRectF(0.0, -sheet.height, sheet.width, sheet.height)

    def drawBackground(self, painter, rect):
        super().drawBackground(painter, rect)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, False)
        sheet = self.sheet_rect()

        infinite = getattr(self.canvas, "infinite_canvas", True)
        show_sheet = getattr(self.canvas, "show_sheet", True)

        if show_sheet:
            painter.setPen(QtCore.Qt.PenStyle.NoPen)
            painter.setBrush(S.SHEET_SHADOW)
            painter.drawRect(sheet.adjusted(1.5, 1.5, 1.5, 1.5))
            painter.setBrush(S.PAPER)
            painter.drawRect(sheet)

        step = (self.canvas.model.sheet.grid or 10.0) * getattr(self.canvas, "visual_scale", 1.0)
        scale = self.canvas.view.transform().m11() or 1.0
        while step * scale < 5.0:
            step *= 5.0

        painter.save()
        if not infinite and show_sheet:
            painter.setClipRect(sheet)

        area = rect if infinite else sheet.intersected(rect)
        if not area.isEmpty():
            x = math.floor(area.left() / step) * step
            while x <= area.right():
                major = abs(x % (step * 5.0)) < 1e-6
                painter.setPen(QtGui.QPen(S.GRID_MAJOR if major else S.GRID, 0))
                painter.drawLine(QtCore.QPointF(x, area.top()),
                                 QtCore.QPointF(x, area.bottom()))
                x += step
            y = math.floor(area.top() / step) * step
            while y <= area.bottom():
                major = abs(y % (step * 5.0)) < 1e-6
                painter.setPen(QtGui.QPen(S.GRID_MAJOR if major else S.GRID, 0))
                painter.drawLine(QtCore.QPointF(area.left(), y),
                                 QtCore.QPointF(area.right(), y))
                y += step
        painter.restore()

        if show_sheet:
            painter.setPen(QtGui.QPen(S.SHEET_EDGE, 0))
            painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
            painter.drawRect(sheet)

    def drawForeground(self, painter, rect):
        super().drawForeground(painter, rect)
        show_sheet = getattr(self.canvas, "show_sheet", True)
        if not show_sheet:
            return
        sheet = self.sheet_rect()
        model = self.canvas.model
        k = self.canvas.px(1.0)
        painter.save()
        painter.translate(sheet.left() + 10.0 * k, sheet.bottom() - 10.0 * k)
        painter.scale(k, k)
        painter.setPen(QtGui.QPen(S.INK_LIGHT))
        painter.setFont(S.font(16.0, bold=True))
        painter.drawText(QtCore.QPointF(0, 0), model.sheet.title or "Free Body Diagram")
        painter.restore()


def _navigation_style() -> str:
    """Best-effort read of FreeCAD's own Navigation Style preference, so
    panning follows whatever style the rest of the application is set to.

    Falls back to the CAD default (this addon's original, hardcoded
    behaviour) when FreeCAD, the preference, or a GUI session isn't
    available -- e.g. under the offscreen test harness, where FreeCAD is a
    bare stub with no ParamGet.
    """
    try:
        import FreeCAD as App
        pref = App.ParamGet("User parameter:BaseApp/Preferences/View")
        raw = pref.GetString("NavigationStyle", "CADNavigationStyle")
    except Exception:
        raw = "CADNavigationStyle"
    raw = raw.rsplit("::", 1)[-1]        # e.g. "Gui::BlenderNavigationStyle"
    return raw[:-len("NavigationStyle")] if raw.endswith("NavigationStyle") else raw


class View(QtWidgets.QGraphicsView):
    """Drafting view with FreeCAD CAD/Blender/Touchpad/OpenCASCADE navigation styles."""

    def __init__(self, scene, canvas):
        super().__init__(scene)
        self.canvas = canvas
        self.setRenderHints(QtGui.QPainter.RenderHint.Antialiasing
                            | QtGui.QPainter.RenderHint.TextAntialiasing)
        self.setTransformationAnchor(QtWidgets.QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QtWidgets.QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setDragMode(QtWidgets.QGraphicsView.DragMode.RubberBandDrag)
        self.setMouseTracking(True)
        self.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        # Without focus the view never receives a key press, so the
        # drawing shortcuts and Ctrl+Z would go nowhere.
        self.setFocusPolicy(QtCore.Qt.FocusPolicy.StrongFocus)
        self._panning = False
        self._pan_from = QtCore.QPoint()
        self._rmb_press_pos = None
        self._rmb_panning = False
        self.nav_style = _navigation_style()
        # RMB-drag-to-pan matches FreeCAD's CAD/OpenCascade/Contact styles;
        # Blender, Touchpad, Revit and Gesture styles reserve the right
        # button for something else, so leave plain right-clicks alone there.
        self._rmb_pan_enabled = self.nav_style not in ("Blender", "Touchpad", "Revit", "Gesture")
        self._shift_panning = False
        self._shift_last_pos = QtCore.QPoint()


    def wheelEvent(self, event):
        delta = event.angleDelta().y()
        if delta == 0:
            return
        factor = pow(1.0012, delta)
        current = abs(self.transform().m11())
        target = current * factor
        if target < 0.04:
            factor = 0.04 / current
        elif target > 60.0:
            factor = 60.0 / current
        self.scale(factor, factor)
        self.canvas.zoom_changed()

    def mousePressEvent(self, event):
        button = event.button()
        modifiers = event.modifiers()
        pos = event.position().toPoint()

        is_mmb = (button == QtCore.Qt.MouseButton.MiddleButton)
        is_rmb = (button == QtCore.Qt.MouseButton.RightButton)
        is_lmb_pan = (button == QtCore.Qt.MouseButton.LeftButton and 
                      bool(modifiers & (QtCore.Qt.KeyboardModifier.ShiftModifier | QtCore.Qt.KeyboardModifier.ControlModifier)))

        if is_mmb or is_lmb_pan:
            self._shift_panning = False
            self._panning = True
            self._pan_from = pos
            self.setCursor(QtCore.Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return

        if is_rmb:
            self._rmb_press_pos = pos
            self._rmb_panning = False

        if button == QtCore.Qt.MouseButton.LeftButton:
            scene_pos = self.mapToScene(pos)
            if self.canvas.popup_hit(scene_pos):
                super().mousePressEvent(event)
                return
            if self.canvas.handle_click(scene_pos):
                event.accept()
                return

        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        pos = event.position().toPoint()
        buttons = event.buttons()
        shift_held = bool(event.modifiers() & QtCore.Qt.KeyboardModifier.ShiftModifier)

        # Hold Shift and move the cursor -- no button needed -- to pan.
        # Takes priority over hover highlighting and tool previews, and
        # steps aside the moment an explicit drag (Shift+left-drag, RMB,
        # MMB) is already panning via the block below.
        if shift_held and not buttons and not self._panning:
            if not self._shift_panning:
                self._shift_panning = True
                self._shift_last_pos = pos
                self.setCursor(QtCore.Qt.CursorShape.ClosedHandCursor)
            else:
                delta = pos - self._shift_last_pos
                self._shift_last_pos = pos
                self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() - delta.x())
                self.verticalScrollBar().setValue(self.verticalScrollBar().value() - delta.y())
            event.accept()
            return
        elif self._shift_panning:
            self._shift_panning = False
            self.unsetCursor()

        # FreeCAD / OpenCASCADE Right-Click Drag-Pan (only for nav styles that
        # reserve RMB for this; Blender/Touchpad/Revit/Gesture leave a plain
        # right-click alone so it stays a normal context-menu click)
        if (self._rmb_pan_enabled and (buttons & QtCore.Qt.MouseButton.RightButton)
                and self._rmb_press_pos):
            dist = (pos - self._rmb_press_pos).manhattanLength()
            if dist > 5 and not self._rmb_panning:
                self._rmb_panning = True
                self._panning = True
                self._pan_from = pos
                self.setCursor(QtCore.Qt.CursorShape.ClosedHandCursor)

        if self._panning:
            delta = pos - self._pan_from
            self._pan_from = pos
            self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() - delta.x())
            self.verticalScrollBar().setValue(self.verticalScrollBar().value() - delta.y())
            event.accept()
            return

        self.canvas.handle_move(self.mapToScene(pos))
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        button = event.button()
        if button == QtCore.Qt.MouseButton.MiddleButton or (self._panning and not self._rmb_panning):
            self._panning = False
            self.unsetCursor()
            event.accept()
            return

        if button == QtCore.Qt.MouseButton.RightButton:
            if self._rmb_panning:
                self._rmb_panning = False
                self._panning = False
                self.unsetCursor()
                event.accept()
                return

        super().mouseReleaseEvent(event)
        self.canvas.handle_release()

    def mouseDoubleClickEvent(self, event):
        """Open the on-page editor for whatever was double-clicked.

        Qt's default sends this straight to the item under the cursor,
        and no item implements it, so without this the event is lost
        and Editor.handle_double_click is never reached.
        """
        if event.button() == QtCore.Qt.MouseButton.LeftButton:
            scene_pos = self.mapToScene(event.position().toPoint())
            if not self.canvas.popup_hit(scene_pos):
                if self.canvas.handle_double_click(scene_pos):
                    event.accept()
                    return
        super().mouseDoubleClickEvent(event)

    def contextMenuEvent(self, event):
        scene_pos = self.mapToScene(event.pos())
        for item in self.scene().items(scene_pos):
            if type(item).__name__ == "SupportItem":
                super().contextMenuEvent(event)
                return
        if hasattr(self.canvas, "show_context_menu"):
            self.canvas.show_context_menu(event.globalPos())

    def keyPressEvent(self, event):
        if self.canvas.handle_key(event.key(), event.modifiers()):
            event.accept()
            return
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event):
        if event.key() == QtCore.Qt.Key.Key_Shift and self._shift_panning:
            self._shift_panning = False
            self.unsetCursor()
        super().keyReleaseEvent(event)

    def pixels_to_scene(self, pixels: float) -> float:
        scale = abs(self.transform().m11()) or 1.0
        return pixels / scale
