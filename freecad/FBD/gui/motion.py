# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Ryan Kembrey <ryan.FreeCAD@gmail.com>
# SPDX-FileNotice: Part of the FBD addon.

"""The motion layer: drivers on the page, and a way to watch them run.

Everything here follows the same two rules as the rest of the canvas. The
model is never mutated to animate: the running pose lives in an overlay that
draws on top, so scrubbing a simulation can never dirty the document or land
in the undo stack. And symbols are drawn in device pixels through
ItemIgnoresTransformations, so a motor is the same size on screen at every
zoom, exactly like a support.

Drawing conventions, continuing the ones already in use:

    motor           circle with a curved arrow, ground hatching under it,
                    drawn at the joint it turns about
    actuator        barrel and rod along the member, with the stroke shown
    trace           the path a joint sweeps, thin and dashed
    running pose    the linkage drawn in the motion colour over the diagram,
                    which stays visible underneath as the home position
"""

import math

from PySide6 import QtCore, QtGui, QtWidgets

from . import style as S
from .canvas import popup as P
from .canvas.items import _Item, _arrow_head, _hatch, px_text, fmt, to_scene
from ..engine.results import StaticResult, Reaction, MemberForces
from .canvas.tools import Tool
from .engine_bridge import edit
from ..engine import model as M
from ..engine import kinematics


# Motion has its own colour so a running pose is never mistaken for the
# drawn diagram or for a solved result. Teal sits clear of red (applied),
# blue (reaction) and green (internal).
MOTION = QtGui.QColor("#00897b")
MOTION_GHOST = QtGui.QColor(0, 137, 123, 60)
TRACE = QtGui.QColor(0, 137, 123, 130)
DRIVER = QtGui.QColor("#ef6c00")
# Driver effort is a force like any other, so it reads in the applied-load red
# rather than inventing a sixth colour for it.
FORCE = S.APPLIED


# === items


class MotorItem(_Item):
    kind = "motor"
    anchored = True
    R = 17.0  # pixels

    def __init__(self, ident, canvas):
        super().__init__(ident, canvas)
        self.setZValue(22)
        self.setToolTip("Motor. Double-click to set its speed and sweep.")
        self.sync()

    def motor(self):
        return self.model.motors.get(self.ident)

    def anchor_point(self):
        mo = self.motor()
        if not mo:
            return QtCore.QPointF()
        n = self.model.nodes.get(mo.node)
        return to_scene(n.x, n.y, self.sc()) if n else QtCore.QPointF()

    def boundingRect(self):
        r = self.R * 2.4
        return QtCore.QRectF(-r, -r, 2 * r, 2 * r)

    def shape(self):
        p = QtGui.QPainterPath()
        p.addEllipse(QtCore.QPointF(0, 0), self.R * 1.4, self.R * 1.4)
        return p

    def paint(self, painter, option, widget=None):
        mo = self.motor()
        if not mo:
            return
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        col = self.ink(DRIVER)
        r = self.R
        painter.setPen(QtGui.QPen(col, 1.6))
        painter.setBrush(QtGui.QColor(255, 255, 255, 220))
        painter.drawEllipse(QtCore.QPointF(0, 0), r, r)

        # Which way it turns, drawn the same way a couple is drawn.
        ccw = mo.speed >= 0
        rect = QtCore.QRectF(-r * 0.62, -r * 0.62, r * 1.24, r * 1.24)
        painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
        painter.drawArc(rect, 40 * 16, (-250 * 16) if ccw else (250 * 16))
        end = math.radians(-(40 + (-250 if ccw else 250)))
        hx, hy = r * 0.62 * math.cos(end), r * 0.62 * math.sin(end)
        tangent = end + (math.pi / 2 if ccw else -math.pi / 2)
        path = QtGui.QPainterPath()
        _arrow_head(path, QtCore.QPointF(hx, hy), math.cos(tangent), math.sin(tangent), r * 0.42)
        painter.setBrush(col)
        painter.setPen(QtCore.Qt.PenStyle.NoPen)
        painter.drawPath(path)

        # It needs something to react against, so it stands on the ground.
        painter.setPen(QtGui.QPen(col, 1.3))
        painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
        a = QtCore.QPointF(-r * 1.15, r * 1.25)
        b = QtCore.QPointF(r * 1.15, r * 1.25)
        painter.drawLine(a, b)
        _hatch(painter, a, b, r * 0.42, count=6)

        if self.canvas.label_loads:
            painter.setFont(S.font(12.0))
            painter.setPen(QtGui.QPen(col))
            result = getattr(self.canvas, "motion_result", None)
            torque = None
            if result and result.ok and result.frames:
                torque = result.frame_at(getattr(self.canvas, "motion_time", 0.0)).effort.get(mo.id)
            if torque:
                text = fmt(abs(torque), "N.mm") + (" ccw" if torque > 0 else " cw")
            else:
                text = f"{abs(mo.speed):g} deg/s"
                if mo.motion == M.SWEEP:
                    text += f", sweep {abs(mo.sweep):g}"
            width = QtGui.QFontMetricsF(S.font(12.0)).horizontalAdvance(text)
            painter.drawText(QtCore.QPointF(-width / 2, -r - 8.0), text)

    def open_editor(self, scene_pos):
        mo = self.motor()
        if mo is None:
            return
        form = P.PopupForm()
        form.add_spin(
            "Speed",
            mo.speed,
            lambda v: self.canvas.edit(lambda: setattr(mo, "speed", v)),
            lo=-100000,
            hi=100000,
            decimals=2,
            suffix="deg/s",
            tooltip="Counter-clockwise is positive.",
        )
        form.add_combo(
            "Motion",
            [(M.CONTINUOUS, "Continuous"), (M.SWEEP, "Sweep back and forth")],
            mo.motion,
            lambda v: self.canvas.edit(lambda: setattr(mo, "motion", v), rebuild=True),
        )
        if mo.motion == M.SWEEP:
            form.add_spin(
                "Sweep",
                mo.sweep,
                lambda v: self.canvas.edit(lambda: setattr(mo, "sweep", v)),
                lo=1.0,
                hi=360.0,
                decimals=1,
                suffix="deg",
                tooltip="Either side of the starting angle.",
            )
        member = self.model.members.get(mo.member)
        if member:
            form.add_readonly("Drives", member.label)
        result = getattr(self.canvas, "motion_result", None)
        if result and result.ok:
            peak = result.peak_effort().get(mo.id)
            if peak is not None:
                form.add_readonly("Peak torque", fmt(peak, "N.mm"))
        form.add_note(
            "The joint under a motor must be pinned: a motor needs something to turn against."
        )
        self.canvas.open_popup(self.anchor_point(), form)


class ActuatorItem(_Item):
    """Drawn along its member, in scene space, because it is the member."""

    kind = "actuator"

    def __init__(self, ident, canvas):
        super().__init__(ident, canvas)
        self.setZValue(12)
        self.setToolTip("Linear actuator. Double-click to set stroke and speed.")

    def actuator(self):
        return self.model.actuators.get(self.ident)

    def ends(self):
        ac = self.actuator()
        member = self.model.members.get(ac.member) if ac else None
        if not member:
            return None
        a = self.model.nodes.get(member.start)
        b = self.model.nodes.get(member.end)
        if not a or not b:
            return None
        return to_scene(a.x, a.y, self.sc()), to_scene(b.x, b.y, self.sc())

    def boundingRect(self):
        pair = self.ends()
        if not pair:
            return QtCore.QRectF()
        a, b = pair
        pad = self.canvas.px(16.0)
        return QtCore.QRectF(a, b).normalized().adjusted(-pad, -pad, pad, pad)

    def shape(self):
        pair = self.ends()
        p = QtGui.QPainterPath()
        if not pair:
            return p
        a, b = pair
        p.moveTo(a)
        p.lineTo(b)
        stroker = QtGui.QPainterPathStroker()
        stroker.setWidth(self.canvas.px(12.0))
        return stroker.createStroke(p)

    def paint(self, painter, option, widget=None):
        pair = self.ends()
        if not pair:
            return
        a, b = pair
        dx, dy = b.x() - a.x(), b.y() - a.y()
        length = math.hypot(dx, dy) or 1.0
        ux, uy = dx / length, dy / length
        nx, ny = -uy, ux
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        col = self.ink(DRIVER)
        half = self.canvas.px(5.0)

        # Barrel over the first 55 per cent, rod for the rest: the shorthand
        # every hydraulic schematic uses.
        cut = 0.55
        bx = QtCore.QPointF(a.x() + dx * cut, a.y() + dy * cut)
        pen = QtGui.QPen(col, 2.0)
        pen.setCosmetic(True)
        painter.setPen(pen)
        painter.setBrush(QtGui.QColor(255, 255, 255, 210))
        barrel = QtGui.QPolygonF(
            [
                QtCore.QPointF(a.x() + nx * half, a.y() + ny * half),
                QtCore.QPointF(bx.x() + nx * half, bx.y() + ny * half),
                QtCore.QPointF(bx.x() - nx * half, bx.y() - ny * half),
                QtCore.QPointF(a.x() - nx * half, a.y() - ny * half),
            ]
        )
        painter.drawPolygon(barrel)
        painter.drawLine(bx, b)

        if self.canvas.label_loads:
            ac = self.actuator()
            mid = QtCore.QPointF((a.x() + b.x()) / 2.0, (a.y() + b.y()) / 2.0)
            text, colour = self._label(ac)
            px_text(
                painter,
                self.canvas,
                QtCore.QPointF(
                    mid.x() + nx * self.canvas.px(12.0), mid.y() + ny * self.canvas.px(12.0)
                ),
                text,
                colour,
                12.0,
            )

    def _label(self, ac):
        """Stroke and speed until it has been run, force once it has.

        Sizing a ram is a force question, so the moment there is an answer the
        force takes the label and the travel moves to the popup. It is the
        force at the frame on screen, not the peak, so scrubbing through the
        stroke shows where the demand actually is.
        """
        result = getattr(self.canvas, "motion_result", None)
        if not (result and result.ok and result.frames):
            return f"{ac.stroke:g} mm at {abs(ac.speed):g} mm/s", self.ink(DRIVER)
        frame = result.frame_at(getattr(self.canvas, "motion_time", 0.0))
        force = frame.effort.get(ac.id)
        if force is None:
            # A held frame at a limit position: the force there is unbounded,
            # so quoting a number would be a lie.
            return "at its limit", self.ink(DRIVER)
        if abs(force) < 1e-6:
            return "no load on it", S.INK_LIGHT
        way = "push" if force > 0 else "pull"
        return f"{fmt(abs(force), 'N')} {way}", self.ink(FORCE)

    def open_editor(self, scene_pos):
        ac = self.actuator()
        if ac is None:
            return
        form = P.PopupForm()
        form.add_spin(
            "Stroke",
            ac.stroke,
            lambda v: self.canvas.edit(lambda: setattr(ac, "stroke", v)),
            lo=-100000,
            hi=100000,
            decimals=1,
            suffix="mm",
            tooltip="How far it travels. Negative retracts first.",
        )
        form.add_spin(
            "Speed",
            ac.speed,
            lambda v: self.canvas.edit(lambda: setattr(ac, "speed", v)),
            lo=0.0,
            hi=100000,
            decimals=2,
            suffix="mm/s",
        )
        form.add_combo(
            "Motion",
            [
                (M.CYCLE, "Out and back, repeating"),
                (M.EXTEND, "Out once, then hold"),
                (M.SINE, "Smooth (sine)"),
            ],
            ac.motion,
            lambda v: self.canvas.edit(lambda: setattr(ac, "motion", v)),
        )
        member = self.model.members.get(ac.member)
        if member:
            form.add_readonly("Closed length", f"{self.model.member_length(member):,.1f} mm")
        result = getattr(self.canvas, "motion_result", None)
        if result and result.ok:
            peak = result.peak_effort().get(ac.id)
            if peak is not None:
                way = "push" if peak > 0 else "pull"
                form.add_readonly("Peak force", f"{fmt(abs(peak), 'N')} {way}")
                form.add_readonly("Force now", self._label(ac)[0])
                form.add_note(
                    "Force needed to drive the applied loads, from virtual "
                    "work: positive pushes, negative pulls. Size the ram on "
                    "the peak, and add your own margin for friction and for "
                    "getting it moving, which this does not include."
                )
            else:
                form.add_note("Run the motion to see the force it needs.")
        else:
            form.add_note("Run the motion to see the force it needs.")
        self.canvas.open_popup(scene_pos, form)


class MotionOverlay(QtWidgets.QGraphicsItem):
    """The running pose, traces and ghosts, drawn over the drawn diagram.

    Reads canvas.motion_result and canvas.motion_time only. The model is never
    touched, so playing a simulation cannot dirty the document.
    """

    def __init__(self, canvas):
        super().__init__()
        self.canvas = canvas
        self.setZValue(45)
        self.setAcceptedMouseButtons(QtCore.Qt.MouseButton.NoButton)

    def sc(self):
        sheet = getattr(getattr(self.canvas, "model", None), "sheet", None)
        return getattr(sheet, "unit_scale", 1.0) or 1.0

    def boundingRect(self):
        result = getattr(self.canvas, "motion_result", None)
        model = self.canvas.model
        xs, ys = [], []
        if result and result.frames:
            for f in result.frames:
                for x, y in f.positions.values():
                    xs.append(x)
                    ys.append(-y)
        for n in model.nodes.values():
            xs.append(n.x)
            ys.append(-n.y)
        if not xs:
            return QtCore.QRectF()
        sc = self.sc() or 1.0
        return QtCore.QRectF(
            min(xs) / sc, min(ys) / sc, (max(xs) - min(xs)) / sc, (max(ys) - min(ys)) / sc
        ).adjusted(-60, -60, 60, 60)

    def paint(self, painter, option, widget=None):
        result = getattr(self.canvas, "motion_result", None)
        if not result or not result.ok or not result.frames:
            return
        if not getattr(self.canvas, "show_motion", True):
            return
        model = self.canvas.model
        sc = self.sc()
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)

        def point(pos):
            return to_scene(pos[0], pos[1], sc)

        # Traces first, so the linkage sits on top of its own path.
        if model.motion.trace:
            pen = QtGui.QPen(TRACE, 1.0, QtCore.Qt.PenStyle.DashLine)
            pen.setCosmetic(True)
            painter.setPen(pen)
            painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
            grounded = {s.node for s in model.supports.values() if s.grounds_position()}
            for nid in model.nodes:
                if nid in grounded:
                    continue
                path = result.path(nid)
                if len(path) < 2:
                    continue
                painter.drawPolyline(QtGui.QPolygonF([point(p) for p in path]))

        ghosts = max(0, int(model.motion.ghosts or 0))
        frame = result.frame_at(getattr(self.canvas, "motion_time", 0.0))
        if ghosts:
            total = len(result.frames)
            index = result.frames.index(frame) if frame in result.frames else 0
            for k in range(1, ghosts + 1):
                past = result.frames[(index - k * max(1, total // (ghosts * 6))) % total]
                self._draw_pose(painter, model, past, MOTION_GHOST, 1.4)

        self._draw_pose(painter, model, frame, MOTION, 3.0)

        # Speed readout on the fastest joint, so the numbers are not just in a
        # table somewhere: the diagram is the report.
        if getattr(self.canvas, "label_motion", True):
            fastest, best = None, 0.0
            for nid, v in frame.velocities.items():
                speed = math.hypot(*v)
                if speed > best:
                    fastest, best = nid, speed
            if fastest is not None and best > 1e-6:
                at = point(frame.positions[fastest])
                px_text(
                    painter,
                    self.canvas,
                    QtCore.QPointF(at.x(), at.y() - self.canvas.px(16.0)),
                    fmt(best, "mm/s"),
                    MOTION,
                    12.0,
                )

    def _draw_pose(self, painter, model, frame, colour, width):
        sc = self.sc()
        pen = QtGui.QPen(colour, width)
        pen.setCosmetic(True)
        pen.setCapStyle(QtCore.Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
        for member in model.members.values():
            a = frame.positions.get(member.start)
            b = frame.positions.get(member.end)
            if a and b:
                painter.drawLine(to_scene(a[0], a[1], sc), to_scene(b[0], b[1], sc))
        radius = self.canvas.px(3.4)
        painter.setBrush(colour)
        painter.setPen(QtCore.Qt.PenStyle.NoPen)
        for nid, pos in frame.positions.items():
            painter.drawEllipse(to_scene(pos[0], pos[1], sc), radius, radius)


class EffortGraphOverlay(QtWidgets.QGraphicsItem):
    """Driver effort against travel, sitting on the page like the results table.

    Against travel, not time, because time is an accident of the speed you
    typed and travel is a property of the machine: this is the curve you size
    a cylinder or a motor from. Every driver is drawn at once, so a linkage
    with two rams shows which one is working hardest and where.

    A playhead follows the animation, so the curve and the mechanism read
    together: the dot is where the machine is on screen right now.
    """

    MIN_SCALE = 0.6
    MAX_SCALE = 3.0
    HANDLE = 12.0  # pixels: corner grab zone, as on the results table
    W = 260.0  # base size in pixels, before the user scales it
    H = 150.0

    # Enough to tell drivers apart without inventing a new language: the
    # existing accents, in the order they are least likely to collide.
    SERIES = [S.APPLIED, S.REACTION, S.INTERNAL, S.SPRING_COL, DRIVER]

    def __init__(self, canvas):
        super().__init__()
        self.canvas = canvas
        self.setZValue(58)
        self.setFlag(QtWidgets.QGraphicsItem.GraphicsItemFlag.ItemIsMovable, True)
        self.setFlag(QtWidgets.QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations, True)
        self.setFlag(QtWidgets.QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges, True)
        self.setAcceptHoverEvents(True)
        self.setCursor(QtCore.Qt.CursorShape.OpenHandCursor)
        self._rect = QtCore.QRectF(0, 0, self.W, self.H)
        self._drag_start = None
        self._resizing = False
        self._resize_start_screen = None
        self._resize_start_scale = 1.0
        if getattr(canvas, "graph_pos", None) is not None:
            self.setPos(canvas.graph_pos)
        else:
            sheet = canvas.scene.sheet_rect()
            self.setPos(sheet.left() + 20, sheet.bottom() - self.H - 20)

    @property
    def graph_scale(self):
        return max(self.MIN_SCALE, min(self.MAX_SCALE, getattr(self.canvas, "graph_scale", 1.0)))

    def boundingRect(self):
        return self._rect

    def _visible(self):
        result = getattr(self.canvas, "motion_result", None)
        return (
            getattr(self.canvas, "show_graph", False)
            and result is not None
            and result.ok
            and bool(getattr(self.canvas, "motion_curves", None))
        )

    def sync(self):
        """Hide it outright when there is nothing to plot.

        Painting nothing is not enough: an item with a bounding rect still
        takes the mouse, so a hidden graph would leave an invisible draggable
        box sitting over the page.
        """
        self.setVisible(self._visible())

    # ---- drag and resize, exactly as the results table behaves

    def _near_corner(self, pos) -> bool:
        r = self._rect
        return (r.right() - pos.x()) <= self.HANDLE and (r.bottom() - pos.y()) <= self.HANDLE

    def hoverMoveEvent(self, event):
        self.setCursor(
            QtCore.Qt.CursorShape.SizeFDiagCursor
            if self._near_corner(event.pos())
            else QtCore.Qt.CursorShape.OpenHandCursor
        )
        super().hoverMoveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == QtCore.Qt.MouseButton.LeftButton:
            if self._near_corner(event.pos()):
                self._resizing = True
                self._resize_start_screen = event.screenPos()
                self._resize_start_scale = self.graph_scale
            else:
                self._drag_start = event.screenPos()
                self._pos_start = self.pos()
                self.setCursor(QtCore.Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._resizing:
            delta = event.screenPos() - self._resize_start_screen
            span = max(delta.x(), delta.y())
            self.canvas.graph_scale = max(
                self.MIN_SCALE, min(self.MAX_SCALE, self._resize_start_scale * (1.0 + span / 140.0))
            )
            self.prepareGeometryChange()
            self.update()
            event.accept()
            return
        if self._drag_start is not None:
            delta = event.screenPos() - self._drag_start
            scale = self.canvas.view.transform().m11() or 1.0
            new_pos = QtCore.QPointF(
                self._pos_start.x() + delta.x() / scale, self._pos_start.y() + delta.y() / scale
            )
            self.setPos(new_pos)
            self.canvas.graph_pos = new_pos
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._resizing or self._drag_start is not None:
            self._resizing = False
            self._drag_start = None
            self.setCursor(QtCore.Qt.CursorShape.OpenHandCursor)
            self.canvas.save()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    # ---- drawing

    def paint(self, painter, option, widget=None):
        if not self._visible():
            return
        curves = self.canvas.motion_curves
        k = self.graph_scale
        w, h = self.W * k, self.H * k
        self._rect = QtCore.QRectF(0, 0, w, h)

        pad = 8.0 * k
        title_h = 15.0 * k
        legend_h = (11.0 * k) * len(curves) + 4.0 * k
        plot = QtCore.QRectF(
            pad + 26.0 * k,
            title_h + pad * 0.5,
            w - pad * 2 - 26.0 * k,
            h - title_h - legend_h - pad * 1.5,
        )
        if plot.width() < 10 or plot.height() < 10:
            return

        painter.save()
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        painter.setPen(QtGui.QPen(S.INK, 1.0))
        painter.setBrush(QtGui.QColor(255, 255, 255, 240))
        painter.drawRect(self._rect)

        font_title = QtGui.QFont("DejaVu Sans")
        font_title.setPixelSize(max(6, round(10 * k)))
        font_title.setBold(True)
        font_small = QtGui.QFont("DejaVu Sans")
        font_small.setPixelSize(max(6, round(8 * k)))

        painter.setFont(font_title)
        painter.setPen(S.INK)
        painter.drawText(
            QtCore.QRectF(pad, 2.0 * k, w, title_h),
            int(QtCore.Qt.AlignmentFlag.AlignVCenter | QtCore.Qt.AlignmentFlag.AlignLeft),
            "Effort through the stroke",
        )

        # One shared scale per unit, so two rams are directly comparable and a
        # motor alongside them is not silently squashed to the same axis.
        peak = {}
        span = {}
        for c in curves:
            peak[c["unit"]] = max(peak.get(c["unit"], 0.0), abs(c["peak"]))
            lo, hi = c["x_range"]
            span[c["x_unit"]] = max(span.get(c["x_unit"], 0.0), abs(hi - lo), 1e-9)

        painter.setPen(QtGui.QPen(S.INK_LIGHT, 1.0))
        painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
        painter.drawRect(plot)
        zero_y = plot.center().y()
        zero_pen = QtGui.QPen(S.INK_LIGHT, 1.0, QtCore.Qt.PenStyle.DashLine)
        painter.setPen(zero_pen)
        painter.drawLine(QtCore.QPointF(plot.left(), zero_y), QtCore.QPointF(plot.right(), zero_y))

        painter.setFont(font_small)
        painter.setPen(S.INK_LIGHT)
        first = curves[0]
        top = peak.get(first["unit"], 1.0)
        painter.drawText(QtCore.QPointF(pad * 0.4, plot.top() + 7.0 * k), fmt(top, first["unit"]))
        painter.drawText(QtCore.QPointF(pad * 0.4, zero_y + 3.0 * k), "0")
        painter.drawText(
            QtCore.QPointF(plot.right() - 30.0 * k, plot.bottom() + 9.0 * k),
            f"{span.get(first['x_unit'], 0):,.0f} {first['x_unit']}",
        )

        fraction = 0.0
        result = self.canvas.motion_result
        if result.duration > 1e-9:
            fraction = max(0.0, min(1.0, self.canvas.motion_time / result.duration))

        legend_y = plot.bottom() + 12.0 * k
        for index, curve in enumerate(curves):
            colour = self.SERIES[index % len(self.SERIES)]
            top = peak.get(curve["unit"], 1.0) or 1.0
            width = span.get(curve["x_unit"], 1.0) or 1.0
            lo = curve["x_range"][0]

            def place(point, lo=lo, width=width, top=top):
                x = plot.left() + (point[0] - lo) / width * plot.width()
                y = zero_y - (point[1] / top) * (plot.height() / 2.0 - 2.0)
                return QtCore.QPointF(x, y)

            pen = QtGui.QPen(colour, 1.6 * k)
            painter.setPen(pen)
            painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
            painter.drawPolyline(QtGui.QPolygonF([place(p) for p in curve["points"]]))

            # Playhead: the dot is where the machine is on screen right now.
            at = curve["points"][
                min(len(curve["points"]) - 1, int(round(fraction * (len(curve["points"]) - 1))))
            ]
            dot = place(at)
            if index == 0:
                painter.setPen(QtGui.QPen(MOTION, 1.0, QtCore.Qt.PenStyle.DashLine))
                painter.drawLine(
                    QtCore.QPointF(dot.x(), plot.top()), QtCore.QPointF(dot.x(), plot.bottom())
                )
            painter.setPen(QtCore.Qt.PenStyle.NoPen)
            painter.setBrush(colour)
            painter.drawEllipse(dot, 2.6 * k, 2.6 * k)

            painter.setFont(font_small)
            painter.setPen(colour)
            painter.drawText(
                QtCore.QPointF(pad, legend_y + index * 11.0 * k),
                f"{curve['label']}  {fmt(at[1], curve['unit'])} now, "
                f"{fmt(curve['peak'], curve['unit'])} peak"
                + ("  reverses" if curve["reverses"] else ""),
            )

        # Resize handle, the same three ticks as the results table.
        painter.setPen(QtGui.QPen(S.INK_LIGHT, 1.1))
        for i in (4.0, 8.0, 12.0):
            painter.drawLine(QtCore.QPointF(w - i, h), QtCore.QPointF(w, h - i))
        painter.restore()


class DriverPreview(QtWidgets.QGraphicsItem):
    """A faint ghost of the driver the tool is about to place.

    Placing a driver changes what a member *is*, not just what is drawn on it,
    so it is worth seeing the answer before committing to it. Reads
    canvas.preview_driver only, and never touches the model.
    """

    def __init__(self, canvas):
        super().__init__()
        self.canvas = canvas
        self.setZValue(14)
        self.setAcceptedMouseButtons(QtCore.Qt.MouseButton.NoButton)

    def sc(self):
        sheet = getattr(getattr(self.canvas, "model", None), "sheet", None)
        return getattr(sheet, "unit_scale", 1.0) or 1.0

    def _target(self):
        pending = getattr(self.canvas, "preview_driver", None)
        if not pending:
            return None
        kind, member_id = pending
        member = self.canvas.model.members.get(member_id)
        if member is None:
            return None
        a = self.canvas.model.nodes.get(member.start)
        b = self.canvas.model.nodes.get(member.end)
        if not a or not b:
            return None
        return kind, to_scene(a.x, a.y, self.sc()), to_scene(b.x, b.y, self.sc())

    def boundingRect(self):
        target = self._target()
        if not target:
            return QtCore.QRectF()
        _kind, a, b = target
        pad = self.canvas.px(26.0)
        return QtCore.QRectF(a, b).normalized().adjusted(-pad, -pad, pad, pad)

    def paint(self, painter, option, widget=None):
        target = self._target()
        if not target:
            return
        kind, a, b = target
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        ghost = QtGui.QColor(DRIVER)
        ghost.setAlpha(90)
        dx, dy = b.x() - a.x(), b.y() - a.y()
        length = math.hypot(dx, dy) or 1.0
        nx, ny = -dy / length, dx / length

        if kind == "actuator":
            half = self.canvas.px(5.0)
            mid = QtCore.QPointF(a.x() + dx * 0.55, a.y() + dy * 0.55)
            pen = QtGui.QPen(ghost, 2.0)
            pen.setCosmetic(True)
            painter.setPen(pen)
            painter.setBrush(QtGui.QColor(255, 255, 255, 120))
            painter.drawPolygon(QtGui.QPolygonF([
                QtCore.QPointF(a.x() + nx * half, a.y() + ny * half),
                QtCore.QPointF(mid.x() + nx * half, mid.y() + ny * half),
                QtCore.QPointF(mid.x() - nx * half, mid.y() - ny * half),
                QtCore.QPointF(a.x() - nx * half, a.y() - ny * half)]))
            painter.drawLine(mid, b)
        else:
            # A motor goes on the end that is pinned, or the end nearest the
            # cursor when neither is, which is what the tool will pick.
            at = getattr(self.canvas, "preview_at", None) or a
            r = self.canvas.px(17.0)
            pen = QtGui.QPen(ghost, 2.0)
            pen.setCosmetic(True)
            painter.setPen(pen)
            painter.setBrush(QtGui.QColor(255, 255, 255, 120))
            painter.drawEllipse(at, r, r)

        painter.setPen(QtGui.QPen(ghost, 1.0, QtCore.Qt.PenStyle.DashLine))
        painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
        painter.drawLine(a, b)


# === tools


class _DriverTool(Tool):
    """Shared hover preview, so both drivers show the change before it lands."""

    preview_kind = ""

    def move(self, scene_pos, model_pos) -> bool:
        del model_pos
        member_id = self.canvas.member_near(scene_pos)
        pending = (self.preview_kind, member_id) if member_id is not None else None
        if pending != getattr(self.canvas, "preview_driver", None):
            self.canvas.preview_driver = pending
            self.canvas.preview_at = scene_pos
            self.canvas.driver_preview.prepareGeometryChange()
            self.canvas.driver_preview.update()
        return pending is not None

    def cancel(self):
        if getattr(self.canvas, "preview_driver", None) is not None:
            self.canvas.preview_driver = None
            self.canvas.driver_preview.prepareGeometryChange()
            self.canvas.driver_preview.update()
        super().cancel()


class MotorTool(_DriverTool):
    name = "Motor"
    prompt = "Click a link to drive it. It turns about its pinned end."
    wants_member = True
    preview_kind = "motor"

    def click(self, scene_pos, model_pos) -> bool:
        del model_pos
        member_id = self.canvas.member_near(scene_pos)
        if member_id is None:
            return True
        model = self.canvas.model
        member = model.members[member_id]
        grounded = [
            n
            for n in (member.start, member.end)
            if (model.support_at(n) and model.support_at(n).grounds_position())
        ]
        if grounded:
            node_id = grounded[0]
        else:
            # No pin yet: put one at the end nearest the click, because a motor
            # without a ground is the single most common way to get this wrong.
            node_id = self._nearest_end(scene_pos, member)
            edit(self.canvas, "Ground the motor joint", lambda m: m.add_support(node_id, M.PIN))
        motor = edit(self.canvas, "Add motor", lambda m: m.add_motor(node_id, member_id))
        self.canvas.preview_driver = None
        self.canvas.select_entity("motor", motor.id)
        self.canvas.set_prompt(f"{member.label} is driven. Press Run Motion to watch it.")
        return True

    def _nearest_end(self, scene_pos, member):
        model = self.canvas.model
        best, best_d = member.start, None
        for nid in (member.start, member.end):
            node = model.nodes[nid]
            p = to_scene(node.x, node.y, self.canvas.global_scale)
            d = math.hypot(p.x() - scene_pos.x(), p.y() - scene_pos.y())
            if best_d is None or d < best_d:
                best, best_d = nid, d
        return best


class ActuatorTool(_DriverTool):
    name = "Actuator"
    prompt = "Click a member to turn it into a linear actuator."
    wants_member = True
    preview_kind = "actuator"

    def click(self, scene_pos, model_pos) -> bool:
        del model_pos
        member_id = self.canvas.member_near(scene_pos)
        if member_id is None:
            return True
        length = self.canvas.model.member_length(self.canvas.model.members[member_id])
        stroke = max(10.0, round(length * 0.3, -1))
        actuator = edit(
            self.canvas, "Add actuator", lambda m: m.add_actuator(member_id, stroke=stroke)
        )
        self.canvas.preview_driver = None
        self.canvas.select_entity("actuator", actuator.id)
        self.canvas.set_prompt(
            "Actuator added. Run Motion to see the force it needs.")
        return True


def _static_result_from_frame(model, frame):
    """A motion frame, reshaped to look like a static solve, so the whole
    existing results toolbar -- reaction arrows, the table, the axial
    annotation on a selected member -- can read it without knowing whether
    it came from Solve or from the middle of a running mechanism.

    A rigid pin-jointed link only ever carries a uniform axial force, so
    two identical samples stand in for "constant along its length": enough
    for the existing diagram code to draw a flat band rather than a wedge
    tapering to nothing at one end, without claiming a shape it doesn't
    have. There is deliberately nothing here for shear or moment -- a
    moving mechanism is not an elastic beam, and showing a false zero would
    be worse than showing nothing.
    """
    result = StaticResult(ok=True, message="From the current instant of the motion run.")
    for ident, (fx, fy) in frame.reactions.items():
        result.reactions[ident] = Reaction(node=ident, fx=fx, fy=fy, m=0.0)
    for member_id, value in frame.axial.items():
        result.members[member_id] = MemberForces(member=member_id, axial=[value, value])
    result.equilibrium_error = frame.equilibrium_error
    return result


def motion_tools(canvas):
    """The mechanism half of the palette, appended by tools.build_tools."""
    return [MotorTool(canvas), ActuatorTool(canvas)]


# === playback


class MotionBar(QtWidgets.QFrame):
    """Transport controls, floating at the foot of the canvas.

    Styled to match DisplayHUD deliberately: it is the same kind of object,
    a quiet panel over the page rather than a dock or a dialog.
    """

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
                background: transparent; border: none; border-radius: 5px;
                padding: 6px 12px; color: #5b6270; font-weight: bold;
                font-family: "DejaVu Sans", sans-serif; font-size: 11px;
            }
            QPushButton:hover { background: rgba(0,0,0,12); color: #1d2025; }
            QPushButton:checked { background: #e0f2f1; color: #00695c; }
            QPushButton:disabled { color: #b8bec9; }
            QLabel { color: #5b6270; font-size: 11px; font-family: "DejaVu Sans"; }
            QSlider::groove:horizontal { height: 3px; background: #c3c8d2; }
            QSlider::handle:horizontal {
                background: #00897b; width: 11px; margin: -5px 0;
                border-radius: 5px;
            }
        """)
        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(6)

        self.btn_run = QtWidgets.QPushButton("Run")
        self.btn_run.setToolTip("Solve the motion and play it")
        self.btn_run.clicked.connect(editor.run_motion)
        layout.addWidget(self.btn_run)

        self.btn_play = QtWidgets.QPushButton("Play")
        self.btn_play.setCheckable(True)
        self.btn_play.clicked.connect(editor.toggle_play)
        layout.addWidget(self.btn_play)

        self.slider = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self.slider.setMinimumWidth(220)
        self.slider.setRange(0, 1000)
        self.slider.valueChanged.connect(self._scrub)
        layout.addWidget(self.slider)

        self.time_label = QtWidgets.QLabel("0.00 s")
        self.time_label.setMinimumWidth(52)
        layout.addWidget(self.time_label)

        self.btn_trace = QtWidgets.QPushButton("Trace")
        self.btn_trace.setCheckable(True)
        self.btn_trace.clicked.connect(editor.toggle_trace)
        layout.addWidget(self.btn_trace)

        self.btn_static = QtWidgets.QPushButton("Statics")
        self.btn_static.setToolTip(
            "Show the static solve on the toolbar again, without clearing this run.")
        self.btn_static.clicked.connect(lambda: editor.set_display_mode("static"))
        layout.addWidget(self.btn_static)

        self.btn_graph = QtWidgets.QPushButton("Graph")
        self.btn_graph.setCheckable(True)
        self.btn_graph.setToolTip("Effort against travel, for every driver at once")
        self.btn_graph.clicked.connect(editor.toggle_graph)
        layout.addWidget(self.btn_graph)

        self.btn_clear = QtWidgets.QPushButton("Clear")
        self.btn_clear.setToolTip("Take the run off the page. The diagram is untouched.")
        self.btn_clear.clicked.connect(editor.clear_motion)
        layout.addWidget(self.btn_clear)

        self.status = QtWidgets.QLabel("")
        layout.addWidget(self.status)

    def _scrub(self, value):
        result = self.editor.motion_result
        if not result or not result.frames:
            return
        self.editor.set_motion_time(result.duration * value / 1000.0)

    def sync_state(self):
        editor = self.editor
        result = editor.motion_result
        has = bool(result and result.ok and result.frames)
        driven = editor.model.has_drivers()
        self.btn_run.setEnabled(driven)
        self.btn_play.setEnabled(has)
        self.slider.setEnabled(has)
        self.btn_trace.setEnabled(has)
        self.btn_graph.setEnabled(has)
        self.btn_clear.setEnabled(has)
        self.btn_static.setEnabled(has and editor._display_mode == "motion")
        for button, checked in (
            (self.btn_play, editor.playing),
            (self.btn_trace, editor.model.motion.trace),
            (self.btn_graph, editor.show_graph),
        ):
            button.blockSignals(True)
            button.setChecked(checked)
            button.blockSignals(False)
        self.btn_play.setText("Pause" if editor.playing else "Play")
        self.time_label.setText(f"{editor.motion_time:.2f} s")
        if has:
            self.slider.blockSignals(True)
            self.slider.setValue(int(1000 * editor.motion_time / max(1e-6, result.duration)))
            self.slider.blockSignals(False)
        message = ""
        if result and not result.ok:
            message = result.message
        elif not driven:
            message = "Add a motor or an actuator"
        self.status.setText(message)


class MotionController:
    """Mixin for Editor: everything the transport bar and the overlay need.

    Kept as a mixin rather than another few hundred lines in editor.py, so the
    statics editor stays readable and the motion code can be read on its own.
    """

    def init_motion(self):
        self.motion_result = None
        self.motion_time = 0.0
        self.playing = False
        self.show_motion = True
        self.label_motion = True
        self.default_lever_length = 200.0
        self._display_mode = "static"   # "static" or "motion": which result the toolbar shows
        self._motion_timer = QtCore.QTimer(self)
        self._motion_timer.setInterval(33)
        self._motion_timer.timeout.connect(self._advance)
        self.show_graph = True
        self.graph_pos = None
        self.graph_scale = 1.0
        self.motion_curves = []
        self.preview_driver = None
        self.preview_at = None
        self.driver_preview = DriverPreview(self)
        self.scene.addItem(self.driver_preview)
        self.motion_overlay = MotionOverlay(self)
        self.scene.addItem(self.motion_overlay)
        self.motion_graph = EffortGraphOverlay(self)
        self.scene.addItem(self.motion_graph)

    @property
    def display_result(self):
        """Whichever result the static-results toolbar should show right
        now: the live motion frame while a run is the more recent thing the
        user did, the ordinary static solve otherwise."""
        if (self._display_mode == "motion" and self.motion_result
                and self.motion_result.ok and self.motion_result.frames):
            return _static_result_from_frame(
                self.model, self.motion_result.frame_at(self.motion_time))
        return self.result

    def run_motion(self):
        self.playing = False
        self._motion_timer.stop()
        self.motion_result = kinematics.simulate(self.model)
        self.motion_curves = (
            kinematics.effort_curves(self.model, self.motion_result)
            if self.motion_result.ok
            else []
        )
        self.motion_time = 0.0
        if self.motion_result.ok:
            self.playing = True
            self._motion_timer.start()
            self._display_mode = "motion"
            # Neither applies to a moving mechanism: a rigid pin-jointed
            # link carries no bending at all, so leaving a stale toggle on
            # would draw a diagram this result has nothing to say about.
            self.show_shear = False
            self.show_moment = False
        self.refresh_motion_items()
        self.set_prompt(self.motion_result.message)
        for warning in self.motion_result.warnings:
            self.set_prompt(warning)
        self.refresh_geometry()
        self.notify()
        return self.motion_result

    def refresh_motion_items(self):
        try:
            self.motion_graph.sync()
        except RuntimeError:
            return
        for item in (self.motion_overlay, self.motion_graph):
            try:
                item.prepareGeometryChange()
                item.update()
            except RuntimeError:
                pass

    def set_display_mode(self, mode):
        self._display_mode = mode
        self.refresh_geometry()

    def toggle_play(self):
        if not (self.motion_result and self.motion_result.ok):
            return
        self.playing = not self.playing
        if self.playing:
            self._motion_timer.start()
        else:
            self._motion_timer.stop()
        self.refresh_geometry()

    def toggle_trace(self):
        self.model.motion.trace = not self.model.motion.trace
        self.save()
        self.refresh_geometry()

    def set_motion_time(self, t):
        result = self.motion_result
        if not result or not result.frames:
            return
        self.motion_time = max(0.0, min(result.duration, float(t)))
        self.refresh_motion_items()
        self.refresh_geometry()

    def invalidate_motion(self):
        """Editing the diagram invalidates a run, exactly as it does a solve."""
        if self.motion_result is not None:
            self.motion_result = None
            self.motion_curves = []
            self.playing = False
            self._motion_timer.stop()
            if self._display_mode == "motion":
                self._display_mode = "static"

    def clear_motion(self):
        """Take the run off the page without touching the diagram.

        Dragging the mechanism used to be the only way to get rid of the
        traces and the running pose, which meant editing the model just to
        tidy the screen. This leaves the model, the undo stack and the saved
        document exactly as they were.
        """
        self.invalidate_motion()
        self.motion_time = 0.0
        self._display_mode = "static"
        self.refresh_motion_items()
        self.refresh_geometry()
        self.notify()
        self.set_prompt("Motion cleared. The diagram is untouched.")

    def toggle_graph(self):
        self.show_graph = not self.show_graph
        self.refresh_motion_items()
        self.refresh_geometry()

    def _advance(self):
        result = self.motion_result
        if not result or not result.frames:
            self._motion_timer.stop()
            return
        step = self._motion_timer.interval() / 1000.0
        t = self.motion_time + step
        if result.period and result.period > 1e-6:
            # The simulated segment is a whole number of natural cycles, so
            # wrapping here lands exactly back on frame zero's own pose:
            # nothing to see, which is the point.
            t = t % result.period
        elif t > result.duration:
            # Nothing periodic to close the loop against (an EXTEND
            # actuator, say): finish the run and hold there, rather than
            # snapping back to a pose the mechanism never actually returns
            # to on its own.
            t = result.duration
            self.playing = False
            self._motion_timer.stop()
        self.motion_time = t
        try:
            self.motion_overlay.update()
            self.motion_graph.update()
            self.motion_bar.sync_state()
        except RuntimeError:
            self._motion_timer.stop()

    def motion_rows(self):
        """Motion results for the on-page table, in the same shape as statics."""
        rows = []
        result = self.motion_result
        if not (result and result.ok):
            return rows
        peaks = result.peak_effort()
        for mo in self.model.motors.values():
            if mo.id in peaks:
                rows.append((f"Torque {mo.label}", fmt(peaks[mo.id], "N.mm"), "peak over the run"))
        for ac in self.model.actuators.values():
            if ac.id in peaks:
                value = peaks[ac.id]
                rows.append(
                    (
                        f"Force {ac.label}",
                        fmt(abs(value), "N"),
                        ("peak push" if value > 0 else "peak pull"),
                    )
                )
        peak_reactions = {}
        for f in result.frames:
            if not f.ok:
                continue
            for ident, (fx, fy) in f.reactions.items():
                mag = math.hypot(fx, fy)
                if mag > peak_reactions.get(ident, 0.0):
                    peak_reactions[ident] = mag
        for support in self.model.supports.values():
            mag = peak_reactions.get(support.holds)
            if mag and mag > 1e-6:
                rows.append((
                    f"Reaction {self.model.entity_label(support.holds)}",
                    fmt(mag, "N"),
                    "peak over the run",
                ))

        speed = result.peak_speed()
        if speed > 1e-9:
            rows.append(("Peak joint speed", fmt(speed, "mm/s"), "any joint"))
        return rows
