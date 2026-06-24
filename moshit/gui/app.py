"""Main window and application entry point for the Moshit GUI.

Layout: a media library, a preview with transport, and a schema-driven effect
inspector across the top; a multi-track timeline (with a sequence switcher)
across the bottom. The window holds an :class:`AppController` and wires widget
signals to it.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QDoubleSpinBox, QFileDialog, QFormLayout, QFrame, QHBoxLayout, QInputDialog,
    QLabel, QMainWindow, QMessageBox, QPushButton, QScrollArea, QSpinBox,
    QSplitter, QVBoxLayout, QWidget,
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


class ProjectSettingsDialog(QDialog):
    """Choose the sequence geometry and frame rate.

    Everything imported is normalised to these, so they are picked up front
    (before any media is imported). A handful of presets fill the fields; the
    spin boxes allow any custom size.
    """

    PRESETS = [
        ("720p · 1280×720 @ 30", 1280, 720, 30.0),
        ("1080p · 1920×1080 @ 30", 1920, 1080, 30.0),
        ("1080p · 1920×1080 @ 24", 1920, 1080, 24.0),
        ("720p · 1280×720 @ 60", 1280, 720, 60.0),
        ("SD · 640×480 @ 30", 640, 480, 30.0),
        ("Vertical · 1080×1920 @ 30", 1080, 1920, 30.0),
    ]

    def __init__(self, parent, width: int, height: int, fps: float):
        super().__init__(parent)
        self.setWindowTitle("Project settings")
        form = QFormLayout(self)

        self.w_spin = QSpinBox()
        self.w_spin.setRange(16, 7680)
        self.w_spin.setSingleStep(2)
        self.h_spin = QSpinBox()
        self.h_spin.setRange(16, 4320)
        self.h_spin.setSingleStep(2)
        self.fps_spin = QDoubleSpinBox()
        self.fps_spin.setRange(1.0, 120.0)
        self.fps_spin.setDecimals(3)
        self.w_spin.setValue(int(width))
        self.h_spin.setValue(int(height))
        self.fps_spin.setValue(float(fps))

        self.preset = QComboBox()
        self.preset.addItem("Custom…")
        for label, w, h, f in self.PRESETS:
            self.preset.addItem(label, (w, h, f))
        self.preset.currentIndexChanged.connect(self._apply_preset)

        form.addRow("Preset", self.preset)
        form.addRow("Width", self.w_spin)
        form.addRow("Height", self.h_spin)
        form.addRow("Frame rate", self.fps_spin)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                   | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)
        self._select_matching_preset()

    def _apply_preset(self, idx: int) -> None:
        data = self.preset.itemData(idx)
        if data:
            w, h, f = data
            self.w_spin.setValue(w)
            self.h_spin.setValue(h)
            self.fps_spin.setValue(f)

    def _select_matching_preset(self) -> None:
        cur = (self.w_spin.value(), self.h_spin.value(), float(self.fps_spin.value()))
        for i in range(1, self.preset.count()):
            if self.preset.itemData(i) == cur:
                self.preset.setCurrentIndex(i)
                return
        self.preset.setCurrentIndex(0)

    def values(self):
        return self.w_spin.value(), self.h_spin.value(), float(self.fps_spin.value())


class ExportDialog(QDialog):
    """Pick a delivery format and whether to mux source audio."""

    def __init__(self, parent, profiles):
        super().__init__(parent)
        self.setWindowTitle("Export")
        form = QFormLayout(self)
        self.fmt = QComboBox()
        self.fmt.addItems(profiles)
        self.audio = QCheckBox("Include audio from source clips")
        self.audio.setChecked(True)
        self.audio.setToolTip("Clips keep their source audio (clean edits stay "
                              "perfectly in sync; baked clips are silent).")
        form.addRow("Format", self.fmt)
        form.addRow(self.audio)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                   | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def values(self):
        return self.fmt.currentText(), self.audio.isChecked()


class FlowDialog(QDialog):
    """Pick a motion driver and parameters for an optical-flow transfer."""

    def __init__(self, parent, choices, backend: str):
        super().__init__(parent)
        self.setWindowTitle("Optical-flow transfer")
        form = QFormLayout(self)

        self.motion = QComboBox()
        for label, media_id in choices:
            self.motion.addItem(label, media_id)

        self.strength = QDoubleSpinBox()
        self.strength.setRange(0.1, 5.0)
        self.strength.setSingleStep(0.1)
        self.strength.setValue(1.0)
        self.preset = QComboBox()
        self.preset.addItems(["ultrafast", "fast", "medium"])
        self.preset.setCurrentText("fast")
        self.hold = QCheckBox("Hold first frame (melt)")
        self.hold.setChecked(True)
        self.accumulate = QCheckBox("Accumulate flow (drift)")
        self.accumulate.setChecked(True)

        form.addRow("Motion source", self.motion)
        form.addRow("Strength", self.strength)
        form.addRow("Quality", self.preset)
        form.addRow(self.hold)
        form.addRow(self.accumulate)
        backend_lbl = QLabel(f"Compute: {backend}")
        backend_lbl.setStyleSheet("color:#8a92a6;")
        form.addRow(backend_lbl)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                   | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def values(self):
        return self.motion.currentData(), {
            "hold": self.hold.isChecked(),
            "accumulate": self.accumulate.isChecked(),
            "strength": self.strength.value(),
            "preset": self.preset.currentText(),
        }


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

        # The inspector scrolls inside its pane, so its (tall) content can never
        # force the whole window to grow vertically when a clip is selected.
        insp_scroll = QScrollArea()
        insp_scroll.setWidget(self.inspector)
        insp_scroll.setWidgetResizable(True)
        insp_scroll.setFrameShape(QFrame.Shape.NoFrame)
        insp_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        insp_scroll.setMinimumWidth(260)

        top = QSplitter(Qt.Orientation.Horizontal)
        top.addWidget(self.library)
        top.addWidget(self.preview)
        top.addWidget(insp_scroll)
        top.setStretchFactor(0, 2)
        top.setStretchFactor(1, 5)
        top.setStretchFactor(2, 3)

        bottom = QWidget()
        bv = QVBoxLayout(bottom)
        bv.setContentsMargins(0, 0, 0, 0)
        bv.setSpacing(4)
        bv.addWidget(self._build_sequence_bar())
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
        self._build_shortcuts()
        self.timeline.set_sequence(self.controller.current_seq_id)
        self.timeline.set_project(self.controller.project)
        self._refresh_sequence_bar()
        self.inspector.set_presets(self.controller.preset_names())
        self.inspector.set_flow_sources(self.controller.media_choices())
        self.inspector.set_beat_provider(self.controller.beat_positions)

    # -- toolbar ------------------------------------------------------------ #

    # -- menu / toolbar ----------------------------------------------------- #

    def _build_menu(self) -> None:
        m = self.menuBar().addMenu("&File")
        a_new = m.addAction("&New project")
        a_new.setShortcut("Ctrl+N")
        a_new.triggered.connect(self._new_project)
        a_open = m.addAction("&Open project…")
        a_open.setShortcut("Ctrl+O")
        a_open.triggered.connect(self._open_project)
        a_save = m.addAction("&Save project…")
        a_save.setShortcut("Ctrl+S")
        a_save.triggered.connect(self._save_project)
        m.addSeparator()
        a_settings = m.addAction("Project se&ttings…")
        a_settings.triggered.connect(self._project_settings)
        m.addSeparator()
        a_exp = m.addAction("&Export…")
        a_exp.setShortcut("Ctrl+E")
        a_exp.triggered.connect(self._export)
        a_frame = m.addAction("Save &frame as image…")
        a_frame.setShortcut("Ctrl+Shift+S")
        a_frame.triggered.connect(self._save_frame)
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
        edit.addSeparator()
        a_dup = edit.addAction("&Duplicate clip")
        a_dup.setShortcut("Ctrl+D")
        a_dup.triggered.connect(self._duplicate_selected)
        a_split = edit.addAction("&Split at playhead")
        a_split.setShortcut("S")
        a_split.triggered.connect(self.timeline.request_split_at_playhead)

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

    def _build_sequence_bar(self) -> QWidget:
        """Sequence switcher + track/precompose actions, above the timeline."""
        bar = QWidget()
        h = QHBoxLayout(bar)
        h.setContentsMargins(8, 2, 8, 0)
        h.addWidget(QLabel("Sequence:"))
        self.seq_combo = QComboBox()
        self.seq_combo.setMinimumWidth(150)
        self.seq_combo.setToolTip("Switch which sequence the timeline edits "
                                  "(double-click a precomp clip to enter it)")
        self.seq_combo.currentIndexChanged.connect(self._on_seq_combo)
        h.addWidget(self.seq_combo)
        btn_track = QPushButton("+ Track")
        btn_track.setMaximumWidth(80)
        btn_track.setToolTip("Add a video track to this sequence")
        btn_track.clicked.connect(lambda: self.controller.add_video_track())
        h.addWidget(btn_track)
        self.btn_precompose = QPushButton("Precompose")
        self.btn_precompose.setMaximumWidth(100)
        self.btn_precompose.setToolTip("Move the selected clip into a new "
                                       "sequence (precomp)")
        self.btn_precompose.clicked.connect(self._on_precompose)
        h.addWidget(self.btn_precompose)
        h.addStretch(1)
        return bar

    def _refresh_sequence_bar(self) -> None:
        self.seq_combo.blockSignals(True)
        self.seq_combo.clear()
        for s in self.controller.project.sequences:
            self.seq_combo.addItem(s.name, s.id)
        idx = self.seq_combo.findData(self.controller.current_seq_id)
        if idx >= 0:
            self.seq_combo.setCurrentIndex(idx)
        self.seq_combo.blockSignals(False)

    def _on_seq_combo(self, _idx: int) -> None:
        sid = self.seq_combo.currentData()
        if sid:
            self.controller.set_current_sequence(sid)

    def _on_precompose(self) -> None:
        if not self._selected_clip:
            self.statusBar().showMessage("Select a clip to precompose.")
            return
        self.controller.precompose([self._selected_clip])

    def _on_sequence_changed(self) -> None:
        self.timeline.set_sequence(self.controller.current_seq_id)
        self.timeline.set_project(self.controller.project)
        self._refresh_sequence_bar()
        self._schedule_auto_refresh()

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

    def _build_shortcuts(self) -> None:
        """Playback/transport keys, scoped to the window. Editable widgets
        consume their own text keys first, so these only fire when focus is on
        the preview/timeline/library rather than a parameter field."""
        for seq, slot in (
            ("Space", self.preview.toggle),
            (",", lambda: self.preview.step(-1)),
            (".", lambda: self.preview.step(1)),
            ("Home", self.preview.go_start),
            ("End", self.preview.go_end),
        ):
            QShortcut(QKeySequence(seq), self, activated=slot)

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
        c.preview_audio.connect(self.preview.set_audio)
        c.preview_waveform.connect(self.timeline.set_waveform)
        self.preview.muteToggled.connect(self.controller.set_preview_muted)
        c.busy.connect(self._on_busy)
        c.error.connect(self._on_error)
        c.status.connect(self.statusBar().showMessage)

        self.timeline.clipSelected.connect(self._on_clip_selected)
        self.timeline.moveRequested.connect(self.controller.move_clip)
        self.timeline.trimRequested.connect(self._on_trim)
        self.timeline.removeRequested.connect(self._on_remove)
        self.timeline.seekRequested.connect(self._on_seek)
        self.timeline.splitRequested.connect(self.controller.split_clip)
        self.timeline.duplicateRequested.connect(self.controller.duplicate_clip)
        self.timeline.addTrackRequested.connect(self.controller.add_video_track)
        self.timeline.removeTrackRequested.connect(self.controller.remove_track)
        self.timeline.reorderTrackRequested.connect(self.controller.reorder_track)
        self.timeline.trackEnabledToggled.connect(self.controller.set_track_enabled)
        self.timeline.addClipToTrackRequested.connect(self._on_add_clip_to_track)
        self.timeline.enterSequenceRequested.connect(
            self.controller.set_current_sequence)
        c.sequence_changed.connect(self._on_sequence_changed)
        self.preview.frameChanged.connect(self._on_preview_frame)

        self.inspector.effectAddRequested.connect(self._on_effect_add)
        self.inspector.effectUpdateRequested.connect(self._on_effect_update)
        self.inspector.effectRemoveRequested.connect(self.controller.remove_effect)
        self.inspector.effectMoveRequested.connect(self.controller.move_effect)
        self.inspector.effectEnabledChanged.connect(self.controller.set_effect_enabled)
        self.inspector.presetSaveRequested.connect(self._on_preset_save)
        self.inspector.presetApplyRequested.connect(self._on_preset_apply)
        self.inspector.presetDeleteRequested.connect(self._on_preset_delete)
        self.inspector.pixelFxAddRequested.connect(self._on_pixel_add)
        self.inspector.pixelFxRemoveRequested.connect(self._on_pixel_remove)
        self.inspector.pixelFxParamsChanged.connect(self._on_pixel_params)
        self.inspector.rawFxAddRequested.connect(self._on_raw_add)
        self.inspector.rawFxRemoveRequested.connect(self._on_raw_remove)
        self.inspector.rawFxParamsChanged.connect(self._on_raw_params)
        self.inspector.maskChanged.connect(self._on_mask_changed)
        self.inspector.bakeRequested.connect(self._on_bake)
        self.inspector.revertRequested.connect(lambda: c.revert_last_bake())
        self.inspector.clipPropsChanged.connect(self._on_clip_props)
        self.inspector.flowTransferRequested.connect(self._on_flow_transfer)
        self.inspector.flowChanged.connect(self._on_flow_changed)

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

    def _on_add_clip_to_track(self, track_id: str) -> None:
        media_id = self.library.selected_media_id()
        if not media_id:
            self.statusBar().showMessage("Select a media item in the library first.")
            return
        self.controller.add_clip_for_media(media_id, track_id)

    def _on_project_changed(self) -> None:
        self._set_dirty(True)
        self.timeline.set_sequence(self.controller.current_seq_id)
        self.timeline.set_project(self.controller.project)
        self._refresh_sequence_bar()
        self.inspector.set_motion_labels(self.controller.motion_labels())
        self.inspector.set_flow_sources(self.controller.media_choices())
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
        self.inspector.set_flow_sources(self.controller.media_choices())
        self.inspector.set_enabled_for_clip(
            clip_id, label, clip=clip,
            effects=self.controller.clip_effects(clip_id))

    def _on_clip_props(self, props: dict) -> None:
        if not self._selected_clip:
            return
        self.controller.set_clip_props(self._selected_clip, props)
        self._schedule_auto_refresh(immediate=True)

    def _on_effect_add(self, mode: str, params: dict, region) -> None:
        if not self._selected_clip:
            return
        self.controller.add_effect(self._selected_clip, mode, params, region)
        self._schedule_auto_refresh(immediate=True)

    def _on_effect_update(self, op_id: str, mode: str, params: dict, region) -> None:
        self.controller.update_effect(op_id, mode, params, region)
        self._schedule_auto_refresh(immediate=True)

    def _on_preset_save(self) -> None:
        if not self._selected_clip:
            self.statusBar().showMessage("Select a clip whose stack you want to save.")
            return
        name, ok = QInputDialog.getText(self, "Save preset", "Preset name:")
        if ok and name.strip():
            if self.controller.save_stack_as_preset(self._selected_clip, name.strip()):
                self.inspector.set_presets(self.controller.preset_names())

    def _on_preset_apply(self, name: str) -> None:
        if not self._selected_clip:
            self.statusBar().showMessage("Select a clip to apply the preset to.")
            return
        self.controller.apply_preset(self._selected_clip, name)
        self._schedule_auto_refresh(immediate=True)

    def _on_preset_delete(self, name: str) -> None:
        self.controller.delete_preset(name)
        self.inspector.set_presets(self.controller.preset_names())

    def _on_pixel_add(self, name: str) -> None:
        if self._selected_clip:
            self.controller.add_pixel_fx(self._selected_clip, name)
            self._schedule_auto_refresh(immediate=True)

    def _on_pixel_remove(self, index: int) -> None:
        if self._selected_clip:
            self.controller.remove_pixel_fx(self._selected_clip, index)
            self._schedule_auto_refresh(immediate=True)

    def _on_pixel_params(self, index: int, params: dict) -> None:
        if self._selected_clip:
            self.controller.update_pixel_fx(self._selected_clip, index, params)
            self._schedule_auto_refresh(immediate=True)

    def _on_raw_add(self, name: str) -> None:
        if self._selected_clip:
            self.controller.add_raw_fx(self._selected_clip, name)
            self._schedule_auto_refresh(immediate=True)

    def _on_raw_remove(self, index: int) -> None:
        if self._selected_clip:
            self.controller.remove_raw_fx(self._selected_clip, index)
            self._schedule_auto_refresh(immediate=True)

    def _on_raw_params(self, index: int, params: dict) -> None:
        if self._selected_clip:
            self.controller.update_raw_fx(self._selected_clip, index, params)
            self._schedule_auto_refresh(immediate=True)

    def _on_mask_changed(self, kind: str, spec) -> None:
        if self._selected_clip:
            self.controller.set_clip_mask(self._selected_clip, kind, spec)
            self._schedule_auto_refresh(immediate=True)

    def _on_flow_transfer(self) -> None:
        if not self._selected_clip:
            self.statusBar().showMessage("Select a main clip to transfer motion onto.")
            return
        if not self.controller.flow_available():
            QMessageBox.warning(
                self, "Optical-flow transfer",
                "This needs OpenCV + numpy (GPU via OpenCL when available).\n\n"
                "    pip install 'moshit[flow]'")
            return
        choices = self.controller.media_choices()
        if not choices:
            QMessageBox.information(self, "Optical-flow transfer",
                                    "Import a clip to drive the motion first.")
            return
        dlg = FlowDialog(self, choices, self.controller.flow_backend())
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        motion_id, params = dlg.values()
        self.controller.apply_optical_flow(self._selected_clip, motion_id, **params)

    def _on_flow_changed(self, flow_transfer) -> None:
        if not self._selected_clip:
            return
        self.controller.set_flow_transfer(self._selected_clip, flow_transfer)
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
        effects = self.controller.clip_effects(self._selected_clip)
        if not any(e["enabled"] for e in effects):
            self.statusBar().showMessage("Add or enable an effect before baking.")
            return
        self.controller.bake_clip(self._selected_clip)

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
        cfg = self.controller.config
        dlg = ProjectSettingsDialog(self, cfg.width, cfg.height, cfg.fps)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        w, h, fps = dlg.values()
        self.controller.new_project()        # clears media so settings can change
        self.controller.set_project_config(width=w, height=h, fps=fps)
        self._selected_clip = None
        self.library.list.clear()
        self.inspector.set_enabled_for_clip(None, None)
        self.preview.set_frames([], 30.0)
        self._set_dirty(False)

    def _project_settings(self) -> None:
        if self.controller.has_media:
            QMessageBox.information(
                self, "Project settings",
                "Resolution and frame rate are locked once media is imported "
                "(every clip is normalised to them).\nStart a New project to "
                "change them.")
            return
        cfg = self.controller.config
        dlg = ProjectSettingsDialog(self, cfg.width, cfg.height, cfg.fps)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            w, h, fps = dlg.values()
            self.controller.set_project_config(width=w, height=h, fps=fps)

    def _duplicate_selected(self) -> None:
        if self._selected_clip:
            self.controller.duplicate_clip(self._selected_clip)
        else:
            self.statusBar().showMessage("Select a clip to duplicate.")

    def _save_frame(self) -> None:
        if self.preview.frame_count() == 0:
            self.statusBar().showMessage("Render a preview first, then save a frame.")
            return
        idx = self.preview.current_index()
        path, _ = QFileDialog.getSaveFileName(
            self, "Save frame", f"frame_{idx:05d}.png", "Images (*.png *.jpg)")
        if path:
            self.controller.export_frame(idx, path)

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
        dlg = ExportDialog(self, profiles)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        profile, audio = dlg.values()
        suffix = _ext_for_profile(profile)
        path, _ = QFileDialog.getSaveFileName(self, "Export to", f"moshit{suffix}",
                                              f"*{suffix}")
        if path:
            self.controller.export(profile, path, audio)

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
