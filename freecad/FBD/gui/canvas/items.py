# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Ryan Kembrey <ryan.FreeCAD@gmail.com>
# SPDX-FileNotice: Part of the FBD addon.

"""The drawn diagram: one item class per thing on the page.

Conventions follow engineering drawing practice, because the output is meant to
look like a free body diagram an engineer would accept:

    member          solid line, open circles at joints
    pin             triangle, apex on the node, ground hatching under the base
    roller          triangle riding on two circles, free to translate one way
    fixed           hatched wall, blocks rotation too
    spring          coil to ground
    applied load    arrow with its head at the point of application
    reaction        arrow with its tail at the node, drawn in the reaction colour
    couple          curved arrow, counter-clockwise positive
    line load       row of arrows capped by a spanning line

Model y is up, Qt scene y is down, so items map through `to_scene`. Text is
counter-flipped where needed so nothing renders mirrored.
"""

import math

from PySide6 import QtCore, QtGui, QtWidgets

from .. import style as S
from . import popup as P
from ...engine import model as M


def to_scene(x, y, scale=1.0):
    sc = scale if scale > 1e-6 else 1.0
    return QtCore.QPointF(float(x) / sc, -float(y) / sc)


def to_model(pt, scale=1.0):
    sc = scale if scale > 1e-6 else 1.0
    return (float(pt.x()) * sc, -float(pt.y()) * sc)


def direction_from_handle(mx, my, magnitude):
    """Given the tail handle dragged to local (device-pixel) point (mx, my),
    return the (fx, fy) that puts the tail there, keeping magnitude fixed.

    Local space is screen pixels (y down); model space is y up. Pure geometry,
    no Qt dependency, so the drag-to-rotate interaction is testable without a
    running event loop.
    """
    r = math.hypot(mx, my)
    if r < 1e-9:
        return 0.0, 0.0
    ux, uy = -mx / r, -my / r  # local travel direction, tail -> tip
    return magnitude * ux, -magnitude * uy


def fmt(value, unit=""):
    """Readable engineering numbers: no scientific notation, grouped thousands."""
    v = float(value)
    a = abs(v)
    if a >= 1000:
        text = f"{v:,.0f}"
    elif a >= 10:
        text = f"{v:.1f}".rstrip("0").rstrip(".")
    elif a >= 0.01:
        text = f"{v:.3g}"
    elif a == 0:
        text = "0"
    else:
        text = f"{v:.2e}"
    return f"{text} {unit}".strip()


# Anchored symbols draw in device pixels via ItemIgnoresTransformations, so a
# support or arrow is the same size at every zoom. Geometric items (members,
# line loads, diagrams) stay in scene units but take their thicknesses and text
# from canvas.px() so they read consistently too.


def px_text(
    painter,
    canvas,
    at: QtCore.QPointF,
    text,
    color,
    size_pt=14.0,
    bold=False,
    centre=True,
    dx=0.0,
    dy=0.0,
):
    """Draw text at a scene point, at constant screen size."""
    k = canvas.px(1.0)
    painter.save()
    painter.translate(at)
    painter.scale(k, k)
    f = S.font(size_pt, bold)
    painter.setFont(f)
    metrics = QtGui.QFontMetricsF(f)
    rect = metrics.boundingRect(str(text))
    x = -rect.width() / 2.0 if centre else 0.0
    path = QtGui.QPainterPath()
    path.addText(x + dx, dy, f, str(text))
    painter.setPen(QtGui.QPen(S.PAPER, 3.0))
    painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
    painter.drawPath(path)
    painter.setPen(QtCore.Qt.PenStyle.NoPen)
    painter.setBrush(color)
    painter.drawPath(path)
    painter.restore()


def _arrow_head(path: QtGui.QPainterPath, tip: QtCore.QPointF, ux, uy, size):
    px, py = -uy, ux
    bx, by = tip.x() - ux * size, tip.y() - uy * size
    path.moveTo(tip)
    path.lineTo(bx + px * size * 0.36, by + py * size * 0.36)
    path.lineTo(bx - px * size * 0.36, by - py * size * 0.36)
    path.closeSubpath()


def _hatch(painter, p1: QtCore.QPointF, p2: QtCore.QPointF, size, count=S.HATCH_COUNT, lean=0.45):
    """Ground hatching: short parallel ticks under a base line."""
    dx, dy = p2.x() - p1.x(), p2.y() - p1.y()
    for i in range(count):
        f = i / max(1, count - 1)
        bx, by = p1.x() + dx * f, p1.y() + dy * f
        painter.drawLine(QtCore.QPointF(bx, by), QtCore.QPointF(bx - size * lean, by + size))


def _draw_text(
    painter, pos: QtCore.QPointF, text, color, size_pt=7.5, bold=False, align_left=True, halo=True
):
    """Screen-upright text at a scene position."""
    painter.save()
    painter.translate(pos)
    f = S.font(size_pt, bold)
    painter.setFont(f)
    metrics = QtGui.QFontMetricsF(f)
    rect = metrics.boundingRect(str(text))
    x = 0.0 if align_left else -rect.width() / 2.0
    if halo:
        painter.setPen(QtGui.QPen(S.PAPER, 1.6))
        path = QtGui.QPainterPath()
        path.addText(x, 0.0, f, str(text))
        painter.strokePath(path, QtGui.QPen(S.PAPER, 1.6))
    painter.setPen(QtGui.QPen(color))
    painter.drawText(QtCore.QPointF(x, 0.0), str(text))
    painter.restore()


class _Item(QtWidgets.QGraphicsItem):
    """Base: knows its entity id and where to read the model from."""

    kind = ""
    anchored = False  # True: draw in device pixels at a fixed point

    def __init__(self, ident, canvas):
        super().__init__()
        self.ident = ident
        self.canvas = canvas
        self.setAcceptHoverEvents(True)
        self._hover = False
        self.setFlag(QtWidgets.QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, True)
        if self.anchored:
            # Constant screen size, whatever the zoom.
            self.setFlag(QtWidgets.QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations, True)

    def sc(self) -> float:
        return (
            getattr(getattr(self.canvas, "model", None), "sheet", None).unit_scale
            if hasattr(self.canvas, "model")
            else 1.0
        )

    def sync(self):
        """Reposition an anchored item after the model moves."""
        if self.anchored:
            try:
                self.setPos(self.anchor_point())
            except RuntimeError:
                pass

    def anchor_point(self) -> QtCore.QPointF:
        return QtCore.QPointF()

    @property
    def model(self):
        return self.canvas.model

    def hoverEnterEvent(self, event):
        self._hover = True
        self.update()

    def hoverLeaveEvent(self, event):
        self._hover = False
        self.update()

    def ink(self, base):
        if self.isSelected():
            return S.SELECT
        if self._hover:
            return S.HOVER
        return base


# ---------------------------------------------------------------- node


class NodeItem(_Item):
    kind = "node"
    anchored = True
    R = 6.8  # pixels

    def __init__(self, ident, canvas):
        super().__init__(ident, canvas)
        self.setZValue(30)
        self.sync()

    def node(self):
        return self.model.nodes.get(self.ident)

    def anchor_point(self):
        n = self.node()
        return to_scene(n.x, n.y, self.sc()) if n else QtCore.QPointF()

    def boundingRect(self):
        r = self.R + 3.0
        return QtCore.QRectF(-r, -r, 2 * r, 2 * r).adjusted(-6, -14, 46, 6)

    def shape(self):
        p = QtGui.QPainterPath()
        r = self.R + 3.0
        p.addEllipse(QtCore.QPointF(0, 0), r, r)
        return p

    def paint(self, painter, option, widget=None):
        n = self.node()
        if n is None:
            return
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        col = self.ink(S.INK)
        painter.setBrush(S.PAPER)
        painter.setPen(QtGui.QPen(col, 1.6))
        painter.drawEllipse(QtCore.QPointF(0, 0), self.R, self.R)
        if self.canvas.show_labels:
            f = S.font(13.0)
            painter.setFont(f)
            painter.setPen(QtGui.QPen(S.INK_LIGHT))
            painter.drawText(QtCore.QPointF(self.R + 5.0, -self.R - 4.0), n.label)

    def open_editor(self, scene_pos):
        node = self.node()
        if node is None:
            return
        model = self.canvas.model
        form = P.PopupForm()
        form.add_text(
            "Name", node.label, lambda v: self.canvas.edit(lambda: setattr(node, "label", v))
        )
        form.add_spin(
            "X", node.x, lambda v: self.canvas.edit(lambda: setattr(node, "x", v)), suffix="mm"
        )
        form.add_spin(
            "Y", node.y, lambda v: self.canvas.edit(lambda: setattr(node, "y", v)), suffix="mm"
        )
        self.canvas.open_popup(self.anchor_point(), form)


# ---------------------------------------------------------------- member


class MemberItem(_Item):
    kind = "member"

    def __init__(self, ident, canvas):
        super().__init__(ident, canvas)
        self.setZValue(10)

    def member(self):
        return self.model.members.get(self.ident)

    def ends(self):
        m = self.member()
        if not m:
            return QtCore.QPointF(), QtCore.QPointF()
        a = self.model.nodes.get(m.start)
        b = self.model.nodes.get(m.end)
        if not a or not b:
            return QtCore.QPointF(), QtCore.QPointF()
        return to_scene(a.x, a.y, self.sc()), to_scene(b.x, b.y, self.sc())

    def boundingRect(self):
        a, b = self.ends()
        return QtCore.QRectF(a, b).normalized().adjusted(-12, -12, 12, 12)

    def shape(self):
        a, b = self.ends()
        p = QtGui.QPainterPath(a)
        p.lineTo(b)
        stroker = QtGui.QPainterPathStroker()
        stroker.setWidth(self.canvas.px(7.0))
        return stroker.createStroke(p)

    def paint(self, painter, option, widget=None):
        a, b = self.ends()
        if a == b:
            return
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        pen = QtGui.QPen(self.ink(S.INK), 3.4)
        pen.setCosmetic(True)  # constant thickness at any zoom
        pen.setCapStyle(QtCore.Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.drawLine(a, b)

        # Internal forces clutter the page, so only annotate the selected
        # member; the full set always lives in the results table.
        res = self.canvas.display_result
        if not (res and self.isSelected() and self.ident in res.members):
            return
        forces = res.members[self.ident]
        axial = forces.axial_max
        if abs(axial) < 1e-6:
            return
        mid = QtCore.QPointF((a.x() + b.x()) / 2, (a.y() + b.y()) / 2)
        dx, dy = b.x() - a.x(), b.y() - a.y()
        length = math.hypot(dx, dy) or 1.0
        nx, ny = -dy / length, dx / length
        if ny < 0:
            nx, ny = -nx, -ny
        off = self.canvas.px(14.0)
        tag = "T" if axial > 0 else "C"
        px_text(
            painter,
            self.canvas,
            QtCore.QPointF(mid.x() + nx * off, mid.y() + ny * off),
            fmt(abs(axial), f"N {tag}"),
            S.INTERNAL,
            13.0,
        )

    def open_editor(self, scene_pos):
        member = self.member()
        if member is None:
            return
        model = self.canvas.model
        form = P.PopupForm()
        form.add_text(
            "Name", member.label, lambda v: self.canvas.edit(lambda: setattr(member, "label", v))
        )
        form.add_readonly("Length", f"{model.member_length(member):,.1f} mm")
        form.add_spin(
            "EA",
            member.EA,
            lambda v: self.canvas.edit(lambda: setattr(member, "EA", v)),
            lo=1.0,
            decimals=0,
            suffix="N",
        )
        form.add_spin(
            "EI",
            member.EI,
            lambda v: self.canvas.edit(lambda: setattr(member, "EI", v)),
            lo=1.0,
            decimals=0,
            suffix="N.mm2",
        )
        form.add_combo(
            "Behaviour",
            [(k, M.BEHAVIOUR_LABELS[k]) for k in M.BEHAVIOURS],
            member.behaviour,
            lambda v: self.canvas.edit(lambda: setattr(member, "behaviour", v)),
            tooltip="A cable cannot push; a strut cannot pull.",
        )
        for label, attr in (("Hinge at start", "release_start"), ("Hinge at end", "release_end")):
            form.add_combo(
                label,
                [(0, "Rigid"), (1, "Hinged")],
                1 if getattr(member, attr) else 0,
                lambda v, a=attr: self.canvas.edit(
                    lambda: setattr(member, a, bool(v)), rebuild=True
                ),
            )
        form.add_spin(
            "Mp start",
            member.mp_start,
            lambda v: self.canvas.edit(lambda: setattr(member, "mp_start", v)),
            lo=0.0,
            decimals=0,
            suffix="N.mm",
            tooltip="Plastic moment capacity. 0 stays elastic.",
        )
        form.add_spin(
            "Self weight",
            member.g,
            lambda v: self.canvas.edit(lambda: setattr(member, "g", v)),
            lo=0.0,
            decimals=4,
            suffix="N/mm",
        )
        form.add_spin(
            "Mass",
            member.mass,
            lambda v: self.canvas.edit(lambda: setattr(member, "mass", v)),
            lo=0.0,
            decimals=3,
            suffix="kg",
            tooltip="Only used by Run Motion: a fast-moving link's own "
                    "inertia adds to the force its driver needs.",
        )
        form.add_note(
            "EA, EI and self weight only change the answer for a statically "
            "indeterminate structure. Mass only matters in Run Motion."
        )
        self.canvas.open_popup(scene_pos, form)


# ---------------------------------------------------------------- support


class SupportItem(_Item):
    kind = "support"
    anchored = True
    SIZE = 30.0  # pixels

    def __init__(self, ident, canvas):
        super().__init__(ident, canvas)
        self.setZValue(20)
        self.setToolTip("Right-click to switch type; double-click to edit.")
        self.sync()

    def support(self):
        return self.model.supports.get(self.ident)

    def anchor_point(self):
        sup = self.support()
        if not sup:
            return QtCore.QPointF()
        xy = self.model.support_xy(sup)          # joint or point on a member
        return to_scene(*xy, scale=self.sc()) if xy else QtCore.QPointF()

    def boundingRect(self):
        r = self.SIZE * 2.2
        return QtCore.QRectF(-r, -r * 0.4, 2 * r, r * 2.4)

    def shape(self):
        s_ = self.SIZE
        p = QtGui.QPainterPath()
        p.addRect(QtCore.QRectF(-s_, -s_ * 0.2, 2 * s_, s_ * 1.8))
        return p

    def paint(self, painter, option, widget=None):
        sup = self.support()
        if not sup:
            return
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        col = self.ink(S.SPRING_COL if sup.kind == M.SPRING else S.SUPPORT)
        size = self.SIZE
        painter.save()
        painter.rotate(-float(sup.angle or 0.0))
        painter.setPen(QtGui.QPen(col, 1.5))
        painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
        if sup.kind == M.FIXED:
            self._fixed(painter, size)
        elif sup.kind == M.SPRING:
            self._spring(painter, size)
        elif sup.kind == M.ROLLER_X:
            self._roller(painter, size)
        elif sup.kind == M.ROLLER_Y:
            painter.rotate(90)
            self._roller(painter, size)
        else:
            self._pin(painter, size)
        painter.restore()

    def _pin(self, painter, size):
        painter.drawPolygon(
            QtGui.QPolygonF(
                [
                    QtCore.QPointF(0, 0),
                    QtCore.QPointF(-size * 0.62, size),
                    QtCore.QPointF(size * 0.62, size),
                ]
            )
        )
        a, b = QtCore.QPointF(-size * 0.95, size), QtCore.QPointF(size * 0.95, size)
        painter.drawLine(a, b)
        _hatch(painter, a, b, size * 0.4)

    def _roller(self, painter, size):
        painter.drawPolygon(
            QtGui.QPolygonF(
                [
                    QtCore.QPointF(0, 0),
                    QtCore.QPointF(-size * 0.62, size * 0.7),
                    QtCore.QPointF(size * 0.62, size * 0.7),
                ]
            )
        )
        r = size * 0.18
        for lx in (-size * 0.33, size * 0.33):
            painter.drawEllipse(QtCore.QPointF(lx, size * 0.7 + r), r, r)
        a = QtCore.QPointF(-size * 0.95, size * 0.7 + 2 * r)
        b = QtCore.QPointF(size * 0.95, size * 0.7 + 2 * r)
        painter.drawLine(a, b)
        _hatch(painter, a, b, size * 0.4)

    def _fixed(self, painter, size):
        a, b = QtCore.QPointF(-size, 0), QtCore.QPointF(size, 0)
        painter.setPen(QtGui.QPen(painter.pen().color(), 2.0))
        painter.drawLine(a, b)
        painter.setPen(QtGui.QPen(painter.pen().color(), 1.3))
        _hatch(painter, a, b, size * 0.45, count=9)

    def _spring(self, painter, size):
        coils, width = 4, size * 0.4
        bottom = size * 1.3
        seg = bottom / (coils * 2 + 2)
        path = QtGui.QPainterPath(QtCore.QPointF(0, 0))
        y = seg
        path.lineTo(QtCore.QPointF(0, y))
        for i in range(coils * 2):
            y += seg
            path.lineTo(QtCore.QPointF(width if i % 2 == 0 else -width, y))
        path.lineTo(QtCore.QPointF(0, bottom - seg))
        path.lineTo(QtCore.QPointF(0, bottom))
        painter.drawPath(path)
        a = QtCore.QPointF(-size * 0.95, bottom)
        b = QtCore.QPointF(size * 0.95, bottom)
        painter.drawLine(a, b)
        _hatch(painter, a, b, size * 0.4)

    def contextMenuEvent(self, event):
        """Right-click: switch the support type in one click, no form needed."""
        support = self.support()
        if support is None:
            return
        options = [(k, M.SUPPORT_LABELS[k]) for k in M.SUPPORT_KINDS]
        P.quick_menu(
            event.widget(), event.screenPos(), options, lambda kind: self._set_kind(support, kind)
        )
        event.accept()

    def _set_kind(self, support, kind):
        def apply():
            support.kind = kind
            if kind == M.SPRING and not support.reaction_components():
                support.ky = 1000.0

        self.canvas.edit(apply, rebuild=True)

    def open_editor(self, scene_pos):
        support = self.support()
        if support is None:
            return
        form = P.PopupForm()
        form.add_combo(
            "Type",
            [(k, M.SUPPORT_LABELS[k]) for k in M.SUPPORT_KINDS],
            support.kind,
            lambda v: self._set_kind(support, v),
            tooltip="What the support restrains.",
        )
        form.add_spin(
            "Angle",
            support.angle,
            lambda v: self.canvas.edit(lambda: setattr(support, "angle", v)),
            lo=-360,
            hi=360,
            decimals=1,
            suffix="deg",
            tooltip="Rotate the support, for an inclined bearing.",
        )
        if support.kind == M.SPRING:
            for label, attr, unit in (
                ("kx", "kx", "N/mm"),
                ("ky", "ky", "N/mm"),
                ("kr", "kr", "N.mm/rad"),
            ):
                form.add_spin(
                    label,
                    getattr(support, attr),
                    lambda v, a=attr: self.canvas.edit(lambda: setattr(support, a, v)),
                    lo=0.0,
                    decimals=2,
                    suffix=unit,
                    tooltip="Zero means unrestrained in this direction.",
                )
        self.canvas.open_popup(self.anchor_point(), form)


# ---------------------------------------------------------------- anchor


class AnchorItem(_Item):
    """A point on a member: lighter than a joint (a small filled diamond,
    not an open circle), since it doesn't connect members or carry a
    support. Today it's an attachment point for a load; later, a pivot."""

    kind = "anchor"
    anchored = True
    R = 5.5  # pixels

    def __init__(self, ident, canvas):
        super().__init__(ident, canvas)
        self.setZValue(25)
        self.setToolTip("A point along the member. Double-click to rename.")
        self.sync()

    def anchor(self):
        return self.model.anchors.get(self.ident)

    def anchor_point(self):
        a = self.anchor()
        if a is None:
            return QtCore.QPointF()
        xy = self.model.anchor_xy(a)
        return to_scene(*xy, scale=self.sc()) if xy else QtCore.QPointF()

    def boundingRect(self):
        r = self.R + 3.0
        return QtCore.QRectF(-r, -r, 2 * r, 2 * r).adjusted(-6, -20, 46, 6)

    def shape(self):
        p = QtGui.QPainterPath()
        r = self.R + 3.0
        p.addEllipse(QtCore.QPointF(0, 0), r, r)
        return p

    def paint(self, painter, option, widget=None):
        a = self.anchor()
        if a is None:
            return
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        col = self.ink(S.SUPPORT)
        r = self.R
        diamond = QtGui.QPolygonF(
            [
                QtCore.QPointF(0, -r),
                QtCore.QPointF(r, 0),
                QtCore.QPointF(0, r),
                QtCore.QPointF(-r, 0),
            ]
        )
        painter.setBrush(S.PAPER)
        painter.setPen(QtGui.QPen(col, 1.6))
        painter.drawPolygon(diamond)
        if self.canvas.show_labels:
            f = S.font(11.0)
            painter.setFont(f)
            painter.setPen(QtGui.QPen(S.INK_LIGHT))
            painter.drawText(QtCore.QPointF(r + 4.0, -r - 2.0), a.label)

    def open_editor(self, scene_pos):
        a = self.anchor()
        if a is None:
            return
        member = self.model.members.get(a.member)
        form = P.PopupForm()
        form.add_text("Name", a.label, lambda v: self.canvas.edit(lambda: setattr(a, "label", v)))
        form.add_spin(
            "Position",
            a.t * 100.0,
            lambda v: self.canvas.edit(lambda: setattr(a, "t", max(0.0, min(1.0, v / 100.0)))),
            lo=0.0,
            hi=100.0,
            decimals=1,
            suffix="%",
            tooltip="Distance along the member from its start, as a percentage.",
        )
        if member:
            form.add_readonly("Along", member.label)
        form.add_note("Loads can attach here the same as to a joint.")
        self.canvas.open_popup(self.anchor_point(), form)


# ---------------------------------------------------------------- loads


class PointLoadItem(_Item):
    kind = "point_load"
    anchored = True
    LEN = 80.0  # pixels
    HEAD = 16.0
    HANDLE_R = 10.0  # pixels: grab radius for the rotate handle

    def __init__(self, ident, canvas):
        super().__init__(ident, canvas)
        self.setZValue(40)
        self.setAcceptHoverEvents(True)
        self._rotating = False
        self.sync()

    def load(self):
        return self.model.point_loads.get(self.ident)

    def anchor_point(self):
        l = self.load()
        if not l:
            return QtCore.QPointF()
        return self.canvas.attachment_scene_pos(l.node, l.anchor)

    def _dir(self):
        l = self.load()
        if not l:
            return None
        mag = l.magnitude()
        if mag < 1e-12:
            return None
        return l.fx / mag, -l.fy / mag, mag

    def _tail(self):
        d = self._dir()
        if not d:
            return None
        ux, uy, _ = d
        return QtCore.QPointF(-ux * self.LEN, -uy * self.LEN)

    def boundingRect(self):
        r = self.LEN + 24
        return QtCore.QRectF(-r, -r, 2 * r, 2 * r)

    def _components(self):
        """Each axis with something to show, as (ux, uy, length, signed
        value): ux/uy point in the arrow's own direction, length already
        scaled so neither component can exceed what the combined arrow
        would have been."""
        l = self.load()
        if not l:
            return []
        mag = l.magnitude()
        if mag < 1e-9:
            return []
        out = []
        if abs(l.fx) > 1e-9:
            ux = 1.0 if l.fx > 0 else -1.0
            out.append((ux, 0.0, self.LEN * abs(l.fx) / mag, l.fx))
        if abs(l.fy) > 1e-9:
            uy = -1.0 if l.fy > 0 else 1.0   # screen y is flipped from model y
            out.append((0.0, uy, self.LEN * abs(l.fy) / mag, l.fy))
        return out

    def shape(self):
        p = QtGui.QPainterPath()
        stroker = QtGui.QPainterPathStroker()
        stroker.setWidth(9.0)
        if getattr(self.canvas, "show_components", False):
            for ux, uy, length, _v in self._components():
                p.moveTo(QtCore.QPointF(-ux * length, -uy * length))
                p.lineTo(QtCore.QPointF(0, 0))
            return stroker.createStroke(p)
        d = self._dir()
        if not d:
            return p
        ux, uy, _ = d
        p.moveTo(QtCore.QPointF(-ux * self.LEN, -uy * self.LEN))
        p.lineTo(QtCore.QPointF(0, 0))
        stroke = stroker.createStroke(p)
        # Include the handle so the whole rotate control is clickable, not
        # just the thin shaft.
        tail = self._tail()
        if tail is not None:
            handle = QtGui.QPainterPath()
            handle.addEllipse(tail, self.HANDLE_R, self.HANDLE_R)
            stroke.addPath(handle)
        return stroke

    def paint(self, painter, option, widget=None):
        if getattr(self.canvas, "show_components", False):
            self._paint_components(painter)
            return
        d = self._dir()
        if not d:
            return
        ux, uy, mag = d
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        col = self.ink(S.APPLIED)
        tail = QtCore.QPointF(-ux * self.LEN, -uy * self.LEN)
        tip = QtCore.QPointF(0.0, 0.0)
        painter.setPen(QtGui.QPen(col, 1.7))
        painter.drawLine(tail, QtCore.QPointF(-ux * self.HEAD, -uy * self.HEAD))
        path = QtGui.QPainterPath()
        _arrow_head(path, tip, ux, uy, self.HEAD)
        painter.setBrush(col)
        painter.setPen(QtCore.Qt.PenStyle.NoPen)
        painter.drawPath(path)
        if self.isSelected():
            # The rotate handle: drag it to aim the force, magnitude fixed.
            ring = S.SELECT
            painter.setPen(QtGui.QPen(ring, 2.0))
            painter.setBrush(QtGui.QColor(255, 255, 255, 235))
            painter.drawEllipse(tail, self.HANDLE_R, self.HANDLE_R)
        if self.canvas.label_loads:
            painter.setFont(S.font(13.0))
            painter.setPen(QtGui.QPen(col))
            text = fmt(mag, "N")
            width = QtGui.QFontMetricsF(S.font(13.0)).horizontalAdvance(text)
            painter.drawText(
                QtCore.QPointF(tail.x() - width / 2 - ux * 6, tail.y() - uy * 6 - 5), text
            )

    def _paint_components(self, painter):
        """Fx and Fy as two separate, axis-aligned arrows: what actually
        acts on the structure in each direction, without the angle of the
        combined vector distracting from the two numbers that matter for a
        by-hand check."""
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        col = self.ink(S.APPLIED)
        for ux, uy, length, value in self._components():
            tail = QtCore.QPointF(-ux * length, -uy * length)
            tip = QtCore.QPointF(0.0, 0.0)
            painter.setPen(QtGui.QPen(col, 1.7))
            painter.drawLine(tail, QtCore.QPointF(-ux * self.HEAD, -uy * self.HEAD))
            path = QtGui.QPainterPath()
            _arrow_head(path, tip, ux, uy, self.HEAD)
            painter.setBrush(col)
            painter.setPen(QtCore.Qt.PenStyle.NoPen)
            painter.drawPath(path)
            if self.canvas.label_loads:
                axis = "Fx" if uy == 0.0 else "Fy"
                text = f"{axis} {fmt(value, chr(78))}"
                painter.setFont(S.font(13.0))
                painter.setPen(QtGui.QPen(col))
                width = QtGui.QFontMetricsF(S.font(13.0)).horizontalAdvance(text)
                painter.drawText(
                    QtCore.QPointF(tail.x() - width / 2 - ux * 6, tail.y() - uy * 6 - 5), text
                )

    # ---- drag-to-rotate ----------------------------------------------

    def _near_handle(self, local_pos) -> bool:
        tail = self._tail()
        if tail is None:
            return False
        return math.hypot(local_pos.x() - tail.x(), local_pos.y() - tail.y()) <= self.HANDLE_R * 1.6

    def mousePressEvent(self, event):
        if self.isSelected() and self._near_handle(event.pos()):
            self._rotating = True
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._rotating:
            l = self.load()
            d = self._dir()
            if l is not None and d is not None:
                _, _, mag = d
                pos = event.pos()
                fx, fy = direction_from_handle(pos.x(), pos.y(), mag)
                l.fx, l.fy = fx, fy
                self.canvas.invalidate_result()
                self.canvas.refresh_geometry()
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._rotating:
            self._rotating = False
            self.canvas.save()
            self.canvas.notify()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def hoverMoveEvent(self, event):
        if self.isSelected() and self._near_handle(event.pos()):
            self.setCursor(QtCore.Qt.CursorShape.CrossCursor)
        else:
            self.unsetCursor()
        super().hoverMoveEvent(event)

    def open_editor(self, scene_pos):
        l = self.load()
        if l is None:
            return
        form = P.PopupForm()
        form.add_spin(
            "Fx",
            l.fx,
            lambda v: self.canvas.edit(lambda: setattr(l, "fx", v)),
            decimals=2,
            suffix="N",
        )
        form.add_spin(
            "Fy",
            l.fy,
            lambda v: self.canvas.edit(lambda: setattr(l, "fy", v)),
            decimals=2,
            suffix="N",
        )
        form.add_readonly("Magnitude", f"{l.magnitude():,.1f} N")
        form.add_note("Drag the small circle on the arrow to rotate it. Applied loads draw red.")
        self.canvas.open_popup(scene_pos, form)


class MomentLoadItem(_Item):
    kind = "moment_load"
    anchored = True
    R = 32.0

    def __init__(self, ident, canvas):
        super().__init__(ident, canvas)
        self.setZValue(40)
        self.sync()

    def load(self):
        return self.model.moment_loads.get(self.ident)

    def anchor_point(self):
        l = self.load()
        if not l:
            return QtCore.QPointF()
        return self.canvas.attachment_scene_pos(l.node, l.anchor)

    def boundingRect(self):
        r = self.R * 2.2
        return QtCore.QRectF(-r, -r, 2 * r, 2 * r)

    def shape(self):
        p = QtGui.QPainterPath()
        p.addEllipse(QtCore.QPointF(0, 0), self.R * 1.35, self.R * 1.35)
        return p

    def paint(self, painter, option, widget=None):
        l = self.load()
        if not l or abs(l.m) < 1e-12:
            return
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        col = self.ink(S.APPLIED)
        draw_moment_arrow(painter, QtCore.QPointF(0, 0), self.R, l.m > 0, col)
        if self.canvas.label_loads:
            painter.setFont(S.font(13.0))
            painter.setPen(QtGui.QPen(col))
            text = fmt(abs(l.m), "N.mm")
            width = QtGui.QFontMetricsF(S.font(13.0)).horizontalAdvance(text)
            painter.drawText(QtCore.QPointF(-width / 2, -self.R - 9.0), text)

    def open_editor(self, scene_pos):
        l = self.load()
        if l is None:
            return
        form = P.PopupForm()
        form.add_spin(
            "M",
            l.m,
            lambda v: self.canvas.edit(lambda: setattr(l, "m", v)),
            decimals=2,
            suffix="N.mm",
            tooltip="Counter-clockwise is positive.",
        )
        self.canvas.open_popup(scene_pos, form)


def draw_moment_arrow(painter, center, radius, ccw, color):
    """Curved arrow, drawn in whatever space the painter is in."""
    painter.setPen(QtGui.QPen(color, 1.7))
    painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
    rect = QtCore.QRectF(center.x() - radius, center.y() - radius, 2 * radius, 2 * radius)
    start = 40 * 16
    span = (-260 * 16) if ccw else (260 * 16)
    painter.drawArc(rect, start, span)
    end_deg = math.radians(-(40 + (-260 if ccw else 260)))
    hx = center.x() + radius * math.cos(end_deg)
    hy = center.y() + radius * math.sin(end_deg)
    tangent = end_deg + (math.pi / 2 if ccw else -math.pi / 2)
    path = QtGui.QPainterPath()
    _arrow_head(path, QtCore.QPointF(hx, hy), math.cos(tangent), math.sin(tangent), radius * 0.5)
    painter.setBrush(color)
    painter.setPen(QtCore.Qt.PenStyle.NoPen)
    painter.drawPath(path)


class LineLoadItem(_Item):
    kind = "line_load"

    def __init__(self, ident, canvas):
        super().__init__(ident, canvas)
        self.setZValue(38)

    def load(self):
        return self.model.line_loads.get(self.ident)

    def geometry(self):
        l = self.load()
        if not l or abs(l.q) < 1e-12:
            return None
        mem = self.model.members.get(l.member)
        if not mem:
            return None
        a = self.model.nodes.get(mem.start)
        b = self.model.nodes.get(mem.end)
        if not a or not b:
            return None
        pa, pb = to_scene(a.x, a.y, self.sc()), to_scene(b.x, b.y, self.sc())
        dx, dy = pb.x() - pa.x(), pb.y() - pa.y()
        length = math.hypot(dx, dy) or 1.0
        if l.direction == "x":
            ux, uy = 1.0, 0.0
        elif l.direction == "perp":
            ux, uy = -dy / length, dx / length
        else:
            ux, uy = 0.0, -1.0
        if l.q < 0:
            ux, uy = -ux, -uy
        height = self.canvas.px(22.0)
        return pa, pb, ux, uy, height

    def boundingRect(self):
        g = self.geometry()
        if not g:
            return QtCore.QRectF()
        pa, pb, ux, uy, h = g
        pad = h + self.canvas.px(26.0)
        return QtCore.QRectF(pa, pb).normalized().adjusted(-pad, -pad, pad, pad)

    def shape(self):
        g = self.geometry()
        p = QtGui.QPainterPath()
        if not g:
            return p
        pa, pb, ux, uy, h = g
        p.addPolygon(
            QtGui.QPolygonF(
                [
                    pa,
                    pb,
                    QtCore.QPointF(pb.x() - ux * h, pb.y() - uy * h),
                    QtCore.QPointF(pa.x() - ux * h, pa.y() - uy * h),
                ]
            )
        )
        return p

    def paint(self, painter, option, widget=None):
        g = self.geometry()
        if not g:
            return
        pa, pb, ux, uy, h = g
        l = self.load()
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        col = self.ink(S.APPLIED)
        pen = QtGui.QPen(col, 1.8)
        pen.setCosmetic(True)
        head = self.canvas.px(12.0)
        n = 7
        tops = []
        for i in range(n):
            f = i / (n - 1)
            bx = pa.x() + (pb.x() - pa.x()) * f
            by = pa.y() + (pb.y() - pa.y()) * f
            tx, ty = bx - ux * h, by - uy * h
            tops.append(QtCore.QPointF(tx, ty))
            painter.setPen(pen)
            painter.drawLine(QtCore.QPointF(tx, ty), QtCore.QPointF(bx - ux * head, by - uy * head))
            path = QtGui.QPainterPath()
            _arrow_head(path, QtCore.QPointF(bx, by), ux, uy, head)
            painter.setBrush(col)
            painter.setPen(QtCore.Qt.PenStyle.NoPen)
            painter.drawPath(path)
        cap = QtGui.QPen(col, 2.6)
        cap.setCosmetic(True)
        painter.setPen(cap)
        painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
        painter.drawPolyline(QtGui.QPolygonF(tops))
        if self.canvas.label_loads:
            mid = tops[len(tops) // 2]
            px_text(
                painter,
                self.canvas,
                QtCore.QPointF(
                    mid.x() - ux * self.canvas.px(4.0), mid.y() - uy * self.canvas.px(4.0)
                ),
                fmt(abs(l.q), "N/mm"),
                col,
                13.0,
                dy=-7.0,
            )

    def open_editor(self, scene_pos):
        l = self.load()
        if l is None:
            return
        form = P.PopupForm()
        form.add_spin(
            "q",
            l.q,
            lambda v: self.canvas.edit(lambda: setattr(l, "q", v)),
            decimals=4,
            suffix="N/mm",
            tooltip="Negative acts downward for a global vertical load.",
        )
        form.add_combo(
            "Direction",
            [
                ("y", "Global vertical"),
                ("x", "Global horizontal"),
                ("perp", "Perpendicular to member"),
            ],
            l.direction,
            lambda v: self.canvas.edit(lambda: setattr(l, "direction", v)),
        )
        member = self.model.members.get(l.member)
        if member:
            total = abs(l.q) * self.model.member_length(member)
            form.add_readonly("Total", f"{total:,.1f} N")
        self.canvas.open_popup(scene_pos, form)


# ---------------------------------------------------------------- results


def _overlay_bounds(canvas) -> QtCore.QRectF:
    sheet = canvas.model.sheet
    rect = QtCore.QRectF(0.0, -sheet.height, sheet.width, sheet.height)
    nodes = canvas.model.nodes.values()
    if nodes:
        xs = [n.x for n in nodes]
        ys = [-n.y for n in nodes]
        bounds = QtCore.QRectF(QtCore.QPointF(min(xs), min(ys)), QtCore.QPointF(max(xs), max(ys)))
        rect = rect.united(bounds)

        active_diagrams = sum(
            [
                getattr(canvas, "show_axial", False),
                getattr(canvas, "show_shear", False),
                getattr(canvas, "show_moment", False),
            ]
        )
        if active_diagrams > 0 and not getattr(canvas, "diagrams_overlay", False):
            height = max(ys) - min(ys)
            offset_y = (height + 20.0) * active_diagrams
            rect = rect.united(bounds.translated(0, offset_y))

        if getattr(canvas, "results_table_pos", None):
            rect = rect.united(QtCore.QRectF(canvas.results_table_pos, QtCore.QSizeF(300, 300)))

    return rect.adjusted(-120, -120, 120, 120)


class ResultOverlay(QtWidgets.QGraphicsItem):
    """Solved reactions: the other half of the free body diagram.

    Kept deliberately quiet. Arrows are always drawn; the numbers appear only
    when the user asks for them, because a page covered in text is unreadable
    and the results table already carries every figure.
    """

    ARROW = 68.0  # pixels
    HEAD = 16.0

    def __init__(self, canvas):
        super().__init__()
        self.canvas = canvas
        self.setZValue(50)
        self.setAcceptedMouseButtons(QtCore.Qt.MouseButton.NoButton)

    def sc(self) -> float:
        sheet = getattr(getattr(self.canvas, "model", None), "sheet", None)
        return getattr(sheet, "unit_scale", 1.0) or 1.0

    def boundingRect(self):
        return _overlay_bounds(self.canvas)

    def _reaction_arrow(self, painter, c, tip, k):
        head = self.HEAD * k
        dx, dy = tip.x() - c.x(), tip.y() - c.y()
        length = math.hypot(dx, dy)
        if length < 1e-9:
            return
        ux, uy = dx / length, dy / length
        pen = QtGui.QPen(S.REACTION, 1.8)
        pen.setCosmetic(True)
        painter.setPen(pen)
        painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
        painter.drawLine(c, QtCore.QPointF(tip.x() - ux * head, tip.y() - uy * head))
        path = QtGui.QPainterPath()
        _arrow_head(path, tip, ux, uy, head)
        painter.setBrush(S.REACTION)
        painter.setPen(QtCore.Qt.PenStyle.NoPen)
        painter.drawPath(path)

    def paint(self, painter, option, widget=None):
        res = self.canvas.display_result
        if not res or not res.ok or not self.canvas.show_reactions:
            return
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        model = self.canvas.model
        k = self.canvas.px(1.0)
        arrow = self.ARROW * k
        components = getattr(self.canvas, "show_components", False)

        for node_id, reaction in res.reactions.items():
            xy = model.entity_xy(node_id)
            if xy is None:
                continue
            c = to_scene(xy[0], xy[1], self.sc())
            mag = reaction.magnitude()
            if mag > 1e-6 and components:
                for value, ux, uy in ((reaction.fx, 1.0, 0.0), (reaction.fy, 0.0, -1.0)):
                    if abs(value) < 1e-6:
                        continue
                    sx, sy = (ux, uy) if value > 0 else (-ux, -uy)
                    length = arrow * abs(value) / mag
                    tip = QtCore.QPointF(c.x() + sx * length, c.y() + sy * length)
                    self._reaction_arrow(painter, c, tip, k)
                    if self.canvas.label_reactions:
                        axis = "Fx" if uy == 0.0 else "Fy"
                        px_text(
                            painter, self.canvas,
                            QtCore.QPointF(tip.x() + sx * 6 * k, tip.y() + sy * 6 * k),
                            f"{axis} {fmt(value, 'N')}", S.REACTION, 13.0, dy=-4.0,
                        )
            elif mag > 1e-6:
                ux, uy = reaction.fx / mag, -reaction.fy / mag
                tip = QtCore.QPointF(c.x() + ux * arrow, c.y() + uy * arrow)
                self._reaction_arrow(painter, c, tip, k)
                if self.canvas.label_reactions:
                    px_text(
                        painter,
                        self.canvas,
                        QtCore.QPointF(tip.x() + ux * 6 * k, tip.y() + uy * 6 * k),
                        fmt(mag, "N"),
                        S.REACTION,
                        13.0,
                        dy=-4.0,
                    )
            if abs(reaction.m) > 1e-6:
                draw_moment_arrow_scene(
                    painter, self.canvas, c, 22.0 * k, reaction.m > 0, S.REACTION
                )
                if self.canvas.label_reactions:
                    px_text(
                        painter,
                        self.canvas,
                        QtCore.QPointF(c.x(), c.y() + 30.0 * k),
                        fmt(abs(reaction.m), "N.mm"),
                        S.REACTION,
                        13.0,
                    )


def draw_moment_arrow_scene(painter, canvas, center, radius, ccw, color):
    pen = QtGui.QPen(color, 1.8)
    pen.setCosmetic(True)
    painter.setPen(pen)
    painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
    rect = QtCore.QRectF(center.x() - radius, center.y() - radius, 2 * radius, 2 * radius)
    painter.drawArc(rect, 40 * 16, (-260 * 16) if ccw else (260 * 16))
    end_deg = math.radians(-(40 + (-260 if ccw else 260)))
    hx = center.x() + radius * math.cos(end_deg)
    hy = center.y() + radius * math.sin(end_deg)
    tangent = end_deg + (math.pi / 2 if ccw else -math.pi / 2)
    path = QtGui.QPainterPath()
    _arrow_head(path, QtCore.QPointF(hx, hy), math.cos(tangent), math.sin(tangent), radius * 0.45)
    painter.setBrush(color)
    painter.setPen(QtCore.Qt.PenStyle.NoPen)
    painter.drawPath(path)


class StructureBoundsOverlay(QtWidgets.QGraphicsItem):
    """Draws dashed blue bounding box around each connected structure on hover/drag."""

    def __init__(self, canvas):
        super().__init__()
        self.canvas = canvas
        self.setZValue(1)
        self.setAcceptedMouseButtons(QtCore.Qt.MouseButton.NoButton)

    def sc(self) -> float:
        sheet = getattr(getattr(self.canvas, "model", None), "sheet", None)
        return getattr(sheet, "unit_scale", 1.0) or 1.0

    def boundingRect(self):
        return _overlay_bounds(self.canvas)

    def paint(self, painter, option, widget=None):
        model = self.canvas.model
        if not model.nodes:
            return

        from ...engine.checks import _components

        components = _components(model)
        if not components:
            return

        hovered_comp = getattr(self.canvas, "_hovered_component", None)

        painter.save()
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)

        sc = getattr(self.canvas, "global_scale", 1.0)
        for comp in components:
            # to_scene, not a raw multiply: unit_scale is a divisor
            # everywhere else drawn, and a sketch import now sets a real
            # one, so multiplying put this box far from the structure.
            pts = [to_scene(model.nodes[n].x, model.nodes[n].y, sc)
                   for n in comp if n in model.nodes]
            if not pts:
                continue
            xs = [p.x() for p in pts]
            ys = [p.y() for p in pts]

            pad = 18.0
            rect = QtCore.QRectF(
                min(xs) - pad,
                min(ys) - pad,
                (max(xs) - min(xs)) + 2 * pad,
                (max(ys) - min(ys)) + 2 * pad,
            )

            is_hovered = (hovered_comp == comp) or (
                self.canvas._dragging_nodes and set(self.canvas._dragging_nodes) == comp
            )

            if is_hovered:
                # Tint inside blue
                painter.setPen(QtCore.Qt.PenStyle.NoPen)
                painter.setBrush(QtGui.QColor(10, 132, 255, 14))
                painter.drawRect(rect)

                # Dashed blue border
                pen = QtGui.QPen(S.SELECT, 1.2, QtCore.Qt.PenStyle.DashLine)
                pen.setCosmetic(True)
                painter.setPen(pen)
                painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
                painter.drawRect(rect)

                # Corner tick: the same resize hint used everywhere else.
                painter.setPen(QtGui.QPen(S.SELECT, 1.1))
                for i in (4.0, 8.0, 12.0):
                    painter.drawLine(
                        QtCore.QPointF(rect.right() - i, rect.bottom()),
                        QtCore.QPointF(rect.right(), rect.bottom() - i),
                    )

        painter.restore()


class ProjectionLinesOverlay(QtWidgets.QGraphicsItem):
    """Vertical dashed gray guide lines from joints/points extending down past diagrams."""

    def __init__(self, canvas):
        super().__init__()
        self.canvas = canvas
        self.setZValue(2)
        self.setAcceptedMouseButtons(QtCore.Qt.MouseButton.NoButton)

    def sc(self) -> float:
        sheet = getattr(getattr(self.canvas, "model", None), "sheet", None)
        return getattr(sheet, "unit_scale", 1.0) or 1.0

    def boundingRect(self):
        return _overlay_bounds(self.canvas)

    def paint(self, painter, option, widget=None):
        is_overlay = getattr(self.canvas, "diagrams_overlay", False)
        res = getattr(self.canvas, "display_result", None)
        active_kinds = [
            k for k in ("axial", "shear", "moment") if getattr(self.canvas, f"show_{k}", False)
        ]

        if is_overlay or not active_kinds or not res or not res.ok:
            return

        model = self.canvas.model
        if not model.nodes:
            return

        # Compute bottom extent across all active diagrams
        max_diagram_y = -1e9
        node_ys = [-n.y for n in model.nodes.values()]
        max_scene_y = max(node_ys, default=0.0)

        for k in active_kinds:
            diag_item = self.canvas.diagram_overlays.get(k)
            if diag_item:
                y = diag_item.pos().y()
                max_diagram_y = max(max_diagram_y, y + max_scene_y + 15.0)

        painter.save()
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, False)
        pen = QtGui.QPen(QtGui.QColor("#b8bec9"), 0.8, QtCore.Qt.PenStyle.DashLine)
        pen.setCosmetic(True)
        painter.setPen(pen)

        # Collect all X positions (Nodes + Anchors)
        x_positions = []
        for n in model.nodes.values():
            x_positions.append((n.x, -n.y))
        for a in model.anchors.values():
            xy = model.anchor_xy(a)
            if xy:
                x_positions.append((xy[0], -xy[1]))

        for x, start_y in x_positions:
            if max_diagram_y > start_y:
                painter.drawLine(QtCore.QPointF(x, start_y), QtCore.QPointF(x, max_diagram_y))

        painter.restore()


class SingleDiagramOverlay(QtWidgets.QGraphicsItem):
    def sc(self):
        return getattr(self.canvas, "global_scale", 1.0)

    """Independently draggable internal force diagram (Axial, Shear, or Moment)."""

    def __init__(self, canvas, kind):
        super().__init__()
        self.canvas = canvas
        self.kind = kind
        self.setZValue(5)
        self.setAcceptHoverEvents(True)
        self._hover = False
        self._setting_pos = False
        self.setFlag(QtWidgets.QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges, True)
        self.setCursor(QtCore.Qt.CursorShape.SizeVerCursor)

    def hoverEnterEvent(self, event):
        self._hover = True
        self.update()
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event):
        self._hover = False
        self.update()
        super().hoverLeaveEvent(event)

    def sync_pos(self):
        is_overlay = getattr(self.canvas, "diagrams_overlay", False)
        if is_overlay:
            self.setFlag(QtWidgets.QGraphicsItem.GraphicsItemFlag.ItemIsMovable, False)
            self._setting_pos = True
            self.setPos(0, 0)
            self._setting_pos = False
        else:
            self.setFlag(QtWidgets.QGraphicsItem.GraphicsItemFlag.ItemIsMovable, True)
            if self.canvas.diagram_user_dragged.get(self.kind, False):
                curr_y = self.canvas.diagram_offsets.get(self.kind, 0.0)
                self._setting_pos = True
                self.setPos(0, curr_y)
                self._setting_pos = False
            else:
                model = self.canvas.model
                node_ys = [-n.y for n in model.nodes.values()] if model.nodes else [0.0]
                max_scene_y = max(node_ys, default=0.0)
                min_scene_y = min(node_ys, default=0.0)
                structure_h = max_scene_y - min_scene_y

                # Guaranteed unique index for each active diagram
                active = [
                    k
                    for k in ("axial", "shear", "moment")
                    if getattr(self.canvas, f"show_{k}", False)
                ]
                order = [
                    k for k in getattr(self.canvas, "diagram_activation_order", []) if k in active
                ]
                for k in active:
                    if k not in order:
                        order.append(k)

                idx = order.index(self.kind) if self.kind in order else 0
                step = max(structure_h, 15.0) + 20.0
                auto_y = (structure_h + 20.0) + idx * step

                self.canvas.diagram_offsets[self.kind] = auto_y
                self._setting_pos = True
                self.setPos(0, auto_y)
                self._setting_pos = False

    def itemChange(self, change, value):
        if change == QtWidgets.QGraphicsItem.GraphicsItemChange.ItemPositionChange and self.scene():
            if not getattr(self, "_setting_pos", False):
                is_overlay = getattr(self.canvas, "diagrams_overlay", False)
                if not is_overlay:
                    # User manually dragged this item!
                    self.canvas.diagram_user_dragged[self.kind] = True
                    self.canvas.diagram_offsets[self.kind] = value.y()
                    return QtCore.QPointF(0.0, value.y())
        return super().itemChange(change, value)

    def boundingRect(self):
        if not getattr(self.canvas, f"show_{self.kind}", False):
            return QtCore.QRectF()
        res = getattr(self.canvas, "display_result", None)
        if not res or not res.ok:
            return QtCore.QRectF()
        model = self.canvas.model
        if not model.nodes or not model.members:
            return QtCore.QRectF()

        peak = max(
            (
                max((abs(v) for v in getattr(mf, self.kind)), default=0.0)
                for mf in res.members.values()
                if getattr(mf, self.kind)
            ),
            default=0.0,
        )

        pts_x = []
        pts_y = []

        for mid, mf in res.members.items():
            member = model.members.get(mid)
            if not member:
                continue
            a, b = model.nodes.get(member.start), model.nodes.get(member.end)
            if not a or not b:
                continue
            pa, pb = to_scene(a.x, a.y, self.sc()), to_scene(b.x, b.y, self.sc())
            pts_x.extend([pa.x(), pb.x()])
            pts_y.extend([pa.y(), pb.y()])

            values = getattr(mf, self.kind)
            if values and peak > 1e-12:
                dx, dy = pb.x() - pa.x(), pb.y() - pa.y()
                length = math.hypot(dx, dy) or 1.0
                nx, ny = -dy / length, dx / length
                for i, v in enumerate(values):
                    f = i / max(1, len(values) - 1)
                    off = (v / peak) * 10.0
                    pts_x.append(pa.x() + dx * f + nx * off)
                    pts_y.append(pa.y() + dy * f + ny * off)

        if not pts_x:
            return QtCore.QRectF()

        min_x = min(pts_x) - 20.0  # Left margin for title
        max_x = max(pts_x) + 10.0
        min_y = min(pts_y) - 10.0
        max_y = max(pts_y) + 10.0

        return QtCore.QRectF(min_x, min_y, max_x - min_x, max_y - min_y)

    def paint(self, painter, option, widget=None):
        if not getattr(self.canvas, f"show_{self.kind}", False):
            return
        res = getattr(self.canvas, "display_result", None)
        if not res or not res.ok:
            return

        model = self.canvas.model
        fill = {"moment": S.MOMENT_FILL, "shear": S.SHEAR_FILL, "axial": S.AXIAL_FILL}[self.kind]
        edge = {"moment": S.REACTION, "shear": S.INTERNAL, "axial": S.APPLIED}[self.kind]

        node_ys = [-n.y for n in model.nodes.values()]
        max_scene_y = max(node_ys, default=0.0)
        min_scene_y = min(node_ys, default=0.0)

        peak = max(
            (
                max((abs(v) for v in getattr(mf, self.kind)), default=0.0)
                for mf in res.members.values()
                if getattr(mf, self.kind)
            ),
            default=0.0,
        )
        is_zero = peak < 1e-12
        is_overlay = getattr(self.canvas, "diagrams_overlay", False)

        painter.save()

        title = {"moment": "Moment", "shear": "Shear", "axial": "Axial"}[self.kind]
        if is_zero:
            title += " (0)"

        min_x = min((n.x for n in model.nodes.values()), default=0.0)

        # Base line for members
        base_pen = QtGui.QPen(S.INK_LIGHT, 0.5)
        base_pen.setCosmetic(True)
        for member in model.members.values():
            a, b = model.nodes.get(member.start), model.nodes.get(member.end)
            if a and b:
                painter.setPen(base_pen)
                painter.drawLine(to_scene(a.x, a.y, self.sc()), to_scene(b.x, b.y, self.sc()))

        # Sideways Title on Left
        if not is_overlay:
            mid_y = (min_scene_y + max_scene_y) / 2.0
            painter.save()
            painter.translate(min_x - 15.0, mid_y)
            painter.rotate(-90)
            px_text(
                painter,
                self.canvas,
                QtCore.QPointF(0, 0),
                title,
                edge,
                size_pt=11.0,
                bold=True,
                centre=True,
            )
            painter.restore()

        if is_zero:
            # Explicitly label zero on members
            for mid, mf in res.members.items():
                member = model.members.get(mid)
                if not member:
                    continue
                a, b = model.nodes.get(member.start), model.nodes.get(member.end)
                if not a or not b:
                    continue
                pa, pb = to_scene(a.x, a.y, self.sc()), to_scene(b.x, b.y, self.sc())
                mid_pt = QtCore.QPointF((pa.x() + pb.x()) / 2.0, (pa.y() + pb.y()) / 2.0)
                unit = "N" if self.kind in ("axial", "shear") else "N.mm"
                px_text(painter, self.canvas, mid_pt, f"0 {unit}", edge, size_pt=9.0, centre=True)
        else:
            # Draw Non-Zero Graph Polygons
            painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
            pen = QtGui.QPen(edge, 1.1)
            pen.setCosmetic(True)

            for mid, mf in res.members.items():
                member = model.members.get(mid)
                values = getattr(mf, self.kind)
                if not member or not values:
                    continue
                a, b = model.nodes.get(member.start), model.nodes.get(member.end)
                if not a or not b:
                    continue

                pa, pb = to_scene(a.x, a.y, self.sc()), to_scene(b.x, b.y, self.sc())
                dx, dy = pb.x() - pa.x(), pb.y() - pa.y()
                length = math.hypot(dx, dy) or 1.0
                nx, ny = -dy / length, dx / length
                poly = [pa]

                for i, v in enumerate(values):
                    f = i / max(1, len(values) - 1)
                    off = (v / peak) * 10.0
                    poly.append(
                        QtCore.QPointF(pa.x() + dx * f + nx * off, pa.y() + dy * f + ny * off)
                    )
                poly.append(pb)

                painter.setBrush(fill)
                painter.setPen(pen)
                painter.drawPolygon(QtGui.QPolygonF(poly))

                # Peak Values on Graph
                peaks = set()
                if max(values) > 1e-6:
                    peaks.add(max(values))
                if min(values) < -1e-6:
                    peaks.add(min(values))

                for ext_val in peaks:
                    idx = values.index(ext_val)
                    f = idx / max(1, len(values) - 1)
                    off = (ext_val / peak) * 10.0

                    vx = pa.x() + dx * f + nx * off
                    vy = pa.y() + dy * f + ny * off

                    unit = "N" if self.kind in ("axial", "shear") else "N.mm"
                    text = fmt(abs(ext_val), unit)

                    sign = 1 if ext_val >= 0 else -1
                    nudge = self.canvas.px(6.0)
                    cx = vx + nx * sign * nudge
                    cy = vy + ny * sign * nudge

                    px_text(
                        painter,
                        self.canvas,
                        QtCore.QPointF(cx, cy),
                        text,
                        edge,
                        size_pt=9.0,
                        centre=True,
                    )

        painter.restore()

        # Hover Box Indicator (Dashed Blue Box)
        if self._hover and not is_overlay:
            painter.save()
            hover_pen = QtGui.QPen(S.SELECT, 1.0, QtCore.Qt.PenStyle.DashLine)
            hover_pen.setCosmetic(True)
            painter.setPen(hover_pen)
            painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
            painter.drawRect(self.boundingRect().adjusted(1, 1, -1, -1))
            painter.restore()


class ResultsTableOverlay(QtWidgets.QGraphicsItem):
    """Draggable, resizable results table on the page like a schedule."""

    MIN_SCALE = 0.6
    MAX_SCALE = 3.0
    HANDLE = 12.0  # pixels: corner grab zone for resizing

    def __init__(self, canvas):
        super().__init__()
        self.canvas = canvas
        self.setZValue(60)
        self.setFlag(QtWidgets.QGraphicsItem.GraphicsItemFlag.ItemIsMovable, True)
        self.setFlag(QtWidgets.QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations, True)
        self.setFlag(QtWidgets.QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges, True)
        self.setAcceptHoverEvents(True)
        self.setCursor(QtCore.Qt.CursorShape.OpenHandCursor)
        self._rect = QtCore.QRectF(0, 0, 150, 60)
        self._drag_start = None
        self._resizing = False
        self._resize_start_screen = None
        self._resize_start_scale = 1.0

        if getattr(self.canvas, "results_table_pos", None) is not None:
            self.setPos(self.canvas.results_table_pos)
        else:
            sheet = self.canvas.scene.sheet_rect()
            self.setPos(sheet.right() - 170, sheet.top() + 15)

    def sc(self):
        return getattr(self.canvas, "global_scale", 1.0)

    @property
    def table_scale(self):
        return max(
            self.MIN_SCALE, min(self.MAX_SCALE, getattr(self.canvas, "results_table_scale", 1.0))
        )

    def boundingRect(self):
        return getattr(self, "_rect", QtCore.QRectF(0, 0, 150, 60))

    def _near_corner(self, pos) -> bool:
        r = self._rect
        return (r.right() - pos.x()) <= self.HANDLE and (r.bottom() - pos.y()) <= self.HANDLE

    def hoverMoveEvent(self, event):
        if self._near_corner(event.pos()):
            self.setCursor(QtCore.Qt.CursorShape.SizeFDiagCursor)
        else:
            self.setCursor(QtCore.Qt.CursorShape.OpenHandCursor)
        super().hoverMoveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == QtCore.Qt.MouseButton.LeftButton:
            if self._near_corner(event.pos()):
                self._resizing = True
                self._resize_start_screen = event.screenPos()
                self._resize_start_scale = self.table_scale
                self.setCursor(QtCore.Qt.CursorShape.SizeFDiagCursor)
                event.accept()
                return
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
            factor = 1.0 + span / 140.0
            new_scale = max(self.MIN_SCALE, min(self.MAX_SCALE, self._resize_start_scale * factor))
            self.canvas.results_table_scale = new_scale
            self.prepareGeometryChange()
            self.update()
            event.accept()
            return
        if getattr(self, "_drag_start", None) is not None:
            curr_screen = event.screenPos()
            delta_pixels = curr_screen - self._drag_start
            scale = self.canvas.view.transform().m11() or 1.0
            dx = delta_pixels.x() / scale
            dy = delta_pixels.y() / scale
            new_pos = QtCore.QPointF(self._pos_start.x() + dx, self._pos_start.y() + dy)
            self.setPos(new_pos)
            self.canvas.results_table_pos = new_pos
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._resizing:
            self._resizing = False
            self.setCursor(QtCore.Qt.CursorShape.OpenHandCursor)
            self.canvas.save()
            self.canvas.notify()
            event.accept()
            return
        if getattr(self, "_drag_start", None) is not None:
            self._drag_start = None
            self.setCursor(QtCore.Qt.CursorShape.OpenHandCursor)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def paint(self, painter, option, widget=None):
        if not getattr(self.canvas, "show_results_table", True):
            return
        rows = self.canvas.result_rows()
        if not rows:
            return

        painter.save()
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)

        scale = self.table_scale
        f_bold = QtGui.QFont("DejaVu Sans")
        f_bold.setPixelSize(max(6, round(10 * scale)))
        f_bold.setBold(True)
        f_norm = QtGui.QFont("DejaVu Sans")
        f_norm.setPixelSize(max(6, round(9 * scale)))

        fm_norm = QtGui.QFontMetricsF(f_norm)
        pad = 5.0 * scale

        col1_w = max((fm_norm.horizontalAdvance(str(r[0])) for r in rows), default=45) + pad * 2
        col2_w = max((fm_norm.horizontalAdvance(str(r[1])) for r in rows), default=35) + pad * 2
        col3_w = max((fm_norm.horizontalAdvance(str(r[2])) for r in rows), default=55) + pad * 2

        table_w = col1_w + col2_w + col3_w
        line_h = 14.0 * scale
        table_h = (len(rows) + 1) * line_h
        self._rect = QtCore.QRectF(0, 0, table_w, table_h)

        # Background
        painter.setPen(QtGui.QPen(S.INK, 1.0))
        painter.setBrush(QtGui.QColor(255, 255, 255, 240))
        painter.drawRect(self._rect)

        # Header
        painter.setFont(f_bold)
        painter.setPen(S.INK)
        painter.drawText(
            QtCore.QRectF(pad, 0, table_w, line_h),
            int(QtCore.Qt.AlignmentFlag.AlignVCenter | QtCore.Qt.AlignmentFlag.AlignLeft),
            "Results",
        )
        painter.drawLine(QtCore.QPointF(0, line_h), QtCore.QPointF(table_w, line_h))

        # Content Rows
        cy = line_h
        painter.setFont(f_norm)
        for i, (col1, col2, col3) in enumerate(rows):
            painter.setPen(S.INK)
            painter.drawText(
                QtCore.QRectF(pad, cy, col1_w - pad, line_h),
                int(QtCore.Qt.AlignmentFlag.AlignVCenter | QtCore.Qt.AlignmentFlag.AlignLeft),
                str(col1),
            )
            painter.drawText(
                QtCore.QRectF(col1_w + pad, cy, col2_w - pad, line_h),
                int(QtCore.Qt.AlignmentFlag.AlignVCenter | QtCore.Qt.AlignmentFlag.AlignLeft),
                str(col2),
            )
            painter.setPen(S.INK_LIGHT)
            painter.drawText(
                QtCore.QRectF(col1_w + col2_w + pad, cy, col3_w - pad, line_h),
                int(QtCore.Qt.AlignmentFlag.AlignVCenter | QtCore.Qt.AlignmentFlag.AlignLeft),
                str(col3),
            )

            cy += line_h
            if i < len(rows) - 1:
                painter.setPen(QtGui.QPen(S.INK, 0.5))
                painter.drawLine(QtCore.QPointF(0, cy), QtCore.QPointF(table_w, cy))

        # Vertical Separators
        painter.setPen(QtGui.QPen(S.INK, 1.0))
        painter.drawLine(QtCore.QPointF(col1_w, line_h), QtCore.QPointF(col1_w, table_h))
        painter.drawLine(
            QtCore.QPointF(col1_w + col2_w, line_h), QtCore.QPointF(col1_w + col2_w, table_h)
        )

        # Resize handle: three diagonal ticks in the bottom-right corner.
        painter.setPen(QtGui.QPen(S.INK_LIGHT, 1.1))
        for i in (4.0, 8.0, 12.0):
            painter.drawLine(
                QtCore.QPointF(table_w - i, table_h),
                QtCore.QPointF(table_w, table_h - i),
            )

        painter.restore()


class DiagramResizeOverlay(QtWidgets.QGraphicsItem):
    """A handle at the corner of the whole diagram, for resizing it on the
    page by eye.

    Only ever changes canvas.unit_scale, the model-mm-per-paper-mm display
    divisor: a joint's own x, y stay exactly what the sketch or the solver
    need them to be. Every node is translated together as the scale changes,
    purely to keep the far corner of the diagram fixed on screen while the
    near corner follows the cursor, the way any bounding-box resize handle
    behaves; translation alone changes no distance, so nothing about the
    physics moves.
    """

    def __init__(self, canvas):
        super().__init__()
        self.canvas = canvas
        self.setZValue(15)
        self.setAcceptHoverEvents(True)
        self._dragging = False
        self._hover = False

    def _corner_model(self):
        bounds = self.canvas.diagram_bounds_model()
        if bounds is None:
            return None
        _min_x, min_y, max_x, _max_y = bounds
        return max_x, min_y  # bottom-right: a bigger y is up

    def _corner_scene(self):
        corner = self._corner_model()
        if corner is None:
            return None
        return to_scene(corner[0], corner[1], self.canvas.unit_scale)

    def boundingRect(self):
        c = self._corner_scene()
        if c is None:
            return QtCore.QRectF()
        r = self.canvas.px(16.0)
        return QtCore.QRectF(c.x() - r, c.y() - r, 2 * r, 2 * r)

    def shape(self):
        p = QtGui.QPainterPath()
        c = self._corner_scene()
        if c is not None:
            p.addEllipse(c, self.canvas.px(9.0), self.canvas.px(9.0))
        return p

    def paint(self, painter, option, widget=None):
        if not self.canvas.model.nodes:
            return
        c = self._corner_scene()
        if c is None:
            return
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        active = self._hover or self._dragging
        r = self.canvas.px(5.5 if active else 4.2)
        col = S.SELECT if active else S.INK_LIGHT
        painter.setPen(QtGui.QPen(col, 1.4))
        painter.setBrush(QtGui.QColor(255, 255, 255, 220))
        painter.drawEllipse(c, r, r)
        tick = self.canvas.px(4.5)
        painter.drawLine(QtCore.QPointF(c.x() - tick, c.y() + tick),
                         QtCore.QPointF(c.x() + tick, c.y() - tick))

    def hoverEnterEvent(self, event):
        self._hover = True
        self.setCursor(QtCore.Qt.CursorShape.SizeFDiagCursor)
        self.update()
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event):
        self._hover = False
        self.update()
        super().hoverLeaveEvent(event)

    def mousePressEvent(self, event):
        if (event.button() == QtCore.Qt.MouseButton.LeftButton
                and self.canvas.begin_diagram_resize()):
            self._dragging = True
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._dragging:
            self.canvas.update_diagram_resize(event.scenePos())
            self.prepareGeometryChange()
            self.update()
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._dragging:
            self._dragging = False
            self.canvas.end_diagram_resize()
            event.accept()
            return
        super().mouseReleaseEvent(event)
