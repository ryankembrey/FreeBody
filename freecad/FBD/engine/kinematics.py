# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Ryan Kembrey <ryan.FreeCAD@gmail.com>
# SPDX-FileNotice: Part of the FBD addon.

"""Planar kinematics: the same diagram read as a mechanism.

Nothing new is drawn to make a linkage. A member is a rigid link, a shared
joint is a pin, a pin or fixed support is that joint bolted to the ground, and
a roller is a joint free to slide along one line. Add a motor or an actuator
and the thing moves.

The maths is deliberately the textbook one, solved numerically rather than
with a closed form per linkage type, because a closed form only exists for the
linkages someone has already written down and this has to cope with whatever
gets drawn.

    unknowns    the x and y of every joint
    constraints one per member (its length is fixed), two per grounded joint,
                one per roller, and two per motor, which replace the driven
                link's own length equation
    solution    Newton-Raphson at each time step, started from the previous
                step, which is what keeps the linkage on the branch it was
                already on instead of flipping through a dead centre
    velocity    differentiate the constraints once: J q' = -dC/dt, solved with
                the Jacobian already to hand
    effort      virtual work. The power a driver puts in equals the power the
                applied loads take out, so a torque or a ram force falls out
                of the velocities for free, with no second analysis

Mobility is checked before any of that runs, so an under-driven linkage is
told it needs another driver rather than being handed to Newton to fail at.
"""

from typing import Dict, List, Optional, Tuple
import math

import numpy as np

from .model import (
    Model,
    Motor,
    Actuator,
    PIN,
    ROLLER_X,
    ROLLER_Y,
    FIXED,
    SPRING,
    CONTINUOUS,
    SWEEP,
    EXTEND,
    CYCLE,
    SINE,
)
from .results import MotionResult, Frame


# Constraint kinds, as tags on the rows we assemble.
_LENGTH = "length"
_GROUND = "ground"
_SLIDE = "slide"
_PIVOT = "pivot"  # a grounded point part way along a member
_MOTOR = "motor"

_TOL = 1e-7  # Newton convergence, relative to model size
_MAX_NEWTON = 60


# === drivers over time


def motor_angle(motor: Motor, t: float, theta0: float) -> Tuple[float, float]:
    """(angle, angular velocity) in radians and radians per second."""
    omega = math.radians(motor.speed)
    if motor.motion == SWEEP:
        half = math.radians(abs(motor.sweep))
        if half < 1e-9 or abs(omega) < 1e-12:
            return theta0, 0.0
        # A triangle wave: rock from theta0 - half to theta0 + half and back,
        # at the set speed, so the sweep angle sets the period rather than a
        # separate control the user would have to keep consistent.
        period = 4.0 * half / abs(omega)
        phase = (t % period) / period  # 0..1
        if phase < 0.5:
            offset = -half + 4.0 * half * phase
            rate = omega
        else:
            offset = 3.0 * half - 4.0 * half * phase
            rate = -omega
        return theta0 + offset, rate
    return theta0 + omega * t, omega


def actuator_length(actuator: Actuator, t: float, length0: float) -> Tuple[float, float]:
    """(length, rate of change) in mm and mm per second.

    start_extended treats the member's own drawn length as the
    ram's fully extended end rather than its retracted end: the
    stroke's own sign is flipped, nothing else, so the same three
    profiles below run unchanged but travel downward from the
    drawn length instead of upward from it. Without it, length0 is
    the retracted end and the ram extends out by the stroke; with
    it, length0 is the sketch's own fully extended pose and the
    ram contracts in by the stroke.
    """
    stroke = float(actuator.stroke)
    if actuator.start_extended:
        stroke = -stroke
    speed = abs(float(actuator.speed))
    if abs(stroke) < 1e-9 or speed < 1e-12:
        return length0, 0.0
    travel_time = abs(stroke) / speed
    sign = 1.0 if stroke >= 0 else -1.0

    if actuator.motion == EXTEND:
        if t >= travel_time:
            return length0 + stroke, 0.0
        return length0 + sign * speed * t, sign * speed

    if actuator.motion == SINE:
        period = 2.0 * travel_time
        w = 2.0 * math.pi / period
        # Eases out from the home end, eases back. No jerk at either end.
        s = 0.5 * stroke * (1.0 - math.cos(w * t))
        ds = 0.5 * stroke * w * math.sin(w * t)
        return length0 + s, ds

    # CYCLE: out and back at constant speed, repeating.
    period = 2.0 * travel_time
    phase = t % period
    if phase < travel_time:
        return length0 + sign * speed * phase, sign * speed
    return (length0 + stroke - sign * speed * (phase - travel_time), -sign * speed)


def _mechanism_period(model: Model) -> Optional[float]:
    """How long before every active driver's own profile exactly repeats.

    A crank closes after one revolution, a cycling ram after one out-and-back,
    a sweeping motor after one full rock: each has a length of time after
    which its position and its direction of travel are exactly what they were
    at t=0, not merely close to it. None means nothing here is periodic on
    its own -- an actuator left at EXTEND runs once and stops, so there is no
    length of time to loop a playback over without a seam.
    """
    periods: List[float] = []
    for mo in model.motors.values():
        omega = math.radians(mo.speed)
        if abs(omega) < 1e-12:
            continue
        if mo.motion == SWEEP:
            half = math.radians(abs(mo.sweep))
            if half < 1e-9:
                continue
            periods.append(4.0 * half / abs(omega))
        else:
            periods.append(2.0 * math.pi / abs(omega))
    for ac in model.actuators.values():
        if ac.motion == EXTEND:
            continue
        stroke = abs(float(ac.stroke))
        speed = abs(float(ac.speed))
        if stroke < 1e-9 or speed < 1e-12:
            continue
        periods.append(2.0 * stroke / speed)  # CYCLE and SINE share this
    if not periods:
        return None
    return _common_period(periods)


def _common_period(periods: List[float], tol: float = 1e-6, max_cycles: int = 48) -> float:
    """The shortest time after which every one of these periods lines up.

    With one driver, the common case by far, this is just its own period.
    With several, only the least common multiple closes every driver's cycle
    at once, and that is unreliable to compute exactly on floats, so this
    searches small integer multiples instead. If nothing lines up within the
    search it settles for the longer of the two: an honest best effort, not
    a guaranteed seamless loop, for the rare diagram with two drivers on
    genuinely unrelated cycles.
    """
    result = periods[0]
    for p in periods[1:]:
        found = None
        for n in range(1, max_cycles + 1):
            candidate = n * p
            multiple = round(candidate / result)
            if multiple >= 1 and abs(candidate - multiple * result) <= tol * max(result, p):
                found = multiple * result
                break
        result = found if found is not None else max(result, p)
    return result


def _frame_accelerations(frames, period):
    """Acceleration of every joint at every frame, by finite difference of
    the velocities the position/velocity solve already computed.

    Central difference where two neighbours exist. When the segment is a
    whole number of natural cycles -- the ordinary case now that duration
    rounds up to one -- frame zero and the last frame are the same instant,
    so the boundary can borrow its neighbour from the far end instead of
    falling back to a one-sided, less accurate estimate there. A held
    (non-converged) frame carries no real velocity, so acceleration is left
    out wherever a held frame sits within one step: differencing through it
    would invent a number rather than measure one.
    """
    n = len(frames)
    if n < 3:
        return [dict() for _ in frames]
    wrap = period is not None and period > 1e-6
    out = []
    for i in range(n):
        prev_i = i - 1 if i > 0 else (n - 2 if wrap else None)
        next_i = i + 1 if i < n - 1 else (1 if wrap else None)
        acc = {}
        if prev_i is not None and next_i is not None \
                and frames[i].ok and frames[prev_i].ok and frames[next_i].ok:
            dt = (frames[1].t - frames[0].t) + (frames[-1].t - frames[-2].t) \
                if wrap and (i == 0 or i == n - 1) \
                else frames[next_i].t - frames[prev_i].t
            if abs(dt) > 1e-9:
                for nid in frames[i].velocities:
                    if nid in frames[prev_i].velocities and nid in frames[next_i].velocities:
                        vp = frames[prev_i].velocities[nid]
                        vn = frames[next_i].velocities[nid]
                        acc[nid] = ((vn[0] - vp[0]) / dt, (vn[1] - vp[1]) / dt)
        out.append(acc)
    return out


def _inertial_load(model: Model, system: "MechanismSystem", accel: dict) -> np.ndarray:
    """d'Alembert forces for every member with mass, added to the ordinary
    applied-load vector.

    A uniform rigid rod's distributed mass, expressed at its own two end
    joints, is the standard two-node "consistent mass" split: two thirds of
    each end's own acceleration, one third of the other end's. That
    reproduces both F=ma for straight-line motion and I*alpha=ML^2/12*alpha
    for rotation about its own centre exactly, not approximately -- verified
    both ways by hand before this was trusted. A cruder lumped half-and-half
    split would overstate a spinning member's own rotational inertia by a
    factor of three, which is enough to matter for anything that actually
    turns rather than just translates.
    """
    Q = np.zeros(system.unknowns)
    for m in model.members.values():
        if m.mass <= 0:
            continue
        aA = accel.get(m.start)
        aB = accel.get(m.end)
        if aA is None or aB is None:
            continue
        if m.start not in system.index or m.end not in system.index:
            continue
        ka, kb = 2 * system.index[m.start], 2 * system.index[m.end]
        # kg * mm/s^2 is millinewtons; everything else here is in N, and
        # 1 N = 1000 kg.mm/s^2, so this is where that gets reconciled.
        M = m.mass / 1000.0
        for axis in (0, 1):
            Q[ka + axis] += -(M / 3.0) * aA[axis] - (M / 6.0) * aB[axis]
            Q[kb + axis] += -(M / 6.0) * aA[axis] - (M / 3.0) * aB[axis]
    return Q


# === the constraint system


def _schedule_window(schedule, t):
    """(local_t, active) for this instant on the shared timeline.

    local_t is what the driver's own profile function should be given;
    active says whether it is actually moving right now, as opposed to
    holding still at one end of its turn because it either hasn't started
    yet or has already finished. Before start it sits at its own t=0 pose;
    once its turn ends it holds at wherever local_t its turn lasted.
    """
    if schedule is None or not schedule.scheduled:
        return t, True
    local = t - schedule.start
    if local < 0.0:
        return 0.0, False
    if schedule.duration is not None and local > schedule.duration:
        return schedule.duration, False
    return local, True


class MechanismSystem:
    """The assembled constraint system for one model, reusable across frames."""

    def __init__(self, model: Model):
        self.model = model
        self.node_ids = sorted(model.nodes)
        self.index = {nid: k for k, nid in enumerate(self.node_ids)}
        self.q0 = np.array(
            [c for nid in self.node_ids for c in (model.nodes[nid].x, model.nodes[nid].y)], float
        )

        self.scale = max(1.0, float(np.max(np.abs(self.q0))) if self.q0.size else 1.0)

        self.rows: List[dict] = []
        self.driven_members = set()
        self.motor_start: Dict[int, float] = {}  # motor id -> start angle
        self.member_length0: Dict[int, float] = {}
        self.warnings: List[str] = []
        self._build()

    # ---
    def _xy(self, q, nid):
        k = 2 * self.index[nid]
        return q[k], q[k + 1]

    def _build(self):
        model = self.model

        for m in model.members.values():
            if m.start not in self.index or m.end not in self.index:
                continue
            self.member_length0[m.id] = model.member_length(m)

        # Motors take priority: they replace their link's length equation with
        # two equations that place the far end outright, which removes the
        # branch ambiguity a scalar angle equation would leave behind.
        grounded = {s.node for s in model.supports.values() if s.grounds_position()}
        for mo in model.motors.values():
            member = model.members.get(mo.member)
            if member is None or mo.node not in self.index:
                self.warnings.append(f"{mo.label or 'A motor'} is not attached to a link.")
                continue
            if mo.node not in (member.start, member.end):
                self.warnings.append(f"{mo.label or 'A motor'} does not sit on the link it drives.")
                continue
            if mo.node not in grounded:
                self.warnings.append(
                    f"{mo.label or 'A motor'} has nothing to turn against: "
                    "add a pin or fixed support at its joint."
                )
            far = member.end if mo.node == member.start else member.start
            ax, ay = model.nodes[mo.node].xy()
            bx, by = model.nodes[far].xy()
            length = math.hypot(bx - ax, by - ay)
            if length < 1e-9:
                self.warnings.append(f"{mo.label or 'A motor'} drives a zero length link.")
                continue
            self.motor_start[mo.id] = math.atan2(by - ay, bx - ax)
            self.driven_members.add(member.id)
            self.rows.append(
                {
                    "kind": _MOTOR,
                    "motor": mo.id,
                    "pivot": mo.node,
                    "far": far,
                    "length": length,
                    "axis": 0,
                }
            )
            self.rows.append(
                {
                    "kind": _MOTOR,
                    "motor": mo.id,
                    "pivot": mo.node,
                    "far": far,
                    "length": length,
                    "axis": 1,
                }
            )

        for m in model.members.values():
            if m.id in self.driven_members:
                continue
            if m.start not in self.index or m.end not in self.index:
                continue
            self.rows.append(
                {
                    "kind": _LENGTH,
                    "member": m.id,
                    "a": m.start,
                    "b": m.end,
                    "length": self.member_length0[m.id],
                    "actuator": (model.actuator_on(m.id).id if model.actuator_on(m.id) else None),
                }
            )

        # A welded, ungrounded joint: every member meeting here moves as
        # one rigid body through this point rather than hinging. A "star"
        # of virtual, invisible length constraints from one far end to
        # every other far end -- the same reason a diagonal brace stops a
        # truss bay from racking, reused rather than inventing a new
        # constraint kind. N members meeting at a point need only N-1
        # braces to fix every angle between them; a full pairwise set
        # would just be redundant.
        for node_id, node in model.nodes.items():
            if not getattr(node, "rigid", False) or node_id not in self.index:
                continue
            far_ends = []
            for m in model.members.values():
                if m.start == node_id and m.end in self.index:
                    far_ends.append(m.end)
                elif m.end == node_id and m.start in self.index:
                    far_ends.append(m.start)
            if len(far_ends) < 2:
                continue
            ref = far_ends[0]
            rx, ry = model.nodes[ref].xy()
            for other in far_ends[1:]:
                ox, oy = model.nodes[other].xy()
                length0 = math.hypot(ox - rx, oy - ry)
                self.rows.append({"kind": _LENGTH, "member": None, "a": ref,
                                  "b": other, "length": length0, "actuator": None})

        for s in model.supports.values():
            if s.anchor is not None:
                anchor = model.anchors.get(s.anchor)
                member = model.members.get(anchor.member) if anchor else None
                if member is None or member.start not in self.index:
                    continue
                xy = model.anchor_xy(anchor)
                if xy is None:
                    continue
                if s.kind in (PIN, FIXED):
                    # The pivot of a lever: a point on the bar held in place,
                    # so the bar swings about it and both arms stay in line
                    # because they are one bar.
                    for axis in (0, 1):
                        self.rows.append(
                            {
                                "kind": _PIVOT,
                                "a": member.start,
                                "b": member.end,
                                "t": anchor.t,
                                "axis": axis,
                                "value": xy[axis],
                                "anchor": anchor.id,
                            }
                        )
                else:
                    self.warnings.append(
                        "Only a pin or fixed support can pivot a point on a "
                        "member; other kinds are ignored while it runs."
                    )
                continue
            if s.node not in self.index:
                continue
            x0, y0 = model.nodes[s.node].xy()
            if s.kind in (PIN, FIXED):
                self.rows.append({"kind": _GROUND, "node": s.node, "axis": 0, "value": x0})
                self.rows.append({"kind": _GROUND, "node": s.node, "axis": 1, "value": y0})
            elif s.kind in (ROLLER_X, ROLLER_Y):
                a = math.radians(s.angle or 0.0)
                if s.kind == ROLLER_X:  # slides along the local x axis
                    nx, ny = -math.sin(a), math.cos(a)
                else:  # slides along the local y axis
                    nx, ny = math.cos(a), math.sin(a)
                self.rows.append(
                    {"kind": _SLIDE, "node": s.node, "nx": nx, "ny": ny, "value": nx * x0 + ny * y0}
                )
            elif s.kind == SPRING:
                self.warnings.append(
                    "A spring support does not restrain a mechanism: it is "
                    "ignored while the motion runs."
                )

    # ---
    @property
    def unknowns(self) -> int:
        return 2 * len(self.node_ids)

    @property
    def equations(self) -> int:
        return len(self.rows)

    @property
    def mobility(self) -> int:
        """Degrees of freedom left after the drivers have had their say."""
        return self.unknowns - self.equations

    def driver_targets(
        self, t: float
    ) -> Tuple[Dict[int, Tuple[float, float]], Dict[int, Tuple[float, float]]]:
        """(motor angles, actuator lengths) with their time derivatives.

        Each driver is given its own scheduled instant rather than t
        directly, so one holding position outside its turn reports a target
        that has stopped changing, and -- just as importantly -- a rate of
        zero rather than the rate its profile would have if it were still
        running: the difference between actually being still and merely
        being asked about a frozen position.
        """
        angles = {}
        for mo in self.model.motors.values():
            if mo.id in self.motor_start:
                local_t, active = _schedule_window(getattr(mo, "schedule", None), t)
                theta, omega = motor_angle(mo, local_t, self.motor_start[mo.id])
                angles[mo.id] = (theta, omega if active else 0.0)
        lengths = {}
        for ac in self.model.actuators.values():
            base = self.member_length0.get(ac.member)
            if base is not None:
                local_t, active = _schedule_window(getattr(ac, "schedule", None), t)
                length, rate = actuator_length(ac, local_t, base)
                lengths[ac.id] = (length, rate if active else 0.0)
        return angles, lengths

    def residual(self, q, t, length_overrides=None) -> np.ndarray:
        angles, lengths = self.driver_targets(t)
        if length_overrides:
            lengths = dict(lengths)
            for aid, val in length_overrides.items():
                lengths[aid] = (val, 0.0)
        out = np.zeros(len(self.rows))
        for i, row in enumerate(self.rows):
            kind = row["kind"]
            if kind == _LENGTH:
                ax, ay = self._xy(q, row["a"])
                bx, by = self._xy(q, row["b"])
                target = row["length"]
                if row["actuator"] is not None and row["actuator"] in lengths:
                    target = lengths[row["actuator"]][0]
                out[i] = 0.5 * ((bx - ax) ** 2 + (by - ay) ** 2 - target**2)
            elif kind == _GROUND:
                k = 2 * self.index[row["node"]] + row["axis"]
                out[i] = q[k] - row["value"]
            elif kind == _SLIDE:
                x, y = self._xy(q, row["node"])
                out[i] = row["nx"] * x + row["ny"] * y - row["value"]
            elif kind == _PIVOT:
                ia = 2 * self.index[row["a"]] + row["axis"]
                ib = 2 * self.index[row["b"]] + row["axis"]
                t = row["t"]
                out[i] = (1.0 - t) * q[ia] + t * q[ib] - row["value"]
            else:  # _MOTOR
                px, py = self._xy(q, row["pivot"])
                fx, fy = self._xy(q, row["far"])
                theta = angles.get(row["motor"], (0.0, 0.0))[0]
                if row["axis"] == 0:
                    out[i] = (fx - px) - row["length"] * math.cos(theta)
                else:
                    out[i] = (fy - py) - row["length"] * math.sin(theta)
        return out

    def jacobian(self, q) -> np.ndarray:
        J = np.zeros((len(self.rows), self.unknowns))
        for i, row in enumerate(self.rows):
            kind = row["kind"]
            if kind == _LENGTH:
                ia, ib = 2 * self.index[row["a"]], 2 * self.index[row["b"]]
                dx = q[ib] - q[ia]
                dy = q[ib + 1] - q[ia + 1]
                J[i, ia] = -dx
                J[i, ia + 1] = -dy
                J[i, ib] = dx
                J[i, ib + 1] = dy
            elif kind == _GROUND:
                J[i, 2 * self.index[row["node"]] + row["axis"]] = 1.0
            elif kind == _SLIDE:
                k = 2 * self.index[row["node"]]
                J[i, k] = row["nx"]
                J[i, k + 1] = row["ny"]
            elif kind == _PIVOT:
                t = row["t"]
                J[i, 2 * self.index[row["a"]] + row["axis"]] = 1.0 - t
                J[i, 2 * self.index[row["b"]] + row["axis"]] = t
            else:
                ip = 2 * self.index[row["pivot"]] + row["axis"]
                iff = 2 * self.index[row["far"]] + row["axis"]
                J[i, ip] = -1.0
                J[i, iff] = 1.0
        return J

    def dc_dt(self, t) -> np.ndarray:
        """How each constraint moves with time, holding the joints still."""
        angles, lengths = self.driver_targets(t)
        out = np.zeros(len(self.rows))
        for i, row in enumerate(self.rows):
            if row["kind"] == _LENGTH and row["actuator"] is not None:
                pair = lengths.get(row["actuator"])
                if pair:
                    length, rate = pair
                    out[i] = -length * rate
            elif row["kind"] == _MOTOR:
                theta, omega = angles.get(row["motor"], (0.0, 0.0))
                if row["axis"] == 0:
                    out[i] = row["length"] * math.sin(theta) * omega
                else:
                    out[i] = -row["length"] * math.cos(theta) * omega
        return out

    # ---
    def solve_position(self, q_guess, t, length_overrides=None) -> Tuple[np.ndarray, float, bool]:
        q = np.array(q_guess, float)
        tol = _TOL * self.scale * self.scale
        for _ in range(_MAX_NEWTON):
            r = self.residual(q, t, length_overrides)
            err = float(np.max(np.abs(r))) if r.size else 0.0
            if err <= tol:
                return q, err, True
            J = self.jacobian(q)
            try:
                step, *_ = np.linalg.lstsq(J, -r, rcond=None)
            except np.linalg.LinAlgError:
                return q, err, False
            # Damp a wild step so a near singular pose does not throw the
            # linkage across the page and lose its branch.
            biggest = float(np.max(np.abs(step))) if step.size else 0.0
            if biggest > 0.5 * self.scale:
                step *= (0.5 * self.scale) / biggest
            q = q + step
        r = self.residual(q, t, length_overrides)
        return q, float(np.max(np.abs(r))) if r.size else 0.0, False

    def solve_velocity(self, q, t) -> np.ndarray:
        J = self.jacobian(q)
        b = -self.dc_dt(t)
        try:
            v, *_ = np.linalg.lstsq(J, b, rcond=None)
        except np.linalg.LinAlgError:
            v = np.zeros(self.unknowns)
        return v


# === effort, by virtual work


def _driver_effort(model: Model, system: MechanismSystem, q, v, t) -> Dict[int, float]:
    """Torque (N.mm) for each motor and force (N) for each actuator.

    The applied loads take power out of the mechanism at a rate F.v; the
    driver must put the same in. With one driver that is a division, and with
    several it is shared out in proportion to each driver's own rate, which is
    exact when only one driver is moving and a reasonable split when more than
    one is.
    """

    def node_velocity(nid):
        k = 2 * system.index[nid]
        return v[k], v[k + 1]

    power_out = 0.0
    for p in model.point_loads.values():
        if p.node is not None and p.node in system.index:
            vx, vy = node_velocity(p.node)
        elif p.anchor is not None:
            anchor = model.anchors.get(p.anchor)
            member = model.members.get(anchor.member) if anchor else None
            if member is None or member.start not in system.index:
                continue
            vax, vay = node_velocity(member.start)
            vbx, vby = node_velocity(member.end)
            vx = vax + anchor.t * (vbx - vax)
            vy = vay + anchor.t * (vby - vay)
        else:
            continue
        power_out += p.fx * vx + p.fy * vy

    for l in model.line_loads.values():
        member = model.members.get(l.member)
        if member is None or member.start not in system.index:
            continue
        length = model.member_length(member)
        total = l.q * length
        vax, vay = node_velocity(member.start)
        vbx, vby = node_velocity(member.end)
        # The resultant of a uniform load acts at mid span, and for a rigid
        # link the mid span velocity is the mean of the two ends.
        vx, vy = 0.5 * (vax + vbx), 0.5 * (vay + vby)
        if l.direction == "x":
            power_out += total * vx
        elif l.direction == "perp":
            a, b = model.member_ends(member)
            dx, dy = b.x - a.x, b.y - a.y
            if length > 1e-9:
                ux, uy = -dy / length, dx / length
                power_out += total * (ux * vx + uy * vy)
        else:
            power_out += total * vy

    angles, lengths = system.driver_targets(t)
    rates = {}
    for mo in model.motors.values():
        if mo.id in angles:
            rates[("motor", mo.id)] = angles[mo.id][1]
    for ac in model.actuators.values():
        if ac.id in lengths:
            rates[("actuator", ac.id)] = lengths[ac.id][1]

    active = {k: r for k, r in rates.items() if abs(r) > 1e-12}
    effort: Dict[int, float] = {}
    if not active:
        return effort
    share = 1.0 / len(active)
    for (kind, ident), rate in active.items():
        effort[ident] = -power_out * share / rate
    return effort


# === public API


def _generalized_load(model: Model, system: "MechanismSystem") -> np.ndarray:
    """External applied force at each joint's own x, y degree of freedom.

    A load on an anchor splits across the two end joints by (1-t)/t, the
    same weighting the pivot constraint itself uses; a line load's resultant
    splits evenly across its member's two ends. Both match the same
    virtual-work principle _driver_effort already relies on, just written
    out as a vector once instead of folded straight into a power sum.
    """
    Q = np.zeros(system.unknowns)

    def add(nid, fx, fy):
        if nid not in system.index:
            return
        k = 2 * system.index[nid]
        Q[k] += fx
        Q[k + 1] += fy

    for p in model.point_loads.values():
        if p.node is not None:
            add(p.node, p.fx, p.fy)
        elif p.anchor is not None:
            anchor = model.anchors.get(p.anchor)
            member = model.members.get(anchor.member) if anchor else None
            if member is None:
                continue
            add(member.start, p.fx * (1.0 - anchor.t), p.fy * (1.0 - anchor.t))
            add(member.end, p.fx * anchor.t, p.fy * anchor.t)

    for l in model.line_loads.values():
        member = model.members.get(l.member)
        if member is None or member.start not in system.index:
            continue
        length = model.member_length(member)
        total = l.q * length
        if l.direction == "x":
            fx, fy = total, 0.0
        elif l.direction == "perp":
            a, b = model.member_ends(member)
            dx, dy = b.x - a.x, b.y - a.y
            ux, uy = (-dy / length, dx / length) if length > 1e-9 else (0.0, 0.0)
            fx, fy = total * ux, total * uy
        else:
            fx, fy = 0.0, total
        add(member.start, 0.5 * fx, 0.5 * fy)
        add(member.end, 0.5 * fx, 0.5 * fy)

    return Q


def _frame_forces(model: Model, system: "MechanismSystem", q, extra_load=None):
    """Reactions, member axial force, and every driver's effort, all from
    the Lagrange multipliers of the same Jacobian solve_velocity uses.

    A support reaction, a pivot reaction, and the tension or compression in
    a member are the generalized force each of those constraints must supply
    to hold the mechanism in equilibrium under the load: exactly what a
    Lagrange multiplier is. That is the same principle _driver_effort already
    applies, through a power balance, for the driver alone; this solves the
    same system for every constraint at once. Checked against
    _driver_effort's own answer at several points through a stroke and a
    full rotation, matching to five figures every time, so it is used here
    to add the new capability rather than to replace what was already
    trusted: frame.effort still comes from _driver_effort whenever nothing
    has mass, and from here only once inertia needs including.

    extra_load adds to the ordinary applied-load vector before solving --
    this is where an inertial force goes, so the same code path serves the
    quasi-static case (nothing extra) and the case with mass.
    """
    Q = _generalized_load(model, system)
    if extra_load is not None:
        Q = Q + extra_load
    J = system.jacobian(q)
    try:
        lam, *_ = np.linalg.lstsq(J.T, -Q, rcond=None)
    except np.linalg.LinAlgError:
        return {}, {}, {}, float("nan")

    reactions: Dict[int, Tuple[float, float]] = {}
    axial: Dict[int, float] = {}
    effort: Dict[int, float] = {}
    motor_rows: Dict[int, Dict[int, Tuple[int, dict]]] = {}

    for i, row in enumerate(system.rows):
        kind = row["kind"]
        if kind == _GROUND:
            fx, fy = reactions.get(row["node"], (0.0, 0.0))
            if row["axis"] == 0:
                fx = lam[i]
            else:
                fy = lam[i]
            reactions[row["node"]] = (fx, fy)
        elif kind == _SLIDE:
            fx, fy = reactions.get(row["node"], (0.0, 0.0))
            fx += lam[i] * row["nx"]
            fy += lam[i] * row["ny"]
            reactions[row["node"]] = (fx, fy)
        elif kind == _PIVOT:
            key = row["anchor"]
            fx, fy = reactions.get(key, (0.0, 0.0))
            if row["axis"] == 0:
                fx = lam[i]
            else:
                fy = lam[i]
            reactions[key] = (fx, fy)
        elif kind == _LENGTH:
            ax, ay = system._xy(q, row["a"])
            bx, by = system._xy(q, row["b"])
            length = math.hypot(bx - ax, by - ay)
            # The length residual is written as a half-squared-distance form
            # for a constant Jacobian, not distance-minus-target directly, so
            # its multiplier needs one factor of the current length to read
            # as an ordinary force; tension positive, matching every other
            # axial number this addon reports.
            force = -lam[i] * length
            if row["member"] is not None:
                axial[row["member"]] = force
            if row["actuator"] is not None:
                effort[row["actuator"]] = -force   # push positive, matching _driver_effort
        elif kind == _MOTOR:
            motor_rows.setdefault(row["motor"], {})[row["axis"]] = (i, row)

    for motor_id, pair in motor_rows.items():
        if 0 not in pair or 1 not in pair:
            continue
        i0, row0 = pair[0]
        i1, _row1 = pair[1]
        px, py = system._xy(q, row0["pivot"])
        fx_, fy_ = system._xy(q, row0["far"])
        theta = math.atan2(fy_ - py, fx_ - px)
        length = row0["length"]
        # d(constraint)/d(theta) dotted with the multiplier gives the
        # generalized force conjugate to the angle itself: the torque.
        dCdtheta = (length * math.sin(theta), -length * math.cos(theta))
        effort[motor_id] = -(dCdtheta[0] * lam[i0] + dCdtheta[1] * lam[i1])

    residual = float(np.max(np.abs(J.T @ lam + Q))) if lam.size else 0.0
    scale = max(1.0, float(np.max(np.abs(Q))) if Q.size else 1.0)
    return reactions, axial, effort, residual / scale


def _q_from_frame(system: "MechanismSystem", frame: Frame) -> np.ndarray:
    q = np.zeros(system.unknowns)
    for nid, k in system.index.items():
        if nid in frame.positions:
            q[2 * k], q[2 * k + 1] = frame.positions[nid]
    return q


def check_mechanism(model: Model) -> Tuple[bool, str, int]:
    """(runnable, message, mobility). Never raises."""
    if not model.members:
        return False, "No links. Draw members to make a mechanism.", 0
    if not model.has_drivers():
        return (
            False,
            (
                "Nothing drives this yet. Add a motor to a link, or "
                "turn a member into a linear actuator."
            ),
            0,
        )
    system = MechanismSystem(model)
    mobility = system.mobility
    if mobility > 0:
        return (
            False,
            (
                f"Under-driven: {mobility} degree"
                f"{'s' if mobility != 1 else ''} of freedom left. Add "
                f"{mobility} more driver"
                f"{'s' if mobility != 1 else ''}, or ground another joint."
            ),
            mobility,
        )
    if mobility < 0:
        return (
            True,
            (
                f"Over-constrained by {-mobility}. It will still run if "
                "the extra links are consistent, but check for a "
                "duplicated link or a joint grounded twice."
            ),
            mobility,
        )
    return True, "Ready to run.", mobility


def _limit_message(model: Model, system: MechanismSystem, t: float) -> str:
    """Which driver asked for the impossible, and what it could actually reach.

    Reported in the driver's own units, because "reduce the stroke to 24 mm" is
    something you can act on and "the Jacobian went singular" is not.
    """
    angles, lengths = system.driver_targets(t)
    for ac in model.actuators.values():
        pair = lengths.get(ac.id)
        base = system.member_length0.get(ac.member)
        if pair is None or base is None:
            continue
        reached = abs(pair[0] - base)
        member = model.members.get(ac.member)
        name = member.label if member else (ac.label or "the ram")
        return (
            f"{name} runs out of travel at about {reached:,.0f} mm of its "
            f"{abs(ac.stroke):,.0f} mm stroke: it lines up with the link it "
            "drives, which is as far as that joint can be pushed. Shorten "
            "the stroke, or move the ram's anchor."
        )
    for mo in model.motors.values():
        if mo.id in angles:
            return (
                f"{mo.label or 'The motor'} cannot carry the linkage past "
                "this position: the link lengths will not close. Check them, "
                "or reduce the sweep."
            )
    return "A driver asked for a position the linkage cannot reach."


def simulate(
    model: Model, duration: Optional[float] = None, fps: Optional[int] = None
) -> MotionResult:
    """Run the mechanism. Never raises: failures come back as ok=False."""
    result = MotionResult()
    runnable, message, mobility = check_mechanism(model)
    result.mobility = mobility
    if not runnable:
        result.message = message
        return result

    system = MechanismSystem(model)
    result.warnings = list(system.warnings)
    requested = float(duration if duration is not None else model.motion.duration)

    scheduled = any(getattr(mo, "schedule", None) and mo.schedule.scheduled
                    for mo in model.motors.values()) \
        or any(getattr(ac, "schedule", None) and ac.schedule.scheduled
               for ac in model.actuators.values())

    if scheduled:
        # A choreographed sequence: cover every driver's own turn in full,
        # and don't try to force a seamless loop over the whole thing --
        # the point is a one-off performance, not a repeating cycle, so a
        # duration that lands mid-stroke for some driver is expected here,
        # not a seam to round away.
        sequence_end = requested
        for driver in list(model.motors.values()) + list(model.actuators.values()):
            sc = getattr(driver, "schedule", None)
            if sc and sc.scheduled:
                span = sc.duration if sc.duration is not None else max(0.0, requested - sc.start)
                sequence_end = max(sequence_end, sc.start + span)
        duration = sequence_end
        period = None
    else:
        period = _mechanism_period(model)
        if period and 1e-6 < period:
            # Round up to a whole number of cycles, so the last frame's pose is
            # exactly the first frame's pose and playback can loop with nothing
            # to see at the seam. Capped in absolute terms, not by how many
            # cycles that takes, so an unusually slow driver can't balloon this
            # into simulating minutes of frames nobody asked for.
            cycles = max(1, round(requested / period))
            candidate = cycles * period
            if candidate <= 60.0:
                duration = candidate
            else:
                duration = requested
                period = None
        else:
            duration = requested
            period = None
    result.period = period
    fps = int(fps if fps is not None else model.motion.fps)
    fps = max(1, min(240, fps))
    steps = max(1, int(round(duration * fps)))

    q = system.q0.copy()
    frames: List[Frame] = []
    limit_t = None  # first instant the drivers asked for the impossible
    recovered = False
    stalled = 0

    for step in range(steps + 1):
        t = duration * step / steps
        attempt, residual, converged = system.solve_position(q, t)
        if converged:
            q = attempt
            if limit_t is not None:
                recovered = True
        else:
            # A limit position, not a failure. The commonest case by far is a
            # ram and the link it drives coming into line: the joint is then as
            # far from the ram's anchor as it can ever get, and asking for one
            # millimetre more has no solution at all. So hold the last pose it
            # could reach and keep going, because a cycling driver retracts and
            # the linkage picks straight back up. Only a run that never
            # recovers is actually jammed.
            if limit_t is None:
                limit_t = t
            stalled += 1
            frames.append(
                Frame(
                    t=t,
                    positions={
                        nid: (float(q[2 * k]), float(q[2 * k + 1]))
                        for nid, k in system.index.items()
                    },
                    velocities={nid: (0.0, 0.0) for nid in system.index},
                    residual=float(residual),
                    ok=False,
                )
            )
            continue

        v = system.solve_velocity(q, t)
        positions = {nid: (float(q[2 * k]), float(q[2 * k + 1])) for nid, k in system.index.items()}
        velocities = {
            nid: (float(v[2 * k]), float(v[2 * k + 1])) for nid, k in system.index.items()
        }
        frame = Frame(
            t=t, positions=positions, velocities=velocities, residual=float(residual), ok=True
        )
        frame.effort = _driver_effort(model, system, q, v, t)
        reactions, axial, _effort_check, force_residual = _frame_forces(model, system, q)
        frame.reactions = reactions
        frame.axial = axial
        frame.equilibrium_error = force_residual
        frames.append(frame)

    result.frames = frames
    if limit_t is not None:
        result.warnings.append(_limit_message(model, system, limit_t))
        if not recovered and stalled > steps // 2:
            result.ok = len(frames) > 1
            result.message = (
                f"Stops dead at {limit_t:.2f} s and never comes back. "
                + _limit_message(model, system, limit_t)
            )
            return result

    if not frames:
        result.message = "Nothing to run."
        return result

    if any(m.mass > 0 for m in model.members.values()):
        # Only worth the extra pass when something actually has mass:
        # acceleration by finite difference of the velocities already
        # solved, then re-solve with the inertial force folded into the
        # same generalized load every other force already goes through.
        # This is what replaces frame.effort's quasi-static answer with one
        # that accounts for a fast-moving link's own inertia, verified
        # against the work-energy theorem before being trusted here.
        accels = _frame_accelerations(frames, result.period)
        for frame, accel in zip(frames, accels):
            if not frame.ok or not accel:
                continue
            q_frame = _q_from_frame(system, frame)
            extra = _inertial_load(model, system, accel)
            reactions, axial, effort, force_residual = _frame_forces(
                model, system, q_frame, extra_load=extra)
            frame.reactions = reactions
            frame.axial = axial
            frame.effort = effort
            frame.equilibrium_error = force_residual

    result.ok = True
    result.message = f"{len(frames)} frames over {duration:g} s." if mobility == 0 else message
    return result


def solve_for_target(model: Model, actuator_id: int, target_fn, samples: int = 25,
                     tol: float = 1e-4):
    """Find the ram length that makes target_fn(positions) cross zero.

    target_fn takes the dict of joint positions a solved pose produces and
    returns a signed number -- positive on one side of the target,
    negative on the other, zero at it. Bisection, not Newton, and
    deliberately so: this only ever needs the function's SIGN at a given
    length, never its derivative, which is what makes it robust across a
    mechanism's whole geometry rather than only near wherever it happens
    to start.

    Scans the ram's plausible travel first to find a bracket where the
    sign actually changes, rather than assuming the whole range is
    monotonic in one direction, then bisects within it. The scan also
    marches the Newton guess along from sample to sample, the same trick
    pose_at() uses, so a mechanism that would lose its branch from a cold
    guess at an extreme length doesn't lose it here either.

    Returns (stroke, ok, message). stroke is measured from the member's
    own drawn length, so it can come back negative -- meaning the target
    needs the ram shorter than drawn, not longer.
    """
    actuator = model.actuators.get(actuator_id)
    if actuator is None:
        return None, False, "No such actuator."
    system = MechanismSystem(model)
    base = system.member_length0.get(actuator.member)
    driven_ids = {row["actuator"] for row in system.rows
                 if row["kind"] == _LENGTH and row["actuator"] is not None}
    if base is None or actuator_id not in driven_ids:
        return None, False, "This actuator isn't driving anything in the mechanism."

    def evaluate(length, q_guess):
        q, _err, ok = system.solve_position(q_guess, 0.0, {actuator_id: length})
        if not ok:
            return None, q
        positions = {nid: (float(q[2 * k]), float(q[2 * k + 1]))
                    for nid, k in system.index.items()}
        return target_fn(positions), q

    lo_len = max(1e-3, base * 0.2)
    hi_len = base * 3.0
    xs = [lo_len + (hi_len - lo_len) * i / (samples - 1) for i in range(samples)]

    lo = hi = None
    v_lo = v_hi = None
    q_guess = system.q0.copy()
    for x in xs:
        v, q_guess = evaluate(x, q_guess)
        if v is None:
            continue
        if lo is not None and (v_lo > 0) != (v > 0):
            hi, v_hi = x, v
            break
        lo, v_lo = x, v

    if hi is None:
        return None, False, (
            "No stroke within a plausible travel range reaches that target. "
            "Check the target is actually reachable, and that this ram is "
            "on the member you expect."
        )

    q_guess = system.q0.copy()
    for _ in range(60):
        mid = 0.5 * (lo + hi)
        v, q_mid = evaluate(mid, q_guess)
        if v is None:
            hi = mid
            continue
        q_guess = q_mid
        if abs(v) < tol or (hi - lo) < 1e-6:
            lo = hi = mid
            break
        if (v > 0) == (v_lo > 0):
            lo, v_lo = mid, v
        else:
            hi, v_hi = mid, v

    found_length = 0.5 * (lo + hi)
    stroke = found_length - base
    return stroke, True, f"Stroke {stroke:+.1f} mm from the drawn length reaches the target."


def solve_for_member_angle(model: Model, actuator_id: int, member_id: int, target_deg: float):
    """The stroke that puts a member at a specific angle, measured from
    the positive x axis in degrees, the same convention as everywhere
    else in this engine.
    """
    member = model.members.get(member_id)
    if member is None:
        return None, False, "No such member."
    target_rad = math.radians(target_deg)

    def target_fn(positions):
        a = positions.get(member.start)
        b = positions.get(member.end)
        if a is None or b is None:
            return 0.0
        angle = math.atan2(b[1] - a[1], b[0] - a[0])
        diff = angle - target_rad
        # Wrapped to -pi..pi so bisection sees one clean crossing rather
        # than one that wraps around through +-180 degrees.
        return (diff + math.pi) % (2 * math.pi) - math.pi

    return solve_for_target(model, actuator_id, target_fn)


def solve_for_joint_travel(model: Model, actuator_id: int, node_id: int, target_mm: float):
    """The stroke that moves a joint the given distance from its drawn
    position -- not to an arbitrary (x, y): a single ram is one degree of
    freedom, so it can only be asked for a distance along whatever path
    the mechanism's own geometry actually carries that joint on, not an
    independent x and y.
    """
    node = model.nodes.get(node_id)
    if node is None:
        return None, False, "No such joint."
    home = (node.x, node.y)

    def target_fn(positions):
        pos = positions.get(node_id)
        if pos is None:
            return 0.0
        dist = math.hypot(pos[0] - home[0], pos[1] - home[1])
        return dist - target_mm

    return solve_for_target(model, actuator_id, target_fn)


def pose_at(model: Model, t: float) -> Optional[Frame]:
    """One frame on its own, for scrubbing without simulating the whole run."""
    runnable, _msg, _mob = check_mechanism(model)
    if not runnable:
        return None
    system = MechanismSystem(model)
    q = system.q0.copy()
    # March up to t so the branch is the one continuation would have found.
    fps = max(1, model.motion.fps)
    steps = max(1, int(round(abs(t) * fps)))
    for step in range(1, steps + 1):
        q, _r, ok = system.solve_position(q, t * step / steps)
        if not ok:
            return None
    v = system.solve_velocity(q, t)
    return Frame(
        t=t,
        positions={nid: (float(q[2 * k]), float(q[2 * k + 1])) for nid, k in system.index.items()},
        velocities={nid: (float(v[2 * k]), float(v[2 * k + 1])) for nid, k in system.index.items()},
    )


# === levers, which are mechanisms simple enough to answer by hand


def lever_report(model: Model, pivot_node: int) -> dict:
    """Mechanical advantage and balance for a bar on a pivot.

    A first class lever has its pivot between the two forces, and this reads
    whatever has been drawn rather than assuming it: arm lengths come from the
    joints actually attached to the pivot, so a pivot in the centre reports an
    advantage of one and moving it changes the answer immediately.
    """
    out = {
        "ok": False,
        "message": "",
        "advantage": 0.0,
        "effort_arm": 0.0,
        "load_arm": 0.0,
        "balance": 0.0,
        "net_moment": 0.0,
    }
    anchor = model.anchors.get(pivot_node)
    if anchor is not None:
        # A pivot on the bar: the arms are just the two parts of the member,
        # so they follow from t and no search is needed.
        member = model.members.get(anchor.member)
        if member is None:
            out["message"] = "The pivot has lost its bar."
            return out
        length = model.member_length(member)
        a_len, b_len = anchor.t * length, (1.0 - anchor.t) * length
        effort_arm, load_arm = max(a_len, b_len), min(a_len, b_len)
        far = member.start if a_len < b_len else member.end
        pivot_xy = model.anchor_xy(anchor)
        return _lever_numbers(model, out, pivot_xy, effort_arm, load_arm, far)

    pivot = model.nodes.get(pivot_node)
    if pivot is None:
        out["message"] = "No such joint."
        return out

    reach: List[Tuple[int, float]] = []
    for m in model.members.values():
        if m.start == pivot_node or m.end == pivot_node:
            far = m.end if m.start == pivot_node else m.start
            node = model.nodes.get(far)
            if node:
                reach.append((far, math.hypot(node.x - pivot.x, node.y - pivot.y)))
    if len(reach) < 2:
        out["message"] = "A lever needs an arm either side of the pivot."
        return out

    reach.sort(key=lambda pair: pair[1], reverse=True)
    (effort_node, effort_arm), (load_node, load_arm) = reach[0], reach[1]
    return _lever_numbers(model, out, pivot.xy(), effort_arm, load_arm, load_node)


def _lever_numbers(model, out, pivot_xy, effort_arm, load_arm, load_ident):
    """Advantage and balance, once the two arms are known."""
    out["effort_arm"], out["load_arm"] = effort_arm, load_arm
    out["advantage"] = effort_arm / load_arm if load_arm > 1e-9 else float("inf")

    net = 0.0
    for p in model.point_loads.values():
        xy = model.attachment_xy(p.node, p.anchor)
        if xy is None:
            continue
        x, y = xy
        net += (x - pivot_xy[0]) * p.fy - (y - pivot_xy[1]) * p.fx
    for mo in model.moment_loads.values():
        net += mo.m
    out["net_moment"] = net

    load_xy = model.entity_xy(load_ident)
    if load_xy and load_arm > 1e-9:
        # The vertical force at the short arm that would bring it to rest.
        lever_x = load_xy[0] - pivot_xy[0]
        if abs(lever_x) > 1e-9:
            out["balance"] = -net / lever_x
    out["ok"] = True
    out["message"] = (
        f"Mechanical advantage {out['advantage']:.3g} "
        f"({effort_arm:,.0f} mm against {load_arm:,.0f} mm)."
    )
    return out


# === effort against travel, which is what you size a driver from


def effort_curves(model: Model, result: MotionResult) -> List[dict]:
    """One curve per driver: effort against how far that driver has moved.

    Against travel rather than against time, because time is an accident of
    the speed you happened to type and travel is a property of the machine.
    Reading it off tells you where in the stroke the demand peaks, which is
    the thing that decides the cylinder bore or the motor frame size.

    Travel is measured from the pose as drawn: millimetres of extension for a
    ram, degrees turned for a motor. Effort is positive when the driver pushes
    (or turns counter-clockwise) and negative when it holds back, so a curve
    that crosses zero is telling you the load reverses on it part way through,
    which a single-acting cylinder cannot do.

    Held frames at a limit position carry no effort and are left out, since
    the force there is unbounded and plotting it would just be a spike.
    """
    curves: List[dict] = []
    frames = [f for f in result.frames if f.ok]
    if not frames:
        return curves

    def positions(frame, nid):
        return frame.positions.get(nid)

    for ac in model.actuators.values():
        member = model.members.get(ac.member)
        if member is None:
            continue

        def length_at(frame, member=member):
            a, b = positions(frame, member.start), positions(frame, member.end)
            if a is None or b is None:
                return None
            return math.hypot(b[0] - a[0], b[1] - a[1])

        base = length_at(frames[0])
        if base is None:
            continue
        points = []
        for f in frames:
            length = length_at(f)
            force = f.effort.get(ac.id)
            if length is None or force is None:
                continue
            points.append((length - base, force))
        if len(points) > 1:
            curves.append(
                {
                    "id": ac.id,
                    "label": member.label or ac.label,
                    "unit": "N",
                    "x_unit": "mm",
                    "points": points,
                }
            )

    for mo in model.motors.values():
        member = model.members.get(mo.member)
        if member is None or mo.node not in model.nodes:
            continue
        far = member.end if mo.node == member.start else member.start

        def angle_at(frame, pivot=mo.node, far=far):
            p, q = positions(frame, pivot), positions(frame, far)
            if p is None or q is None:
                return None
            return math.atan2(q[1] - p[1], q[0] - p[0])

        first = angle_at(frames[0])
        if first is None:
            continue
        points = []
        previous = first
        turned = 0.0
        for f in frames:
            angle = angle_at(f)
            torque = f.effort.get(mo.id)
            if angle is None:
                continue
            # Unwrap, so a crank that goes round twice reads 720 degrees and
            # the curve does not fold back over itself.
            step = angle - previous
            while step > math.pi:
                step -= 2.0 * math.pi
            while step < -math.pi:
                step += 2.0 * math.pi
            turned += step
            previous = angle
            if torque is not None:
                points.append((math.degrees(turned), torque))
        if len(points) > 1:
            curves.append(
                {
                    "id": mo.id,
                    "label": mo.label or (member.label if member else "Motor"),
                    "unit": "N.mm",
                    "x_unit": "deg",
                    "points": points,
                }
            )

    for curve in curves:
        values = [v for _x, v in curve["points"]]
        travel = [x for x, _v in curve["points"]]
        curve["peak"] = max(values, key=abs)
        curve["peak_at"] = travel[values.index(curve["peak"])]
        curve["y_range"] = (min(values), max(values))
        curve["x_range"] = (min(travel), max(travel))
        curve["reverses"] = min(values) < -1e-9 and max(values) > 1e-9
    return curves
