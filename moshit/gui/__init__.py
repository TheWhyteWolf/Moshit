"""Qt (PySide6) GUI for Moshit.

Run with::

    python -m moshit.gui

Requires PySide6 (``pip install PySide6``) in addition to ffmpeg.
"""
from .app import MainWindow, launch

__all__ = ["MainWindow", "launch"]
