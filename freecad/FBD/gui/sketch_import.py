# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileNotice: Part of the FBD addon.

"""Bring Sketcher geometry into a diagram, and keep it there.

Import reads lines as members and points as standalone joints, in the sketch's
own local 2D coordinates, so a sketch on any plane keeps its true dimensions
and orientation instead of being flattened as if seen from Top.

Linking is the part that matters. A re-import that rebuilt the model would
throw away every support and load the user had placed, so resync never
rebuilds: it *moves* the joints that are still there. Because ids survive, the
supports, loads, anchors and drivers hanging off those joints survive with
them, without any of them having to know a sketch exists.

Matching, in order of confidence:

    1. geometry index      a member remembers which sketch line made it
    2. position            a joint within tolerance of where it used to be
    3. give up gracefully  a joint whose line has gone is deleted, unless it
                           carries a support, a load or a driver, in which
                           case it is kept and cut loose from the sketch, and
                           the report says so

Hand-drawn joints and members are never touched, so a sketch can be the frame
and everything else drawn on top of it.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
import math

COINCIDENT_TOL = 1e-3  # mm: sketch endpoints the constraints tied together


def _fit_placement(pairs):
    """The similarity transform taking sketch space to where the user put it.

    Imported geometry gets dragged around the sheet, and calibrating the first
    member multiplies every coordinate. Both are placement, not shape, and a
    resync that ignored them would haul the whole structure back to the sketch
    origin in the bottom left corner every time the sketch changed, which is
    exactly the bug this exists to stop.

    So instead of trusting raw sketch coordinates, fit translation, rotation
    and uniform scale across every joint that survived the sync, in the least
    squares sense. Genuine sketch edits are not absorbed by this: moving one
    line among many barely shifts the fit, while moving the whole sketch is
    read as placement and correctly leaves the diagram where it sits.

    pairs is [((sketch x, sketch y), (model x, model y)), ...].
    Returns a callable mapping a sketch point to a model point.
    """
    usable = [(p, q) for p, q in pairs if p is not None and q is not None]
    if not usable:
        return lambda x, y: (x, y)
    if len(usable) == 1:
        (px, py), (qx, qy) = usable[0]
        dx, dy = qx - px, qy - py
        return lambda x, y: (x + dx, y + dy)

    n = float(len(usable))
    pcx = sum(p[0] for p, _ in usable) / n
    pcy = sum(p[1] for p, _ in usable) / n
    qcx = sum(q[0] for _, q in usable) / n
    qcy = sum(q[1] for _, q in usable) / n

    sxx = sxy = syx = syy = norm = 0.0
    for (px, py), (qx, qy) in usable:
        ax, ay = px - pcx, py - pcy
        bx, by = qx - qcx, qy - qcy
        sxx += ax * bx
        sxy += ax * by
        syx += ay * bx
        syy += ay * by
        norm += ax * ax + ay * ay
    if norm < 1e-12:
        dx, dy = qcx - pcx, qcy - pcy
        return lambda x, y: (x + dx, y + dy)

    # Closed form for the best rotation in 2D, then the scale that goes with it.
    theta = math.atan2(sxy - syx, sxx + syy)
    ca, sa = math.cos(theta), math.sin(theta)
    scale = (ca * (sxx + syy) + sa * (sxy - syx)) / norm
    if not (1e-6 < abs(scale) < 1e6):
        scale = 1.0

    def place(x, y):
        ax, ay = x - pcx, y - pcy
        return (qcx + scale * (ca * ax - sa * ay), qcy + scale * (sa * ax + ca * ay))

    return place


@dataclass
class SyncReport:
    ok: bool = False
    message: str = ""
    moved: int = 0
    added_nodes: int = 0
    added_members: int = 0
    removed_members: int = 0
    kept_orphans: List[str] = field(default_factory=list)
    dropped: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def summary(self) -> str:
        if not self.ok:
            return self.message
        bits = []
        if self.moved:
            bits.append(f"{self.moved} joint{'s' if self.moved != 1 else ''} moved")
        if self.added_members:
            bits.append(
                f"{self.added_members} member{'s' if self.added_members != 1 else ''} added"
            )
        if self.removed_members:
            bits.append(f"{self.removed_members} removed")
        if self.kept_orphans:
            bits.append(f"{len(self.kept_orphans)} kept because they carry something")
        return "Sketch synced: " + (", ".join(bits) if bits else "nothing changed") + "."


# === reading the sketch


def read_sketch(sketch) -> Tuple[List[tuple], List[tuple], set]:
    """(segments, points, skipped kinds) in sketch local coordinates.

    segments are (geo_index, (x1, y1), (x2, y2)); points are (geo_index, (x, y)).
    """
    import Part

    geometry = getattr(sketch, "Geometry", None)
    if geometry is None:
        raise ValueError(f"'{getattr(sketch, 'Label', sketch)}' has no sketch geometry")

    def is_construction(index) -> bool:
        try:
            return bool(sketch.getConstruction(index))
        except Exception:
            return False

    segments, points, skipped = [], [], set()
    for index, geo in enumerate(geometry):
        if is_construction(index):
            continue
        if isinstance(geo, Part.LineSegment):
            segments.append(
                (index, (geo.StartPoint.x, geo.StartPoint.y), (geo.EndPoint.x, geo.EndPoint.y))
            )
        elif isinstance(geo, Part.Point):
            points.append((index, (geo.X, geo.Y)))
        else:
            skipped.add(type(geo).__name__)
    return segments, points, skipped


def _warn(message):
    try:
        import FreeCAD as App

        App.Console.PrintWarning("FBD: " + message + "\n")
    except Exception:
        pass


# === first import


def import_sketch(model, sketch, link: bool = True) -> Tuple[List, List]:
    """Read `sketch` into `model`, optionally linking it for later resync.

    Returns (nodes, members) that were added or reused, so the caller can
    report a count.
    """
    from ..engine.model import SketchLink

    segments, points, skipped = read_sketch(sketch)
    touched: Dict[int, object] = {}
    members = []

    for index, (x1, y1), (x2, y2) in segments:
        n1 = model.node_at(x1, y1, tol=COINCIDENT_TOL) or model.add_node(x1, y1, source_geo=index)
        n2 = model.node_at(x2, y2, tol=COINCIDENT_TOL) or model.add_node(x2, y2, source_geo=index)
        n1.sx, n1.sy = x1, y1
        n2.sx, n2.sy = x2, y2
        touched[n1.id] = n1
        touched[n2.id] = n2
        if n1.id == n2.id:
            continue  # zero length in the sketch
        member = model.add_member(n1.id, n2.id)
        member.source_geo = index
        members.append(member)

    for index, (x, y) in points:
        node = model.node_at(x, y, tol=COINCIDENT_TOL) or model.add_node(x, y, source_geo=index)
        node.sx, node.sy = x, y
        touched[node.id] = node

    if skipped:
        _warn(
            "skipped %s geometry in '%s' (only lines and points import)."
            % (", ".join(sorted(skipped)), getattr(sketch, "Label", sketch))
        )

    if link:
        model.sketch_link = SketchLink(
            object_name=getattr(sketch, "Name", ""),
            label=getattr(sketch, "Label", ""),
            auto_sync=True,
            tolerance=1.0,
        )

    return list(touched.values()), members


# === resync


def _carries_something(model, node_id) -> bool:
    if model.support_at(node_id) is not None:
        return True
    if any(p.node == node_id for p in model.point_loads.values()):
        return True
    if any(m.node == node_id for m in model.moment_loads.values()):
        return True
    if any(mo.node == node_id for mo in model.motors.values()):
        return True
    return False


def resync(model, sketch) -> SyncReport:
    """Re-read a linked sketch in place. Never raises."""
    report = SyncReport()
    try:
        segments, points, skipped = read_sketch(sketch)
    except Exception as exc:
        report.message = f"Could not read the sketch: {exc}"
        return report
    if skipped:
        report.warnings.append(
            "Skipped " + ", ".join(sorted(skipped)) + ": only lines and points import."
        )

    link = model.sketch_link
    tol = float(getattr(link, "tolerance", 1.0) or 1.0)

    old_members = {m.source_geo: m for m in model.members.values() if m.from_sketch}
    old_nodes = {nid: n for nid, n in model.nodes.items() if n.from_sketch}

    # Every joint the new geometry wants, merged the same way the sketch's own
    # coincident constraints merge them.
    wanted: List[Tuple[float, float, int]] = []

    def want(x, y, index):
        for k, (wx, wy, _g) in enumerate(wanted):
            if math.hypot(wx - x, wy - y) <= COINCIDENT_TOL:
                return k
        wanted.append((x, y, index))
        return len(wanted) - 1

    new_segments = []
    for index, (x1, y1), (x2, y2) in segments:
        a, b = want(x1, y1, index), want(x2, y2, index)
        if a != b:
            new_segments.append((index, a, b))
    for index, (x, y) in points:
        want(x, y, index)

    # Pass 1: a member that kept its geometry index hands us its two joints.
    slot_to_node: Dict[int, int] = {}
    used_nodes = set()
    for index, a, b in new_segments:
        member = old_members.get(index)
        if member is None:
            continue
        for slot, node_id in ((a, member.start), (b, member.end)):
            if slot not in slot_to_node and node_id in old_nodes and node_id not in used_nodes:
                slot_to_node[slot] = node_id
                used_nodes.add(node_id)

    # Pass 2: anything left, matched by where it used to be.
    for slot, (x, y, _g) in enumerate(wanted):
        if slot in slot_to_node:
            continue
        best, best_d = None, tol
        for nid, node in old_nodes.items():
            if nid in used_nodes:
                continue
            d = math.hypot(node.x - x, node.y - y)
            if d <= best_d:
                best, best_d = nid, d
        if best is not None:
            slot_to_node[slot] = best
            used_nodes.add(best)

    # Where the user has put this structure, read off the joints that survived.
    place = _fit_placement(
        [
            ((node.sx, node.sy), (node.x, node.y))
            for node in (model.nodes[n] for n in slot_to_node.values() if n in model.nodes)
        ]
    )

    # Apply: move what matched, create what did not, all in the user's frame.
    for slot, (x, y, index) in enumerate(wanted):
        px, py = place(x, y)
        nid = slot_to_node.get(slot)
        if nid is None:
            node = model.add_node(px, py, source_geo=index)
            node.sx, node.sy = x, y
            slot_to_node[slot] = node.id
            report.added_nodes += 1
            continue
        node = model.nodes[nid]
        if math.hypot(node.x - px, node.y - py) > 1e-9:
            node.x, node.y = px, py
            report.moved += 1
        node.source_geo = index
        node.sx, node.sy = x, y

    # Members: keep matched ones pointing at the right joints, add the new.
    seen_geo = set()
    for index, a, b in new_segments:
        seen_geo.add(index)
        start, end = slot_to_node[a], slot_to_node[b]
        member = old_members.get(index)
        if member is None:
            member = model.add_member(start, end)
            member.source_geo = index
            report.added_members += 1
        else:
            member.start, member.end = start, end

    for index, member in old_members.items():
        if index in seen_geo:
            continue
        label = member.label
        model.delete("member", member.id)  # cascades to its line loads
        report.removed_members += 1
        report.dropped.append(label)

    # Joints the sketch no longer has. Keep the ones doing a job.
    live = set(slot_to_node.values())
    for nid, node in list(old_nodes.items()):
        if nid in live:
            continue
        if _carries_something(model, nid) or model.members_at(nid):
            node.source_geo = -1  # cut loose, hand drawn from now on
            report.kept_orphans.append(node.label)
        else:
            model.delete("node", nid)

    report.ok = True
    report.message = report.summary()
    return report


def find_linked_sketch(model, doc):
    """The linked sketch object, or None if the link is broken."""
    link = getattr(model, "sketch_link", None)
    if not link or not link.object_name or doc is None:
        return None
    obj = doc.getObject(link.object_name)
    if obj is None or getattr(obj, "TypeId", "") != "Sketcher::SketchObject":
        return None
    return obj
