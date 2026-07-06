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

from PySide6.QtCore import QSettings, Qt, QTimer
from PySide6.QtGui import QAction, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QDoubleSpinBox, QFileDialog, QFormLayout, QFrame, QHBoxLayout, QInputDialog,
    QLabel, QMainWindow, QMessageBox, QProgressBar, QPushButton, QScrollArea,
    QSpinBox, QSplitter, QVBoxLayout, QWidget,
)

from ..engine import EngineConfig, _ext_for_profile
from .controller import AppController, _friendly_error
from .widgets import (InspectorPanel, MediaLibrary, PreviewWidget, TimelinePane,
                      TimelineWidget)

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


class _Toast(QLabel):
    """Non-modal transient notice, bottom-centre over the main window.

    Replaces modal error dialogs: a failing auto-refresh render used to spam
    a QMessageBox per edit. Click to dismiss; auto-hides after a few seconds.
    """

    def __init__(self, parent):
        super().__init__(parent)
        self.setVisible(False)
        self.setWordWrap(True)
        self.setMaximumWidth(560)
        self.setStyleSheet(
            "background: #5c2a33; color: #ffe3e3; border: 1px solid #a4454f;"
            "border-radius: 6px; padding: 10px 14px; font-size: 12px;")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("Click to dismiss")
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.hide)

    def show_message(self, text: str, msecs: int = 8000) -> None:
        if len(text) > 700:                        # keep the toast glanceable
            text = text[:700] + " …"
        self.setText(text)
        self.adjustSize()
        self.reposition()
        self.raise_()
        self.show()
        self._timer.start(msecs)

    def reposition(self) -> None:
        pw, ph = self.parent().width(), self.parent().height()
        self.move((pw - self.width()) // 2, ph - self.height() - 46)

    def mousePressEvent(self, _event) -> None:     # noqa: N802 (Qt signature)
        self.hide()


class MainWindow(QMainWindow):
    def __init__(self, config: Optional[EngineConfig] = None,
                 ffmpeg_bin: Optional[str] = None,
                 ffprobe_bin: Optional[str] = None):
        super().__init__()
        self.resize(1180, 760)
        self._settings = QSettings("moshit", "moshit")

        self.controller = AppController(config=config, ffmpeg_bin=ffmpeg_bin,
                                        ffprobe_bin=ffprobe_bin)
        self._selected_clip: Optional[str] = None
        self._live_editing = False             # a non-modal param editor is open
        self._dirty = False
        self._project_path: Optional[str] = None    # set by Open / Save As
        self._streamed = 0                          # decode-progress counter
        self._update_title()

        # Auto-refresh: edits re-render the preview after a short debounce.
        self.auto_refresh = True
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.timeout.connect(self._auto_refresh_fire)

        self.library = MediaLibrary()
        self.preview = PreviewWidget()
        self.inspector = InspectorPanel()
        self.timeline = TimelineWidget()
        self.timeline_pane = TimelinePane(self.timeline)

        # The inspector scrolls inside its pane, so its (tall) content can never
        # force the whole window to grow vertically when a clip is selected.
        insp_scroll = QScrollArea()
        insp_scroll.setWidget(self.inspector)
        insp_scroll.setWidgetResizable(True)
        insp_scroll.setFrameShape(QFrame.Shape.NoFrame)
        insp_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        insp_scroll.setMinimumWidth(260)

        top = self._top_split = QSplitter(Qt.Orientation.Horizontal)
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
        bv.addWidget(self.timeline_pane, 1)
        bv.addWidget(self._build_tool_strip())

        split = self._main_split = QSplitter(Qt.Orientation.Vertical)
        split.addWidget(top)
        split.addWidget(bottom)
        split.setStretchFactor(0, 5)
        split.setStretchFactor(1, 2)
        self.setCentralWidget(split)

        self._toast = _Toast(self)
        self._build_menu()
        self._build_toolbar()
        self._build_status_widgets()
        self.statusBar().showMessage("Ready. Import a video to begin.")
        self._wire()
        self._build_shortcuts()
        self.timeline.set_sequence(self.controller.current_seq_id)
        self.timeline.set_project(self.controller.project)
        self._refresh_sequence_bar()
        self.inspector.set_presets(self.controller.preset_names())
        self.inspector.set_flow_sources(self.controller.media_choices())
        self.inspector.set_beat_provider(self.controller.beat_positions)
        self._restore_ui_state()

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
        self._recent_menu = m.addMenu("Open &recent")
        self._rebuild_recent_menu()
        a_save = m.addAction("&Save project")
        a_save.setShortcut("Ctrl+S")
        a_save.triggered.connect(self._save_project)
        a_save_as = m.addAction("Save project &as…")
        a_save_as.setShortcut("Ctrl+Shift+S")
        a_save_as.triggered.connect(self._save_project_as)
        m.addSeparator()
        a_settings = m.addAction("Project se&ttings…")
        a_settings.triggered.connect(self._project_settings)
        a_relink = m.addAction("Relink offline &media…")
        a_relink.triggered.connect(self._relink_offline_media)
        m.addSeparator()
        a_exp = m.addAction("&Export…")
        a_exp.setShortcut("Ctrl+E")
        a_exp.triggered.connect(self._export)
        a_frame = m.addAction("Save &frame as image…")
        a_frame.setShortcut("Ctrl+Shift+F")
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
        self.act_easy = QAction("Easy mode", self)
        self.act_easy.setCheckable(True)
        self.act_easy.setToolTip(
            "Datamosh as you edit: every clip added after another one starts "
            "with the keyframe at its cut deleted, so the picture melts across "
            "the cut. Chain as many clips as you like; all the usual editing "
            "tools stay available, and each transition shows in the clip's "
            "effect stack (iframe_removal) where it can be tweaked or removed.")
        self.act_easy.setChecked(
            bool(self._settings.value("edit/easy_mode", False, type=bool)))
        self.controller.set_easy_mode(self.act_easy.isChecked())
        self.act_easy.toggled.connect(self._set_easy_mode)
        self.act_export = QAction("Export…", self)
        self.act_export.triggered.connect(self._export)

        for act in (self.act_import, self.act_preview, self.act_auto,
                    self.act_easy, self.act_export):
            tb.addAction(act)

    def _build_status_widgets(self) -> None:
        """A busy progress bar + Cancel button, parked in the status bar."""
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)              # indeterminate busy animation
        self.progress.setMaximumWidth(150)
        self.progress.setTextVisible(False)
        self.progress.setVisible(False)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setMaximumWidth(80)
        self.cancel_btn.setToolTip("Stop the current render / export")
        self.cancel_btn.clicked.connect(self._on_cancel)
        self.cancel_btn.setVisible(False)
        self.statusBar().addPermanentWidget(self.progress)
        self.statusBar().addPermanentWidget(self.cancel_btn)

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

        # zoom controls (mirror Ctrl+wheel / the -, +, 0 shortcuts)
        h.addWidget(QLabel("Zoom:"))
        btn_zo = QPushButton("−")
        btn_zo.setToolTip("Zoom out (-)")
        btn_zo.clicked.connect(self.timeline_pane.zoom_out)
        btn_zi = QPushButton("+")
        btn_zi.setToolTip("Zoom in (=)  ·  Ctrl+wheel zooms at the cursor")
        btn_zi.clicked.connect(self.timeline_pane.zoom_in)
        btn_fit = QPushButton("Fit")
        btn_fit.setToolTip("Fit the whole sequence to the pane (0)")
        btn_fit.clicked.connect(self.timeline_pane.zoom_fit)
        self.zoom_label = QLabel("1.0×")
        self.zoom_label.setMinimumWidth(38)
        self.timeline_pane.zoomChanged.connect(
            lambda z: self.zoom_label.setText(f"{z:.1f}×"))
        for b in (btn_zo, btn_fit, btn_zi):
            b.setMaximumWidth(40)
            h.addWidget(b)
        h.addWidget(self.zoom_label)
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
            ("=", self.timeline_pane.zoom_in),        # timeline zoom
            ("+", self.timeline_pane.zoom_in),
            ("-", self.timeline_pane.zoom_out),
            ("0", self.timeline_pane.zoom_fit),
        ):
            QShortcut(QKeySequence(seq), self, activated=slot)

    # -- signal wiring ------------------------------------------------------ #

    def _wire(self) -> None:
        c = self.controller
        self.library.importRequested.connect(self._import)
        self.library.addToTrackRequested.connect(self._add_to_timeline)

        c.media_added.connect(self.library.add_media)
        c.media_relinked.connect(lambda _items: self._reload_library())
        c.media_added.connect(lambda _:
                              self.inspector.set_motion_labels(c.motion_labels()))
        c.project_changed.connect(self._on_project_changed)
        c.project_changed.connect(self._schedule_auto_refresh)
        c.preview_begin.connect(self.preview.begin_stream)
        c.preview_batch.connect(self.preview.append_frames)
        c.preview_done.connect(self.preview.end_stream)
        c.progress.connect(self._on_progress)
        c.preview_begin.connect(self._on_stream_begin)     # decode-phase progress
        c.preview_batch.connect(self._on_stream_batch)
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

        self.inspector.effectAddBegin.connect(self._on_effect_add_begin)
        self.inspector.effectEditBegin.connect(self._on_effect_edit_begin)
        self.inspector.effectLiveUpdate.connect(self._on_effect_live_update)
        self.inspector.effectEditEnd.connect(self._on_effect_edit_end)
        self.inspector.effectRemoveRequested.connect(self.controller.remove_effect)
        self.inspector.effectMoveRequested.connect(self.controller.move_effect)
        self.inspector.effectEnabledChanged.connect(self.controller.set_effect_enabled)
        self.inspector.presetSaveRequested.connect(self._on_preset_save)
        self.inspector.presetApplyRequested.connect(self._on_preset_apply)
        self.inspector.presetDeleteRequested.connect(self._on_preset_delete)
        self.inspector.pixelFxAddBegin.connect(
            lambda name: self._on_fx_add_begin("pixel", name))
        self.inspector.pixelFxEditBegin.connect(
            lambda i: self._on_fx_edit_begin("pixel", i))
        self.inspector.pixelFxLiveUpdate.connect(
            lambda i, p: self._on_fx_live_update("pixel", i, p))
        self.inspector.pixelFxEditEnd.connect(
            lambda i, ok: self._on_fx_edit_end("pixel", i, ok))
        self.inspector.pixelFxRemoveRequested.connect(self._on_pixel_remove)
        self.inspector.rawFxAddBegin.connect(
            lambda name: self._on_fx_add_begin("raw", name))
        self.inspector.rawFxEditBegin.connect(
            lambda i: self._on_fx_edit_begin("raw", i))
        self.inspector.rawFxLiveUpdate.connect(
            lambda i, p: self._on_fx_live_update("raw", i, p))
        self.inspector.rawFxEditEnd.connect(
            lambda i, ok: self._on_fx_edit_end("raw", i, ok))
        self.inspector.rawFxRemoveRequested.connect(self._on_raw_remove)
        self.inspector.maskChanged.connect(self._on_mask_changed)
        self.inspector.bakeRequested.connect(self._on_bake)
        self.inspector.revertRequested.connect(lambda: c.revert_last_bake())
        self.inspector.clipPropsChanged.connect(self._on_clip_props)
        self.inspector.flowTransferRequested.connect(self._on_flow_transfer)
        self.inspector.flowChanged.connect(self._on_flow_changed)

    # -- handlers ----------------------------------------------------------- #

    def _import(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Import video",
                                              self._start_dir("import"),
                                              _VIDEO_FILTER)
        if path:
            self._remember_dir("import", path)
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
        # keep inspector in sync with the selected clip's current op -- but not
        # while a live param editor is open, or every drag tick would tear down
        # and rebuild the inspector body (and the effect list) underneath it.
        if self._selected_clip and not self._live_editing:
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

    # -- live effect editing (non-modal, re-renders as you drag) ------------- #

    def _on_effect_add_begin(self, mode: str) -> None:
        if not self._selected_clip:
            return
        op = self.controller.begin_effect_add(self._selected_clip, mode)
        if op is None:
            return
        # begin_effect_add emitted project_changed, so the inspector's effect
        # list already holds the new op; open its live editor bound to it.
        self._live_editing = True
        self.inspector.open_live_editor(op.id)
        self._schedule_auto_refresh()              # debounced: show the default

    def _on_effect_edit_begin(self, op_id: str) -> None:
        self._live_editing = True
        self.controller.begin_effect_edit(op_id)

    def _on_effect_live_update(self, op_id: str, mode: str, params: dict,
                               region) -> None:
        self.controller.live_update_effect(op_id, mode, params, region)
        self._schedule_auto_refresh()              # debounced so drags coalesce

    def _on_effect_edit_end(self, op_id: str, committed: bool) -> None:
        self._live_editing = False
        self.controller.end_effect_edit(op_id, commit=committed)
        if self._selected_clip:                    # resync the inspector body once
            self._on_clip_selected(self._selected_clip)
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

    # -- live pixel / raw FX editing (mirrors the mosh live flow) ------------ #

    def _on_fx_add_begin(self, kind: str, name: str) -> None:
        if not self._selected_clip:
            return
        index = self.controller.begin_fx_add(kind, self._selected_clip, name)
        if index is None:
            return
        self._live_editing = True
        opener = (self.inspector.open_pixel_live_editor if kind == "pixel"
                  else self.inspector.open_raw_live_editor)
        opener(index)
        self._schedule_auto_refresh()

    def _on_fx_edit_begin(self, kind: str, index: int) -> None:
        if not self._selected_clip:
            return
        self._live_editing = True
        self.controller.begin_fx_edit(kind, self._selected_clip, index)

    def _on_fx_live_update(self, kind: str, index: int, params: dict) -> None:
        if self._selected_clip:
            self.controller.live_update_fx(kind, self._selected_clip, index, params)
            self._schedule_auto_refresh()

    def _on_fx_edit_end(self, kind: str, index: int, committed: bool) -> None:
        self._live_editing = False
        if self._selected_clip:
            self.controller.end_fx_edit(kind, self._selected_clip, index,
                                        commit=committed)
            self._on_clip_selected(self._selected_clip)
        self._schedule_auto_refresh(immediate=True)

    def _on_pixel_remove(self, index: int) -> None:
        if self._selected_clip:
            self.controller.remove_pixel_fx(self._selected_clip, index)
            self._schedule_auto_refresh(immediate=True)

    def _on_raw_remove(self, index: int) -> None:
        if self._selected_clip:
            self.controller.remove_raw_fx(self._selected_clip, index)
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

    def _set_easy_mode(self, on: bool) -> None:
        self._settings.setValue("edit/easy_mode", bool(on))
        self.controller.set_easy_mode(on)

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
            self.library.add_media(
                media, offline=self.controller.is_media_offline(media))
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
        self._set_project_path(None)
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
            self, "Save frame", self._in_start_dir("export", f"frame_{idx:05d}.png"),
            "Images (*.png *.jpg)")
        if path:
            self._remember_dir("export", path)
            self.controller.export_frame(idx, path)

    def _open_project(self) -> None:
        if not self._maybe_save():
            return
        path, _ = QFileDialog.getOpenFileName(self, "Open project",
                                              self._start_dir("project"),
                                              "Project (*.json)")
        if path:
            self._remember_dir("project", path)
            self._open_path(path)

    def _open_recent(self, path: str) -> None:
        if not self._maybe_save():
            return
        if not Path(path).exists():
            self._on_error(f"Project file not found: {path}")
            self._remove_recent(path)
            return
        self._open_path(path)

    def _open_path(self, path: str) -> None:
        try:
            self.controller.open_project(path)
            self._selected_clip = None
            self._reload_library()
            self.inspector.set_enabled_for_clip(None, None)
            self._set_project_path(path)
            self._set_dirty(False)
        except Exception as exc:                  # surface load errors
            self._on_error(_friendly_error(exc))
            return
        self._offer_relink()

    def _offer_relink(self) -> None:
        """Prompt to relink any offline media right after a project opens."""
        missing = self.controller.missing_media()
        if not missing:
            return
        names = "\n".join(f"  • {m.label}" for m in missing[:8])
        if len(missing) > 8:
            names += "\n  …"
        resp = QMessageBox.question(
            self, "Missing media",
            f"{len(missing)} media file(s) are offline (their cached video "
            f"is missing):\n{names}\n\nRelink them now? (You can also use "
            "File → Relink offline media… later.)",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes)
        if resp == QMessageBox.StandardButton.Yes:
            self._relink_offline_media()

    def _relink_offline_media(self) -> None:
        missing = self.controller.missing_media()
        if not missing:
            self.statusBar().showMessage("No offline media.")
            return
        mapping = {}
        for m in missing:
            path, _ = QFileDialog.getOpenFileName(
                self, f"Locate media for '{m.label}'",
                self._start_dir("import"), _VIDEO_FILTER)
            if path:
                mapping[m.id] = path
                self._remember_dir("import", path)
        if mapping:
            self._set_dirty(True)
            self.controller.relink_media(mapping)

    def _save_project(self) -> bool:
        """Quick save: write to the known project file, or fall back to
        Save As for a never-saved project."""
        if not self._project_path:
            return self._save_project_as()
        return self._save_to(self._project_path)

    def _save_project_as(self) -> bool:
        default = (self._project_path
                   or self._in_start_dir("project",
                                         f"{self.controller.project.name}.json"))
        path, _ = QFileDialog.getSaveFileName(self, "Save project", default,
                                              "Project (*.json)")
        if not path:
            return False
        self._remember_dir("project", path)
        return self._save_to(path)

    def _save_to(self, path: str) -> bool:
        try:
            self.controller.save_project(path)
        except Exception as exc:
            self._on_error(str(exc))
            return False
        self._set_project_path(path)
        self._set_dirty(False)
        return True

    def _set_project_path(self, path: Optional[str]) -> None:
        self._project_path = path
        if path:
            self.controller.project.name = Path(path).stem
            self._add_recent(path)
        self._update_title()

    # -- persistent UI state (QSettings) ------------------------------------- #

    def _start_dir(self, key: str) -> str:
        """Last directory used for this dialog category ('' on first use)."""
        return str(self._settings.value(f"dir/{key}", "") or "")

    def _remember_dir(self, key: str, path: str) -> None:
        self._settings.setValue(f"dir/{key}", str(Path(path).parent))

    def _in_start_dir(self, key: str, filename: str) -> str:
        d = self._start_dir(key)
        return str(Path(d) / filename) if d else filename

    def _recent_projects(self) -> list:
        val = self._settings.value("recent/projects") or []
        if isinstance(val, str):                  # QSettings collapses 1-elem lists
            val = [val]
        return [str(p) for p in val if p]

    def _add_recent(self, path: str) -> None:
        rec = [p for p in self._recent_projects() if p != path]
        rec.insert(0, path)
        self._settings.setValue("recent/projects", rec[:8])
        self._rebuild_recent_menu()

    def _remove_recent(self, path: str) -> None:
        rec = [p for p in self._recent_projects() if p != path]
        self._settings.setValue("recent/projects", rec)
        self._rebuild_recent_menu()

    def _rebuild_recent_menu(self) -> None:
        menu = self._recent_menu
        menu.clear()
        rec = self._recent_projects()
        for p in rec:
            act = menu.addAction(Path(p).name)
            act.setToolTip(p)
            act.triggered.connect(lambda _=False, p=p: self._open_recent(p))
        menu.setEnabled(bool(rec))
        if rec:
            menu.addSeparator()
            menu.addAction("Clear list",
                           lambda: (self._settings.setValue("recent/projects", []),
                                    self._rebuild_recent_menu()))

    def _restore_ui_state(self) -> None:
        g = self._settings.value("win/geometry")
        if g is not None:
            self.restoreGeometry(g)
        for key, sp in (("win/top_split", self._top_split),
                        ("win/main_split", self._main_split)):
            st = self._settings.value(key)
            if st is not None:
                sp.restoreState(st)

    def _save_ui_state(self) -> None:
        s = self._settings
        s.setValue("win/geometry", self.saveGeometry())
        s.setValue("win/top_split", self._top_split.saveState())
        s.setValue("win/main_split", self._main_split.saveState())

    def _update_title(self) -> None:
        name = (Path(self._project_path).stem if self._project_path
                else getattr(self.controller.project, "name", "") or "untitled")
        self.setWindowTitle(f"{name}[*] — Moshit")

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
        path, _ = QFileDialog.getSaveFileName(self, "Export to",
                                              self._in_start_dir(
                                                  "export", f"moshit{suffix}"),
                                              f"*{suffix}")
        if path:
            self._remember_dir("export", path)
            self.controller.export(profile, path, audio)

    def _on_cancel(self) -> None:
        self.controller.cancel()
        self.preview.cancel_stream()            # finalize any partial preview frames

    def _on_busy(self, busy: bool, message: str) -> None:
        for act in (self.act_import, self.act_preview, self.act_export):
            act.setEnabled(not busy)
        self.inspector.setEnabled(not busy)
        if busy:
            self.progress.setRange(0, 0)        # indeterminate until steps arrive
        self.progress.setVisible(busy)
        self.cancel_btn.setVisible(busy)        # Cancel stays clickable while busy
        self.preview.set_rendering(busy, message)
        if busy and message:
            self.statusBar().showMessage(message)
        self.setCursor(Qt.CursorShape.BusyCursor if busy else Qt.CursorShape.ArrowCursor)

    def _on_progress(self, done: int, total: int, label: str) -> None:
        """Determinate render progress (per-clip / per-layer steps)."""
        if total > 0:
            self.progress.setRange(0, total)
            self.progress.setValue(done)
        if label:
            self.statusBar().showMessage(label)

    def _on_stream_begin(self, total: int, _fps: float) -> None:
        """Preview decode streams frames back — track them on the bar too."""
        self._streamed = 0
        if total > 0:
            self.progress.setRange(0, total)
            self.progress.setValue(0)
            self.statusBar().showMessage("Loading preview frames…")

    def _on_stream_batch(self, frames: list) -> None:
        self._streamed += len(frames)
        if self.progress.maximum() > 0:
            self.progress.setValue(min(self._streamed, self.progress.maximum()))

    def _on_error(self, message: str) -> None:
        """Non-modal error surface: first line to the status bar, full text on
        a transient toast (a failing auto-refresh must never spam dialogs)."""
        self.statusBar().showMessage(message.splitlines()[0] if message else "")
        self._toast.show_message(message)

    # -- lifecycle ---------------------------------------------------------- #

    def resizeEvent(self, event) -> None:          # noqa: N802 (Qt signature)
        super().resizeEvent(event)
        if self._toast.isVisible():
            self._toast.reposition()

    def closeEvent(self, event) -> None:
        if not self._maybe_save():
            event.ignore()
            return
        self._save_ui_state()
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
