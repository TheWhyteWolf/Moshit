"""Main window and application entry point for the Moshit GUI.

Layout: a media library, a preview with transport, and a schema-driven effect
inspector across the top; a two-track timeline across the bottom. The window
holds an :class:`AppController` and wires widget signals to it.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QFileDialog, QHBoxLayout, QInputDialog, QLabel,
    QMainWindow, QMessageBox, QPushButton, QSplitter, QVBoxLayout, QWidget,
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
        self.setWindowTitle("Moshit[*]")
        self.resize(1180, 760)

        self.controller = AppController(config=config, ffmpeg_bin=ffmpeg_bin,
                                        ffprobe_bin=ffprobe_bin)
        self._selected_clip: Optional[str] = None
        self._dirty = False

        # Auto-refresh: edits re-render the preview after a short debounce.
        self.auto_refresh = True
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.timeout.connect(self._auto_refresh_fire)

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

        bottom = QWidget()
        bv = QVBoxLayout(bottom)
        bv.setContentsMargins(0, 0, 0, 0)
        bv.setSpacing(4)
        bv.addWidget(self.timeline, 1)
        bv.addWidget(self._build_tool_strip())

        split = QSplitter(Qt.Orientation.Vertical)
        split.addWidget(top)
        split.addWidget(bottom)
        split.setStretchFactor(0, 5)
        split.setStretchFactor(1, 2)
        self.setCentralWidget(split)

        self._build_menu()
        self._build_toolbar()
        self.statusBar().showMessage("Ready. Import a video to begin.")
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

        edit = self.menuBar().addMenu("&Edit")
        self.act_undo = edit.addAction("&Undo")
        self.act_undo.setShortcut("Ctrl+Z")
        self.act_undo.triggered.connect(self.controller.undo)
        self.act_redo = edit.addAction("&Redo")
        self.act_redo.setShortcuts(["Ctrl+Shift+Z", "Ctrl+Y"])
        self.act_redo.triggered.connect(self.controller.redo)
        self.act_undo.setEnabled(False)
        self.act_redo.setEnabled(False)

        gen = self.menuBar().addMenu("&Generate")
        for label, kind in (("&Zoom in", "zoom_in"), ("Zoom &out", "zoom_out"),
                             ("Pan &horizontal", "pan_x"),
                             ("Pan &vertical", "pan_y"), ("&Rotate", "rotate")):
            act = gen.addAction(f"{label} motion source")
            act.triggered.connect(
                lambda _checked=False, k=kind: self.controller.add_transform_source(k))

    def _build_toolbar(self) -> None:
        tb = self.addToolBar("Main")
        tb.setMovable(False)

        self.act_import = QAction("Import video", self)
        self.act_import.triggered.connect(self._import)
        self.act_preview = QAction("Refresh preview", self)
        self.act_preview.triggered.connect(
            lambda: self._schedule_auto_refresh(immediate=True))
        self.act_auto = QAction("Auto-refresh", self)
        self.act_auto.setCheckable(True)
        self.act_auto.setChecked(True)
        self.act_auto.setToolTip("Re-render the preview automatically after edits")
        self.act_auto.toggled.connect(self._set_auto_refresh)
        self.act_export = QAction("Export…", self)
        self.act_export.triggered.connect(self._export)

        for act in (self.act_import, self.act_preview, self.act_auto, self.act_export):
            tb.addAction(act)

    def _build_tool_strip(self) -> QWidget:
        """The clip-editing tools, placed directly under the timeline so it's
        clear they act on it."""
        strip = QWidget()
        h = QHBoxLayout(strip)
        h.setContentsMargins(8, 0, 8, 2)
        h.addWidget(QLabel("Timeline tools:"))
        self.tool_group = QButtonGroup(self)
        self.tool_group.setExclusive(True)
        self.btn_pointer = QPushButton("Pointer")
        self.btn_pointer.setCheckable(True)
        self.btn_pointer.setChecked(True)
        self.btn_pointer.setToolTip("Select, move and trim clips")
        self.btn_pointer.clicked.connect(lambda: self.timeline.set_tool("pointer"))
        self.btn_cut = QPushButton("Cut")
        self.btn_cut.setCheckable(True)
        self.btn_cut.setToolTip("Click a clip to split it at that frame")
        self.btn_cut.clicked.connect(lambda: self.timeline.set_tool("cut"))
        self.tool_group.addButton(self.btn_pointer)
        self.tool_group.addButton(self.btn_cut)
        for b in (self.btn_pointer, self.btn_cut):
            b.setMaximumWidth(90)
            h.addWidget(b)
        h.addStretch(1)
        return strip

    # -- signal wiring ------------------------------------------------------ #

    def _wire(self) -> None:
        c = self.controller
        self.library.importRequested.connect(self._import)
        self.library.addToTrackRequested.connect(self._add_to_timeline)

        c.media_added.connect(self.library.add_media)
        c.media_added.connect(lambda _:
                              self.inspector.set_motion_labels(c.motion_labels()))
        c.project_changed.connect(self._on_project_changed)
        c.project_changed.connect(self._schedule_auto_refresh)
        c.preview_begin.connect(self.preview.begin_stream)
        c.preview_batch.connect(self.preview.append_frames)
        c.preview_done.connect(self.preview.end_stream)
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

    def _import(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Import video", "",
                                              _VIDEO_FILTER)
        if path:
            self.controller.import_media(path)

    def _add_to_timeline(self, media_id: str, track: str = "main") -> None:
        if not media_id:
            self.statusBar().showMessage("Select a media item first.")
            return
        self.controller.add_clip_for_media(media_id, track)

    def _on_project_changed(self) -> None:
        self._set_dirty(True)
        self.timeline.set_project(self.controller.project)
        self.inspector.set_motion_labels(self.controller.motion_labels())
        self.act_undo.setEnabled(self.controller.can_undo)
        self.act_redo.setEnabled(self.controller.can_redo)
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
        self._schedule_auto_refresh(immediate=True)

    def _set_auto_refresh(self, on: bool) -> None:
        self.auto_refresh = on
        if on:
            self._schedule_auto_refresh()
        else:
            self._refresh_timer.stop()

    def _schedule_auto_refresh(self, *, immediate: bool = False) -> None:
        """Coalesce edit-driven and explicit preview renders onto one timer."""
        if not self.controller.project.main_clips():
            return
        if immediate:
            self._refresh_timer.start(0)
        elif self.auto_refresh:
            self._refresh_timer.start(350)

    def _auto_refresh_fire(self) -> None:
        if not self.controller.project.main_clips():
            return
        if self.controller.is_busy:               # retry once the engine is free
            self._refresh_timer.start(150)
            return
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

    def _set_dirty(self, dirty: bool) -> None:
        self._dirty = dirty
        self.setWindowModified(dirty)

    def _maybe_save(self) -> bool:
        """Offer to save unsaved changes. Returns False only if the user cancels."""
        if not self._dirty:
            return True
        resp = QMessageBox.question(
            self, "Moshit", "You have unsaved changes. Save them?",
            QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Save)
        if resp == QMessageBox.StandardButton.Save:
            return self._save_project()
        if resp == QMessageBox.StandardButton.Discard:
            return True
        return False

    def _new_project(self) -> None:
        if not self._maybe_save():
            return
        self.controller.new_project()
        self._selected_clip = None
        self.library.list.clear()
        self.inspector.set_enabled_for_clip(None, None)
        self.preview.set_frames([], 30.0)
        self._set_dirty(False)

    def _open_project(self) -> None:
        if not self._maybe_save():
            return
        path, _ = QFileDialog.getOpenFileName(self, "Open project", "",
                                              "Project (*.json)")
        if not path:
            return
        try:
            self.controller.open_project(path)
            self._selected_clip = None
            self._reload_library()
            self.inspector.set_enabled_for_clip(None, None)
            self._set_dirty(False)
        except Exception as exc:                  # surface load errors
            self._on_error(str(exc))

    def _save_project(self) -> bool:
        path, _ = QFileDialog.getSaveFileName(self, "Save project", "project.json",
                                              "Project (*.json)")
        if not path:
            return False
        try:
            self.controller.save_project(path)
            self._set_dirty(False)
            return True
        except Exception as exc:
            self._on_error(str(exc))
            return False

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
        for act in (self.act_import, self.act_preview, self.act_export):
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
        if not self._maybe_save():
            event.ignore()
            return
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
