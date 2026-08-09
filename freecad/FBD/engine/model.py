# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Ryan Kembrey <ryan.FreeCAD@gmail.com>
# SPDX-FileNotice: Part of the FBD addon.

"""The domain model: what a free body diagram is made of.

Plain dataclasses keyed by integer id. No FreeCAD, no anaStruct, no Qt. This is
the single source of truth that the canvas draws and the solver reads, and it
round-trips to plain dicts so the whole diagram can be stored as one blob.

The same model now carries three things, deliberately sharing one geometry:

    statics       members, supports, loads. Solved by engine.statics.
    mechanisms    the same members read as rigid links, the same joints read as
                  pins, driven by motors and actuators. Solved by
                  engine.kinematics.
    provenance    where a joint or member came from, so a linked Sketcher
                  sketch can be re-read without losing the supports and loads
                  that were placed on it.

Units are FreeCAD-native and consistent throughout:

    length      mm
    force       N
    moment      N.mm
    line load   N/mm
    EA          N
    EI          N.mm^2
    spring k    N/mm (translation), N.mm/rad (rotation)
    speed       deg/s (motors), mm/s (actuators)

Ids are stable for the life of a document, so results and undo snapshots can
refer to entities safely.
"""

from dataclasses import dataclass, field, asdict, fields as dc_fields
from typing import Dict, List, Optional, Tuple
import math


# Support kinds. Kept as strings so the serialized form stays readable and
# forward compatible.
PIN = "pin"
ROLLER_X = "roller_x"  # rolls along x, reacts vertically
ROLLER_Y = "roller_y"  # rolls along y, reacts horizontally
FIXED = "fixed"
SPRING = "spring"

SUPPORT_KINDS = [PIN, ROLLER_X, ROLLER_Y, FIXED, SPRING]

SUPPORT_LABELS = {
    PIN: "Pin",
    ROLLER_X: "Roller (rolls horizontally)",
    ROLLER_Y: "Roller (rolls vertically)",
    FIXED: "Fixed",
    SPRING: "Spring",
}

# How many independent reaction components each kind supplies.
SUPPORT_REACTIONS = {
    PIN: ("fx", "fy"),
    ROLLER_X: ("fy",),
    ROLLER_Y: ("fx",),
    FIXED: ("fx", "fy", "m"),
    SPRING: (),  # filled in per instance from which stiffnesses are set
}

DEFAULT_EA = 2.1e5 * 3.0e3  # nominal steel section, N
DEFAULT_EI = 2.1e5 * 2.0e6  # nominal steel section, N.mm^2

# How a member carries axial force. "both" is an ordinary member; the other two
# are the cheapest useful non-linearity there is, and are resolved by iteration
# in engine.statics rather than by the backend.
BOTH = "both"
TENSION_ONLY = "tension"  # a cable or tie: goes slack in compression
COMPRESSION_ONLY = "compression"  # a strut or a bearing contact: cannot pull

BEHAVIOURS = [BOTH, TENSION_ONLY, COMPRESSION_ONLY]
BEHAVIOUR_LABELS = {
    BOTH: "Normal (tension and compression)",
    TENSION_ONLY: "Tension only (cable)",
    COMPRESSION_ONLY: "Compression only (strut)",
}

# Driver motion profiles.
CONTINUOUS = "continuous"  # motor spins on forever at its set speed
SWEEP = "sweep"  # motor rocks back and forth across its sweep angle
EXTEND = "extend"  # actuator runs out once and holds
CYCLE = "cycle"  # actuator runs out and back, repeating
SINE = "sine"  # actuator follows a smooth sine, no jerk at the ends


def _make(cls, data: dict):
    """Build a dataclass from a dict, ignoring keys it does not know.

    A diagram written by a newer version must not crash an older one, and a
    field added here must not require every stored document to be migrated.
    """
    known = {f.name for f in dc_fields(cls)}
    return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class Node:
    id: int
    x: float
    y: float
    label: str = ""
    # Provenance: the sketch geometry index this joint came from, or -1 when it
    # was drawn by hand, and the sketch coordinate it was made from. The pair
    # is what lets a resync tell where the sketch put a joint apart from where
    # the user has since dragged it, so a sync updates the shape without
    # dragging the whole diagram back to the sketch origin.
    source_geo: int = -1
    sx: float = 0.0
    sy: float = 0.0

    def xy(self) -> Tuple[float, float]:
        return (self.x, self.y)

    @property
    def from_sketch(self) -> bool:
        return self.source_geo >= 0


@dataclass
class Member:
    id: int
    start: int
    end: int
    EA: float = DEFAULT_EA
    EI: float = DEFAULT_EI
    label: str = ""

    # Non-linear and release behaviour. All of these are ignored by the
    # kinematics module, which reads every member as a rigid link.
    release_start: bool = False  # moment released at the start joint (a hinge)
    release_end: bool = False
    k_start: float = 0.0  # rotational spring at the start, N.mm/rad
    k_end: float = 0.0  # zero with a release set means a true hinge
    mp_start: float = 0.0  # plastic moment capacity, N.mm; 0 = elastic
    mp_end: float = 0.0
    behaviour: str = BOTH
    g: float = 0.0  # self weight, N/mm, applied downward

    # Provenance, as for Node.
    source_geo: int = -1

    @property
    def from_sketch(self) -> bool:
        return self.source_geo >= 0

    @property
    def is_truss(self) -> bool:
        """Released at both ends: an axial-only bar."""
        return self.release_start and self.release_end and self.k_start <= 0 and self.k_end <= 0

    @property
    def is_nonlinear(self) -> bool:
        return (self.behaviour != BOTH) or self.mp_start > 0 or self.mp_end > 0

    def released_equations(self) -> int:
        """Moment continuity equations given up at this member's ends.

        A hinge at one end of a member releases one equation. A member hinged
        at both ends releases two, except that one of those is absorbed by the
        bar's own rigid body rotation, so an axial-only bar counts as one.
        """
        count = int(self.release_start and self.k_start <= 0) + int(
            self.release_end and self.k_end <= 0
        )
        return 1 if count == 2 else count


@dataclass
class Support:
    """Holds a joint, or a point along a member.

    A pivot part way along a bar is the ordinary case for a lever, and it has
    to be a point on the member rather than a joint between two members, or
    the two arms are free to fold and the thing stops being a lever.
    """

    id: int
    node: int = 0  # the joint it holds, or 0 when it holds a point
    kind: str = PIN
    angle: float = 0.0  # degrees, for inclined supports and symbol rotation
    kx: float = 0.0  # spring stiffness, N/mm
    ky: float = 0.0
    kr: float = 0.0  # rotational spring, N.mm/rad
    anchor: Optional[int] = None  # set instead of node to hold a point on a member

    @property
    def holds(self) -> int:
        """The id of whatever this support holds, joint or point."""
        return self.anchor if self.anchor is not None else self.node

    def reaction_components(self) -> Tuple[str, ...]:
        if self.kind != SPRING:
            return SUPPORT_REACTIONS[self.kind]
        out = []
        if self.kx > 0:
            out.append("fx")
        if self.ky > 0:
            out.append("fy")
        if self.kr > 0:
            out.append("m")
        return tuple(out)

    def grounds_position(self) -> bool:
        """True when this support pins the joint in place for kinematics."""
        return self.kind in (PIN, FIXED)


@dataclass
class Anchor:
    """A point on a member at fraction t of its length (0 = start, 1 = end).

    Lighter than a Node: it doesn't join members or carry a support, it's just
    an attachment location, for a load applied mid-span or a pivot on a lever.
    Its position is derived from the host member's current endpoints, so it
    moves with the member automatically, in statics and in motion alike.
    """

    id: int
    member: int
    t: float = 0.5
    label: str = ""


@dataclass
class PointLoad:
    id: int
    node: Optional[int] = None
    anchor: Optional[int] = None
    fx: float = 0.0
    fy: float = 0.0
    label: str = ""

    def magnitude(self) -> float:
        return math.hypot(self.fx, self.fy)


@dataclass
class MomentLoad:
    id: int
    node: Optional[int] = None
    anchor: Optional[int] = None
    m: float = 0.0  # counter-clockwise positive, N.mm
    label: str = ""


@dataclass
class LineLoad:
    """Uniform load along a member, N/mm.

    direction 'y' is global vertical (the common case, e.g. self weight or a
    floor load), 'x' is global horizontal, 'perp' acts perpendicular to the
    member (e.g. wind on a sloping roof).
    """

    id: int
    member: int
    q: float = 0.0
    direction: str = "y"
    label: str = ""


@dataclass
class Motor:
    """A rotary driver: turns one link about one grounded joint.

    The joint it turns about must be held in place (a pin or fixed support),
    because a motor needs something to react against. Positive speed is
    counter-clockwise, matching the moment convention everywhere else.
    """

    id: int
    node: int  # the grounded joint it turns about
    member: int  # the link it drives
    speed: float = 60.0  # deg/s
    motion: str = CONTINUOUS  # CONTINUOUS or SWEEP
    sweep: float = 90.0  # degrees either side of the start angle
    label: str = ""


@dataclass
class Actuator:
    """A linear driver: a member whose length is prescribed over time.

    The member keeps its EA and EI, so the same entity is a ram in motion and
    an ordinary member in statics, which is what makes the two views agree.
    """

    id: int
    member: int
    stroke: float = 100.0  # mm of travel, positive extends
    speed: float = 50.0  # mm/s
    motion: str = CYCLE  # EXTEND, CYCLE or SINE
    label: str = ""


@dataclass
class Motion:
    """How long to simulate for and how finely."""

    duration: float = 4.0  # seconds
    fps: int = 30
    trace: bool = True  # draw the path swept by moving joints
    ghosts: int = 0  # faint copies of earlier frames, 0 = none


@dataclass
class SketchLink:
    """A live link to a Sketcher sketch.

    Holding the object's Name rather than the object keeps the engine free of
    FreeCAD, and the Name is stable for the life of the document where the
    Label is not.
    """

    object_name: str = ""
    label: str = ""
    auto_sync: bool = True
    tolerance: float = 1.0  # mm: how far a joint may have moved and still
    # be recognised as the same joint


@dataclass
class Analysis:
    """Solver settings that are properties of the study, not the structure."""

    geometric_nonlinear: bool = False
    max_iter: int = 200
    discretisation: int = 50  # sampling points per member for the diagrams


@dataclass
class Sheet:
    """Paper space. The diagram is a drawing, so it lives on a sheet."""

    width: float = 420.0  # A3 landscape
    height: float = 297.0
    name: str = "A3 landscape"
    title: str = "Free Body Diagram"
    grid: float = 10.0
    unit_scale: float = 1.0
    calibrated: bool = False
    grid_unit: float = 10.0
    scale: float = 1.0


SHEET_PRESETS = {
    "A4 landscape": (297.0, 210.0),
    "A4 portrait": (210.0, 297.0),
    "A3 landscape": (420.0, 297.0),
    "A3 portrait": (297.0, 420.0),
    "A2 landscape": (594.0, 420.0),
}


@dataclass
class Model:
    nodes: Dict[int, Node] = field(default_factory=dict)
    members: Dict[int, Member] = field(default_factory=dict)
    supports: Dict[int, Support] = field(default_factory=dict)
    anchors: Dict[int, Anchor] = field(default_factory=dict)
    point_loads: Dict[int, PointLoad] = field(default_factory=dict)
    moment_loads: Dict[int, MomentLoad] = field(default_factory=dict)
    line_loads: Dict[int, LineLoad] = field(default_factory=dict)
    motors: Dict[int, Motor] = field(default_factory=dict)
    actuators: Dict[int, Actuator] = field(default_factory=dict)
    sheet: Sheet = field(default_factory=Sheet)
    motion: Motion = field(default_factory=Motion)
    analysis: Analysis = field(default_factory=Analysis)
    sketch_link: Optional[SketchLink] = None
    _next_id: int = 1

    # === ids

    def new_id(self) -> int:
        i = self._next_id
        self._next_id += 1
        return i

    # === building

    def add_node(self, x: float, y: float, label: str = "", source_geo: int = -1) -> Node:
        n = Node(self.new_id(), float(x), float(y), label, source_geo)
        if not n.label:
            n.label = f"N{len(self.nodes) + 1}"
        self.nodes[n.id] = n
        return n

    def add_member(self, start: int, end: int, **kw) -> Member:
        m = Member(self.new_id(), start, end, **kw)
        if not m.label:
            m.label = f"M{len(self.members) + 1}"
        self.members[m.id] = m
        return m

    def add_support(self, node: int, kind: str = PIN, **kw) -> Support:
        # One support per node: replace rather than stack.
        for sid, s in list(self.supports.items()):
            if s.node == node:
                del self.supports[sid]
        s = Support(self.new_id(), node, kind, **kw)
        self.supports[s.id] = s
        return s

    def add_support_on(self, anchor: int, kind: str = PIN, **kw) -> Support:
        """Support a point along a member: a pivot, rather than a joint."""
        for sid, s in list(self.supports.items()):
            if s.anchor == anchor:
                del self.supports[sid]
        s = Support(self.new_id(), 0, kind, anchor=anchor, **kw)
        self.supports[s.id] = s
        return s

    def add_anchor(self, member: int, t: float, label: str = "") -> Anchor:
        """A point on a member at fraction t (0..1) of its length, for a
        mid-span load or a pivot."""
        t = max(0.0, min(1.0, float(t)))
        a = Anchor(self.new_id(), member, t, label)
        if not a.label:
            a.label = f"P{len(self.anchors) + 1}"
        self.anchors[a.id] = a
        return a

    def add_point_load(
        self,
        node: Optional[int] = None,
        fx: float = 0.0,
        fy: float = 0.0,
        anchor: Optional[int] = None,
    ) -> PointLoad:
        p = PointLoad(self.new_id(), node, anchor, float(fx), float(fy))
        self.point_loads[p.id] = p
        return p

    def add_moment_load(
        self, node: Optional[int] = None, m: float = 0.0, anchor: Optional[int] = None
    ) -> MomentLoad:
        mm = MomentLoad(self.new_id(), node, anchor, float(m))
        self.moment_loads[mm.id] = mm
        return mm

    def add_line_load(self, member: int, q: float = 0.0, direction: str = "y") -> LineLoad:
        l = LineLoad(self.new_id(), member, float(q), direction)
        self.line_loads[l.id] = l
        return l

    def add_motor(self, node: int, member: int, **kw) -> Motor:
        # One motor per joint: a joint cannot be driven twice.
        for mid, existing in list(self.motors.items()):
            if existing.node == node:
                del self.motors[mid]
        mo = Motor(self.new_id(), node, member, **kw)
        if not mo.label:
            mo.label = f"Motor {len(self.motors) + 1}"
        self.motors[mo.id] = mo
        return mo

    def add_actuator(self, member: int, **kw) -> Actuator:
        for aid, existing in list(self.actuators.items()):
            if existing.member == member:
                del self.actuators[aid]
        a = Actuator(self.new_id(), member, **kw)
        if not a.label:
            a.label = f"Ram {len(self.actuators) + 1}"
        self.actuators[a.id] = a
        return a

    def add_lever(
        self, x: float, y: float, length: float = 200.0, angle: float = 0.0, ratio: float = 0.5
    ) -> dict:
        """A first class lever: one bar, pinned at a point along its length.

        Deliberately one member with a pivot point on it, not two members
        meeting at a joint. Two members would be free to fold about that joint,
        so dragging an end would break the bar rather than swing it; one member
        keeps both arms colinear because there is only one bar to be colinear
        with. ratio is where the pivot sits along it, so 0.5 is the textbook
        first class lever with a mechanical advantage of one.
        """
        ratio = max(0.05, min(0.95, float(ratio)))
        theta = math.radians(angle)
        ux, uy = math.cos(theta), math.sin(theta)
        a_len = length * ratio
        b_len = length * (1.0 - ratio)
        a = self.add_node(x - ux * a_len, y - uy * a_len, "Effort")
        b = self.add_node(x + ux * b_len, y + uy * b_len, "Load")
        bar = self.add_member(a.id, b.id)
        pivot = self.add_anchor(bar.id, ratio, "Pivot")
        self.add_support_on(pivot.id, PIN)
        return {
            "effort": a.id,
            "load": b.id,
            "pivot": pivot.id,
            "member": bar.id,
            "advantage": (a_len / b_len) if b_len > 1e-9 else float("inf"),
        }

    # === queries

    def support_at(self, node: int) -> Optional[Support]:
        for s in self.supports.values():
            if s.node == node:
                return s
        return None

    def support_on_anchor(self, anchor: int) -> Optional[Support]:
        for s in self.supports.values():
            if s.anchor == anchor:
                return s
        return None

    def support_holding(self, ident: int) -> Optional[Support]:
        """The support on this joint or point, whichever it is.

        Ids are unique across the whole model, so one lookup covers both and
        results can be keyed by a single id without ambiguity.
        """
        for s in self.supports.values():
            if s.holds == ident:
                return s
        return None

    def entity_xy(self, ident: int) -> Optional[Tuple[float, float]]:
        """Where a joint or a point currently sits."""
        node = self.nodes.get(ident)
        if node is not None:
            return node.xy()
        anchor = self.anchors.get(ident)
        return self.anchor_xy(anchor) if anchor else None

    def entity_label(self, ident: int) -> str:
        node = self.nodes.get(ident)
        if node is not None:
            return node.label
        anchor = self.anchors.get(ident)
        return anchor.label if anchor else str(ident)

    def support_xy(self, s: Support) -> Optional[Tuple[float, float]]:
        return self.entity_xy(s.holds)

    def support_home_node(self, s: Support) -> Optional[int]:
        """A joint in the same connected group as this support.

        A support on a point belongs to whichever group its host member is in,
        which is what the stability check needs to count reactions per group.
        """
        if s.anchor is None:
            return s.node
        anchor = self.anchors.get(s.anchor)
        member = self.members.get(anchor.member) if anchor else None
        return member.start if member else None

    def motor_at(self, node: int) -> Optional[Motor]:
        for mo in self.motors.values():
            if mo.node == node:
                return mo
        return None

    def actuator_on(self, member: int) -> Optional[Actuator]:
        for a in self.actuators.values():
            if a.member == member:
                return a
        return None

    def anchor_xy(self, anchor: "Anchor") -> Optional[Tuple[float, float]]:
        member = self.members.get(anchor.member)
        if member is None:
            return None
        a = self.nodes.get(member.start)
        b = self.nodes.get(member.end)
        if a is None or b is None:
            return None
        t = anchor.t
        return (a.x + t * (b.x - a.x), a.y + t * (b.y - a.y))

    def attachment_xy(
        self, node: Optional[int], anchor: Optional[int]
    ) -> Optional[Tuple[float, float]]:
        """Resolve a load's position, whichever kind of attachment it has."""
        if node is not None:
            n = self.nodes.get(node)
            return n.xy() if n else None
        if anchor is not None:
            a = self.anchors.get(anchor)
            return self.anchor_xy(a) if a else None
        return None

    def anchors_on(self, member: int) -> List[Anchor]:
        return sorted((a for a in self.anchors.values() if a.member == member), key=lambda a: a.t)

    def members_at(self, node: int) -> List[Member]:
        return [m for m in self.members.values() if m.start == node or m.end == node]

    def loads_at(self, node: int) -> List[PointLoad]:
        return [p for p in self.point_loads.values() if p.node == node]

    def loads_on_anchor(self, anchor: int):
        return [p for p in self.point_loads.values() if p.anchor == anchor] + [
            m for m in self.moment_loads.values() if m.anchor == anchor
        ]

    def member_length(self, member: Member) -> float:
        a, b = self.nodes[member.start], self.nodes[member.end]
        return math.hypot(b.x - a.x, b.y - a.y)

    def member_ends(self, member: Member) -> Tuple[Node, Node]:
        return self.nodes[member.start], self.nodes[member.end]

    def node_at(self, x: float, y: float, tol: float = 1e-6) -> Optional[Node]:
        for n in self.nodes.values():
            if math.hypot(n.x - x, n.y - y) <= tol:
                return n
        return None

    def bounds(self) -> Tuple[float, float, float, float]:
        if not self.nodes:
            return (0.0, 0.0, self.sheet.width, self.sheet.height)
        xs = [n.x for n in self.nodes.values()]
        ys = [n.y for n in self.nodes.values()]
        return (min(xs), min(ys), max(xs), max(ys))

    def is_empty(self) -> bool:
        return not self.nodes and not self.members

    def has_drivers(self) -> bool:
        return bool(self.motors) or bool(self.actuators)

    def has_nonlinear(self) -> bool:
        return any(m.is_nonlinear for m in self.members.values())

    def has_releases(self) -> bool:
        return any(m.release_start or m.release_end for m in self.members.values())

    # === deletion, with referential integrity

    def delete(self, kind: str, ident: int) -> None:
        """Delete an entity and anything that depends on it."""
        if kind == "node":
            self.nodes.pop(ident, None)
            for mid in [m.id for m in self.members.values() if m.start == ident or m.end == ident]:
                self.delete("member", mid)
            for sid in [s.id for s in self.supports.values() if s.node == ident]:
                self.supports.pop(sid, None)
            for pid in [p.id for p in self.point_loads.values() if p.node == ident]:
                self.point_loads.pop(pid, None)
            for mid in [m.id for m in self.moment_loads.values() if m.node == ident]:
                self.moment_loads.pop(mid, None)
            for mid in [mo.id for mo in self.motors.values() if mo.node == ident]:
                self.motors.pop(mid, None)
        elif kind == "member":
            self.members.pop(ident, None)
            for lid in [l.id for l in self.line_loads.values() if l.member == ident]:
                self.line_loads.pop(lid, None)
            for aid in [a.id for a in self.anchors.values() if a.member == ident]:
                self.delete("anchor", aid)
            for mid in [mo.id for mo in self.motors.values() if mo.member == ident]:
                self.motors.pop(mid, None)
            for aid in [ac.id for ac in self.actuators.values() if ac.member == ident]:
                self.actuators.pop(aid, None)
        elif kind == "anchor":
            self.anchors.pop(ident, None)
            for sid in [s.id for s in self.supports.values() if s.anchor == ident]:
                self.supports.pop(sid, None)
            for p in [p.id for p in self.point_loads.values() if p.anchor == ident]:
                self.point_loads.pop(p, None)
            for m in [m.id for m in self.moment_loads.values() if m.anchor == ident]:
                self.moment_loads.pop(m, None)
        elif kind == "support":
            self.supports.pop(ident, None)
            # A motor needs something to react against, so it goes with the
            # support that was holding it.
            grounded = {s.node for s in self.supports.values() if s.grounds_position()}
            for mo in list(self.motors.values()):
                if mo.node not in grounded:
                    self.motors.pop(mo.id, None)
        elif kind == "point_load":
            self.point_loads.pop(ident, None)
        elif kind == "moment_load":
            self.moment_loads.pop(ident, None)
        elif kind == "line_load":
            self.line_loads.pop(ident, None)
        elif kind == "motor":
            self.motors.pop(ident, None)
        elif kind == "actuator":
            self.actuators.pop(ident, None)

    # === serialization

    def to_dict(self) -> dict:
        return {
            "version": 2,
            "next_id": self._next_id,
            "sheet": asdict(self.sheet),
            "motion": asdict(self.motion),
            "analysis": asdict(self.analysis),
            "sketch_link": asdict(self.sketch_link) if self.sketch_link else None,
            "nodes": [asdict(n) for n in self.nodes.values()],
            "members": [asdict(m) for m in self.members.values()],
            "supports": [asdict(s) for s in self.supports.values()],
            "anchors": [asdict(a) for a in self.anchors.values()],
            "point_loads": [asdict(p) for p in self.point_loads.values()],
            "moment_loads": [asdict(m) for m in self.moment_loads.values()],
            "line_loads": [asdict(l) for l in self.line_loads.values()],
            "motors": [asdict(m) for m in self.motors.values()],
            "actuators": [asdict(a) for a in self.actuators.values()],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Model":
        m = cls()
        if not data:
            return m
        m._next_id = int(data.get("next_id", 1))
        m.sheet = _make(Sheet, data.get("sheet") or {})
        m.motion = _make(Motion, data.get("motion") or {})
        m.analysis = _make(Analysis, data.get("analysis") or {})
        link = data.get("sketch_link")
        m.sketch_link = _make(SketchLink, link) if link else None
        for d in data.get("nodes", []):
            m.nodes[d["id"]] = _make(Node, d)
        for d in data.get("members", []):
            m.members[d["id"]] = _make(Member, d)
        for d in data.get("supports", []):
            m.supports[d["id"]] = _make(Support, d)
        for d in data.get("anchors", []):
            m.anchors[d["id"]] = _make(Anchor, d)
        for d in data.get("point_loads", []):
            m.point_loads[d["id"]] = _make(PointLoad, d)
        for d in data.get("moment_loads", []):
            m.moment_loads[d["id"]] = _make(MomentLoad, d)
        for d in data.get("line_loads", []):
            m.line_loads[d["id"]] = _make(LineLoad, d)
        for d in data.get("motors", []):
            m.motors[d["id"]] = _make(Motor, d)
        for d in data.get("actuators", []):
            m.actuators[d["id"]] = _make(Actuator, d)
        # Repair a corrupted counter rather than handing out duplicate ids.
        used = (
            [n for n in m.nodes]
            + [i for i in m.members]
            + [i for i in m.supports]
            + [i for i in m.anchors]
            + [i for i in m.point_loads]
            + [i for i in m.moment_loads]
            + [i for i in m.line_loads]
            + [i for i in m.motors]
            + [i for i in m.actuators]
        )
        if used:
            m._next_id = max(m._next_id, max(used) + 1)
        return m

    def copy(self) -> "Model":
        return Model.from_dict(self.to_dict())
