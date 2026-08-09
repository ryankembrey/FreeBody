#!/usr/bin/env bash
# Run every test suite. None of them needs FreeCAD.
set -e
cd "$(dirname "${BASH_SOURCE[0]}")"
echo "== engine (statics vs textbook) =="   && python3 tests/test_engine.py
echo && echo "== imports (as FreeCAD loads it) ==" && python3 tests/test_imports.py
echo && echo "== editor (real Qt, offscreen) ==" && python3 tests/test_editor.py
echo && echo "All suites passed."
