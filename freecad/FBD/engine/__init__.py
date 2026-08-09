# SPDX-License-Identifier: LGPL-2.1-or-later
# SPDX-FileNotice: Part of the FBD addon.

"""Calculation core. No FreeCAD, no Qt: importable and testable on its own.

Two solvers sit behind one façade and share one model. statics answers what a
held structure carries; kinematics answers where a driven mechanism goes. A
diagram can be read either way without being redrawn, which is the whole point
of keeping them in the same model.
"""

from .model import (Model, Node, Member, Support, Anchor, PointLoad,          # noqa: F401
                    MomentLoad, LineLoad, Motor, Actuator, Motion, Analysis,
                    SketchLink, Sheet, SHEET_PRESETS,
                    PIN, ROLLER_X, ROLLER_Y, FIXED, SPRING,
                    SUPPORT_KINDS, SUPPORT_LABELS,
                    BOTH, TENSION_ONLY, COMPRESSION_ONLY,
                    BEHAVIOURS, BEHAVIOUR_LABELS,
                    CONTINUOUS, SWEEP, EXTEND, CYCLE, SINE,
                    DEFAULT_EA, DEFAULT_EI)
from .results import (StaticResult, Reaction, MemberForces,                   # noqa: F401
                      MotionResult, Frame)
from .checks import (check, Diagnosis, Issue, OK, WARNING, ERROR,             # noqa: F401
                     DETERMINATE, INDETERMINATE, MECHANISM, DRIVEN)
from .statics import solve, backend_available, backend_version                # noqa: F401
from .kinematics import (simulate, pose_at, check_mechanism,                  # noqa: F401
                         lever_report, MechanismSystem)
