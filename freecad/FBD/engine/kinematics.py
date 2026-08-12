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
    """(length, rate of change) in mm and mm per second."""
    stroke = float(actuator.stroke)
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
        # Starts retracted, eases out, eases back. No jerk at the ends.
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


# === the constraint system


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
        """(motor angles, actuator lengths) with their time derivatives."""
        angles = {}
        for mo in self.model.motors.values():
            if mo.id in self.motor_start:
                angles[mo.id] = motor_angle(mo, t, self.motor_start[mo.id])
        lengths = {}
        for ac in self.model.actuators.values():
            base = self.member_length0.get(ac.member)
            if base is not None:
                lengths[ac.id] = actuator_length(ac, t, base)
        return angles, lengths

    def residual(self, q, t) -> np.ndarray:
        angles, lengths = self.driver_targets(t)
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
    def solve_position(self, q_guess, t) -> Tuple[np.ndarray, float, bool]:
        q = np.array(q_guess, float)
        tol = _TOL * self.scale * self.scale
        for _ in range(_MAX_NEWTON):
            r = self.residual(q, t)
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
        r = self.residual(q, t)
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

    result.ok = True
    result.message = f"{len(frames)} frames over {duration:g} s." if mobility == 0 else message
    return result


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
