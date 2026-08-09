# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileCopyrightText: 2026 Ryan Kembrey <ryan.FreeCAD@gmail.com>
# SPDX-FileNotice: Part of the FBD addon.

"""Every model change goes through here.

Because the whole diagram is one serializable model, undo is a snapshot stack
rather than a command hierarchy. `edit()` takes a callable that mutates the
model, snapshots first, then tells the canvas to refresh and marks results
stale. Keeping this in one place means no tool can forget a step.
"""


def edit(canvas, description, mutate):
    """Snapshot, mutate, refresh. Returns whatever `mutate` returns."""
    canvas.push_undo(description)
    outcome = mutate(canvas.model)
    canvas.model_changed()
    return outcome
