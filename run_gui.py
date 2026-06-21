#!/usr/bin/env python3
"""Launch the Moshit GUI.

The simplest way to start the app:

    python run_gui.py

This works no matter what directory you run it from, as long as this file stays
next to the inner ``moshit`` package folder. (The alternative,
``python -m moshit.gui``, only works when run from this same folder.)
"""
import os
import sys

# Make the inner ``moshit`` package importable regardless of the current dir.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from moshit.gui.app import launch
except ModuleNotFoundError as exc:
    if "PySide6" in str(exc):
        sys.exit("The GUI needs PySide6. Install it with:\n\n    pip install PySide6\n")
    sys.exit(
        f"Could not import the moshit package ({exc}).\n"
        "Keep run_gui.py in the project root, next to the 'moshit' folder "
        "that contains __init__.py."
    )

if __name__ == "__main__":
    raise SystemExit(launch())
