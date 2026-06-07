"""Main window and application entry point for the Moshit GUI.

Layout: a media library, a preview with transport, and a schema-driven effect
inspector across the top; a two-track timeline across the bottom. The window
holds an :class:`AppController` and wires widget signals to it.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QActionGroup
from PySide6.QtWidgets import (
    QApplication, QFileDialog, QInputDialog, QMainWindow, QMessageBox,
    QSplitter, QWidget,
)

from ..engine import EngineConfig, _ext_for_profile
from .controller import AppController
from .widgets import InspectorPanel, MediaLibrary, PreviewWidget, TimelineWidget

_VIDEO_FILTER = "Video (*.mp4 *.mov *.mkv *.avi *.webm *.m4v *.gif);;All files (*)"

_STYLE = """
QMainWindow, QWidget { background:#14171c; color:#e6e9ef; }
QPushButton { background:#2a2f37; border:1px solid #3a414c; border-radius:4px;
              padding:5px 10px; }
QPushButton:hover { background:#333a44; }
QPushButton:disabled { color:#6a7280; border-color:#262b33; }
QListWidget, QComboBox, QSpinBox, QDoubleSpinBox, QLineEdit {
    background:#1c1f24; border:1px solid #3a414c; border-radius:4px; padding:3px; }
QListWidget::item:selected { background:#3b6ea5; }
QSlider::groove:horizontal { height:4px; background:#3a414c; border-radius:2px; }
QSlider::handle:horizontal { background:#9fb4d6; width:12px; margin:-5px 0;
                             border-radius:6px; }
QToolBar { background:#1c1f24; border:0; spacing:4px; padding:3px; }
QStatusBar { background:#1c1f24; color:#9fb4d6; }
"""


class MainWindow(QMainWindow):
    def __init__(self, config: Optional[EngineConfig] = None,
                 ffmpeg_bin: Optional[str] = None,
                 ffprobe_bin: Optional[str] = None):
        super().__init__()
        self.setWindowTitle("Moshit")
        self.resize(1180, 760)

        self.controller = AppController(config=config, ffmpeg_bin=ffmpeg_bin,
                                        ffprobe_bin=ffprobe_bin)
        self._selected_clip: Optional[str] = None

        self.library = MediaLibrary()
        self.preview = PreviewWidget()
        self.inspector = InspectorPanel()
        self.timeline = TimelineWidget()

        top = QSplitter(Qt.Orientation.Horizontal)
        top.addWidget(self.library)
        top.addWidget(self.preview)
        top.addWidget(self.inspector)
        top.setStretchFactor(0, 2)
        top.setStretchFactor(1, 5)
        top.setStretchFactor(2, 3)

        split = QSplitter(Qt.Orientation.Vertical)
        split.addWidget(top)
        split.addWidget(self.timeline)
        split.setStretchFactor(0, 5)
        split.setStretchFactor(1, 2)
        self.setCentralWidget(split)

        self._build_menu()
        self._build_toolbar()
        self.statusBar().showMessage("Ready. Import a base clip and a motion clip.")
        self._wire()
        self.timeline.set_project(self.controller.project)

    # -- toolbar ------------------------------------------------------------ #

    # -- menu / toolbar ----------------------------------------------------- #

    def _build_menu(self) -> None:
        m = self.menuBar().addMenu("&File")
        a_new = m.addAction("&New project")
        a_new.triggered.connect(self._new_project)
        a_open = m.addAction("&Open project…")
        a_open.setShortcut("Ctrl+O")
        a_open.triggered.connect(self._open_project)
        a_save = m.addAction("&Save project…")
        a_save.setShortcut("Ctrl+S")
        a_save.triggered.connect(self._save_project)
        m.addSeparator()
        a_exp = m.addAction("&Export…")
        a_exp.setShortcut("Ctrl+E")
        a_exp.triggered.connect(self._export)
        m.addSeparator()
        a_quit = m.addAction("&Quit")
        a_quit.setShortcut("Ctrl+Q")
        a_quit.triggered.connect(self.close)

    def _build_toolbar(self) -> None:
        tb = self.addToolBar("Main")
        tb.setMovable(False)

        self.act_base = QAction("Import base", self)
        self.act_base.triggered.connect(lambda: self._import("main"))
        self.act_motion = QAction("Import motion", self)
        self.act_motion.triggered.connect(lambda: self._import("motion"))
        self.act_preview = QAction("Refresh preview", self)
        self.act_preview.triggered.connect(self.controller.refresh_preview)
        self.act_export = QAction("Export…", self)
        self.act_export.triggered.connect(self._export)

        for act in (self.act_base, self.act_motion, self.act_preview, self.act_export):
            tb.addAction(act)

        tb.addSeparator()
        tools = QActionGroup(self)
        tools.setExclusive(True)
        self.act_pointer = QAction("Pointer", self)
        self.act_pointer.setCheckable(True)
        self.act_pointer.setChecked(True)
        self.act_pointer.setToolTip("Select, move and trim clips")
        self.act_pointer.triggered.connect(lambda: self.timeline.set_tool("pointer"))
        self.act_cut = QAction("Cut", self)
        self.act_cut.setCheckable(True)
        self.act_cut.setToolTip("Click a clip to split it at that frame")
        self.act_cut.triggered.connect(lambda: self.timeline.set_tool("cut"))
        tools.addAction(self.act_pointer)
        tools.addAction(self.act_cut)
        tb.addAction(self.act_pointer)
        tb.addAction(self.act_cut)

    # -- signal wiring ------------------------------------------------------ #

    def _wire(self) -> None:
        c = self.controller
        self.library.importRequested.connect(self._import)
        self.library.addRequested.connect(self._add_to_timeline)

        c.media_added.connect(self.library.add_media)
        c.media_added.connect(lambda _:
                              self.inspector.set_motion_labels(c.motion_labels()))
        c.project_changed.connect(self._on_project_changed)
        c.preview_ready.connect(self.preview.set_frames)
        c.busy.connect(self._on_busy)
        c.error.connect(self._on_error)
        c.status.connect(self.statusBar().showMessage)

        self.timeline.clipSelected.connect(self._on_clip_selected)
        self.timeline.reorderRequested.connect(self.controller.reorder_main_clip)
        self.timeline.trimRequested.connect(self._on_trim)
        self.timeline.removeRequested.connect(self._on_remove)
        self.timeline.seekRequested.connect(self._on_seek)
        self.timeline.splitRequested.connect(self.controller.split_clip)
        self.preview.frameChanged.connect(self._on_preview_frame)

        self.inspector.applyRequested.connect(self._on_apply)
        self.inspector.bakeRequested.connect(self._on_bake)
        self.inspector.revertRequested.connect(lambda: c.revert_last_bake())

    # -- handlers ----------------------------------------------------------- #

    def _import(self, role: str) -> None:
        what = "base" if role == "main" else "motion"
        path, _ = QFileDialog.getOpenFileName(self, f"Import {what} clip", "",
                                              _VIDEO_FILTER)
        if path:
            self.controller.import_media(path, role)

    def _add_to_timeline(self, media_id: str) -> None:
        if not media_id:
            self.statusBar().showMessage("Select a media item first.")
            return
        self.controller.add_clip_for_media(media_id)

    def _on_project_changed(self) -> None:
        self.timeline.set_project(self.controller.project)
        self.inspector.set_motion_labels(self.controller.motion_labels())
        # keep inspector in sync with the selected clip's current op
        if self._selected_clip:
            self._on_clip_selected(self._selected_clip)

    def _on_clip_selected(self, clip_id: str) -> None:
        self._selected_clip = clip_id
        try:
            clip = self.controller.project.clip(clip_id)
        except KeyError:
            self.inspector.set_enabled_for_clip(None, None)
            return
        if clip.track != "main":
            media = self.controller.project.media.get(clip.media_id)
            label = media.label if media else clip_id
            self.inspector.set_enabled_for_clip(
                None, label)
            self.statusBar().showMessage(
                f"{label} is a motion source — effects apply to main-track clips.")
            return
        media = self.controller.project.media.get(clip.media_id)
        label = media.label if media else clip_id
        op = self.controller.active_op_for_clip(clip_id)
        self.inspector.set_enabled_for_clip(
            clip_id, label,
            mode=op.mode if op else None,
            params=op.params if op else None)

    def _on_apply(self, mode: str, params: dict) -> None:
        if not self._selected_clip:
            return
        self.controller.set_mosh(self._selected_clip, mode, params)
        self.controller.refresh_preview()

    def _on_bake(self) -> None:
        if not self._selected_clip:
            return
        op = self.controller.active_op_for_clip(self._selected_clip)
        if not op:
            self.statusBar().showMessage("Apply an effect to this clip before baking.")
            return
        self.controller.bake(op.id)

    def _on_trim(self, clip_id: str, in_pt: int, out_pt: int) -> None:
        self.controller.trim_clip(
            clip_id,
            in_pt if in_pt >= 0 else None,
            out_pt if out_pt >= 0 else None)

    def _on_seek(self, frac: float) -> None:
        """Timeline scrubber → preview position."""
        n = self.preview.frame_count()
        if n > 0:
            self.preview.seek_to(round(frac * (n - 1)))

    def _on_preview_frame(self, idx: int) -> None:
        """Preview position → timeline playhead (proportional)."""
        n = self.preview.frame_count()
        self.timeline.set_play_fraction(idx / (n - 1) if n > 1 else 0.0)

    def _on_remove(self, clip_id: str) -> None:
        if self._selected_clip == clip_id:
            self._selected_clip = None
            self.inspector.set_enabled_for_clip(None, None)
        self.controller.remove_clip(clip_id)

    def _reload_library(self) -> None:
        self.library.list.clear()
        for media in self.controller.project.media.values():
            self.library.add_media(media)
        self.inspector.set_motion_labels(self.controller.motion_labels())
        self.timeline.set_project(self.controller.project)

    def _new_project(self) -> None:
        self.controller.new_project()
        self._selected_clip = None
        self.library.list.clear()
        self.inspector.set_enabled_for_clip(None, None)
        self.preview.set_frames([], 30.0)

    def _open_project(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Open project", "",
                                              "Project (*.json)")
        if not path:
            return
        try:
            self.controller.open_project(path)
            self._selected_clip = None
            self._reload_library()
            self.inspector.set_enabled_for_clip(None, None)
        except Exception as exc:                  # surface load errors
            self._on_error(str(exc))

    def _save_project(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Save project", "project.json",
                                              "Project (*.json)")
        if not path:
            return
        try:
            self.controller.save_project(path)
        except Exception as exc:
            self._on_error(str(exc))

    def _export(self) -> None:
        profiles = self.controller.export_profiles()
        if not profiles:
            QMessageBox.warning(self, "Export",
                                "No export encoders available in this ffmpeg build.")
            return
        profile, ok = QInputDialog.getItem(self, "Export", "Format:", profiles, 0, False)
        if not ok:
            return
        suffix = _ext_for_profile(profile)
        path, _ = QFileDialog.getSaveFileName(self, "Export to", f"moshit{suffix}",
                                              f"*{suffix}")
        if path:
            self.controller.export(profile, path)

    def _on_busy(self, busy: bool, message: str) -> None:
        for act in (self.act_base, self.act_motion, self.act_preview, self.act_export):
            act.setEnabled(not busy)
        self.inspector.setEnabled(not busy)
        if busy and message:
            self.statusBar().showMessage(message)
        self.setCursor(Qt.CursorShape.BusyCursor if busy else Qt.CursorShape.ArrowCursor)

    def _on_error(self, message: str) -> None:
        self.statusBar().showMessage(message)
        QMessageBox.warning(self, "Moshit", message)

    # -- lifecycle ---------------------------------------------------------- #

    def closeEvent(self, event) -> None:
        try:
            self.controller.cleanup()
        finally:
            super().closeEvent(event)


def launch(argv=None) -> int:
    app = QApplication.instance() or QApplication(sys.argv if argv is None else argv)
    app.setStyleSheet(_STYLE)
    win = MainWindow()
    win.show()
    return app.exec()
