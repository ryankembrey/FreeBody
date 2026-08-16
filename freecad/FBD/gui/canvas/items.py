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


def format_angle_label(deg):
    deg = deg % 360.0
    norm = round(deg, 1)
    if norm.is_integer():
        return f"{int(norm)}°"
    return f"{norm:.1f}°"


def direction_from_handle_snapped(mx, my, magnitude, modifiers=None):
    r = math.hypot(mx, my)
    if r < 1e-9:
        return 0.0, 0.0, 0.0, False

    raw_math_rad = math.atan2(my / r, -mx / r)
    raw_math_deg = math.degrees(raw_math_rad) % 360.0
    raw_angle_from_down = (raw_math_deg + 90.0) % 360.0

    shift_held = False
    ctrl_held = False
    if modifiers is not None:
        shift_held = bool(modifiers & QtCore.Qt.KeyboardModifier.ShiftModifier)
        ctrl_held = bool(modifiers & (QtCore.Qt.KeyboardModifier.ControlModifier | QtCore.Qt.KeyboardModifier.AltModifier))

    if ctrl_held:
        final_angle_from_down = raw_angle_from_down
        is_snapped = False
    elif shift_held:
        step = 45.0
        final_angle_from_down = round(raw_angle_from_down / step) * step % 360.0
        is_snapped = True
    else:
        candidate = round(raw_angle_from_down / 15.0) * 15.0 % 360.0
        diff = abs((raw_angle_from_down - candidate + 180.0) % 360.0 - 180.0)
        thresh = 6.0 if (candidate % 45.0 == 0) else 4.0
        if diff <= thresh:
            final_angle_from_down = candidate
            is_snapped = True
        else:
            final_angle_from_down = raw_angle_from_down
            is_snapped = False

    final_math_deg = (final_angle_from_down - 90.0) % 360.0
    math_rad = math.radians(final_math_deg)
    
    fx = magnitude * math.cos(math_rad)
    fy = magnitude * math.sin(math_rad)
    return fx, fy, final_angle_from_down, is_snapped


def direction_from_handle(mx, my, magnitude):
    fx, fy, _deg, _snapped = direction_from_handle_snapped(mx, my, magnitude, modifiers=None)
    return fx, fy


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
    angle=0.0,
):
    """Draw text at a scene point, at constant screen size.

    angle rotates it about that point, in degrees, for a label meant to
    read along a member's own direction rather than sit flat on the page.
    """
    k = canvas.px(1.0)
    painter.save()
    painter.translate(at)
    if angle:
        painter.rotate(angle)
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
        model = getattr(self.canvas, "model", None)
        sheet = getattr(model, "sheet", None) if model else None
        return getattr(sheet, "unit_scale", 1.0) if sheet else 1.0

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
        _ = event
        self._hover = True
        self.update()

    def hoverLeaveEvent(self, event):
        _ = event
        self._hover = False
        self.update()

    def ink(self, base):
        if self.isSelected():
            return S.SELECT
        if self._hover:
            return S.HOVER
        return base


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
        _ = (option, widget)
        n = self.node()
        if n is None:
            return
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        col = self.ink(S.INK)
        painter.setBrush(S.PAPER)
        painter.setPen(QtGui.QPen(col, 1.6))
        if getattr(n, "rigid", False):
            # Solid, not open: welded, the way a filled joint marker reads
            # in ordinary engineering drawing convention, next to the open
            # circle every hinged joint already uses.
            painter.setBrush(col)
            r = self.R * 0.82
            painter.drawRect(QtCore.QRectF(-r, -r, 2 * r, 2 * r))
        else:
            painter.drawEllipse(QtCore.QPointF(0, 0), self.R, self.R)
        if self.canvas.show_labels:
            f = S.font(13.0)
            painter.setFont(f)
            painter.setPen(QtGui.QPen(S.INK_LIGHT))
            painter.drawText(QtCore.QPointF(self.R + 5.0, -self.R - 4.0), n.label)

        if self.isSelected():
            result = getattr(self.canvas, "motion_result", None)
            if result and result.ok and result.frames:
                frame = result.frame_at(getattr(self.canvas, "motion_time", 0.0))
                pos = frame.positions.get(self.ident)
                if pos is not None:
                    dist = math.hypot(pos[0] - n.x, pos[1] - n.y)
                    if dist > 1e-3:
                        painter.setFont(S.font(12.0))
                        painter.setPen(QtGui.QPen(QtGui.QColor("#00897b")))
                        painter.drawText(
                            QtCore.QPointF(self.R + 5.0, self.R + 16.0),
                            fmt(dist, "mm") + " moved",
                        )

    def open_editor(self, _scene_pos):
        _ = _scene_pos
        node = self.node()
        if node is None:
            return
        from ..commands import icon_path
        form = P.PopupForm("Edit Joint", icon_path("tool_node.svg"))
        form.add_text(
            "Name", node.label, lambda v: self.canvas.edit(lambda: setattr(node, "label", v)),
            tooltip="Name of the joint"
        )
        form.add_spin(
            "X coordinate", node.x, lambda v: self.canvas.edit(lambda: setattr(node, "x", v)), suffix="mm", tooltip="X coordinate"
        )
        form.add_spin(
            "Y coordinate", node.y, lambda v: self.canvas.edit(lambda: setattr(node, "y", v)), suffix="mm", tooltip="Y coordinate"
        )
        form.add_combo(
            "Connection",
            [(False, "Free (hinges)"), (True, "Rigid (welded)")],
            node.rigid,
            lambda v: self.canvas.edit(lambda: setattr(node, "rigid", v), rebuild=True),
            tooltip="Rigid welds every member meeting here into one body, without grounding the joint itself. For a joint fixed to the world, use a Fixed support instead.",
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
        _ = (option, widget)
        a, b = self.ends()
        if a == b:
            return
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        pen = QtGui.QPen(self.ink(S.INK), 3.4)
        pen.setCosmetic(True)  # constant thickness at any zoom
        pen.setCapStyle(QtCore.Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.drawLine(a, b)

        # A rotation reading during motion, alongside the existing axial
        # annotation below -- the same "selected only" rule, extended.
        if self.isSelected():
            motion_result = getattr(self.canvas, "motion_result", None)
            if motion_result and motion_result.ok and motion_result.frames:
                m = self.member()
                a0 = self.model.nodes.get(m.start) if m else None
                b0 = self.model.nodes.get(m.end) if m else None
                frame = motion_result.frame_at(getattr(self.canvas, "motion_time", 0.0))
                a1 = frame.positions.get(m.start) if m else None
                b1 = frame.positions.get(m.end) if m else None
                if a0 and b0 and a1 and b1:
                    ang0 = math.degrees(math.atan2(b0.y - a0.y, b0.x - a0.x))
                    ang1 = math.degrees(math.atan2(b1[1] - a1[1], b1[0] - a1[0]))
                    delta = ((ang1 - ang0 + 180.0) % 360.0) - 180.0
                    if abs(delta) > 0.05:
                        mid_pt = QtCore.QPointF((a.x() + b.x()) / 2, (a.y() + b.y()) / 2)
                        dx, dy = b.x() - a.x(), b.y() - a.y()
                        length = math.hypot(dx, dy) or 1.0
                        nx, ny = -dy / length, dx / length
                        if ny < 0:
                            nx, ny = -nx, -ny
                        off = self.canvas.px(14.0)
                        px_text(
                            painter,
                            self.canvas,
                            QtCore.QPointF(mid_pt.x() - nx * off, mid_pt.y() - ny * off),
                            fmt(delta, "deg") + " rotated",
                            QtGui.QColor("#00897b"),
                            13.0,
                        )

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

    def open_editor(self, _scene_pos):
        member = self.member()
        if member is None:
            return
        model = self.canvas.model
        from ..commands import icon_path
        form = P.PopupForm("Edit Member", icon_path("tool_member.svg"))
        form.add_text(
            "Name", member.label, lambda v: self.canvas.edit(lambda: setattr(member, "label", v)),
            tooltip="Name of the member"
        )
        form.add_readonly("Length", f"{model.member_length(member):,.1f} mm", tooltip="Length of the member")
        form.add_section("Stiffness")
        form.add_spin(
            "EA",
            member.EA,
            lambda v: self.canvas.edit(lambda: setattr(member, "EA", v)),
            lo=1.0,
            decimals=0,
            suffix="N",
            tooltip="Axial stiffness (EA). Only changes the answer for statically indeterminate structures."
        )
        form.add_spin(
            "EI",
            member.EI,
            lambda v: self.canvas.edit(lambda: setattr(member, "EI", v)),
            lo=1.0,
            decimals=0,
            suffix="N.mm2",
            tooltip="Bending stiffness (EI). Only changes the answer for statically indeterminate structures."
        )
        form.add_section("Behaviour")
        form.add_combo(
            "Type",
            [(k, M.BEHAVIOUR_LABELS[k]) for k in M.BEHAVIOURS],
            member.behaviour,
            lambda v: self.canvas.edit(lambda: setattr(member, "behaviour", v)),
            tooltip="Normal carries both; a cable cannot push, a strut cannot pull.",
        )
        for label, attr in (("Start hinge", "release_start"), ("End hinge", "release_end")):
            form.add_combo(
                label,
                [(0, "Rigid"), (1, "Hinged")],
                1 if getattr(member, attr) else 0,
                lambda v, a=attr: self.canvas.edit(
                    lambda: setattr(member, a, bool(v)), rebuild=True
                ),
                tooltip=f"Moment release at {label.split()[0].lower()}"
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
        form.add_section("Physical")
        form.add_spin(
            "Self weight",
            member.g,
            lambda v: self.canvas.edit(lambda: setattr(member, "g", v)),
            lo=0.0,
            decimals=4,
            suffix="N/mm",
            tooltip="Self weight downward force per unit length. Only changes the answer for statically indeterminate structures."
        )
        form.add_spin(
            "Mass",
            member.mass,
            lambda v: self.canvas.edit(lambda: setattr(member, "mass", v)),
            lo=0.0,
            decimals=3,
            suffix="kg",
            tooltip="Only used by Run Motion: a fast-moving link's own inertia adds to the force its driver needs.",
        )
        self.canvas.open_popup(_scene_pos, form)


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
        xy = self.model.support_xy(sup)  # joint or point on a member
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
        _ = (option, widget)
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
        # Vertical stem connecting joint (0, 0) to ground plate at y = size
        stem_pen = QtGui.QPen(painter.pen().color(), 3.0)
        stem_pen.setCapStyle(QtCore.Qt.PenCapStyle.SquareCap)
        painter.setPen(stem_pen)
        painter.drawLine(QtCore.QPointF(0, 0), QtCore.QPointF(0, size))
        
        # Ground base plate at y = size (30px below joint, matching Pin/Roller)
        a, b = QtCore.QPointF(-size * 0.85, size), QtCore.QPointF(size * 0.85, size)
        plate_pen = QtGui.QPen(painter.pen().color(), 2.2)
        painter.setPen(plate_pen)
        painter.drawLine(a, b)
        
        # Ground hatching
        hatch_pen = QtGui.QPen(painter.pen().color(), 1.3)
        painter.setPen(hatch_pen)
        _hatch(painter, a, b, size * 0.4, count=8)

    def _spring(self, painter, size):
        coils, width = 4, size * 0.4
        bottom = size
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
        a = QtCore.QPointF(-size * 0.85, bottom)
        b = QtCore.QPointF(size * 0.85, bottom)
        painter.drawLine(a, b)
        _hatch(painter, a, b, size * 0.4)

    def _set_kind(self, support, kind):
        def apply():
            support.kind = kind
            if kind == M.SPRING and not support.reaction_components():
                support.ky = 1000.0

        self.canvas.edit(apply, rebuild=True)

    def open_editor(self, _scene_pos):
        _ = _scene_pos
        support = self.support()
        if support is None:
            return
        from ..commands import icon_path
        icon_name = f"tool_{support.kind if support.kind in ('pin', 'fixed', 'spring') else 'roller'}.svg"
        form = P.PopupForm("Edit Support", icon_path(icon_name))
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
                ("Spring kx", "kx", "N/mm"),
                ("Spring ky", "ky", "N/mm"),
                ("Spring kr", "kr", "N.mm/rad"),
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
        _ = (option, widget)
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

    def open_editor(self, _scene_pos):
        _ = _scene_pos
        a = self.anchor()
        if a is None:
            return
        member = self.model.members.get(a.member)
        from ..commands import icon_path
        form = P.PopupForm("Edit Point", icon_path("tool_anchor.svg"))
        form.add_text("Name", a.label, lambda v: self.canvas.edit(lambda: setattr(a, "label", v)), tooltip="Name of the point")
        form.add_spin(
            "Position",
            a.t * 100.0,
            lambda v: self.canvas.edit(lambda: setattr(a, "t", max(0.0, min(1.0, v / 100.0)))),
            lo=0.0,
            hi=100.0,
            decimals=1,
            suffix="%",
            tooltip="Distance along the member from its start, as a percentage. Loads can attach here the same as to a joint.",
        )
        if member:
            form.add_readonly("Along member", member.label, tooltip="Member this point lies on")
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
        self._current_angle = 0.0
        self._is_snapped = False
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
        r = self.LEN + 50.0
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
            uy = -1.0 if l.fy > 0 else 1.0  # screen y is flipped from model y
            out.append((0.0, uy, self.LEN * abs(l.fy) / mag, l.fy))
        return out

    def shape(self):
        p = QtGui.QPainterPath()
        stroker = QtGui.QPainterPathStroker()
        stroker.setWidth(9.0)
        if getattr(self.canvas, "show_components", False):
            for ux, uy, length, _ in self._components():
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

    def _paint_rotation_guides(self, painter, ux, uy, tail):
        painter.save()
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        
        r = self.LEN
        
        # 1. Subtle dashed protractor circle around tip (0, 0)
        circle_pen = QtGui.QPen(QtGui.QColor(180, 190, 205, 120), 1.0, QtCore.Qt.PenStyle.DashLine)
        circle_pen.setCosmetic(True)
        painter.setPen(circle_pen)
        painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
        painter.drawEllipse(QtCore.QPointF(0, 0), r, r)
        
        # 2. Radial guide rays for 8 major directions (0, 45, 90, 135, 180, 225, 270, 315 relative to down)
        ray_pen = QtGui.QPen(QtGui.QColor(180, 190, 205, 100), 1.0, QtCore.Qt.PenStyle.DashLine)
        ray_pen.setCosmetic(True)
        painter.setPen(ray_pen)
        
        for angle_from_down in (0, 45, 90, 135, 180, 225, 270, 315):
            math_rad = math.radians(angle_from_down - 90.0)
            tx = -math.cos(math_rad) * r
            ty = math.sin(math_rad) * r
            painter.drawLine(QtCore.QPointF(tx * 0.8, ty * 0.8), QtCore.QPointF(tx * 1.1, ty * 1.1))
            
        # 3. Handle Ring (Selection blue S.SELECT)
        ring_col = S.SELECT
        painter.setPen(QtGui.QPen(ring_col, 2.0))
        painter.setBrush(QtGui.QColor(255, 255, 255, 240))
        painter.drawEllipse(tail, self.HANDLE_R * 1.15, self.HANDLE_R * 1.15)
        
        # 4. Angle text OUTWARD of the circle (beyond tail handle, screen-constant font size)
        angle_deg = getattr(self, "_current_angle", 0.0)
        text = format_angle_label(angle_deg)
        
        offset = self.HANDLE_R + 14.0
        text_pos = QtCore.QPointF(-ux * (self.LEN + offset), -uy * (self.LEN + offset))
        
        f = S.font(13.0, bold=True)
        metrics = QtGui.QFontMetricsF(f)
        rect = metrics.boundingRect(text)
        
        x = text_pos.x() - rect.width() / 2.0
        y = text_pos.y() + metrics.capHeight() / 2.0
        
        path = QtGui.QPainterPath()
        path.addText(x, y, f, text)
        
        # White background stroke halo for crisp contrast
        painter.setPen(QtGui.QPen(S.PAPER, 3.0, QtCore.Qt.PenStyle.SolidLine, QtCore.Qt.PenCapStyle.RoundCap, QtCore.Qt.PenJoinStyle.RoundJoin))
        painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
        painter.drawPath(path)
        
        # Text fill in selection blue
        painter.setPen(QtCore.Qt.PenStyle.NoPen)
        painter.setBrush(S.SELECT)
        painter.drawPath(path)
        
        painter.restore()

    def paint(self, painter, option, widget=None):
        _ = (option, widget)
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

        if getattr(self, "_rotating", False):
            self._paint_rotation_guides(painter, ux, uy, tail)

        painter.setPen(QtGui.QPen(col, 1.7))
        painter.drawLine(tail, QtCore.QPointF(-ux * self.HEAD, -uy * self.HEAD))
        path = QtGui.QPainterPath()
        _arrow_head(path, tip, ux, uy, self.HEAD)
        painter.setBrush(col)
        painter.setPen(QtCore.Qt.PenStyle.NoPen)
        painter.drawPath(path)

        if self.isSelected() and not getattr(self, "_rotating", False):
            ring = S.SELECT
            painter.setPen(QtGui.QPen(ring, 2.0))
            painter.setBrush(QtGui.QColor(255, 255, 255, 235))
            painter.drawEllipse(tail, self.HANDLE_R, self.HANDLE_R)

        if self.canvas.label_loads and not getattr(self, "_rotating", False):
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
                modifiers = event.modifiers()
                fx, fy, deg, is_snapped = direction_from_handle_snapped(
                    pos.x(), pos.y(), mag, modifiers
                )
                l.fx, l.fy = fx, fy
                self._current_angle = deg
                self._is_snapped = is_snapped
                self.canvas.invalidate_result()
                self.canvas.refresh_geometry()
                self.update()
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._rotating:
            self._rotating = False
            self.canvas.save()
            self.canvas.notify()
            self.update()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def open_editor(self, _scene_pos):
        l = self.load()
        if l is None:
            return
        from ..commands import icon_path
        form = P.PopupForm("Edit Force", icon_path("tool_force.svg"))
        form.add_spin(
            "Fx",
            l.fx,
            lambda v: self.canvas.edit(lambda: setattr(l, "fx", v)),
            decimals=2,
            suffix="N",
            tooltip="Horizontal force. Drag the small circle on the arrow to rotate it. Applied loads draw red."
        )
        form.add_spin(
            "Fy",
            l.fy,
            lambda v: self.canvas.edit(lambda: setattr(l, "fy", v)),
            decimals=2,
            suffix="N",
            tooltip="Vertical force. Drag the small circle on the arrow to rotate it. Applied loads draw red."
        )
        form.add_readonly("Magnitude", f"{l.magnitude():,.1f} N", tooltip="Total force magnitude")
        self.canvas.open_popup(_scene_pos, form)


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
        _ = (option, widget)
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

    def open_editor(self, _scene_pos):
        l = self.load()
        if l is None:
            return
        from ..commands import icon_path
        form = P.PopupForm("Edit Moment", icon_path("tool_moment.svg"))
        form.add_spin(
            "Moment",
            l.m,
            lambda v: self.canvas.edit(lambda: setattr(l, "m", v)),
            decimals=2,
            suffix="N.mm",
            tooltip="Counter-clockwise is positive.",
        )
        self.canvas.open_popup(_scene_pos, form)


def draw_moment_arrow(painter, center, radius, ccw, color):
    """Curved moment arrow with a chord-aligned arrowhead capping the arc."""
    painter.save()
    painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
    
    # 1. Arrowhead dimensions and angular span along the circle
    head_size = max(9.0, radius * 0.40)
    delta_rad = head_size / max(1.0, radius)
    delta_deg = math.degrees(delta_rad)
    
    # 2. Total sweep 270°. Arc line stops short at base_deg so arrowhead caps it seamlessly.
    if ccw:
        start_deg = 45.0
        arc_span = 270.0 - delta_deg
        end_deg = 315.0
        base_deg = end_deg - delta_deg
    else:
        start_deg = 135.0
        arc_span = -(270.0 - delta_deg)
        end_deg = 225.0
        base_deg = end_deg + delta_deg

    # 3. Draw Arc Line (terminating at base_deg)
    rect = QtCore.QRectF(center.x() - radius, center.y() - radius, 2 * radius, 2 * radius)
    pen = QtGui.QPen(color, 1.8)
    pen.setCosmetic(True)
    painter.setPen(pen)
    painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
    painter.drawArc(rect, int(round(start_deg * 16)), int(round(arc_span * 16)))

    # 4. Positions of Arrowhead Base and Tip
    base_rad = math.radians(base_deg)
    end_rad = math.radians(end_deg)

    bx = center.x() + radius * math.cos(base_rad)
    by = center.y() - radius * math.sin(base_rad)

    tx = center.x() + radius * math.cos(end_rad)
    ty = center.y() - radius * math.sin(end_rad)

    # 5. Unit Direction Vector from Base to Tip
    dx = tx - bx
    dy = ty - by
    dist = math.hypot(dx, dy)
    if dist < 1e-6:
        painter.restore()
        return

    ux = dx / dist
    uy = dy / dist

    # 6. Perpendicular Vector for Arrowhead Base Width
    px = -uy
    py = ux
    half_width = head_size * 0.36

    # 7. Construct Arrowhead Polygon (Tip -> Right Base -> Left Base)
    path = QtGui.QPainterPath()
    path.moveTo(QtCore.QPointF(tx, ty))
    path.lineTo(QtCore.QPointF(bx + px * half_width, by + py * half_width))
    path.lineTo(QtCore.QPointF(bx - px * half_width, by - py * half_width))
    path.closeSubpath()

    painter.setBrush(color)
    painter.setPen(QtCore.Qt.PenStyle.NoPen)
    painter.drawPath(path)
    painter.restore()


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
        pa, pb, _, _, h = g
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
        _ = (option, widget)
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

    def open_editor(self, _scene_pos):
        l = self.load()
        if l is None:
            return
        from ..commands import icon_path
        form = P.PopupForm("Edit Line Load", icon_path("tool_lineload.svg"))
        form.add_spin(
            "Load q",
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
            tooltip="Direction of the distributed load"
        )
        member = self.model.members.get(l.member)
        if member:
            total = abs(l.q) * self.model.member_length(member)
            form.add_readonly("Total load", f"{total:,.1f} N", tooltip="Total equivalent point load magnitude")
        self.canvas.open_popup(_scene_pos, form)


# ---------------------------------------------------------------- results


def _overlay_bounds(canvas) -> QtCore.QRectF:
    sheet = canvas.model.sheet
    rect = QtCore.QRectF(0.0, -sheet.height, sheet.width, sheet.height)
    nodes = canvas.model.nodes.values()
    if nodes:
        # Scene coordinates, not raw model ones -- the same fix as fit():
        # a joint's real x, y can be real engineering mm, which is not the
        # same number line as the paper-mm sheet this gets unioned with.
        sc = getattr(canvas, "global_scale", 1.0)
        pts = [to_scene(n.x, n.y, sc) for n in nodes]
        xs = [p.x() for p in pts]
        ys = [p.y() for p in pts]
        bounds = QtCore.QRectF(QtCore.QPointF(min(xs), min(ys)), QtCore.QPointF(max(xs), max(ys)))
        rect = rect.united(bounds)

        # Each diagram sits wherever it was dropped rather than in a fixed
        # stack, so fold in its actual position instead of assuming one.
        positions = getattr(canvas, "diagram_positions", {}) or {}
        for kind, pos in positions.items():
            if pos is None or not getattr(canvas, f"show_{kind}", False):
                continue
            rect = rect.united(bounds.translated(pos[0], pos[1]))

        if getattr(canvas, "results_table_pos", None):
            rect = rect.united(QtCore.QRectF(canvas.results_table_pos, QtCore.QSizeF(300, 300)))

    return rect.adjusted(-120, -120, 120, 120)


def _snap_diagram_offset(canvas, point):
    """Where a dragged diagram settles.

    Close to the structure's own x -- directly above or below it -- locks
    to x = 0 and keeps sliding vertically. Close to its own y -- directly
    beside it -- locks to y = 0 and keeps sliding horizontally. Close to
    both at once locks straight onto the structure. Anywhere else, it's
    free. The catch radius is generous on purpose: this is meant to catch
    a rough drag, not demand a pixel-perfect one.
    """
    threshold = canvas.px(20.0)
    x, y = point.x(), point.y()
    near_x = abs(x) <= threshold
    near_y = abs(y) <= threshold
    if near_x and near_y:
        return QtCore.QPointF(0.0, 0.0), "overlay"
    if near_x:
        return QtCore.QPointF(0.0, y), "vertical"
    if near_y:
        return QtCore.QPointF(x, 0.0), "horizontal"
    return QtCore.QPointF(x, y), "free"


# Cursor for whichever kind of placement a drag is currently headed for.
_DRAG_CURSORS = {
    "overlay": QtCore.Qt.CursorShape.PointingHandCursor,
    "vertical": QtCore.Qt.CursorShape.SizeVerCursor,
    "horizontal": QtCore.Qt.CursorShape.SizeHorCursor,
    "free": QtCore.Qt.CursorShape.ClosedHandCursor,
}


class ResultOverlay(QtWidgets.QGraphicsItem):
    """Solved reactions: the other half of the free body diagram."""

    ARROW = 110.0  # pixels
    HEAD = 16.0

    def __init__(self, canvas):
        super().__init__()
        self.canvas = canvas
        self.setZValue(50)
        self.setAcceptedMouseButtons(QtCore.Qt.MouseButton.NoButton)

    def sc(self) -> float:
        model = getattr(self.canvas, "model", None)
        sheet = getattr(model, "sheet", None) if model else None
        return getattr(sheet, "unit_scale", 1.0) if sheet else 1.0

    def boundingRect(self):
        return _overlay_bounds(self.canvas)

    def _reaction_arrow(self, painter, tail, tip, k):
        head = self.HEAD * k
        dx, dy = tip.x() - tail.x(), tip.y() - tail.y()
        length = math.hypot(dx, dy)
        if length < 1e-9:
            return
        ux, uy = dx / length, dy / length
        pen = QtGui.QPen(S.REACTION, 1.8)
        pen.setCosmetic(True)
        painter.setPen(pen)
        painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
        painter.drawLine(tail, QtCore.QPointF(tip.x() - ux * head, tip.y() - uy * head))
        path = QtGui.QPainterPath()
        _arrow_head(path, tip, ux, uy, head)
        painter.setBrush(S.REACTION)
        painter.setPen(QtCore.Qt.PenStyle.NoPen)
        painter.drawPath(path)

    def paint(self, painter, option, widget=None):
        _ = (option, widget)
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

            # Minimum distance from node to arrow tail (in paper pixels)
            GAP = 7.0 * k

            if mag > 1e-6 and components:
                for value, ux, uy in ((reaction.fx, 1.0, 0.0), (reaction.fy, 0.0, -1.0)):
                    if abs(value) < 1e-6:
                        continue
                    sx, sy = (ux, uy) if value > 0 else (-ux, -uy)
                    length = max(40.0 * k, arrow * abs(value) / mag)

                    # Pointing-in: arrowhead (tip) touches the node boundary, tail sits further out
                    tip = QtCore.QPointF(c.x() - sx * GAP, c.y() - sy * GAP)
                    tail = QtCore.QPointF(tip.x() - sx * length, tip.y() - sy * length)

                    self._reaction_arrow(painter, tail, tip, k)
                    if self.canvas.label_reactions:
                        axis = "Fx" if uy == 0.0 else "Fy"
                        # Qt draws text upwards from its baseline. If the text is below the arrow (sy < 0),
                        # we must add an extra vertical offset to clear the font height.
                        extra_y = 15.0 * k if sy < -0.1 else 0.0
                        px_text(
                            painter,
                            self.canvas,
                            QtCore.QPointF(
                                tail.x() - sx * 10 * k, tail.y() - sy * 10 * k + extra_y
                            ),
                            f"{axis} {fmt(value, 'N')}",
                            S.REACTION,
                            13.0,
                            dy=-4.0,
                        )
            elif mag > 1e-6:
                ux, uy = reaction.fx / mag, -reaction.fy / mag

                # Pointing-in: arrowhead (tip) touches the node boundary, tail sits further out
                tip = QtCore.QPointF(c.x() - ux * GAP, c.y() - uy * GAP)
                tail = QtCore.QPointF(
                    tip.x() - ux * max(50.0 * k, arrow), tip.y() - uy * max(50.0 * k, arrow)
                )

                self._reaction_arrow(painter, tail, tip, k)
                if self.canvas.label_reactions:
                    # Qt draws text upwards from its baseline. If the text is below the arrow (uy < 0),
                    # we must add an extra vertical offset to clear the font height.
                    extra_y = 15.0 * k if uy < -0.1 else 0.0
                    px_text(
                        painter,
                        self.canvas,
                        QtCore.QPointF(tail.x() - ux * 10 * k, tail.y() - uy * 10 * k + extra_y),
                        fmt(mag, "N"),
                        S.REACTION,
                        13.0,
                        dy=-4.0,
                    )

            if abs(reaction.m) > 1e-6:
                draw_moment_arrow(painter, c, 22.0 * k, reaction.m > 0, S.REACTION)
                if self.canvas.label_reactions:
                    px_text(
                        painter,
                        self.canvas,
                        QtCore.QPointF(c.x(), c.y() + 30.0 * k),
                        fmt(abs(reaction.m), "N.mm"),
                        S.REACTION,
                        13.0,
                    )


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
        _ = (option, widget)
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
            pts = [
                to_scene(model.nodes[n].x, model.nodes[n].y, sc) for n in comp if n in model.nodes
            ]
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

                # Corner tick: pixel-constant regardless of zoom, unlike
                # a raw scene-unit size which would render at a wildly
                # different number of screen pixels depending on how far
                # in the user happens to be looking right now.
                tick_pen = QtGui.QPen(S.SELECT, 1.1)
                tick_pen.setCosmetic(True)
                painter.setPen(tick_pen)
                for i in (4.0, 8.0, 12.0):
                    d = self.canvas.px(i)
                    painter.drawLine(
                        QtCore.QPointF(rect.right() - d, rect.bottom()),
                        QtCore.QPointF(rect.right(), rect.bottom() - d),
                    )

        painter.restore()


class ProjectionLinesOverlay(QtWidgets.QGraphicsItem):
    """Dashed guide lines from every joint to a diagram lined up with the
    structure -- straight down when the diagram is locked to the same x,
    straight across when it's locked to the same y. Nothing is drawn for a
    diagram sitting right on the structure, or one left free-floating
    somewhere else on the page: a guide line's whole job is showing what a
    diagram is aligned with, and neither of those is aligned with anything.
    """

    def __init__(self, canvas):
        super().__init__()
        self.canvas = canvas
        self.setZValue(2)
        self.setAcceptedMouseButtons(QtCore.Qt.MouseButton.NoButton)

    def sc(self):
        sheet = getattr(getattr(self.canvas, "model", None), "sheet", None)
        return getattr(sheet, "unit_scale", 1.0) or 1.0

    def boundingRect(self):
        return _overlay_bounds(self.canvas)

    def paint(self, painter, option, widget=None):
        _ = (option, widget)
        res = getattr(self.canvas, "display_result", None)
        active_kinds = [
            k
            for k in ("axial", "shear", "moment", "deflection")
            if getattr(self.canvas, f"show_{k}", False)
        ]
        if not active_kinds or not res or not res.ok:
            return

        model = self.canvas.model
        if not model.nodes:
            return

        alignment = getattr(self.canvas, "diagram_alignment", {})
        positions = getattr(self.canvas, "diagram_positions", {})

        # Guide lines have to live in the same coordinate system as the
        # diagrams and the structure itself -- paper-space "scene" units,
        # scaled by the sheet's own unit_scale -- not raw model
        # millimetres, or a diagram sitting right on the structure would
        # anchor its guide lines however far away unit_scale happens to
        # put the raw numbers.
        sc = self.sc()
        node_pts = [to_scene(n.x, n.y, sc) for n in model.nodes.values()]
        node_ys = [p.y() for p in node_pts]
        node_xs = [p.x() for p in node_pts]
        max_scene_y = max(node_ys, default=0.0)
        min_scene_y = min(node_ys, default=0.0)
        max_scene_x = max(node_xs, default=0.0)
        min_scene_x = min(node_xs, default=0.0)

        vertical_offsets = []
        horizontal_offsets = []
        for k in active_kinds:
            pos = positions.get(k)
            if pos is None:
                continue
            mode = alignment.get(k)
            if mode == "vertical":
                vertical_offsets.append(pos[1])
            elif mode == "horizontal":
                horizontal_offsets.append(pos[0])

        if not vertical_offsets and not horizontal_offsets:
            return

        painter.save()
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, False)
        pen = QtGui.QPen(QtGui.QColor("#b8bec9"), 0.8, QtCore.Qt.PenStyle.DashLine)
        pen.setCosmetic(True)
        painter.setPen(pen)

        # Every joint and anchor's own scene position, scaled the same
        # way as everything else on the page.
        x_positions = list(zip(node_xs, node_ys))
        for a in model.anchors.values():
            xy = model.anchor_xy(a)
            if xy:
                p = to_scene(xy[0], xy[1], sc)
                x_positions.append((p.x(), p.y()))

        if vertical_offsets:
            farthest = max(vertical_offsets, key=abs)
            target_y = (
                (max_scene_y + farthest + 15.0)
                if farthest >= 0
                else (min_scene_y + farthest - 15.0)
            )
            for x, start_y in x_positions:
                painter.drawLine(QtCore.QPointF(x, start_y), QtCore.QPointF(x, target_y))

        if horizontal_offsets:
            farthest = max(horizontal_offsets, key=abs)
            target_x = (
                (max_scene_x + farthest + 15.0)
                if farthest >= 0
                else (min_scene_x + farthest - 15.0)
            )
            for x, start_y in x_positions:
                painter.drawLine(QtCore.QPointF(x, start_y), QtCore.QPointF(target_x, start_y))

        painter.restore()


class SingleDiagramOverlay(QtWidgets.QGraphicsItem):
    def sc(self):
        return getattr(self.canvas, "global_scale", 1.0)

    """A draggable internal force diagram (Axial, Shear, or Moment).

    Free to drop anywhere on the page. See _snap_diagram_offset for how it
    locks onto the structure's own axes -- or straight onto the structure
    itself -- once it gets close.
    """

    def __init__(self, canvas, kind):
        super().__init__()
        self.canvas = canvas
        self.kind = kind
        self.setZValue(5)
        self.setAcceptHoverEvents(True)
        self._hover = False
        self._setting_pos = False
        self.setFlag(QtWidgets.QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges, True)
        self.setFlag(QtWidgets.QGraphicsItem.GraphicsItemFlag.ItemIsMovable, True)
        self.setCursor(QtCore.Qt.CursorShape.OpenHandCursor)

    def hoverEnterEvent(self, event):
        _ = event
        self._hover = True
        self.update()
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event):
        _ = event
        self._hover = False
        self.update()
        super().hoverLeaveEvent(event)

    def sync_pos(self):
        """Wherever this diagram was last dropped, or straight onto the
        structure the first time it is switched on."""
        self.setFlag(QtWidgets.QGraphicsItem.GraphicsItemFlag.ItemIsMovable, True)
        stored = self.canvas.diagram_positions.get(self.kind)
        if stored is None:
            stored = (0.0, 0.0)
            self.canvas.diagram_positions[self.kind] = stored
            self.canvas.diagram_alignment[self.kind] = "overlay"
        self._setting_pos = True
        self.setPos(*stored)
        self._setting_pos = False

    def itemChange(self, change, value):
        if change == QtWidgets.QGraphicsItem.GraphicsItemChange.ItemPositionChange and self.scene():
            if not getattr(self, "_setting_pos", False):
                snapped, alignment = _snap_diagram_offset(self.canvas, value)
                self.canvas.diagram_user_dragged[self.kind] = True
                self.canvas.diagram_positions[self.kind] = (snapped.x(), snapped.y())
                self.canvas.diagram_alignment[self.kind] = alignment
                self.setCursor(_DRAG_CURSORS.get(alignment, QtCore.Qt.CursorShape.ClosedHandCursor))
                try:
                    self.canvas.projection_overlay.update()
                except RuntimeError:
                    pass
                return snapped
        return super().itemChange(change, value)

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        self.setCursor(QtCore.Qt.CursorShape.OpenHandCursor)
        self.canvas.save()
        self.canvas.notify()

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

    def shape(self):
        p = QtGui.QPainterPath()
        res = getattr(self.canvas, "display_result", None)
        if not res or not res.ok:
            return p
        model = self.canvas.model
        sc = self.sc()
        peak = max(
            (
                max((abs(v) for v in getattr(mf, self.kind)), default=0.0)
                for mf in res.members.values()
                if getattr(mf, self.kind)
            ),
            default=0.0,
        )
        if peak < 1e-12:
            return p
        for mid, mf in res.members.items():
            member = model.members.get(mid)
            values = getattr(mf, self.kind)
            if not member or not values:
                continue
            a, b = model.nodes.get(member.start), model.nodes.get(member.end)
            if not a or not b:
                continue
            pa, pb = to_scene(a.x, a.y, sc), to_scene(b.x, b.y, sc)
            dx, dy = pb.x() - pa.x(), pb.y() - pa.y()
            length = math.hypot(dx, dy) or 1.0
            nx, ny = -dy / length, dx / length
            poly = [pa]
            for i, v in enumerate(values):
                f = i / max(1, len(values) - 1)
                off = (v / peak) * 10.0
                poly.append(QtCore.QPointF(pa.x() + dx * f + nx * off, pa.y() + dy * f + ny * off))
            poly.append(pb)
            p.addPolygon(QtGui.QPolygonF(poly))
        stroker = QtGui.QPainterPathStroker()
        stroker.setWidth(8.0)
        return p + stroker.createStroke(p)

    def paint(self, painter, option, widget=None):
        _ = (option, widget)
        if not getattr(self.canvas, f"show_{self.kind}", False):
            return
        res = getattr(self.canvas, "display_result", None)
        if not res or not res.ok:
            return

        model = self.canvas.model
        fill = {"moment": S.MOMENT_FILL, "shear": S.SHEAR_FILL, "axial": S.AXIAL_FILL}[self.kind]
        edge = {"moment": S.REACTION, "shear": S.INTERNAL, "axial": S.APPLIED}[self.kind]

        pts_all = [to_scene(n.x, n.y, self.sc()) for n in model.nodes.values()]
        node_ys = [p.y() for p in pts_all]
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
        is_overlay = self.canvas.diagram_alignment.get(self.kind) == "overlay"

        painter.save()

        title = {"moment": "Moment", "shear": "Shear", "axial": "Axial"}[self.kind]
        if is_zero:
            title += " (0)"

        min_x = min((p.x() for p in pts_all), default=0.0)

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

        if is_overlay and not self.canvas.diagram_user_dragged.get(self.kind, False):
            px_text(
                painter,
                self.canvas,
                QtCore.QPointF(min_x, min_scene_y - self.canvas.px(14.0)),
                "drag to reposition",
                S.INK_LIGHT,
                size_pt=9.0,
                centre=False,
            )

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

        # Hover Box Indicator (Dashed Green Box)
        if self._hover and not is_overlay:
            painter.save()
            hover_pen = QtGui.QPen(S.HOVER, 1.0, QtCore.Qt.PenStyle.DashLine)
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
        _ = (option, widget)
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
        _, min_y, max_x, _ = bounds
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
        _ = (option, widget)
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
        painter.drawLine(
            QtCore.QPointF(c.x() - tick, c.y() + tick), QtCore.QPointF(c.x() + tick, c.y() - tick)
        )

    def hoverEnterEvent(self, event):
        self._hover = True
        self.setCursor(QtCore.Qt.CursorShape.SizeFDiagCursor)
        self.update()
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event):
        _ = event
        self._hover = False
        self.update()
        super().hoverLeaveEvent(event)

    def mousePressEvent(self, event):
        if (
            event.button() == QtCore.Qt.MouseButton.LeftButton
            and self.canvas.begin_diagram_resize()
        ):
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


class DeflectionOverlay(QtWidgets.QGraphicsItem):
    """Draws the elastically deflected shape of the structure. Draggable
    the same way the force diagrams are -- see _snap_diagram_offset.
    """

    def __init__(self, canvas):
        super().__init__()
        self.canvas = canvas
        self.kind = "deflection"
        self.setZValue(44)
        self.setAcceptHoverEvents(True)
        self._hover = False
        self._setting_pos = False
        self.setFlag(QtWidgets.QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges, True)
        self.setFlag(QtWidgets.QGraphicsItem.GraphicsItemFlag.ItemIsMovable, True)
        self.setCursor(QtCore.Qt.CursorShape.OpenHandCursor)

    def sc(self):
        model = getattr(self.canvas, "model", None)
        sheet = getattr(model, "sheet", None) if model else None
        return getattr(sheet, "unit_scale", 1.0) if sheet else 1.0

    def sync_pos(self):
        self.setFlag(QtWidgets.QGraphicsItem.GraphicsItemFlag.ItemIsMovable, True)
        stored = self.canvas.diagram_positions.get(self.kind)
        if stored is None:
            stored = (0.0, 0.0)
            self.canvas.diagram_positions[self.kind] = stored
            self.canvas.diagram_alignment[self.kind] = "overlay"
        self._setting_pos = True
        self.setPos(*stored)
        self._setting_pos = False

    def itemChange(self, change, value):
        if change == QtWidgets.QGraphicsItem.GraphicsItemChange.ItemPositionChange and self.scene():
            if not getattr(self, "_setting_pos", False):
                snapped, alignment = _snap_diagram_offset(self.canvas, value)
                self.canvas.diagram_user_dragged[self.kind] = True
                self.canvas.diagram_positions[self.kind] = (snapped.x(), snapped.y())
                self.canvas.diagram_alignment[self.kind] = alignment
                self.setCursor(_DRAG_CURSORS.get(alignment, QtCore.Qt.CursorShape.ClosedHandCursor))
                try:
                    self.canvas.projection_overlay.update()
                except RuntimeError:
                    pass
                return snapped
        return super().itemChange(change, value)

    def hoverEnterEvent(self, event):
        _ = event
        self._hover = True
        self.update()
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event):
        _ = event
        self._hover = False
        self.update()
        super().hoverLeaveEvent(event)

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        self.setCursor(QtCore.Qt.CursorShape.OpenHandCursor)
        self.canvas.save()
        self.canvas.notify()

    def boundingRect(self):
        model = getattr(self.canvas, "model", None)
        if not model or not model.nodes:
            return QtCore.QRectF()
        sc = self.sc()
        pts = [to_scene(n.x, n.y, sc) for n in model.nodes.values()]
        xs = [p.x() for p in pts]
        ys = [p.y() for p in pts]
        pad = 40.0
        return QtCore.QRectF(
            min(xs) - pad,
            min(ys) - pad,
            (max(xs) - min(xs)) + 2 * pad,
            (max(ys) - min(ys)) + 2 * pad,
        )

    def shape(self):
        p = QtGui.QPainterPath()
        res = getattr(self.canvas, "display_result", None)
        if not res or not res.ok or not getattr(res, "displacements", None):
            return p
        model = self.canvas.model
        sc = self.sc()
        max_disp = 0.0
        for nid, (ux, uy, _) in res.displacements.items():
            disp = math.hypot(ux, uy)
            if disp > max_disp:
                max_disp = disp
        if max_disp < 1e-9:
            return p
        target_px = 25.0 * sc
        scale_factor = target_px / max_disp
        for member in model.members.values():
            a = model.nodes.get(member.start)
            b = model.nodes.get(member.end)
            if not a or not b:
                continue
            disp_a = res.displacements.get(a.id, (0, 0, 0))
            disp_b = res.displacements.get(b.id, (0, 0, 0))
            p_ax = a.x + disp_a[0] * scale_factor
            p_ay = a.y + disp_a[1] * scale_factor
            p_bx = b.x + disp_b[0] * scale_factor
            p_by = b.y + disp_b[1] * scale_factor
            p.moveTo(to_scene(p_ax, p_ay, sc))
            p.lineTo(to_scene(p_bx, p_by, sc))
        stroker = QtGui.QPainterPathStroker()
        stroker.setWidth(12.0)
        return stroker.createStroke(p)

    def paint(self, painter, option, widget=None):
        _ = (option, widget)
        if not getattr(self.canvas, "show_deflection", False):
            return
        res = getattr(self.canvas, "display_result", None)
        if not res or not res.ok or not getattr(res, "displacements", None):
            return

        model = self.canvas.model
        sc = self.sc()
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)

        max_disp = 0.0
        max_node = None
        for nid, (ux, uy, _) in res.displacements.items():
            disp = math.hypot(ux, uy)
            if disp > max_disp:
                max_disp = disp
                max_node = nid

        if max_disp < 1e-9:
            return

        target_px = 25.0 * sc
        scale_factor = target_px / max_disp

        pen = QtGui.QPen(QtGui.QColor("#d81b60"), 1.5, QtCore.Qt.PenStyle.DashLine)
        pen.setCosmetic(True)
        painter.setPen(pen)
        painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)

        for member in model.members.values():
            a = model.nodes.get(member.start)
            b = model.nodes.get(member.end)
            if not a or not b:
                continue

            disp_a = res.displacements.get(a.id, (0, 0, 0))
            disp_b = res.displacements.get(b.id, (0, 0, 0))

            orig_dx = b.x - a.x
            orig_dy = b.y - a.y
            L_orig = math.hypot(orig_dx, orig_dy)

            if L_orig > 1e-9:
                ux, uy = orig_dx / L_orig, orig_dy / L_orig
                nx, ny = -uy, ux

                uA = disp_a[0] * ux + disp_a[1] * uy
                vA = disp_a[0] * nx + disp_a[1] * ny
                thetaA = -disp_a[2]  # AnaStruct is CW, Hermite is CCW

                uB = disp_b[0] * ux + disp_b[1] * uy
                vB = disp_b[0] * nx + disp_b[1] * ny
                thetaB = -disp_b[2]

                poly = []
                steps = 12
                for i in range(steps + 1):
                    t = i / float(steps)
                    h00 = 2 * t**3 - 3 * t**2 + 1
                    h10 = t**3 - 2 * t**2 + t
                    h01 = -2 * t**3 + 3 * t**2
                    h11 = t**3 - t**2

                    v_t = (
                        h00 * (vA * scale_factor)
                        + h10 * (thetaA * scale_factor * L_orig)
                        + h01 * (vB * scale_factor)
                        + h11 * (thetaB * scale_factor * L_orig)
                    )
                    u_t = (1 - t) * (uA * scale_factor) + t * (uB * scale_factor)

                    x_l, y_l = t * L_orig + u_t, v_t
                    x_g, y_g = a.x + x_l * ux + y_l * nx, a.y + x_l * uy + y_l * ny
                    poly.append(to_scene(x_g, y_g, sc))

                path = QtGui.QPainterPath(poly[0])
                for pt in poly[1:]:
                    path.lineTo(pt)
                painter.drawPath(path)
            else:
                p_ax = a.x + disp_a[0] * scale_factor
                p_ay = a.y + disp_a[1] * scale_factor
                p_bx = b.x + disp_b[0] * scale_factor
                p_by = b.y + disp_b[1] * scale_factor
                painter.drawLine(to_scene(p_ax, p_ay, sc), to_scene(p_bx, p_by, sc))

        if max_node is not None:
            n = model.nodes.get(max_node)
            if n:
                d_v = res.displacements[max_node]
                pt = to_scene(n.x + d_v[0] * scale_factor, n.y + d_v[1] * scale_factor, sc)
                px_text(
                    painter,
                    self.canvas,
                    pt,
                    f"Maximum Deflection: {fmt(max_disp, 'mm')}",
                    QtGui.QColor("#d81b60"),
                    size_pt=11.0,
                    dy=-12.0,
                )

        if self.canvas.diagram_alignment.get(
            "deflection"
        ) == "overlay" and not self.canvas.diagram_user_dragged.get("deflection", False):
            hint_pts = [to_scene(nd.x, nd.y, sc) for nd in model.nodes.values()]
            if hint_pts:
                hx = min(p.x() for p in hint_pts)
                hy = min(p.y() for p in hint_pts)
                px_text(
                    painter,
                    self.canvas,
                    QtCore.QPointF(hx, hy - self.canvas.px(14.0)),
                    "drag to reposition",
                    QtGui.QColor("#8a94a6"),
                    size_pt=9.0,
                )


class LoadPreview(QtWidgets.QGraphicsItem):
    """Faint semi-transparent ghost preview when placing a load."""

    LEN = 80.0  # pixels
    HEAD = 16.0

    def __init__(self, canvas):
        super().__init__()
        self.canvas = canvas
        self.setZValue(42)
        self.setFlag(QtWidgets.QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations, True)
        self.setAcceptedMouseButtons(QtCore.Qt.MouseButton.NoButton)

    def boundingRect(self):
        r = self.LEN + 20.0
        return QtCore.QRectF(-r, -r, 2 * r, 2 * r)

    def paint(self, painter, option, widget=None):
        _ = (option, widget)
        preview = getattr(self.canvas, "preview_load", None)
        if not preview or not preview[0] or not preview[1]:
            return

        kind, _scene_pos = preview
        painter.save()
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)

        ghost = QtGui.QColor(S.APPLIED)
        ghost.setAlpha(110)

        if kind == "point_load":
            tail = QtCore.QPointF(0.0, -self.LEN)
            tip = QtCore.QPointF(0.0, 0.0)

            painter.setPen(QtGui.QPen(ghost, 1.8, QtCore.Qt.PenStyle.DashLine))
            painter.drawLine(tail, QtCore.QPointF(0.0, -self.HEAD))

            path = QtGui.QPainterPath()
            _arrow_head(path, tip, 0.0, 1.0, self.HEAD)

            painter.setBrush(ghost)
            painter.setPen(QtCore.Qt.PenStyle.NoPen)
            painter.drawPath(path)

        elif kind == "moment_load":
            draw_moment_arrow(painter, QtCore.QPointF(0, 0), 32.0, True, ghost)

        painter.restore()
