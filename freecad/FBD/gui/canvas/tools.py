# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Ryan Kembrey <ryan.FreeCAD@gmail.com>
# SPDX-FileNotice: Part of the FBD addon.

"""Drawing tools, as a small state machine."""

from PySide6 import QtCore
from . import items as I
from ..engine_bridge import edit
from ...engine import model as M


def snap_member_t(t, snap_tol=0.05):
    """Snap fraction t (0..1) along a member to 0%, 25%, 50%, 75%, 100%."""
    targets = [(0.0, 0), (0.25, 25), (0.50, 50), (0.75, 75), (1.0, 100)]
    for target_t, pct in targets:
        if abs(t - target_t) <= snap_tol:
            return target_t, True, pct
    return t, False, int(round(t * 100))


class Tool:
    """Base tool. `name` is shown in the toolbar, `prompt` in the status line."""

    name = "Tool"
    prompt = ""
    snaps_to_grid = True
    wants_node = False  # highlight nodes while hovering
    wants_member = False  # highlight members while hovering

    def __init__(self, canvas):
        self.canvas = canvas

    def activate(self):
        self.canvas.set_prompt(self.prompt)

    def deactivate(self):
        self.cancel()

    def cancel(self):
        self.canvas.clear_preview()
        if hasattr(self.canvas, "set_custom_snap_point"):
            self.canvas.set_custom_snap_point(None)
        if hasattr(self.canvas, "set_preview_load"):
            self.canvas.set_preview_load(None, None)

    def click(self, scene_pos, model_pos) -> bool:
        del scene_pos, model_pos
        return False

    def move(self, scene_pos, model_pos) -> bool:
        del scene_pos, model_pos
        return False

    def key(self, key) -> bool:
        del key
        return False


class SelectTool(Tool):
    name = "Select"
    prompt = (
        "Click to select. Drag a joint, a member, or the edge of a "
        "structure's box to move the whole thing; its corner resizes it. "
        "A structure's shape always matches its sketch."
    )
    snaps_to_grid = False

    def click(self, scene_pos, model_pos) -> bool:
        del scene_pos, model_pos
        return False


class SupportTool(Tool):
    wants_node = True

    def __init__(self, canvas, kind):
        super().__init__(canvas)
        self.kind = kind
        self.name = M.SUPPORT_LABELS[kind]
        self.prompt = f"Click a joint to add a {M.SUPPORT_LABELS[kind].lower()} support."

    def click(self, scene_pos, model_pos) -> bool:
        del model_pos
        node_id = self.canvas.node_near(scene_pos)
        anchor_id = None if node_id is not None else self.canvas.anchor_near(scene_pos)
        if node_id is None and anchor_id is None:
            return True
        kind = self.kind

        def apply(m):
            kw = {"ky": 1000.0} if kind == M.SPRING else {}
            if anchor_id is not None:
                return m.add_support_on(anchor_id, kind, **kw)
            return m.add_support(node_id, kind, **kw)

        support = edit(self.canvas, f"Add {kind} support", apply)
        self.canvas.select_entity("support", support.id)
        return True


class AnchorTool(Tool):
    name = "Point"
    prompt = "Click a member to add a point on it, where loads can attach."
    wants_member = True
    snaps_to_grid = False

    def move(self, scene_pos, model_pos) -> bool:
        del model_pos
        member_id = self.canvas.member_near(scene_pos)
        if member_id is not None:
            member = self.canvas.model.members.get(member_id)
            if member:
                t_raw = self.canvas.member_point_at(scene_pos, member_id)
                t, snapped, pct = snap_member_t(t_raw)
                a = self.canvas.model.nodes.get(member.start)
                b = self.canvas.model.nodes.get(member.end)
                if a and b:
                    mx = a.x + t * (b.x - a.x)
                    my = a.y + t * (b.y - a.y)
                    snap_pt = I.to_scene(mx, my, self.canvas.global_scale)
                    
                    if pct in (0, 100):
                        tag = "Endpoint (joint exists)"
                    elif pct == 50:
                        tag = f"{member.label} at Midspan (50%)"
                    else:
                        tag = f"{member.label} at {pct}%"
                        
                    self.canvas.set_prompt(f"Click to add point on {tag}")
                    self.canvas.set_custom_snap_point(snap_pt)
                    return True

        self.canvas.set_custom_snap_point(None)
        return False

    def click(self, scene_pos, model_pos) -> bool:
        del model_pos
        member_id = self.canvas.member_near(scene_pos)
        if member_id is None:
            return True
        t_raw = self.canvas.member_point_at(scene_pos, member_id)
        t, snapped, pct = snap_member_t(t_raw)
        if pct in (0, 100):
            self.canvas.set_prompt("Joint already exists at member endpoint.")
            return True
        anchor = edit(self.canvas, "Add point", lambda m: m.add_anchor(member_id, t))
        self.canvas.select_entity("anchor", anchor.id)
        return True


class PivotTool(Tool):
    name = "Pivot"
    prompt = "Click a member to pivot it there. One click makes it a lever."
    wants_member = True
    snaps_to_grid = False

    def click(self, scene_pos, model_pos) -> bool:
        del model_pos
        member_id = self.canvas.member_near(scene_pos)
        if member_id is None:
            return True
        t = self.canvas.member_point_at(scene_pos, member_id)
        info = {}

        def build(m):
            info.update(m.add_pivot(member_id, t))
            return info

        edit(self.canvas, "Add pivot", build)
        if info:
            self.canvas.select_entity("support", info["support"])
            self.canvas.set_prompt(
                "Pivot placed. Drag it along the bar to change the "
                "mechanical advantage; the two sides stay in line because "
                "they are one bar."
            )
        return True


class PointLoadTool(Tool):
    name = "Force"
    prompt = "Click a joint, an existing point, or anywhere on a member for a downward force."
    wants_node = True
    wants_member = True

    def move(self, scene_pos, model_pos) -> bool:
        del model_pos
        node_id = self.canvas.node_near(scene_pos)
        if node_id is not None:
            node = self.canvas.model.nodes.get(node_id)
            if node:
                pt = I.to_scene(node.x, node.y, self.canvas.global_scale)
                self.canvas.set_prompt(f"Click to place force on joint {node.label}")
                self.canvas.set_custom_snap_point(pt)
                self.canvas.set_preview_load("point_load", pt)
                return True

        anchor_id = self.canvas.anchor_near(scene_pos)
        if anchor_id is not None:
            anchor = self.canvas.model.anchors.get(anchor_id)
            if anchor:
                xy = self.canvas.model.anchor_xy(anchor)
                if xy:
                    pt = I.to_scene(xy[0], xy[1], self.canvas.global_scale)
                    self.canvas.set_prompt(f"Click to place force on point {anchor.label}")
                    self.canvas.set_custom_snap_point(pt)
                    self.canvas.set_preview_load("point_load", pt)
                    return True

        member_id = self.canvas.member_near(scene_pos)
        if member_id is not None:
            member = self.canvas.model.members.get(member_id)
            if member:
                t_raw = self.canvas.member_point_at(scene_pos, member_id)
                t, snapped, pct = snap_member_t(t_raw)
                a = self.canvas.model.nodes.get(member.start)
                b = self.canvas.model.nodes.get(member.end)
                if a and b:
                    mx = a.x + t * (b.x - a.x)
                    my = a.y + t * (b.y - a.y)
                    snap_pt = I.to_scene(mx, my, self.canvas.global_scale)
                    
                    if pct == 0:
                        tag = f"joint {a.label}"
                    elif pct == 100:
                        tag = f"joint {b.label}"
                    elif pct == 50:
                        tag = f"{member.label} at Midspan (50%)"
                    else:
                        tag = f"{member.label} at {pct}%"
                        
                    self.canvas.set_prompt(f"Click to place force on {tag}")
                    self.canvas.set_custom_snap_point(snap_pt)
                    self.canvas.set_preview_load("point_load", snap_pt)
                    return True

        self.canvas.set_custom_snap_point(None)
        self.canvas.set_preview_load(None, None)
        return False

    def click(self, scene_pos, model_pos) -> bool:
        del model_pos
        node_id = self.canvas.node_near(scene_pos)
        anchor_id = None if node_id is not None else self.canvas.anchor_near(scene_pos)
        member_id = None if (node_id or anchor_id) else self.canvas.member_near(scene_pos)
        
        magnitude = self.canvas.default_force

        if node_id is not None:
            load = edit(
                self.canvas,
                "Add force",
                lambda m: m.add_point_load(node=node_id, fx=0.0, fy=-magnitude),
            )
            self.canvas.select_entity("point_load", load.id)
            self.cancel()
            return True

        if anchor_id is not None:
            load = edit(
                self.canvas,
                "Add force",
                lambda m: m.add_point_load(anchor=anchor_id, fx=0.0, fy=-magnitude),
            )
            self.canvas.select_entity("point_load", load.id)
            self.cancel()
            return True

        if member_id is not None:
            t_raw = self.canvas.member_point_at(scene_pos, member_id)
            t, snapped, pct = snap_member_t(t_raw)
            member = self.canvas.model.members.get(member_id)
            if not member:
                return True

            if pct == 0:
                load = edit(
                    self.canvas,
                    "Add force",
                    lambda m: m.add_point_load(node=member.start, fx=0.0, fy=-magnitude),
                )
                self.canvas.select_entity("point_load", load.id)
                self.cancel()
                return True

            if pct == 100:
                load = edit(
                    self.canvas,
                    "Add force",
                    lambda m: m.add_point_load(node=member.end, fx=0.0, fy=-magnitude),
                )
                self.canvas.select_entity("point_load", load.id)
                self.cancel()
                return True

            created_load_id = [None]

            def add_load_on_new_anchor(m):
                a = m.add_anchor(member_id, t)
                pl = m.add_point_load(anchor=a.id, fx=0.0, fy=-magnitude)
                created_load_id[0] = pl.id
                return pl

            edit(self.canvas, "Add force on member", add_load_on_new_anchor)
            if created_load_id[0]:
                self.canvas.select_entity("point_load", created_load_id[0])
            self.cancel()
            return True

        return True


class MomentTool(Tool):
    name = "Moment"
    prompt = "Click a joint, an existing point, or anywhere on a member to apply a couple."
    wants_node = True
    wants_member = True

    def move(self, scene_pos, model_pos) -> bool:
        del model_pos
        node_id = self.canvas.node_near(scene_pos)
        if node_id is not None:
            node = self.canvas.model.nodes.get(node_id)
            if node:
                pt = I.to_scene(node.x, node.y, self.canvas.global_scale)
                self.canvas.set_prompt(f"Click to place moment on joint {node.label}")
                self.canvas.set_custom_snap_point(pt)
                self.canvas.set_preview_load("moment_load", pt)
                return True

        anchor_id = self.canvas.anchor_near(scene_pos)
        if anchor_id is not None:
            anchor = self.canvas.model.anchors.get(anchor_id)
            if anchor:
                xy = self.canvas.model.anchor_xy(anchor)
                if xy:
                    pt = I.to_scene(xy[0], xy[1], self.canvas.global_scale)
                    self.canvas.set_prompt(f"Click to place moment on point {anchor.label}")
                    self.canvas.set_custom_snap_point(pt)
                    self.canvas.set_preview_load("moment_load", pt)
                    return True

        member_id = self.canvas.member_near(scene_pos)
        if member_id is not None:
            member = self.canvas.model.members.get(member_id)
            if member:
                t_raw = self.canvas.member_point_at(scene_pos, member_id)
                t, snapped, pct = snap_member_t(t_raw)
                a = self.canvas.model.nodes.get(member.start)
                b = self.canvas.model.nodes.get(member.end)
                if a and b:
                    mx = a.x + t * (b.x - a.x)
                    my = a.y + t * (b.y - a.y)
                    snap_pt = I.to_scene(mx, my, self.canvas.global_scale)
                    
                    if pct == 0:
                        tag = f"joint {a.label}"
                    elif pct == 100:
                        tag = f"joint {b.label}"
                    elif pct == 50:
                        tag = f"{member.label} at Midspan (50%)"
                    else:
                        tag = f"{member.label} at {pct}%"
                        
                    self.canvas.set_prompt(f"Click to place moment on {tag}")
                    self.canvas.set_custom_snap_point(snap_pt)
                    self.canvas.set_preview_load("moment_load", snap_pt)
                    return True

        self.canvas.set_custom_snap_point(None)
        self.canvas.set_preview_load(None, None)
        return False

    def click(self, scene_pos, model_pos) -> bool:
        del model_pos
        node_id = self.canvas.node_near(scene_pos)
        anchor_id = None if node_id is not None else self.canvas.anchor_near(scene_pos)
        member_id = None if (node_id or anchor_id) else self.canvas.member_near(scene_pos)
        
        value = self.canvas.default_moment

        if node_id is not None:
            load = edit(
                self.canvas,
                "Add moment",
                lambda m: m.add_moment_load(node=node_id, m=value),
            )
            self.canvas.select_entity("moment_load", load.id)
            self.cancel()
            return True

        if anchor_id is not None:
            load = edit(
                self.canvas,
                "Add moment",
                lambda m: m.add_moment_load(anchor=anchor_id, m=value),
            )
            self.canvas.select_entity("moment_load", load.id)
            self.cancel()
            return True

        if member_id is not None:
            t_raw = self.canvas.member_point_at(scene_pos, member_id)
            t, snapped, pct = snap_member_t(t_raw)
            member = self.canvas.model.members.get(member_id)
            if not member:
                return True

            if pct == 0:
                load = edit(
                    self.canvas,
                    "Add moment",
                    lambda m: m.add_moment_load(node=member.start, m=value),
                )
                self.canvas.select_entity("moment_load", load.id)
                self.cancel()
                return True

            if pct == 100:
                load = edit(
                    self.canvas,
                    "Add moment",
                    lambda m: m.add_moment_load(node=member.end, m=value),
                )
                self.canvas.select_entity("moment_load", load.id)
                self.cancel()
                return True

            created_load_id = [None]

            def add_moment_on_new_anchor(m):
                a = m.add_anchor(member_id, t)
                ml = m.add_moment_load(anchor=a.id, m=value)
                created_load_id[0] = ml.id
                return ml

            edit(self.canvas, "Add moment on member", add_moment_on_new_anchor)
            if created_load_id[0]:
                self.canvas.select_entity("moment_load", created_load_id[0])
            self.cancel()
            return True

        return True


class LineLoadTool(Tool):
    name = "Line load"
    prompt = "Click a member to apply a uniform load along it."
    wants_member = True

    def click(self, scene_pos, model_pos) -> bool:
        del model_pos
        member_id = self.canvas.member_near(scene_pos)
        if member_id is None:
            return True
        q = self.canvas.default_line_load
        load = edit(self.canvas, "Add line load", lambda m: m.add_line_load(member_id, -q, "y"))
        self.canvas.select_entity("line_load", load.id)
        return True


def build_tools(canvas):
    from ..motion import motion_tools

    return [
        SelectTool(canvas),
        AnchorTool(canvas),
        PivotTool(canvas),
        SupportTool(canvas, M.PIN),
        SupportTool(canvas, M.ROLLER_X),
        SupportTool(canvas, M.FIXED),
        SupportTool(canvas, M.SPRING),
        PointLoadTool(canvas),
        MomentTool(canvas),
        LineLoadTool(canvas),
    ] + motion_tools(canvas)
