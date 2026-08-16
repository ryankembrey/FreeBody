# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Ryan Kembrey <ryan.FreeCAD@gmail.com>
# SPDX-FileNotice: Part of the FBD addon.

"""Static solver: the only module that knows anaStruct exists.

Everything crosses this boundary in domain terms, so the backend can be swapped
without touching the canvas. Two conventions are handled here, both verified
against textbook cases rather than assumed:

  * anaStruct numbers its own nodes and elements from 1, so we keep a two-way
    map between its ids and ours.
  * anaStruct reports node reactions with the opposite sign to the force the
    support exerts on the structure, so we negate. A 90 N downward load on a
    cantilever comes back as Fy = -90 and becomes a +90 N upward reaction.

Lengths are millimetres in the model. anaStruct is unit agnostic, so we hand it
millimetres directly and keep forces in newtons; the whole system stays
consistent and no scaling is needed.

Three kinds of non-linearity are available, in increasing order of how much
they cost:

    end releases        a hinge is a rotational spring of zero stiffness, so it
                        is still one linear solve
    plastic hinges      a member with a plastic moment capacity forms a hinge
                        once it reaches it; anaStruct iterates internally
    geometric           second order effects, switched on per study
    slack members       a cable that cannot push, or a strut that cannot pull.
                        anaStruct has no such element, so the member is removed
                        and the whole thing re-solved until the set of active
                        members stops changing. That loop lives here.
"""

from typing import Dict, List, Optional, Set
import math

from .model import (
    Model,
    PIN,
    ROLLER_X,
    ROLLER_Y,
    FIXED,
    SPRING,
    DEFAULT_EA,
    DEFAULT_EI,
    BOTH,
    TENSION_ONLY,
    COMPRESSION_ONLY,
)
from .results import StaticResult, Reaction, MemberForces, MemberEnvelope, EnvelopeResult
from . import checks


# anaStruct types this parameter as a Literal, so keep the mapping in one place.
_Q_DIRECTIONS = {"y": "y", "x": "x", "perp": "element"}

_MAX_SLACK_PASSES = 12


class SolverUnavailable(RuntimeError):
    """anaStruct is not installed."""


def backend_available() -> bool:
    try:
        from anastruct.fem.system import SystemElements  # noqa: F401

        return True
    except Exception:
        return False


def backend_version() -> str:
    try:
        import anastruct

        return getattr(anastruct, "__version__", "unknown")
    except Exception:
        return "not installed"


class _Mapping:
    """Two-way map between our ids and anaStruct's.

    member_to_as maps a member to the ordered list of anaStruct element ids
    that make it up, start to end: normally just one, but a member with a
    loaded point on it is split into several so the load lands on a real node,
    which is the only way anaStruct can apply a point load mid-span.
    """

    def __init__(self):
        self.node_to_as: Dict[int, int] = {}
        self.as_to_node: Dict[int, int] = {}
        self.anchor_to_as: Dict[int, int] = {}
        self.as_to_anchor: Dict[int, int] = {}
        self.member_to_as: Dict[int, list] = {}
        self.as_to_member: Dict[int, int] = {}


def _element_kwargs(member) -> dict:
    """Springs and plastic capacities, in anaStruct's own vocabulary.

    Key 1 is the element's start node and key 2 its end. A release is a
    rotational spring of zero stiffness, which is precisely what anaStruct
    means by spring={1: 0}; a finite value is a semi-rigid connection.
    """
    kwargs = {}
    spring = {}
    if member.release_start or member.k_start > 0:
        spring[1] = float(member.k_start) if member.k_start > 0 else 0.0
    if member.release_end or member.k_end > 0:
        spring[2] = float(member.k_end) if member.k_end > 0 else 0.0
    if spring:
        kwargs["spring"] = spring
    mp = {}
    if member.mp_start > 0:
        mp[1] = float(member.mp_start)
    if member.mp_end > 0:
        mp[2] = float(member.mp_end)
    if mp:
        kwargs["mp"] = mp
    if member.g:
        kwargs["g"] = float(member.g)
    return kwargs


def _build(model: Model, skip: Set[int] = frozenset()):
    """Translate the model into an anaStruct SystemElements.

    Members in `skip` are left out entirely, which is how a slack cable is
    represented: an element that is not there carries nothing.
    """
    from anastruct.fem.system import SystemElements

    mesh = max(2, int(getattr(model.analysis, "discretisation", 50) or 50))
    system = SystemElements(EA=DEFAULT_EA, EI=DEFAULT_EI, mesh=mesh)
    mapping = _Mapping()

    # Base elements first, one per member: anaStruct creates nodes implicitly
    # from coordinates, so we discover its node ids afterwards by position.
    base_element = {}
    for m in model.members.values():
        if m.id in skip:
            continue
        a, b = model.member_ends(m)
        kwargs = _element_kwargs(m)
        try:
            as_id = system.add_element(
                location=[[a.x, a.y], [b.x, b.y]], EA=float(m.EA), EI=float(m.EI), **kwargs
            )
        except TypeError:
            # An older anaStruct without one of the keywords: fall back rather
            # than refusing to solve at all.
            as_id = system.add_element(
                location=[[a.x, a.y], [b.x, b.y]], EA=float(m.EA), EI=float(m.EI)
            )
        base_element[m.id] = as_id
        mapping.member_to_as[m.id] = [as_id]
        mapping.as_to_member[as_id] = m.id

    for nid, node in model.nodes.items():
        try:
            as_node = system.find_node_id([node.x, node.y])
        except Exception:
            as_node = None
        if as_node is not None:
            mapping.node_to_as[nid] = as_node
            mapping.as_to_node[as_node] = nid

    # Split members at any anchor that actually carries a load. anaStruct has
    # no concept of a point load mid-element, so the physically correct way to
    # apply one is to insert a real node there, which is exactly what a real
    # structural engineer does when modelling one: insert_node cuts the
    # element into two at the given fraction of its length, and everything
    # downstream (line loads, internal force diagrams) is reassembled across
    # the resulting chain so nothing else needs to know the split happened.
    loaded_anchors = {p.anchor for p in model.point_loads.values() if p.anchor is not None}
    loaded_anchors |= {mo.anchor for mo in model.moment_loads.values() if mo.anchor is not None}
    # A pivot part way along a bar needs a node there just as much as a load
    # does, so the same split serves both.
    loaded_anchors |= {s.anchor for s in model.supports.values() if s.anchor is not None}

    for member_id in list(mapping.member_to_as):
        anchors = [a for a in model.anchors_on(member_id) if a.id in loaded_anchors]
        if not anchors:
            continue
        member = model.members[member_id]
        tail_element = base_element[member_id]
        last_t = 0.0
        chain = []
        for anchor in anchors:  # anchors_on() sorts by t
            t = anchor.t
            if t <= 1e-6:
                mapping.anchor_to_as[anchor.id] = mapping.node_to_as.get(member.start)
                continue
            if t >= 1.0 - 1e-6:
                mapping.anchor_to_as[anchor.id] = mapping.node_to_as.get(member.end)
                continue
            span = 1.0 - last_t
            local_factor = (t - last_t) / span if span > 1e-9 else 0.5
            local_factor = max(1e-6, min(1.0 - 1e-6, local_factor))
            result = system.insert_node(tail_element, factor=local_factor)
            mapping.anchor_to_as[anchor.id] = result["new_node_id"]
            mapping.as_to_anchor[result["new_node_id"]] = anchor.id
            chain.append(result["new_element_id1"])
            tail_element = result["new_element_id2"]
            last_t = t
        chain.append(tail_element)
        mapping.member_to_as[member_id] = chain
        for as_id in chain:
            mapping.as_to_member[as_id] = member_id

    # Supports: always attach to an original topology node, never an anchor,
    # so splitting above doesn't affect this at all.
    for s in model.supports.values():
        as_node = (
            mapping.anchor_to_as.get(s.anchor)
            if s.anchor is not None
            else mapping.node_to_as.get(s.node)
        )
        if as_node is None:
            continue
        angle = float(s.angle or 0.0)
        if s.kind == PIN:
            system.add_support_hinged(as_node)
        elif s.kind == ROLLER_X:
            system.add_support_roll(as_node, direction="x", angle=angle if angle else None)
        elif s.kind == ROLLER_Y:
            system.add_support_roll(as_node, direction="y", angle=angle if angle else None)
        elif s.kind == FIXED:
            system.add_support_fixed(as_node)
        elif s.kind == SPRING:
            # translation: 1 = x, 2 = y, 3 = rotation
            if s.kx > 0:
                system.add_support_spring(as_node, translation=1, k=float(s.kx))
            if s.ky > 0:
                system.add_support_spring(as_node, translation=2, k=float(s.ky))
            if s.kr > 0:
                system.add_support_spring(as_node, translation=3, k=float(s.kr))

    # Loads: at a node directly, or at an anchor's split-in node.
    def _as_node(node, anchor):
        if node is not None:
            return mapping.node_to_as.get(node)
        if anchor is not None:
            return mapping.anchor_to_as.get(anchor)
        return None

    for p in model.point_loads.values():
        as_node = _as_node(p.node, p.anchor)
        if as_node is not None and (p.fx or p.fy):
            system.point_load(as_node, Fx=float(p.fx), Fy=float(p.fy))
    for mo in model.moment_loads.values():
        as_node = _as_node(mo.node, mo.anchor)
        if as_node is not None and mo.m:
            system.moment_load(as_node, Tz=float(mo.m))
    for l in model.line_loads.values():
        chain = mapping.member_to_as.get(l.member)
        if not chain or not l.q:
            continue
        direction = _Q_DIRECTIONS.get(l.direction, "y")
        # q is an intensity (force per length), not a total, so the same value
        # applies unchanged to every sub-element of a split member.
        for as_el in chain:
            system.q_load(float(l.q), as_el, direction=direction)  # type: ignore[arg-type]

    return system, mapping


def _run(system, model: Model):
    """Call anaStruct's own solve, with whatever options this study wants."""
    kwargs = {}
    if getattr(model.analysis, "geometric_nonlinear", False):
        kwargs["geometrical_non_linear"] = True
    if getattr(model.analysis, "max_iter", 0):
        kwargs["max_iter"] = int(model.analysis.max_iter)
    try:
        return system.solve(**kwargs)
    except TypeError:
        return system.solve()


def solve(model: Model, run_checks: bool = True) -> StaticResult:
    """Solve the diagram. Never raises: failures come back as ok=False."""
    result = StaticResult()

    if run_checks:
        diagnosis = checks.check(model)
        if not diagnosis.solvable:
            result.message = diagnosis.summary()
            return result

    if not backend_available():
        result.message = (
            "anaStruct is not installed. Install it with "
            "'pip install anastruct' in FreeCAD's Python "
            "environment."
        )
        return result

    if not _has_loads(model):
        # A valid but unloaded diagram: every result is zero. anaStruct refuses
        # to solve this, so answer it directly rather than surfacing its error.
        for s_obj in model.supports.values():
            result.reactions[s_obj.node] = Reaction(node=s_obj.node)
        for mid in model.members:
            result.members[mid] = MemberForces(member=mid)
        result.ok = True
        result.message = "No loads applied: all results are zero."
        return result

    # Slack members change which elements exist, so the whole solve repeats
    # until the active set settles. An ordinary model does one pass and this
    # loop costs nothing.
    slack_capable = [m for m in model.members.values() if m.behaviour != BOTH]
    inactive: Set[int] = set()
    passes = 0
    attempt = StaticResult()

    while True:
        passes += 1
        attempt = _solve_once(model, inactive)
        if not attempt.ok or not slack_capable or passes > _MAX_SLACK_PASSES:
            break
        violators = set()
        for m in slack_capable:
            if m.id in inactive:
                continue
            forces = attempt.members.get(m.id)
            if forces is None or not forces.axial:
                continue
            axial = forces.axial_max
            if m.behaviour == TENSION_ONLY and axial < -1e-6:
                violators.add(m.id)
            elif m.behaviour == COMPRESSION_ONLY and axial > 1e-6:
                violators.add(m.id)
        if not violators:
            break
        inactive |= violators
        if len(inactive) == len(slack_capable) and not any(
            m.behaviour == BOTH for m in model.members.values()
        ):
            attempt.ok = False
            attempt.message = (
                "Every cable or strut went slack: no member "
                "remains to carry the load. Check the load "
                "direction."
            )
            break

    attempt.iterations = passes
    attempt.inactive = sorted(inactive)
    attempt.nonlinear = (
        bool(inactive)
        or model.has_nonlinear()
        or bool(getattr(model.analysis, "geometric_nonlinear", False))
    )
    for mid in inactive:
        forces = attempt.members.get(mid)
        if forces is None:
            attempt.members[mid] = MemberForces(member=mid, active=False)
        else:
            forces.active = False
    if attempt.ok and inactive:
        names = ", ".join(sorted(model.members[i].label for i in inactive if i in model.members))
        attempt.message = (
            f"Solved in {passes} passes. Slack and carrying no load: {names}."
        )
    return attempt


def _solve_once(model: Model, inactive: Set[int]) -> StaticResult:
    result = StaticResult()
    try:
        system, mapping = _build(model, skip=inactive)
    except Exception as exc:
        result.message = f"Could not assemble the model: {exc}"
        return result

    try:
        _run(system, model)
    except Exception as exc:
        name = type(exc).__name__
        text = str(exc)
        if "stab" in (name + text).lower() or "singular" in (name + text).lower():
            result.message = (
                "The solver reports an unstable structure. Verify "
                "that the supports restrain it against translation "
                "and rotation, and that the hinges have not fully "
                "released a joint."
            )
        else:
            result.message = f"Solver failed: {text}"
        return result

    # Reactions. anaStruct's node result sign is opposite to the force the
    # support applies to the structure, so negate.
    try:
        for entry in system.get_node_results_system():
            as_id = int(entry["id"])
            our_node = mapping.as_to_node.get(as_id)
            if our_node is None:
                our_node = mapping.as_to_anchor.get(as_id)
            if our_node is None:
                continue
            if model.support_holding(our_node) is None:
                continue
            result.reactions[our_node] = Reaction(
                node=our_node,
                fx=-float(entry.get("Fx", 0.0)),
                fy=-float(entry.get("Fy", 0.0)),
                m=-float(entry.get("Tz", 0.0)),
            )
    except Exception as exc:
        result.message = f"Could not read reactions: {exc}"
        return result

    # Member internal forces, sampled along each member. These arrive as numpy
    # arrays, so never truth-test them: `array or []` raises.
    def _series(data, key):
        values = data.get(key)
        if values is None:
            return []
        return [float(v) for v in values]

    try:
        for our_id, chain in mapping.member_to_as.items():
            axial, shear, moment = [], [], []
            for as_id in chain:
                data = system.get_element_results(as_id, verbose=True)
                axial += _series(data, "N")
                shear += _series(data, "Q")
                moment += _series(data, "M")
            result.members[our_id] = MemberForces(
                member=our_id, axial=axial, shear=shear, moment=moment
            )
    except Exception as exc:
        result.diagram_note = f"Internal force diagrams unavailable: {exc}"

    # Node displacements, useful for spring supports and indeterminate frames.
    try:
        for our_node, as_id in mapping.node_to_as.items():
            d = system.get_node_displacements(as_id)
            result.displacements[our_node] = (
                float(d.get("ux", 0.0)),
                float(d.get("uy", 0.0)),
                float(d.get("phi_z", 0.0)),
            )
    except Exception:
        pass

    result.equilibrium_error = _equilibrium_error(model, result)
    result.ok = True
    result.message = "Solved."
    return result


def envelope(model: Model, combo_ids: Optional[List[int]] = None) -> EnvelopeResult:
    """Solve every combination and track the extreme of each result.

    Each combination is solved as its own load case, through the ordinary
    solve() above (including its non-linear iteration for slack members and
    plastic hinges), never by scaling and adding cached results together:
    that would silently assume the same members stay active under every
    combination, which is exactly the assumption a tension-only cable or a
    forming hinge can break.
    """
    out = EnvelopeResult()
    combos = (
        [model.combinations[i] for i in combo_ids if i in model.combinations]
        if combo_ids
        else list(model.combinations.values())
    )
    if not combos:
        out.message = "No load combinations to envelope."
        return out

    peak_member: Dict[tuple, tuple] = {}  # (member, quantity) -> (value, combo id)
    peak_reaction: Dict[tuple, tuple] = {}  # (node, component, 'max'/'min') -> (value, combo id)

    for combo in combos:
        scaled = model.for_combination(combo)
        result = solve(scaled)
        out.results[combo.id] = result
        if not result.ok:
            continue
        for mid, forces in result.members.items():
            env = out.members.setdefault(mid, MemberEnvelope(member=mid))
            for quantity in ("axial", "shear", "moment"):
                values = getattr(forces, quantity)
                if not values:
                    continue
                hi_key, lo_key = f"{quantity}_max", f"{quantity}_min"
                hi, lo = getattr(env, hi_key), getattr(env, lo_key)
                if not hi:
                    setattr(env, hi_key, list(values))
                    setattr(env, lo_key, list(values))
                else:
                    for i in range(min(len(hi), len(values))):
                        if values[i] > hi[i]:
                            hi[i] = values[i]
                        if values[i] < lo[i]:
                            lo[i] = values[i]
                extreme = max(values, key=abs)
                key = (mid, quantity)
                if key not in peak_member or abs(extreme) > abs(peak_member[key][0]):
                    peak_member[key] = (extreme, combo.id)
        for nid, reaction in result.reactions.items():
            for comp in ("fx", "fy", "m"):
                value = getattr(reaction, comp)
                for bound, cmp_ in (("max", lambda a, b: a > b), ("min", lambda a, b: a < b)):
                    key = (nid, comp, bound)
                    current = peak_reaction.get(key)
                    if current is None or cmp_(value, current[0]):
                        peak_reaction[key] = (value, combo.id)

    for (mid, quantity), (_value, combo_id) in peak_member.items():
        out.members[mid].governing[quantity] = combo_id
    for nid, comp, bound in {k for k in peak_reaction}:
        value, combo_id = peak_reaction[(nid, comp, bound)]
        target = out.reactions_max if bound == "max" else out.reactions_min
        r = target.setdefault(nid, Reaction(node=nid))
        setattr(r, comp, value)
        out.reaction_governing[f"{nid}_{comp}_{bound}"] = combo_id

    out.ok = any(r.ok for r in out.results.values())
    n_ok = sum(1 for r in out.results.values() if r.ok)
    out.message = (
        f"Envelope of {n_ok} of {len(combos)} combinations." if out.ok else "No combination solved."
    )
    return out


def _has_loads(model: Model) -> bool:
    if any(p.fx or p.fy for p in model.point_loads.values()):
        return True
    if any(m.m for m in model.moment_loads.values()):
        return True
    if any(m.g for m in model.members.values()):
        return True
    return any(l.q for l in model.line_loads.values())


def _equilibrium_error(model: Model, result: StaticResult) -> float:
    """Independent check that the answer actually balances.

    The solver is trusted, but verifying global equilibrium costs nothing and
    catches a sign or mapping mistake immediately, which is exactly the class of
    bug an adapter introduces.
    """
    fx = fy = mz = 0.0
    for p in model.point_loads.values():
        xy = model.attachment_xy(p.node, p.anchor)
        if xy is None:
            continue
        x, y = xy
        fx += p.fx
        fy += p.fy
        mz += x * p.fy - y * p.fx
    for mo in model.moment_loads.values():
        mz += mo.m
    for l in model.line_loads.values():
        member = model.members.get(l.member)
        if member is None:
            continue
        a, b = model.member_ends(member)
        length = model.member_length(member)
        cx, cy = (a.x + b.x) / 2.0, (a.y + b.y) / 2.0
        if l.direction == "x":
            total = l.q * length
            fx += total
            mz += -cy * total
        elif l.direction == "perp":
            dx, dy = b.x - a.x, b.y - a.y
            ux, uy = -dy / length, dx / length
            total = l.q * length
            fx += total * ux
            fy += total * uy
            mz += cx * (total * uy) - cy * (total * ux)
        else:
            total = l.q * length
            fy += total
            mz += cx * total
    for member in model.members.values():
        if not member.g:
            continue
        a, b = model.member_ends(member)
        total = -abs(member.g) * model.member_length(member)
        cx = (a.x + b.x) / 2.0
        fy += total
        mz += cx * total
    for r in result.reactions.values():
        xy = model.entity_xy(r.node)
        if xy is None:
            continue
        fx += r.fx
        fy += r.fy
        mz += xy[0] * r.fy - xy[1] * r.fx + r.m

    scale = max(
        1.0,
        abs(fx),
        abs(fy),
        max((abs(p.magnitude()) for p in model.point_loads.values()), default=1.0),
    )
    return max(abs(fx), abs(fy), abs(mz) / max(scale, 1.0)) / scale
