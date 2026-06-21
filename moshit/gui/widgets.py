"""Custom widgets for the Moshit GUI.

Kept in one module for v1; each class is self-contained. The inspector builds
its controls from a mode's ``Param`` schema, so any mode -- including a
third-party plugin -- gets a usable UI with no changes here.
"""
from __future__ import annotations

from typing import Callable, Dict, List, Optional, Tuple

from PySide6.QtCore import Qt, QPoint, QRect, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPixmap, QPolygon
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout, QFrame, QHBoxLayout,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QPushButton, QSizePolicy,
    QSlider, QSpinBox, QVBoxLayout, QWidget,
)

from ..modes import available_modes, get_mode
from ..modes.base import Param


# --------------------------------------------------------------------------- #
# Schema -> widget
# --------------------------------------------------------------------------- #

def build_param_widget(param: Param) -> Tuple[QWidget, Callable]:
    """Return (control, getter) for a mode parameter. getter() -> current value."""
    if param.kind == "bool":
        w = QCheckBox()
        w.setChecked(bool(param.default))
        return w, w.isChecked
    if param.kind == "int":
        w = QSpinBox()
        w.setRange(int(param.lo) if param.lo is not None else -1_000_000,
                   int(param.hi) if param.hi is not None else 1_000_000)
        w.setValue(int(param.default or 0))
        return w, w.value
    if param.kind == "float":
        w = QDoubleSpinBox()
        w.setDecimals(2)
        w.setSingleStep(0.1)
        w.setRange(float(param.lo) if param.lo is not None else -1e6,
                   float(param.hi) if param.hi is not None else 1e6)
        w.setValue(float(param.default or 0.0))
        return w, w.value
    if param.kind == "choice":
        w = QComboBox()
        w.addItems([str(c) for c in param.choices])
        if param.default is not None:
            w.setCurrentText(str(param.default))
        return w, w.currentText
    if param.kind == "clip_ref":
        w = QComboBox()                       # filled with motion labels by inspector
        return w, w.currentText
    w = QLineEdit(str(param.default or ""))
    return w, w.text


# --------------------------------------------------------------------------- #
# Media library
# --------------------------------------------------------------------------- #

class MediaLibrary(QWidget):
    importRequested = Signal()                # import one video (role-neutral)
    addToTrackRequested = Signal(str, str)    # media_id, track ("main"|"motion")

    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.addWidget(_heading("Media"))

        self.list = QListWidget()
        self.list.itemDoubleClicked.connect(
            lambda _: self._emit_add("main"))     # double-click → main track
        layout.addWidget(self.list, 1)

        btn_import = QPushButton("Import video…")
        btn_import.clicked.connect(lambda: self.importRequested.emit())
        layout.addWidget(btn_import)

        add_main = QPushButton("Add to main")
        add_main.setToolTip("Place the selected clip on the main track")
        add_main.clicked.connect(lambda: self._emit_add("main"))
        add_motion = QPushButton("Add to motion")
        add_motion.setToolTip("Place the selected clip on the motion track")
        add_motion.clicked.connect(lambda: self._emit_add("motion"))
        row = QHBoxLayout()
        row.addWidget(add_main)
        row.addWidget(add_motion)
        layout.addLayout(row)

    def _emit_add(self, track: str) -> None:
        mid = self.selected_media_id()
        if mid:
            self.addToTrackRequested.emit(mid, track)

    def add_media(self, item) -> None:
        entry = QListWidgetItem(f"{item.label}  ·  {item.nb_frames}f")
        entry.setData(Qt.ItemDataRole.UserRole, item.id)
        self.list.addItem(entry)
        self.list.setCurrentItem(entry)

    def selected_media_id(self) -> Optional[str]:
        it = self.list.currentItem()
        return it.data(Qt.ItemDataRole.UserRole) if it else None


# --------------------------------------------------------------------------- #
# Timeline
# --------------------------------------------------------------------------- #

class TimelineWidget(QWidget):
    """Interactive two-track timeline: main lane on top, motion lane below.

    Click a clip to select it. Drag its body to reorder it on the main track,
    drag either edge to trim, and press Delete (or right-click → Remove) to take
    it off the timeline. A frame ruler runs across the top and a playhead line
    follows the preview.
    """

    clipSelected = Signal(str)
    reorderRequested = Signal(str, int)            # clip_id, new index
    trimRequested = Signal(str, int, int)          # clip_id, in|-1, out|-1
    removeRequested = Signal(str)
    seekRequested = Signal(float)                  # scrub position, 0..1
    splitRequested = Signal(str, int)              # clip_id, offset frames

    RULER_H = 20
    LANE_H = 46
    PAD = 8
    LABEL_W = 56
    EDGE = 6                                        # px hot zone for edge-trim

    def __init__(self):
        super().__init__()
        self.setMinimumHeight(self.RULER_H + 2 * self.LANE_H + 3 * self.PAD + 6)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)
        self._project = None
        self._play_frac = 0.0                       # playhead as 0..1 of the preview
        self._total = 1
        self._selected: Optional[str] = None
        self._hits: List[Tuple[QRect, str, str]] = []     # (rect, clip_id, track)
        self._drag: Optional[dict] = None
        self._scrubbing = False
        self._tool = "pointer"                      # "pointer" | "cut"
        self._cursor_x = 0

    def set_project(self, project) -> None:
        self._project = project
        self._total = max(1, self._main_length())
        self.update()

    def set_play_fraction(self, frac: float) -> None:
        self._play_frac = max(0.0, min(1.0, float(frac)))
        self.update()

    def set_tool(self, tool: str) -> None:
        self._tool = "cut" if tool == "cut" else "pointer"
        self.setCursor(Qt.CursorShape.SplitHCursor if self._tool == "cut"
                       else Qt.CursorShape.ArrowCursor)

    def select(self, clip_id: Optional[str]) -> None:
        self._selected = clip_id
        self.update()

    # -- geometry ----------------------------------------------------------- #

    def _main_clips(self):
        return self._project.main_clips() if self._project else []

    def _motion_clips(self):
        if not self._project:
            return []
        return [c for c in self._project.clips
                if c.track == "motion" and not c.archived]

    def _main_length(self) -> int:
        return sum(self._project._clip_length(c) for c in self._main_clips())

    def _track_x(self) -> Tuple[int, int]:
        return self.PAD + self.LABEL_W, self.width() - self.PAD

    def _ppf(self) -> float:
        x0, x1 = self._track_x()
        return max(1, x1 - x0) / max(1, self._total)

    def _lane_y(self, lane: int) -> int:
        return self.RULER_H + self.PAD + lane * (self.LANE_H + self.PAD)

    # -- painting ----------------------------------------------------------- #

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        p.fillRect(self.rect(), QColor("#1c1f24"))
        self._hits = []
        x0, x1 = self._track_x()
        track_w = max(1, x1 - x0)
        ppf = self._ppf()

        font = QFont()
        font.setPointSize(8)
        p.setFont(font)

        # ruler ticks + labels, with a scrub track underneath
        p.setPen(QColor("#4a5160"))
        step = max(1, self._total // 10)
        f = 0
        while f <= self._total:
            tx = int(x0 + f * ppf)
            p.drawLine(tx, 2, tx, 6)
            p.drawText(tx + 2, 12, str(f))
            f += step
        p.setPen(QColor("#3a414c"))
        p.drawLine(x0, self.RULER_H - 2, x1, self.RULER_H - 2)

        # lane labels + backgrounds
        p.setPen(QColor("#8a92a6"))
        p.drawText(self.PAD, self._lane_y(0) + 26, "main")
        p.drawText(self.PAD, self._lane_y(1) + 26, "motion")
        for lane in (0, 1):
            p.fillRect(QRect(x0, self._lane_y(lane), track_w, self.LANE_H),
                       QColor("#262b33"))

        # main clips (sequential)
        cursor = 0
        for clip in self._main_clips():
            length = self._project._clip_length(clip)
            rect = QRect(int(x0 + cursor * ppf), self._lane_y(0),
                         max(2, int(length * ppf) - 2), self.LANE_H)
            self._draw_clip(p, rect, clip, length)
            self._hits.append((rect, clip.id, "main"))
            cursor += length

        # motion clips
        cursor = 0
        for clip in self._motion_clips():
            length = self._project._clip_length(clip)
            rect = QRect(int(x0 + cursor * ppf), self._lane_y(1),
                         max(2, int(length * ppf) - 2), self.LANE_H)
            self._draw_clip(p, rect, clip, length, motion=True)
            self._hits.append((rect, clip.id, "motion"))
            cursor += length

        # drag feedback
        if self._drag:
            p.setPen(QColor("#ffd166"))
            if self._drag["mode"] == "move":
                p.drawLine(self._cursor_x, self.RULER_H, self._cursor_x,
                           self.height() - 2)
            else:
                p.drawLine(self._cursor_x, self.RULER_H, self._cursor_x,
                           self.height() - 2)

        # playhead (proportional to the preview position) + scrub handle
        px = int(x0 + self._play_frac * track_w)
        p.setPen(QColor("#ff5470"))
        p.drawLine(px, self.RULER_H - 2, px, self.height() - 2)
        p.setBrush(QColor("#ff5470"))
        p.drawPolygon(QPolygon([QPoint(px - 4, self.RULER_H - 9),
                                QPoint(px + 4, self.RULER_H - 9),
                                QPoint(px, self.RULER_H - 1)]))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.end()

    def _draw_clip(self, p, rect, clip, length, motion=False) -> None:
        base = QColor("#6a4ea5") if motion else QColor("#3b6ea5")
        if clip.id == self._selected:
            base = base.lighter(135)
        p.fillRect(rect, base)
        p.setPen(QColor("#9fb4d6") if clip.id == self._selected else QColor("#2a2f37"))
        p.drawRect(rect.adjusted(0, 0, -1, -1))
        media = self._project.media.get(clip.media_id)
        label = media.label if media else clip.id
        if media and media.derived:
            label += " (baked)"
        p.setPen(QColor("#eef1f6"))
        p.drawText(rect.adjusted(5, 0, -3, 0),
                   Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                   f"{label}  {length}f")

    # -- hit testing -------------------------------------------------------- #

    def _hit(self, pos) -> Optional[Tuple[QRect, str, str]]:
        for entry in self._hits:
            if entry[0].contains(pos):
                return entry
        return None

    def _drop_index(self, x: int, exclude: str) -> int:
        idx = 0
        for rect, cid, track in self._hits:
            if track != "main" or cid == exclude:
                continue
            if x > rect.center().x():
                idx += 1
        return idx

    # -- interaction -------------------------------------------------------- #

    def _emit_seek(self, x: int) -> None:
        x0, x1 = self._track_x()
        frac = max(0.0, min(1.0, (x - x0) / max(1, x1 - x0)))
        self._play_frac = frac
        self.update()
        self.seekRequested.emit(frac)

    def mousePressEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton or not self._project:
            return
        pos = event.position().toPoint()
        if pos.y() <= self.RULER_H:                  # scrub strip across the top
            self._scrubbing = True
            self._emit_seek(pos.x())
            return
        hit = self._hit(pos)
        if not hit:
            return
        rect, clip_id, track = hit
        self._selected = clip_id
        self.clipSelected.emit(clip_id)

        if self._tool == "cut":                      # split at the clicked frame
            offset = round((pos.x() - rect.left()) / self._ppf())
            self.splitRequested.emit(clip_id, offset)
            self.update()
            return

        clip = self._project.clip(clip_id)
        media = self._project.media[clip.media_id]
        orig_out = clip.out_point if clip.out_point is not None else media.nb_frames
        if abs(pos.x() - rect.left()) <= self.EDGE:
            mode = "trim_l"
        elif abs(pos.x() - rect.right()) <= self.EDGE:
            mode = "trim_r"
        else:
            mode = "move"
        self._drag = {"id": clip_id, "track": track, "mode": mode,
                      "press_x": pos.x(), "in": clip.in_point, "out": orig_out,
                      "ppf": self._ppf()}
        self._cursor_x = pos.x()
        self.update()

    def mouseMoveEvent(self, event) -> None:
        pos = event.position().toPoint()
        self._cursor_x = pos.x()
        if self._scrubbing:
            self._emit_seek(pos.x())
            return
        if self._drag:
            self.update()
            return
        if self._tool == "pointer":                  # edge-trim cursor affordance
            hit = self._hit(pos)
            if hit and pos.y() > self.RULER_H and (
                    abs(pos.x() - hit[0].left()) <= self.EDGE
                    or abs(pos.x() - hit[0].right()) <= self.EDGE):
                self.setCursor(Qt.CursorShape.SizeHorCursor)
            else:
                self.setCursor(Qt.CursorShape.ArrowCursor)

    def mouseReleaseEvent(self, event) -> None:
        if self._scrubbing:
            self._scrubbing = False
            return
        if not self._drag:
            return
        d, self._drag = self._drag, None
        dframes = round((self._cursor_x - d["press_x"]) / d["ppf"])
        if d["mode"] == "move":
            if d["track"] == "main" and dframes != 0:
                self.reorderRequested.emit(d["id"], self._drop_index(self._cursor_x,
                                                                     d["id"]))
        elif d["mode"] == "trim_l" and dframes != 0:
            self.trimRequested.emit(d["id"], max(0, d["in"] + dframes), -1)
        elif d["mode"] == "trim_r" and dframes != 0:
            self.trimRequested.emit(d["id"], -1, d["out"] + dframes)
        self.update()

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace) and self._selected:
            self.removeRequested.emit(self._selected)
        else:
            super().keyPressEvent(event)

    def contextMenuEvent(self, event) -> None:
        from PySide6.QtWidgets import QMenu
        hit = self._hit(event.pos())
        if not hit:
            return
        menu = QMenu(self)
        act = menu.addAction("Remove from timeline")
        if menu.exec(event.globalPos()) == act:
            self.removeRequested.emit(hit[1])


# --------------------------------------------------------------------------- #
# Preview + transport
# --------------------------------------------------------------------------- #

class PreviewWidget(QWidget):
    frameChanged = Signal(int)

    def __init__(self):
        super().__init__()
        self._frames: List[QImage] = []
        self._fps = 30.0
        self._idx = 0
        self._restore_frac: Optional[float] = None
        self._streaming = False

        layout = QVBoxLayout(self)
        layout.addWidget(_heading("Preview"))

        self.view = QLabel("No preview yet — add clips, then Refresh Preview.")
        self.view.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.view.setMinimumSize(360, 240)
        self.view.setStyleSheet(
            "background:#0d0f12; color:#6a7280; border-radius:4px;")
        self.view.setSizePolicy(QSizePolicy.Policy.Expanding,
                                QSizePolicy.Policy.Expanding)
        layout.addWidget(self.view, 1)

        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setEnabled(False)
        self.slider.valueChanged.connect(self._on_slider)
        layout.addWidget(self.slider)

        controls = QHBoxLayout()
        self.play_btn = QPushButton("Play")
        self.play_btn.setEnabled(False)
        self.play_btn.clicked.connect(self._toggle)
        self.frame_lbl = QLabel("0 / 0")
        controls.addWidget(self.play_btn)
        controls.addStretch(1)
        controls.addWidget(self.frame_lbl)
        layout.addLayout(controls)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._advance)

    def _current_fraction(self) -> Optional[float]:
        n = len(self._frames)
        return (self._idx / (n - 1)) if n > 1 else None

    def set_frames(self, frames: List[QImage], fps: float) -> None:
        """Replace all frames at once, keeping the scrub position if possible."""
        frac = self._current_fraction()
        self.timer.stop()
        self.play_btn.setText("Play")
        self._frames = frames
        self._fps = fps or 30.0
        has = bool(frames)
        self.slider.setEnabled(has)
        self.play_btn.setEnabled(has)
        self.slider.blockSignals(True)
        self.slider.setRange(0, max(0, len(frames) - 1))
        self.slider.blockSignals(False)
        if has:
            tgt = round(frac * (len(frames) - 1)) if frac is not None else 0
            self._idx = max(0, min(tgt, len(frames) - 1))
            self._show(self._idx)
        else:
            self._idx = 0
            self.view.setText("Preview produced no frames.")

    # -- streaming (frames arrive in batches as they decode) ---------------- #

    def begin_stream(self, total: int, fps: float) -> None:
        self._restore_frac = self._current_fraction()   # remember where we were
        self.timer.stop()
        self.play_btn.setText("Play")
        self._frames = []
        self._fps = fps or 30.0
        self._idx = 0
        self._streaming = True
        self.slider.setEnabled(False)
        self.play_btn.setEnabled(False)

    def append_frames(self, frames: List[QImage]) -> None:
        if not frames:
            return
        was_empty = not self._frames
        self._frames.extend(frames)
        n = len(self._frames)
        self.slider.blockSignals(True)
        self.slider.setRange(0, max(0, n - 1))
        self.slider.blockSignals(False)
        self.slider.setEnabled(True)
        self.play_btn.setEnabled(True)
        if was_empty:
            self._show(0)                                # show something at once
        else:
            self.frame_lbl.setText(f"{self._idx} / {n - 1}")

    def end_stream(self) -> None:
        self._streaming = False
        if not self._frames:
            self.slider.setEnabled(False)
            self.play_btn.setEnabled(False)
            self.view.setText("Preview produced no frames.")
            return
        if self._restore_frac is not None and len(self._frames) > 1:
            self.seek_to(round(self._restore_frac * (len(self._frames) - 1)))
        else:
            self._show(self._idx)
        self._restore_frac = None

    def frame_count(self) -> int:
        return len(self._frames)

    def seek_to(self, frame: int) -> None:
        """Jump to a frame (used by the timeline scrubber)."""
        if self._frames:
            self.slider.setValue(max(0, min(int(frame), len(self._frames) - 1)))

    def _show(self, idx: int) -> None:
        if not self._frames:
            return
        idx = max(0, min(idx, len(self._frames) - 1))
        self._idx = idx
        pix = QPixmap.fromImage(self._frames[idx])
        self.view.setPixmap(pix.scaled(
            self.view.size(), Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation))
        self.frame_lbl.setText(f"{idx} / {len(self._frames) - 1}")
        self.frameChanged.emit(idx)

    def _on_slider(self, value: int) -> None:
        if value != self._idx:
            self._show(value)

    def _toggle(self) -> None:
        if self.timer.isActive():
            self.timer.stop()
            self.play_btn.setText("Play")
        elif self._frames:
            self.timer.start(int(1000 / max(1.0, self._fps)))
            self.play_btn.setText("Pause")

    def _advance(self) -> None:
        if not self._frames:
            return
        nxt = (self._idx + 1) % len(self._frames)
        self.slider.setValue(nxt)        # triggers _show via _on_slider

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self._frames:
            self._show(self._idx)


# --------------------------------------------------------------------------- #
# Inspector (schema-driven)
# --------------------------------------------------------------------------- #

class InspectorPanel(QWidget):
    applyRequested = Signal(str, dict)        # mode, params
    bakeRequested = Signal()
    revertRequested = Signal()

    def __init__(self):
        super().__init__()
        self._getters: Dict[str, Callable] = {}
        self._clip_ref_combos: List[QComboBox] = []
        self._motion_labels: List[str] = []
        self._clip_id: Optional[str] = None

        layout = QVBoxLayout(self)
        layout.addWidget(_heading("Effect"))

        self.clip_lbl = QLabel("Select a clip on the main track.")
        self.clip_lbl.setWordWrap(True)
        layout.addWidget(self.clip_lbl)

        self.mode_combo = QComboBox()
        self.mode_combo.addItems(available_modes())
        self.mode_combo.currentTextChanged.connect(self._rebuild_params)
        layout.addWidget(self.mode_combo)

        self.desc_lbl = QLabel("")
        self.desc_lbl.setWordWrap(True)
        self.desc_lbl.setStyleSheet("color:#8a92a6;")
        layout.addWidget(self.desc_lbl)

        self._form_host = QWidget()
        self._form = QFormLayout(self._form_host)
        self._form.setContentsMargins(0, 4, 0, 4)
        layout.addWidget(self._form_host)

        self.apply_btn = QPushButton("Apply && Preview")
        self.apply_btn.clicked.connect(self._emit_apply)
        layout.addWidget(self.apply_btn)

        row = QHBoxLayout()
        self.bake_btn = QPushButton("Bake")
        self.bake_btn.clicked.connect(lambda: self.bakeRequested.emit())
        self.revert_btn = QPushButton("Revert bake")
        self.revert_btn.clicked.connect(lambda: self.revertRequested.emit())
        row.addWidget(self.bake_btn)
        row.addWidget(self.revert_btn)
        layout.addLayout(row)
        layout.addStretch(1)

        self.set_enabled_for_clip(None, None)
        self._rebuild_params(self.mode_combo.currentText())

    # -- external state ----------------------------------------------------- #

    def set_motion_labels(self, labels: List[str]) -> None:
        self._motion_labels = list(labels)
        for combo in self._clip_ref_combos:
            current = combo.currentText()
            combo.clear()
            combo.addItems(self._motion_labels)
            if current in self._motion_labels:
                combo.setCurrentText(current)

    def set_enabled_for_clip(self, clip_id: Optional[str], label: Optional[str],
                             mode: Optional[str] = None,
                             params: Optional[dict] = None) -> None:
        self._clip_id = clip_id
        on = clip_id is not None
        self.mode_combo.setEnabled(on)
        self.apply_btn.setEnabled(on)
        self.bake_btn.setEnabled(on)
        self._form_host.setEnabled(on)
        if on:
            self.clip_lbl.setText(f"Clip: <b>{label}</b>")
            if mode and mode != self.mode_combo.currentText():
                self.mode_combo.setCurrentText(mode)   # triggers rebuild
            if params:
                self._apply_values(params)
        else:
            self.clip_lbl.setText("Select a clip on the main track.")

    # -- params form -------------------------------------------------------- #

    def _rebuild_params(self, mode_name: str) -> None:
        while self._form.rowCount():
            self._form.removeRow(0)
        self._getters = {}
        self._clip_ref_combos = []
        if not mode_name:
            return
        mode = get_mode(mode_name)
        self.desc_lbl.setText(mode.description)
        for param in mode.params:
            widget, getter = build_param_widget(param)
            if param.kind == "clip_ref":
                widget.addItems(self._motion_labels)
                self._clip_ref_combos.append(widget)
            if param.help:
                widget.setToolTip(param.help)
            self._form.addRow(param.label or param.name, widget)
            self._getters[param.name] = getter

    def _apply_values(self, params: dict) -> None:
        # Reflect an existing op's params back into the controls (best effort).
        for name, getter in self._getters.items():
            if name not in params:
                continue
            value = params[name]
            w = getattr(getter, "__self__", None)   # the widget bound to the getter
            if isinstance(w, QCheckBox):
                w.setChecked(bool(value))
            elif isinstance(w, QSpinBox):
                w.setValue(int(value))
            elif isinstance(w, QDoubleSpinBox):
                w.setValue(float(value))
            elif isinstance(w, QComboBox):
                w.setCurrentText(str(value))
            elif isinstance(w, QLineEdit):
                w.setText(str(value))

    def _emit_apply(self) -> None:
        params = {name: getter() for name, getter in self._getters.items()}
        # validate required clip_ref params
        mode = get_mode(self.mode_combo.currentText())
        for param in mode.params:
            if param.kind == "clip_ref" and not params.get(param.name):
                self.clip_lbl.setText(
                    "<span style='color:#ff5470'>Import a motion clip and add "
                    "it to the motion track first.</span>")
                return
        self.applyRequested.emit(self.mode_combo.currentText(), params)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _heading(text: str) -> QLabel:
    lbl = QLabel(text)
    f = QFont()
    f.setBold(True)
    f.setPointSize(10)
    lbl.setFont(f)
    lbl.setStyleSheet("color:#cdd3df; padding:2px 0 4px 0;")
    return lbl
