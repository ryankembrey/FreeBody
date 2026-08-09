# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Ryan Kembrey <ryan.FreeCAD@gmail.com>
# SPDX-FileNotice: Part of the FBD addon.

"""What the solvers hand back.

Everything is expressed in the domain model's own ids and units, so the canvas
never has to know a backend exists. Sign conventions, stated once:

    reactions       the force the support exerts *on* the structure, in global
                    x and y; a couple is counter-clockwise positive
    axial           tension positive
    shear, moment   standard beam convention along the member's local axis,
                    sampled start to end
    motion          joint positions in model coordinates, velocities in mm/s,
                    driver effort as N.mm for a motor and N for an actuator,
                    positive meaning the driver is doing work on the load
"""

from dataclasses import dataclass, field
from typing import Dict, List, Tuple
import math


@dataclass
class Reaction:
    node: int
    fx: float = 0.0
    fy: float = 0.0
    m: float = 0.0

    def magnitude(self) -> float:
        return math.hypot(self.fx, self.fy)


@dataclass
class MemberForces:
    member: int
    axial: List[float] = field(default_factory=list)     # sampled start -> end
    shear: List[float] = field(default_factory=list)
    moment: List[float] = field(default_factory=list)
    active: bool = True          # False when a tension-only member has gone slack

    def _extreme(self, values):
        if not values:
            return 0.0
        return max(values, key=abs)

    @property
    def axial_max(self) -> float:
        return self._extreme(self.axial)

    @property
    def shear_max(self) -> float:
        return self._extreme(self.shear)

    @property
    def moment_max(self) -> float:
        return self._extreme(self.moment)


@dataclass
class StaticResult:
    ok: bool = False
    message: str = ""
    reactions: Dict[int, Reaction] = field(default_factory=dict)
    members: Dict[int, MemberForces] = field(default_factory=dict)
    displacements: Dict[int, Tuple[float, float, float]] = field(default_factory=dict)
    equilibrium_error: float = 0.0
    diagram_note: str = ""
    nonlinear: bool = False
    iterations: int = 1
    inactive: List[int] = field(default_factory=list)   # members that went slack

    def reaction_total(self) -> Tuple[float, float]:
        return (sum(r.fx for r in self.reactions.values()),
                sum(r.fy for r in self.reactions.values()))

    def peak_moment(self) -> float:
        if not self.members:
            return 0.0
        return max((abs(m.moment_max) for m in self.members.values()), default=0.0)

    def peak_shear(self) -> float:
        if not self.members:
            return 0.0
        return max((abs(m.shear_max) for m in self.members.values()), default=0.0)

    def peak_axial(self) -> float:
        if not self.members:
            return 0.0
        return max((abs(m.axial_max) for m in self.members.values()), default=0.0)


@dataclass
class Frame:
    """One instant of a mechanism's motion."""
    t: float = 0.0
    positions: Dict[int, Tuple[float, float]] = field(default_factory=dict)
    velocities: Dict[int, Tuple[float, float]] = field(default_factory=dict)
    effort: Dict[int, float] = field(default_factory=dict)   # driver id -> torque or force
    residual: float = 0.0
    ok: bool = True

    def speed(self, node: int) -> float:
        vx, vy = self.velocities.get(node, (0.0, 0.0))
        return math.hypot(vx, vy)


@dataclass
class MotionResult:
    ok: bool = False
    message: str = ""
    mobility: int = 0
    frames: List[Frame] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return self.frames[-1].t if self.frames else 0.0

    def frame_at(self, t: float) -> Frame:
        """The nearest computed frame. Frames are evenly spaced, so this is a
        lookup rather than a search."""
        if not self.frames:
            return Frame()
        if self.duration <= 0:
            return self.frames[0]
        index = int(round(t / self.duration * (len(self.frames) - 1)))
        return self.frames[max(0, min(len(self.frames) - 1, index))]

    def path(self, node: int) -> List[Tuple[float, float]]:
        """Every position this joint passes through, for drawing its trace."""
        return [f.positions[node] for f in self.frames if node in f.positions]

    def peak_speed(self) -> float:
        return max((f.speed(n) for f in self.frames for n in f.velocities),
                   default=0.0)

    def peak_effort(self) -> Dict[int, float]:
        out: Dict[int, float] = {}
        for f in self.frames:
            for ident, value in f.effort.items():
                if abs(value) > abs(out.get(ident, 0.0)):
                    out[ident] = value
        return out
