# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Ryan Kembrey <ryan.FreeCAD@gmail.com>
# SPDX-FileNotice: Part of the FBD addon.

"""Pre-flight checks, run before the solver ever sees the model.

A stiffness solver handed an unstable structure either throws or returns
nonsense, and users draw unstable structures constantly while a diagram is half
finished. So we diagnose first and say something useful.

External stability is not a rule of thumb. For each connected group of members
we build the 3 x r matrix mapping that group's reaction unknowns to the planar
equilibrium equations (sum Fx, sum Fy, sum M) and take its rank. Rank 3 means
the group is held; less means it can still move, and the null space tells us
*how* it moves, which is what makes the message worth reading.

Internal stability and the degree of indeterminacy use the standard planar
count, per connected group:

    degree = (3 * members + reactions) - (3 * joints + released equations)

which reduces to the old external-only count for a rigid-jointed chain, adds
three per closed loop, and subtracts one per internal hinge. A negative degree
is an internal mechanism. The count is necessary rather than sufficient, so a
model can still pass here and be reported unstable by the solver; that is why
statics.py translates the backend's own complaint into plain words too.

A model with a motor or an actuator on it is not an error. It is a mechanism
on purpose, and the message says so and points at Run Motion instead.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set
import math

import numpy as np

from .model import Model, PIN, ROLLER_X, ROLLER_Y, FIXED, SPRING, TENSION_ONLY, COMPRESSION_ONLY


OK = "ok"
WARNING = "warning"
ERROR = "error"

# Classification of the support system.
DETERMINATE = "determinate"
INDETERMINATE = "indeterminate"
MECHANISM = "mechanism"
DRIVEN = "driven"  # a mechanism with drivers on it: run it, don't solve it
UNKNOWN = "unknown"


@dataclass
class Issue:
    level: str
    message: str
    entities: List[tuple] = field(default_factory=list)  # [(kind, id), ...]


@dataclass
class Diagnosis:
    solvable: bool = False
    classification: str = UNKNOWN
    reaction_count: int = 0
    redundancy: int = 0  # reactions beyond determinacy
    releases: int = 0  # moment equations given up at hinges
    nonlinear: bool = False
    issues: List[Issue] = field(default_factory=list)

    @property
    def errors(self) -> List[Issue]:
        return [i for i in self.issues if i.level == ERROR]

    @property
    def warnings(self) -> List[Issue]:
        return [i for i in self.issues if i.level == WARNING]

    def summary(self) -> str:
        if self.errors:
            return self.errors[0].message
        if self.classification == DRIVEN:
            return "Driven mechanism. Use Run Motion rather than Solve."
        if self.classification == DETERMINATE:
            if self.nonlinear:
                return "Statically determinate. Non-linear members will be iterated."
            return "Statically determinate. Ready to solve."
        if self.classification == INDETERMINATE:
            return (
                f"Statically indeterminate to degree {self.redundancy}. "
                "Solvable; results depend on member stiffness."
            )
        if self.classification == MECHANISM:
            return "Mechanism: not held against all movement."
        return "Incomplete diagram."


def _components(model: Model) -> List[Set[int]]:
    """Connected groups of nodes, joined by members."""
    adjacency: Dict[int, Set[int]] = {nid: set() for nid in model.nodes}
    for m in model.members.values():
        if m.start in adjacency and m.end in adjacency:
            adjacency[m.start].add(m.end)
            adjacency[m.end].add(m.start)
    seen: Set[int] = set()
    out: List[Set[int]] = []
    for nid in model.nodes:
        if nid in seen:
            continue
        stack = [nid]
        group: Set[int] = set()
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            group.add(cur)
            stack.extend(adjacency[cur] - seen)
        out.append(group)
    return out


def _equilibrium_matrix(model: Model, group: Set[int]):
    """Columns are unit reactions available to this group; rows are the three
    planar equilibrium equations about the group's own centroid."""
    columns = []
    labels = []
    xs = [model.nodes[n].x for n in group]
    ys = [model.nodes[n].y for n in group]
    ox = sum(xs) / len(xs) if xs else 0.0
    oy = sum(ys) / len(ys) if ys else 0.0

    for s in model.supports.values():
        if model.support_home_node(s) not in group:
            continue
        xy = model.support_xy(s)
        if xy is None:
            continue
        x, y = xy[0] - ox, xy[1] - oy
        theta = math.radians(s.angle or 0.0)
        ca, sa = math.cos(theta), math.sin(theta)
        for comp in s.reaction_components():
            if comp == "fx":
                # Local x direction, rotated by the support angle.
                dx, dy = ca, sa
                columns.append([dx, dy, x * dy - y * dx])
            elif comp == "fy":
                dx, dy = -sa, ca
                columns.append([dx, dy, x * dy - y * dx])
            else:  # moment
                columns.append([0.0, 0.0, 1.0])
            labels.append((s.id, comp))
    if not columns:
        return np.zeros((3, 0)), labels
    return np.array(columns).T, labels


def _group_counts(model: Model, group: Set[int]):
    """(members, joints, reactions, released equations) inside one group."""
    members = [m for m in model.members.values() if m.start in group and m.end in group]
    reactions = sum(
        len(s.reaction_components())
        for s in model.supports.values()
        if model.support_home_node(s) in group
    )
    released = sum(m.released_equations() for m in members)
    joints = len([n for n in group])
    return len(members), joints, reactions, released


def check(model: Model) -> Diagnosis:
    """Diagnose the model. Never raises."""
    d = Diagnosis()

    if not model.nodes:
        d.issues.append(
            Issue(ERROR, "No geometry present. Add joints and members.")
        )
        return d
    if not model.members:
        d.issues.append(
            Issue(ERROR, "No members present. Connect joints to define a structure.")
        )
        return d

    # Referential integrity first: bad references would crash the adapter.
    for m in model.members.values():
        if m.start not in model.nodes or m.end not in model.nodes:
            d.issues.append(
                Issue(ERROR, f"{m.label} refers to a missing node.", [("member", m.id)])
            )
            return d
        if model.member_length(m) < 1e-9:
            d.issues.append(Issue(ERROR, f"{m.label} has zero length.", [("member", m.id)]))
            return d
    for s in model.supports.values():
        if s.anchor is not None:
            if s.anchor not in model.anchors:
                d.issues.append(
                    Issue(ERROR, "A support refers to a missing point.", [("support", s.id)])
                )
                return d
        elif s.node not in model.nodes:
            d.issues.append(
                Issue(ERROR, "A support refers to a missing node.", [("support", s.id)])
            )
            return d
    for a in model.anchors.values():
        if a.member not in model.members:
            d.issues.append(Issue(ERROR, "A point refers to a missing member.", [("anchor", a.id)]))
            return d
    for coll, kind in ((model.point_loads, "point_load"), (model.moment_loads, "moment_load")):
        for item in coll.values():
            if item.node is None and item.anchor is None:
                d.issues.append(
                    Issue(ERROR, "A load is not attached to any entity.", [(kind, item.id)])
                )
                return d
            if item.node is not None and item.node not in model.nodes:
                d.issues.append(
                    Issue(ERROR, "A load refers to a missing joint.", [(kind, item.id)])
                )
                return d
            if item.anchor is not None and item.anchor not in model.anchors:
                d.issues.append(
                    Issue(ERROR, "A load refers to a missing point.", [(kind, item.id)])
                )
                return d
    for l in model.line_loads.values():
        if l.member not in model.members:
            d.issues.append(
                Issue(ERROR, "A line load refers to a missing member.", [("line_load", l.id)])
            )
            return d
    for mo in model.motors.values():
        if mo.member not in model.members or mo.node not in model.nodes:
            d.issues.append(
                Issue(ERROR, "A motor refers to a deleted entity.", [("motor", mo.id)])
            )
            return d
    for ac in model.actuators.values():
        if ac.member not in model.members:
            d.issues.append(
                Issue(ERROR, "An actuator refers to a missing member.", [("actuator", ac.id)])
            )
            return d

    # Springs with no stiffness set restrain nothing, which is a classic trap.
    for s in model.supports.values():
        if s.kind == SPRING and not s.reaction_components():
            d.issues.append(
                Issue(
                    ERROR,
                    "A spring support has no stiffness: set kx, ky or kr.",
                    [("support", s.id)],
                )
            )
            return d

    d.nonlinear = model.has_nonlinear()

    # A driven mechanism is not a broken structure, so say so before the
    # stability test calls it one.
    if model.has_drivers():
        d.classification = DRIVEN
        d.issues.append(
            Issue(
                WARNING,
                "This diagram includes a motor or actuator and is therefore a "
                "mechanism by design. Use Run Motion; Solve will report it as "
                "unstable, which is the expected behaviour for a mechanism.",
            )
        )

    if not model.supports:
        d.issues.append(
            Issue(
                ERROR,
                "No supports present. Add a pin, roller, fixed, or spring "
                "support to restrain the structure.",
            )
        )
        if d.classification != DRIVEN:
            d.classification = MECHANISM
        return d

    # Loose nodes are legal but almost always a mistake.
    connected = set()
    for m in model.members.values():
        connected.add(m.start)
        connected.add(m.end)
    for nid, n in model.nodes.items():
        if nid not in connected:
            d.issues.append(
                Issue(WARNING, f"{n.label} is not attached to any member.", [("node", nid)])
            )

    # Stability, group by group.
    total_reactions = 0
    total_degree = 0
    total_releases = 0
    groups = [g for g in _components(model) if len(g & connected) > 0]
    unstable = []
    internal = []
    for group in groups:
        matrix, labels = _equilibrium_matrix(model, group)
        total_reactions += matrix.shape[1]
        rank = int(np.linalg.matrix_rank(matrix, tol=1e-9)) if matrix.size else 0
        if rank < 3:
            unstable.append((group, matrix.shape[1], rank))
        members, joints, reactions, released = _group_counts(model, group)
        total_releases += released
        degree = (3 * members + reactions) - (3 * joints + released)
        total_degree += degree
        if degree < 0 and rank >= 3:
            internal.append((group, degree, released))

    d.reaction_count = total_reactions
    d.redundancy = total_degree
    d.releases = total_releases

    if unstable:
        group, count, rank = unstable[0]
        names = ", ".join(sorted(model.nodes[n].label for n in list(group)[:4]))
        if count < 3:
            detail = (
                f"only {count} reaction component"
                f"{'s' if count != 1 else ''} for 3 equilibrium equations"
            )
        else:
            detail = (
                "the supports cannot resist every direction of motion "
                "(parallel or concurrent reaction lines)"
            )
        plural = "part of the structure" if len(groups) > 1 else "the structure"
        message = f"Mechanism: {plural} around {names} can still move, because {detail}."
        if d.classification == DRIVEN:
            d.issues.append(
                Issue(
                    WARNING,
                    "This frame is fully held in this pose. Static results "
                    "treat drivers as rigid links.",
                )
            )
            d.solvable = True
            d.issues.append(Issue(ERROR, message, [("node", n) for n in group]))
            d.classification = MECHANISM
            return d

    if internal:
        group, degree, released = internal[0]
        names = ", ".join(sorted(model.nodes[n].label for n in list(group)[:4]))
        message = (
            f"Mechanism: the hinges around {names} release "
            f"{released} more moment connection"
            f"{'s' if released != 1 else ''} than the structure can "
            "accommodate. Remove a release, or add a support."
        )
        if d.classification == DRIVEN:
            d.issues.append(
                Issue(
                    WARNING,
                    "This frame is fully held in this pose. Static results "
                    "treat drivers as rigid links.",
                )
            )
            d.solvable = True
            d.issues.append(Issue(ERROR, message, [("node", n) for n in group]))
            d.classification = MECHANISM
            return d

        if d.classification == DRIVEN:
            d.issues.append(
                Issue(
                    WARNING,
                    "This frame is fully held in this pose. Static results "
                    "treat drivers as rigid links.",
                )
            )
            d.solvable = True

    d.solvable = True
    d.classification = DETERMINATE if d.redundancy == 0 else INDETERMINATE
    if d.redundancy > 0:
        d.issues.append(
            Issue(
                WARNING,
                f"Statically indeterminate to degree {d.redundancy}: reactions depend "
                "on member stiffness (EA and EI), not statics alone.",
            )
        )
    if total_releases:
        d.issues.append(
            Issue(
                WARNING,
                f"{total_releases} moment release{'s' if total_releases != 1 else ''} "
                "in the frame: those members carry no bending at the released end.",
            )
        )
    slack = [m for m in model.members.values() if m.behaviour in (TENSION_ONLY, COMPRESSION_ONLY)]
    if slack:
        d.issues.append(
            Issue(
                WARNING,
                f"{len(slack)} member{'s' if len(slack) != 1 else ''} can go slack, "
                "so the answer is found by iteration and the load path may change.",
            )
        )
    if any(m.mp_start > 0 or m.mp_end > 0 for m in model.members.values()):
        d.issues.append(
            Issue(
                WARNING,
                "Plastic moment capacities are set: the analysis is non-linear and "
                "hinges will form once a member reaches its capacity.",
            )
        )
    if (
        not any(model.point_loads)
        and not any(model.moment_loads)
        and not any(model.line_loads)
        and not any(m.g for m in model.members.values())
    ):
        d.issues.append(Issue(WARNING, "No loads applied: every result will be zero."))
    return d
