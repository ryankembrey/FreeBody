# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Ryan Kembrey <ryan.FreeCAD@gmail.com>
# SPDX-FileNotice: Part of the FBD addon.

"""Document storage."""

import FreeCAD as App  # type: ignore
from ..engine.model import Model, SHEET_PRESETS

TYPE_TAG = "FBD::Diagram"


def _default_model() -> Model:
    """A fresh Model seeded with the user's own preferred defaults from
    Edit > Preferences > FBD, so a newly created diagram starts out the
    way the user actually wants rather than with this addon's own
    factory settings.
    """
    from .preferences import prefs

    model = Model()
    preset = prefs.sheet_preset()
    if preset in SHEET_PRESETS:
        model.sheet.width, model.sheet.height = SHEET_PRESETS[preset]
        model.sheet.name = preset
    model.sheet.grid = prefs.sheet_grid()
    model.sheet.title = prefs.sheet_title()
    model.motion.duration = prefs.motion_duration()
    model.motion.fps = prefs.motion_fps()
    model.motion.trace = prefs.motion_trace()
    model.motion.ghosts = prefs.motion_ghosts()
    model.motion.repeat = prefs.motion_repeat()
    model.analysis.geometric_nonlinear = prefs.geometric_nonlinear()
    model.analysis.max_iter = prefs.max_iterations()
    model.analysis.discretisation = prefs.discretisation()
    return model


class Diagram:
    """Proxy holding one free body diagram."""

    def __init__(self, obj):
        obj.Proxy = self
        obj.addProperty(
            "App::PropertyString", "FBDType", "FBD", "Identifies this object to the FBD workbench"
        )
        obj.FBDType = "Diagram"
        obj.setEditorMode("FBDType", 2)
        obj.addProperty("App::PropertyPythonObject", "Data", "FBD", "The serialized diagram")
        obj.addProperty("App::PropertyLink", "Sketch", "FBD", "Sketch this diagram follows")
        obj.addProperty(
            "App::PropertyBool", "AutoSync", "FBD", "Re-read the sketch whenever it changes"
        )
        obj.AutoSync = True
        obj.Data = _default_model().to_dict()

    def execute(self, obj):
        if _deleting["busy"]:
            # A deletion in progress is driving this recompute purely so
            # other things in the document catch up -- it must never be
            # read as "the sketch changed", or auto-sync would immediately
            # recreate whatever was just deliberately deleted.
            return
        sketch = getattr(obj, "Sketch", None)
        if sketch is None or not getattr(obj, "AutoSync", True):
            return
        from . import sketch_import
        from ..engine.model import Model

        model = Model.from_dict(obj.Data or {})
        if not model.sketch_link:
            return

        # A recompute can be triggered for reasons that have nothing to
        # do with the sketch: an ordinary editor save touches this
        # object's own Data property on every edit -- deleting a load,
        # moving a structure -- and FreeCAD recomputes it regardless of
        # what actually changed. Resyncing every single time would mean
        # any edit at all risks silently re-fitting the whole diagram
        # against the sketch again. Only resync when the sketch's own
        # geometry has actually changed since the last time this ran.
        fingerprint = sketch_import.sketch_fingerprint(sketch)
        if fingerprint is not None and fingerprint == model.sketch_link.last_synced_fingerprint:
            return

        report = sketch_import.resync(model, sketch)
        if not report.ok:
            App.Console.PrintWarning("FBD: " + report.message + "\n")
            return
        if fingerprint is not None:
            model.sketch_link.last_synced_fingerprint = fingerprint
        obj.Data = model.to_dict()
        App.Console.PrintMessage("FBD: " + report.summary() + "\n")
        for orphan in report.kept_orphans:
            App.Console.PrintWarning(
                f"FBD: retained {orphan}; it carries a support or a load.\n"
            )
        from .editor_host import refresh_editor

        refresh_editor(obj)

    def onChanged(self, obj, prop):
        """Catch up an open editor when Data changes from outside its own
        save path.

        FreeCAD's native undo and redo restore this property directly,
        with nothing else in the ordinary edit flow to notice: without
        this, an already-open editor kept showing its stale in-memory
        model after an undo, even though the tree and the Data property
        itself had already gone back to the old state.

        Deferred, the same reason onDelete elsewhere in this file defers
        its own work rather than acting immediately: FreeCAD can call
        this from inside its own undo/redo transaction, mid-restore, and
        doing real work synchronously here -- rebuilding the whole
        graphics scene, which is what refresh_editor ultimately does --
        risks the same kind of corruption that removing objects
        synchronously from onDelete already caused before that was fixed
        the identical way.
        """
        if prop != "Data" or _deleting["busy"]:
            return

        def refresh():
            try:
                from .editor_host import refresh_editor
                refresh_editor(obj)
            except Exception:
                pass

        from PySide6 import QtCore
        QtCore.QTimer.singleShot(0, refresh)

    def __getstate__(self):
        return None

    def __setstate__(self, state):
        return None

    def onDocumentRestored(self, obj):
        """Rebuild the transient Tree View hierarchy when the document is opened."""
        try:
            from freecad.FBD.gui.document import _sync_tree_view, load_model
            _sync_tree_view(obj, load_model(obj))
        except Exception as e:
            import FreeCAD as App
            App.Console.PrintError(f"FBD: Failed to restore tree view: {e}\n")


class ViewProviderDiagram:
    def __init__(self, vobj=None):
        if vobj is not None:
            vobj.Proxy = self

    def attach(self, vobj):
        self.Object = vobj.Object
        # Nothing here ever renders in the 3D view -- everything is drawn
        # on our own 2D canvas -- but FreeCAD's own tree needs a real scene
        # node behind a claimed display mode, or its eye-icon styling has
        # nothing concrete to track regardless of what Visibility says.
        from pivy import coin
        self._node = coin.SoSeparator()
        vobj.addDisplayMode(self._node, "Diagram")

    def getDisplayModes(self, vobj):
        return ["Diagram"]

    def getDefaultDisplayMode(self):
        return "Diagram"

    def onChanged(self, vobj, prop):
        """Toggling the whole diagram cascades to every structure in it,
        which -- through its own onChanged -- cascades to every entity in
        turn. Only ever sets Structure-level Visibility here; the entity
        level is Structure's job, not duplicated at this level too.
        """
        if prop != "Visibility":
            return
        obj = getattr(vobj, "Object", None)
        if obj is None:
            return
        visible = bool(vobj.Visibility)
        for structure in list(getattr(obj, "Group", [])):
            if hasattr(structure, "ViewObject") and structure.ViewObject is not None:
                structure.ViewObject.Visibility = visible
        try:
            from .editor_host import set_editor_visible
            set_editor_visible(obj.Name, visible)
        except Exception:
            pass

    def getIcon(self):
        from .commands import icon_path

        return icon_path("fbd_diagram.svg")

    def doubleClicked(self, vobj):
        from .editor_host import open_editor

        open_editor(vobj.Object)
        return True

    def setEdit(self, vobj, mode=0):
        from .editor_host import open_editor

        open_editor(vobj.Object)
        return True

    def unsetEdit(self, vobj, mode=0):
        return False

    def onDelete(self, vobj, subelements):
        """Let the deletion finish before anything else touches the tree.

        Removing objects from inside onDelete corrupts the command
        FreeCAD is recording for undo, because it is mid-transaction and
        writing the deletion out as a Python string. Everything this
        needs -- which structures and children to remove, and which
        editor page to close -- is captured now, while the object is
        still valid; only the actual cleanup waits for the transaction
        to close.
        """
        obj = getattr(vobj, "Object", None)
        if obj is None or obj.Document is None:
            return True
        doc = obj.Document
        name = obj.Name
        captured = [
            (s.Name, [c.Name for c in list(getattr(s, "Group", []))])
            for s in list(getattr(obj, "Group", []))
        ]

        def cleanup():
            for sname, cnames in captured:
                for cname in cnames:
                    try:
                        doc.removeObject(cname)
                    except Exception:
                        pass
                try:
                    doc.removeObject(sname)
                except Exception:
                    pass
            try:
                from .editor_host import close_editor
                close_editor(name)
            except Exception:
                pass

        from PySide6 import QtCore
        QtCore.QTimer.singleShot(0, cleanup)
        return True

    def claimChildren(self):
        obj = getattr(self, "Object", None)
        return getattr(obj, "Group", []) if obj and hasattr(obj, "Group") else []

    def __getstate__(self):
        return None

    def __setstate__(self, state):
        return None


class ViewProviderStructure:
    def __init__(self, vobj=None):
        if vobj is not None:
            vobj.Proxy = self

    def attach(self, vobj):
        self.Object = vobj.Object
        from pivy import coin
        self._node = coin.SoSeparator()
        vobj.addDisplayMode(self._node, "Structure")

    def getDisplayModes(self, vobj):
        return ["Structure"]

    def getDefaultDisplayMode(self):
        return "Structure"

    def onChanged(self, vobj, prop):
        """Toggling a whole structure hides or shows every entity in it on
        the canvas, and keeps each entity's own Visibility (and so its own
        eye icon) in step with the structure's, the same way toggling one
        entity on its own already works.

        Deferred, the same reason Diagram.onChanged defers its own work:
        this sets Visibility on other objects, which itself re-triggers
        more onChanged calls recursively, all synchronously -- exactly
        the kind of document mutation that must not happen from inside a
        callback FreeCAD can invoke mid-transaction, during its own
        undo/redo restore.
        """
        if prop != "Visibility":
            return
        obj = getattr(vobj, "Object", None)
        if obj is None:
            return
        diagram = _owning_diagram(obj)
        if diagram is None:
            return
        visible = bool(vobj.Visibility)
        children = list(getattr(obj, "Group", []))
        diagram_name = diagram.Name

        def cascade():
            try:
                from .editor_host import _OPEN
                entry = _OPEN.get(diagram_name)
                editor = entry[1] if entry else None
            except Exception:
                editor = None
            for child in children:
                kind, ident = _parse_entity(getattr(child, "Name", ""))
                if kind and editor is not None:
                    item = editor._items.get((kind, ident))
                    if item is not None:
                        item.setVisible(visible)
                if hasattr(child, "ViewObject") and child.ViewObject is not None:
                    child.ViewObject.Visibility = visible

        from PySide6 import QtCore
        QtCore.QTimer.singleShot(0, cascade)

    def onDelete(self, vobj, subelements):
        """Delete the whole structure from the diagram, after the fact.

        Only needs one surviving joint from this structure's own Group
        listing -- the rest of what to delete comes from asking the model
        which whole connected structure that joint belongs to, computed
        fresh rather than trusted from whatever the tree happened to be
        showing. That's what makes this atomic even if the Group listing
        itself were ever incomplete: it's only ever used to find a way in,
        not trusted for the full membership.
        """
        obj = getattr(vobj, "Object", None)
        if obj is None:
            return True
        anchor = None
        for child in list(getattr(obj, "Group", [])):
            kind, ident = _parse_entity(getattr(child, "Name", ""))
            if kind == "node":
                anchor = ident
                break
        targets = [("component", anchor)] if anchor is not None else []
        _delete_later(_owning_diagram(obj), targets)
        return True

    def getIcon(self):
        from .commands import icon_path

        return icon_path("tool_member.svg")

    def doubleClicked(self, vobj):
        from .editor_host import open_editor

        obj = getattr(vobj, "Object", None)
        if obj and obj.Document:
            for parent in obj.Document.Objects:
                if is_diagram(parent) and obj in getattr(parent, "Group", []):
                    open_editor(parent)
                    return True
        return True

    def claimChildren(self):
        obj = getattr(self, "Object", None)
        return getattr(obj, "Group", []) if obj and hasattr(obj, "Group") else []

    def __getstate__(self):
        return None

    def __setstate__(self, state):
        return None


class ViewProviderEntity:
    def __init__(self, vobj=None, icon_name="tool_node.svg"):
        if vobj is not None:
            vobj.Proxy = self
        self.icon_name = icon_name

    def attach(self, vobj):
        self.Object = vobj.Object
        from pivy import coin
        self._node = coin.SoSeparator()
        vobj.addDisplayMode(self._node, "Entity")

    def getDisplayModes(self, vobj):
        return ["Entity"]

    def getDefaultDisplayMode(self):
        return "Entity"

    def onChanged(self, vobj, prop):
        """Mirror the tree's own Visibility toggle onto the canvas item.

        These objects carry no Shape of their own for FreeCAD's 3D view
        to hide -- everything is drawn on our own 2D page instead, so
        Visibility has to be wired there by hand or the toggle does
        nothing at all, which is exactly what was happening.

        Deferred, the same reason every other onChanged in this file
        defers its own work: FreeCAD can call this mid-transaction,
        during its own undo/redo restore, and touching the canvas
        synchronously from inside that callback is not safe.
        """
        if prop != "Visibility":
            return
        obj = getattr(vobj, "Object", None)
        if obj is None:
            return
        kind, ident = _parse_entity(getattr(obj, "Name", ""))
        if not kind:
            return
        diagram = _owning_diagram(obj)
        if diagram is None:
            return
        diagram_name = diagram.Name
        visible = bool(vobj.Visibility)

        def apply_visibility():
            try:
                from .editor_host import _OPEN
                entry = _OPEN.get(diagram_name)
                if not entry:
                    return
                _sub, editor = entry
                item = editor._items.get((kind, ident))
                if item is not None:
                    item.setVisible(visible)
            except Exception:
                pass

        from PySide6 import QtCore
        QtCore.QTimer.singleShot(0, apply_visibility)

    def onDelete(self, vobj, subelements):
        """Delete the entity from the diagram, after the fact.

        The tree is rebuilt from the model on every save, so removing the
        object alone would put it straight back on the next change. The model
        is the thing that has to change, and model.delete cascades, so a joint
        takes its members, supports and loads with it.
        """
        obj = getattr(vobj, "Object", None)
        if obj is not None:
            kind, ident = _parse_entity(getattr(obj, "Name", ""))
            if kind:
                _delete_later(_owning_diagram(obj), [(kind, ident)])
        return True

    def claimChildren(self):
        return []

    def getIcon(self):
        from .commands import icon_path

        icon = getattr(self, "icon_name", "tool_node.svg")
        return icon_path(icon)

    def __getstate__(self):
        return getattr(self, "icon_name", "tool_node.svg")

    def __setstate__(self, state):
        if state:
            self.icon_name = state
        else:
            self.icon_name = "tool_node.svg"


def create(doc=None, label="Diagram"):
    doc = doc or App.ActiveDocument
    if doc is None:
        doc = App.newDocument("FBD")
    obj = doc.addObject("App::FeaturePython", "Diagram")
    Diagram(obj)
    obj.Label = label
    if App.GuiUp and obj.ViewObject is not None:
        ViewProviderDiagram(obj.ViewObject)
    doc.recompute()
    return obj


def is_diagram(obj):
    return getattr(obj, "FBDType", "") == "Diagram"


def find_diagrams(doc=None):
    doc = doc or App.ActiveDocument
    if doc is None:
        return []
    return [o for o in doc.Objects if is_diagram(o)]


def load_model(obj) -> Model:
    try:
        return Model.from_dict(obj.Data or {})
    except Exception:
        return Model()


def store_model(obj, model: Model) -> None:
    obj.Data = model.to_dict()
    obj.touch()
    if App.GuiUp and obj.Document is not None:
        try:
            _sync_tree_view(obj, model)
        except Exception:
            pass


def _sync_tree_view(diagram_obj, model: Model):
    doc = diagram_obj.Document
    if doc is None:
        return

    from ..engine.checks import _components

    all_doc_structures = [o for o in doc.Objects if getattr(o, "FBDType", "") == "Structure"]

    components = _components(model)

    valid_components = []
    for comp in components:
        has_members = any(m.start in comp or m.end in comp for m in model.members.values())
        has_supports = any(s.node in comp for s in model.supports.values())
        has_loads = any(p.node in comp for p in model.point_loads.values())
        if has_members or has_supports or has_loads:
            valid_components.append(comp)

    updated_structures = []
    for idx, comp_nodes in enumerate(valid_components, 1):
        struct_label = f"Structure {idx}" if len(valid_components) > 1 else "Structure"

        if idx - 1 < len(all_doc_structures):
            struct_obj = all_doc_structures[idx - 1]
        else:
            struct_obj = doc.addObject("App::FeaturePython", f"Structure_{idx}")
            struct_obj.addProperty("App::PropertyString", "FBDType", "FBD", "Type")
            struct_obj.FBDType = "Structure"
            struct_obj.addProperty(
                "App::PropertyLinkList", "Group", "FBD", "Entities", 2
            )  # Transient -- see the note on Diagram.Group above
            if hasattr(struct_obj, "ViewObject") and struct_obj.ViewObject is not None:
                ViewProviderStructure(struct_obj.ViewObject)

        struct_obj.Label = struct_label
        updated_structures.append(struct_obj)

        _sync_structure_children(struct_obj, model, comp_nodes)

    unused_structures = set(all_doc_structures) - set(updated_structures)
    for orphan in unused_structures:
        for child in getattr(orphan, "Group", []):
            try:
                doc.removeObject(child.Name)
            except Exception:
                pass
        try:
            doc.removeObject(orphan.Name)
        except Exception:
            pass

    if not hasattr(diagram_obj, "Group"):
        # Transient: excluded from file save, and -- the actual point
        # here -- from participating in FreeCAD's native undo/redo
        # transactions. This list is entirely regenerated from the model
        # on every sync; it never needed to be restorable by FreeCAD's
        # own undo machinery, and letting it be is what let a stale
        # Group value reference a permanently-deleted object during an
        # undo restore.
        diagram_obj.addProperty(
            "App::PropertyLinkList", "Group", "FBD", "Child structures", 2
        )
    diagram_obj.Group = updated_structures


def _sync_structure_children(struct_obj, model: Model, comp_nodes):
    doc = struct_obj.Document
    if doc is None:
        return

    existing_children = {o.Name: o for o in getattr(struct_obj, "Group", [])}
    active_children = []

    def get_child(name, icon):
        child = existing_children.get(name) or doc.getObject(name)
        if child is None:
            child = doc.addObject("App::FeaturePython", name)
            if hasattr(child, "ViewObject") and child.ViewObject is not None:
                ViewProviderEntity(child.ViewObject, icon)
        return child

    for nid in sorted(comp_nodes):
        n = model.nodes.get(nid)
        if not n:
            continue
        obj_name = f"Node_{n.id}"
        child = get_child(obj_name, "tool_node.svg")
        child.Label = f"Joint {n.label}"
        active_children.append(child)

    for m in sorted(model.members.values(), key=lambda x: x.id):
        if m.start in comp_nodes or m.end in comp_nodes:
            obj_name = f"Member_{m.id}"
            child = get_child(obj_name, "tool_member.svg")
            a = model.nodes.get(m.start)
            b = model.nodes.get(m.end)
            a_lbl = a.label if a else str(m.start)
            b_lbl = b.label if b else str(m.end)
            child.Label = f"Member {m.label} ({a_lbl}-{b_lbl})"
            active_children.append(child)

    for s in sorted(model.supports.values(), key=lambda x: x.id):
        if model.support_home_node(s) in comp_nodes:
            obj_name = f"Support_{s.id}"
            icon = f"tool_{s.kind if s.kind in ('pin', 'roller', 'fixed', 'spring') else 'pin'}.svg"
            child = get_child(obj_name, icon)
            child.Label = f"Support {s.kind.title()} at {model.entity_label(s.holds)}"
            active_children.append(child)

    # Shared by every kind below that attaches to a member rather than
    # directly to a joint: a member belongs to this structure if either
    # of its own ends does, matching every other loop in this function.
    member_ids_in_comp = {
        m.id for m in model.members.values()
        if m.start in comp_nodes or m.end in comp_nodes
    }

    def anchor_in_comp(anchor_id):
        a = model.anchors.get(anchor_id) if anchor_id else None
        return bool(a and a.member in member_ids_in_comp)

    for p in sorted(model.point_loads.values(), key=lambda x: x.id):
        if p.node in comp_nodes or anchor_in_comp(p.anchor):
            obj_name = f"Force_{p.id}"
            child = get_child(obj_name, "tool_force.svg")
            child.Label = f"Force {p.magnitude():,.0f} N"
            active_children.append(child)

    for a in sorted(model.anchors.values(), key=lambda x: x.id):
        if a.member in member_ids_in_comp:
            obj_name = f"Anchor_{a.id}"
            child = get_child(obj_name, "tool_anchor.svg")
            child.Label = f"Point {a.label}"
            active_children.append(child)

    for mo in sorted(model.motors.values(), key=lambda x: x.id):
        if mo.node in comp_nodes:
            obj_name = f"Motor_{mo.id}"
            child = get_child(obj_name, "tool_motor.svg")
            child.Label = f"Motor {mo.label}"
            active_children.append(child)

    for ac in sorted(model.actuators.values(), key=lambda x: x.id):
        if ac.member in member_ids_in_comp:
            obj_name = f"Actuator_{ac.id}"
            child = get_child(obj_name, "tool_actuator.svg")
            child.Label = f"Actuator {ac.label}"
            active_children.append(child)

    for ml in sorted(model.moment_loads.values(), key=lambda x: x.id):
        if ml.node in comp_nodes or anchor_in_comp(ml.anchor):
            obj_name = f"Moment_{ml.id}"
            child = get_child(obj_name, "tool_moment.svg")
            child.Label = f"Moment {ml.m:,.0f} N.mm"
            active_children.append(child)

    orphaned_children = set(existing_children.values()) - set(active_children)
    for orphan in orphaned_children:
        try:
            doc.removeObject(orphan.Name)
        except Exception:
            pass

    struct_obj.Group = active_children


def _owning_diagram(obj):
    """The Diagram an entity or a Structure belongs to."""
    doc = getattr(obj, "Document", None)
    if doc is None:
        return None
    for parent in doc.Objects:
        if is_diagram(parent) and obj in getattr(parent, "Group", []):
            return parent
        if getattr(parent, "FBDType", "") == "Structure" \
                and obj in getattr(parent, "Group", []):
            return _owning_diagram(parent)
    return None


def _parse_entity(name):
    """A tree object's name back to the model entity it stands for."""
    prefixes = {"Node": "node", "Member": "member", "Support": "support",
                "Force": "point_load", "Moment": "moment_load",
                "Anchor": "anchor", "Motor": "motor", "Actuator": "actuator"}
    head, _, tail = name.partition("_")
    if head in prefixes and tail.isdigit():
        return prefixes[head], int(tail)
    return None, None


_deleting = {"busy": False}


def _delete_later(diagram_obj, targets):
    """Do it on the next turn of the event loop, not now.

    FreeCAD is part way through its own deletion when onDelete runs, with a
    transaction open and the undo command being written out as text. Removing
    or adding objects underneath that produces a half-written command and a
    syntax error from the recorder, so the work waits until it has finished.
    """
    if diagram_obj is None or not targets:
        return
    from PySide6 import QtCore
    QtCore.QTimer.singleShot(0, lambda: _delete_from_model(diagram_obj, targets))


def _delete_from_model(diagram_obj, targets):
    """Delete from the diagram itself, then let the tree catch up.

    Nothing is removed from the tree by hand: storing the model runs the tree
    sync, which prunes whatever no longer has an entity behind it.
    """
    if diagram_obj is None or not targets or _deleting["busy"]:
        return
    _deleting["busy"] = True
    try:
        model = load_model(diagram_obj)
        for kind, ident in targets:
            model.delete(kind, ident)
        store_model(diagram_obj, model)
        try:
            from .editor_host import refresh_editor
            refresh_editor(diagram_obj)
        except Exception:
            pass
        doc = getattr(diagram_obj, "Document", None)
        if doc is not None:
            doc.recompute()
    except Exception as exc:
        App.Console.PrintWarning(f"FBD: could not apply the deletion. {exc}\n")
    finally:
        _deleting["busy"] = False
