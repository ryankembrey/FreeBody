# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Ryan Kembrey <ryan.FreeCAD@gmail.com>
# SPDX-FileNotice: Part of the FBD addon.

"""Drawing tools, as a small state machine.

Each tool owns its own pending state and its own prompt, so adding a tool is a
new class rather than another branch in a growing conditional. The canvas simply
forwards events to the active tool.

Interaction rules, consistent across every tool:
    left click      act
    Escape          cancel the tool's pending state
    Enter           finish a multi-click tool
    hover           the canvas highlights what would be picked
"""

from PySide6 import QtCore

from ..engine_bridge import edit
from ...engine import model as M


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

    # Return True if the tool consumed the event.
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
        return False  # the view's own rubber-band selection handles it


class SupportTool(Tool):
    """One instance per support kind, so the toolbar reads as distinct buttons."""

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
                # A pin or a fixed support on a point pivots the bar itself.
                return m.add_support_on(anchor_id, kind, **kw)
            return m.add_support(node_id, kind, **kw)

        support = edit(self.canvas, f"Add {kind} support", apply)
        self.canvas.select_entity("support", support.id)
        return True


class AnchorTool(Tool):
    name = "Point"
    prompt = "Click a member to add a point on it, where loads can attach."
    wants_member = True
    snaps_to_grid = False  # its position is defined along the member instead

    def click(self, scene_pos, model_pos) -> bool:
        del model_pos
        member_id = self.canvas.member_near(scene_pos)
        if member_id is None:
            return True
        t = self.canvas.member_point_at(scene_pos, member_id)
        anchor = edit(self.canvas, "Add point", lambda m: m.add_anchor(member_id, t))
        self.canvas.select_entity("anchor", anchor.id)
        return True


class PivotTool(Tool):
    name = "Pivot"
    prompt = "Click a member to pivot it there. One click makes it a lever."
    wants_member = True
    snaps_to_grid = False  # its position is defined along the member instead

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
    prompt = "Click a joint or a point on a member for a downward force."
    wants_node = True

    def click(self, scene_pos, model_pos) -> bool:
        del model_pos
        node_id = self.canvas.node_near(scene_pos)
        anchor_id = None if node_id is not None else self.canvas.anchor_near(scene_pos)
        if node_id is None and anchor_id is None:
            return True
        magnitude = self.canvas.default_force
        load = edit(
            self.canvas,
            "Add force",
            lambda m: m.add_point_load(node=node_id, fx=0.0, fy=-magnitude, anchor=anchor_id),
        )
        self.canvas.select_entity("point_load", load.id)
        return True


class MomentTool(Tool):
    name = "Moment"
    prompt = "Click a joint or a point on a member to apply a couple."
    wants_node = True

    def click(self, scene_pos, model_pos) -> bool:
        del model_pos
        node_id = self.canvas.node_near(scene_pos)
        anchor_id = None if node_id is not None else self.canvas.anchor_near(scene_pos)
        if node_id is None and anchor_id is None:
            return True
        value = self.canvas.default_moment
        load = edit(
            self.canvas,
            "Add moment",
            lambda m: m.add_moment_load(node=node_id, m=value, anchor=anchor_id),
        )
        self.canvas.select_entity("moment_load", load.id)
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
