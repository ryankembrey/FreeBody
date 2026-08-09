# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Ryan Kembrey <ryan.FreeCAD@gmail.com>
# SPDX-FileNotice: Part of the FBD addon.

"""Document storage."""

import FreeCAD as App  # type: ignore
from ..engine.model import Model

TYPE_TAG = "FBD::Diagram"


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
        obj.Data = Model().to_dict()

    def execute(self, obj):
        sketch = getattr(obj, "Sketch", None)
        if sketch is None or not getattr(obj, "AutoSync", True):
            return
        from . import sketch_import
        from ..engine.model import Model

        model = Model.from_dict(obj.Data or {})
        if not model.sketch_link:
            return
        report = sketch_import.resync(model, sketch)
        if not report.ok:
            App.Console.PrintWarning("FBD: " + report.message + "\n")
            return
        obj.Data = model.to_dict()
        App.Console.PrintMessage("FBD: " + report.summary() + "\n")
        for orphan in report.kept_orphans:
            App.Console.PrintWarning(f"FBD: kept {orphan}, it carries a support or a load.\n")
        from .editor_host import refresh_editor

        refresh_editor(obj)

    def __getstate__(self):
        return None

    def __setstate__(self, state):
        return None


class ViewProviderDiagram:
    def __init__(self, vobj=None):
        if vobj is not None:
            vobj.Proxy = self

    def attach(self, vobj):
        self.Object = vobj.Object

    def getDisplayModes(self, vobj):
        return ["Diagram"]

    def getDefaultDisplayMode(self):
        return "Diagram"

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

        Removing objects from inside onDelete corrupts the command FreeCAD is
        recording for undo, because it is mid-transaction and writing the
        deletion out as a Python string. So nothing happens here.
        """
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

    def onDelete(self, vobj, subelements):
        """Delete the whole structure from the diagram, after the fact."""
        obj = getattr(vobj, "Object", None)
        if obj is None:
            return True
        targets = []
        for child in list(getattr(obj, "Group", [])):
            kind, ident = _parse_entity(getattr(child, "Name", ""))
            if kind:
                targets.append((kind, ident))
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
            struct_obj.addProperty("App::PropertyLinkList", "Group", "FBD", "Entities")
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
        diagram_obj.addProperty("App::PropertyLinkList", "Group", "FBD", "Child structures")
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
        child.Label = f"Joint {n.label} ({n.x:,.0f}, {n.y:,.0f})"
        active_children.append(child)

    for m in sorted(model.members.values(), key=lambda x: x.id):
        if m.start in comp_nodes or m.end in comp_nodes:
            obj_name = f"Member_{m.id}"
            child = get_child(obj_name, "tool_member.svg")
            a = model.nodes.get(m.start)
            b = model.nodes.get(m.end)
            a_lbl = a.label if a else str(m.start)
            b_lbl = b.label if b else str(m.end)
            length = model.member_length(m)
            child.Label = f"Member {m.label} ({a_lbl}-{b_lbl}, {length:,.0f}mm)"
            active_children.append(child)

    for s in sorted(model.supports.values(), key=lambda x: x.id):
        if model.support_home_node(s) in comp_nodes:
            obj_name = f"Support_{s.id}"
            icon = f"tool_{s.kind if s.kind in ('pin', 'roller', 'fixed', 'spring') else 'pin'}.svg"
            child = get_child(obj_name, icon)
            child.Label = f"Support {s.kind.title()} at {model.entity_label(s.holds)}"
            active_children.append(child)

    for p in sorted(model.point_loads.values(), key=lambda x: x.id):
        if p.node in comp_nodes or (
            p.anchor
            and model.anchors.get(p.anchor)
            and model.anchors[p.anchor].member
            in [m.id for m in model.members.values() if m.start in comp_nodes]
        ):
            obj_name = f"Force_{p.id}"
            child = get_child(obj_name, "tool_force.svg")
            child.Label = f"Force {p.magnitude():,.0f} N"
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
