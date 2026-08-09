# FBD workbench

Free body diagrams for FreeCAD. Draw a structure on a sheet, add supports and
loads, and solve reactions and internal forces. The point is clean diagrams that
an engineer would accept, plus numbers you can trust.

![status](https://img.shields.io/badge/status-alpha-orange)

## What it does today

- Paper-space drawing canvas: A3 by default, grid, snapping, pan and zoom
- Members drawn by clicking joint to joint, chaining as you go
- Supports: **pin, roller, fixed and spring** (translational and rotational)
- Loads: point forces, applied couples, uniform distributed loads
- Solves reactions, axial force, shear and bending moment
- Bending moment, shear and axial diagrams overlaid on the structure
- Determinacy diagnosis before solving, with a plain-English explanation when a
  model is a mechanism or is statically indeterminate
- Independent equilibrium check on every solve
- Undo and redo, and the diagram stores itself in the FreeCAD document

Dynamics and linkage motion are deliberately not included yet. The engine is
laid out so a motion module can sit beside statics behind the same façade.

## Install

```bash
git clone <this repo> ~/documents/git/fbd
cd ~/documents/git/fbd
./install.sh                 # symlinks into FreeCAD's Mod folder
pip install anastruct        # into FreeCAD's Python environment
```

Restart FreeCAD and pick **FBD** from the workbench selector.

## Using it

**New Diagram** creates a diagram and opens the canvas. Every tool is on the
FreeCAD toolbar, and the properties and results live in the Tasks panel, the
same arrangement Sketcher uses:

| Tool | Shortcut | What it does |
|---|---|---|
| Select | `S` | Click to select, drag joints, Delete to remove |
| Node | `N` | Place a joint |
| Member | `M` | Click two joints; keeps chaining until Escape |
| Pin / Roller / Fixed / Spring | `P` / `F` | Click a joint to support it |
| Force | `L` | Click a joint for a downward force |
| Moment | `T` | Apply a couple |
| Line load | `Q` | Uniform load along a member |
| Solve | | Solve and draw reactions |

Escape cancels a tool, Delete removes the selection, `Ctrl+0` fits the view.
Select anything to edit it in the Tasks panel; there are no modal dialogs while
drawing.

Symbols, arrows and text keep a constant screen size at any zoom, so the page
stays readable whether you are looking at the whole sheet or one joint. Results
are deliberately quiet: reaction arrows are always drawn, values can be switched
off, and a member's internal forces are annotated only while it is selected. The
full set of numbers is always in the results table.

## Conventions

Counter-clockwise moments are positive. Member axial force is positive in
tension. Applied loads draw red, reactions blue, internal forces green, supports
charcoal.

Units are consistent and FreeCAD-native:

| Quantity | Unit |
|---|---|
| length | mm |
| force | N |
| moment | N·mm |
| line load | N/mm |
| EA | N |
| EI | N·mm² |
| spring stiffness | N/mm, N·mm/rad |

Member EA and EI only change the answer for statically indeterminate structures.
For determinate ones the reactions and internal forces follow from statics alone,
so the defaults can be left as they are.

## Architecture

```
freecad/FBD/
    engine/            calculation core, no FreeCAD and no Qt
        model.py       dataclasses, id-keyed, serializable
        checks.py      determinacy and stability pre-flight
        statics.py     the only module that imports anaStruct
        results.py     result containers
    gui/
        style.py       palette, pens, drawing constants
        canvas/        scene, view, items, tools
        editor.py      the drawing surface and its state
        taskpanel.py   FreeCAD task dialog: properties, results, display
        document.py    stores the diagram as one blob
        editor_host.py hosts the editor as an MDI view
        commands.py    workbench commands
```

Two rules keep this maintainable. The engine never imports FreeCAD or Qt, so it
can be tested in milliseconds without launching anything, and a test enforces
that. And anaStruct never leaks past `engine/statics.py`, so the backend can be
replaced without touching the canvas.

The model is the single source of truth; graphics items are views onto it and
are rebuilt on change. Because the whole diagram serializes to a dict, undo is a
snapshot stack rather than a command hierarchy.

## Tests

```bash
./run_tests.sh
```

Three suites, none of which needs FreeCAD:

- **engine**: reactions, fixing moments and peak moments checked against
  closed-form results (`P·a·b/L`, `wL²/8`, `P·L`), spring deflection `P/k`,
  overhang uplift, portal frames, plus every diagnosis case
- **imports**: loads the addon exactly as FreeCAD does with everything stubbed,
  verifying commands register, icons exist, and the engine stays dependency-free
- **editor**: drives the real Qt widgets offscreen through drawing, undo,
  persistence, solving and deletion

`tests/render_sample.py` renders sample diagrams to PNG so drawing changes can
be eyeballed without opening FreeCAD.

## Known limits

- Members are rigid-jointed frame elements. Internal hinges are not implemented,
  so a pin-jointed truss is modelled as a frame; for triangulated trusses the
  axial forces are nearly identical, but member end moments will not be zero.
- Inclined roller supports accept an angle but have had less testing than the
  orthogonal cases.
- The solver is linear elastic and small displacement.
