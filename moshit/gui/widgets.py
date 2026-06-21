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

def _make_spin(param: Param):
    if param.kind == "int":
        s = QSpinBox()
        s.setRange(int(param.lo) if param.lo is not None else -1_000_000,
                   int(param.hi) if param.hi is not None else 1_000_000)
    else:
        s = QDoubleSpinBox()
        s.setDecimals(2)
        s.setSingleStep(0.1)
        s.setRange(float(param.lo) if param.lo is not None else -1e6,
                   float(param.hi) if param.hi is not None else 1e6)
    return s


class AutoParamWidget(QWidget):
    """A numeric control with an **A**(utomate) toggle. Off, it's a plain value;
    on, it becomes a *start → end* ramp and reports a keyframe spec dict."""

    def __init__(self, param: Param):
        super().__init__()
        self._is_int = param.kind == "int"
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(3)
        self.auto_chk = QCheckBox("A")
        self.auto_chk.setToolTip("Automate: ramp this value across the clip "
                                 "(start → end)")
        self.start = _make_spin(param)
        default = param.default if param.default is not None else 0
        self.start.setValue(int(default) if self._is_int else float(default))
        self.arrow = QLabel("→")
        self.end = _make_spin(param)
        self.end.setValue(self.start.value())
        for w in (self.auto_chk, self.start, self.arrow, self.end):
            lay.addWidget(w)
        self.auto_chk.toggled.connect(self._sync)
        self._sync(False)

    def _sync(self, on: bool) -> None:
        self.arrow.setVisible(on)
        self.end.setVisible(on)

    def get_value(self):
        if not self.auto_chk.isChecked():
            return self.start.value()
        return {"__auto__": True, "interp": "linear",
                "keys": [[0.0, self.start.value()], [1.0, self.end.value()]]}

    def set_value(self, value) -> None:
        coerce = int if self._is_int else float
        if isinstance(value, dict) and value.get("__auto__"):
            keys = sorted(value.get("keys", []), key=lambda k: k[0])
            self.auto_chk.setChecked(True)
            if keys:
                self.start.setValue(coerce(keys[0][1]))
                self.end.setValue(coerce(keys[-1][1]))
        else:
            self.auto_chk.setChecked(False)
            self.start.setValue(coerce(value))
        self._sync(self.auto_chk.isChecked())


def build_param_widget(param: Param) -> Tuple[QWidget, Callable]:
    """Return (control, getter) for a mode parameter. getter() -> current value."""
    if param.kind in ("int", "float") and getattr(param, "automatable", False):
        w = AutoParamWidget(param)
        return w, w.get_value
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


def _set_param_value(w, value) -> None:
    """Reflect a stored param value back into its control (best effort)."""
    if isinstance(w, AutoParamWidget):
        w.set_value(value)
    elif isinstance(w, QCheckBox):
        w.setChecked(bool(value))
    elif isinstance(w, QSpinBox):
        w.setValue(int(value))
    elif isinstance(w, QDoubleSpinBox):
        w.setValue(float(value))
    elif isinstance(w, QComboBox):
        w.setCurrentText(str(value))
    elif isinstance(w, QLineEdit):
        w.setText(str(value))


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
    duplicateRequested = Signal(str)               # clip_id

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
        badges = []
        if abs(getattr(clip, "speed", 1.0) - 1.0) > 1e-6:
            badges.append(f"{clip.speed:g}×")
        if getattr(clip, "reverse", False):
            badges.append("⇄")
        if getattr(clip, "fade_in", 0):
            badges.append("⊳")
        if getattr(clip, "fade_out", 0):
            badges.append("⊲")
        suffix = ("   " + " ".join(badges)) if badges else ""
        p.setPen(QColor("#eef1f6"))
        p.drawText(rect.adjusted(5, 0, -3, 0),
                   Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                   f"{label}  {length}f{suffix}")
        # crossfade-from-previous marker: a small wedge on the left edge
        if getattr(clip, "transition_in", 0) and not motion:
            p.setBrush(QColor("#ffd166"))
            p.setPen(Qt.PenStyle.NoPen)
            tl = rect.topLeft()
            p.drawPolygon(QPolygon([QPoint(tl.x(), tl.y()),
                                    QPoint(tl.x() + 11, tl.y()),
                                    QPoint(tl.x(), tl.y() + 11)]))
            p.setBrush(Qt.BrushStyle.NoBrush)

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

    def request_split_at_playhead(self) -> None:
        """Split the main-track clip under the playhead at its current frame.

        The standard NLE 'split at the cursor' action: mirrors a Cut-tool click
        located at the playhead, so it stays consistent with the ruler.
        """
        if not self._project or not self._hits:
            return
        x0, x1 = self._track_x()
        px = int(x0 + self._play_frac * max(1, x1 - x0))
        for rect, cid, track in self._hits:
            if track == "main" and rect.left() <= px <= rect.right():
                offset = round((px - rect.left()) / self._ppf())
                if offset > 0:
                    self.splitRequested.emit(cid, offset)
                return

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
        clip_id, track = hit[1], hit[2]
        self._selected = clip_id
        self.clipSelected.emit(clip_id)
        self.update()
        menu = QMenu(self)
        act_dup = menu.addAction("Duplicate clip")
        act_split = None
        if track == "main":
            act_split = menu.addAction("Split at playhead")
        menu.addSeparator()
        act_remove = menu.addAction("Remove from timeline")
        chosen = menu.exec(event.globalPos())
        if chosen is None:
            return
        if chosen == act_dup:
            self.duplicateRequested.emit(clip_id)
        elif chosen == act_split:
            self.request_split_at_playhead()
        elif chosen == act_remove:
            self.removeRequested.emit(clip_id)


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
        self._loop = False

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
        controls.setSpacing(4)

        def _tbtn(text: str, tip: str, slot) -> QPushButton:
            b = QPushButton(text)
            b.setToolTip(tip)
            b.setMaximumWidth(34)
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)   # keep keyboard shortcuts working
            b.setEnabled(False)
            b.clicked.connect(slot)
            return b

        self.start_btn = _tbtn("⏮", "Jump to start (Home)", self.go_start)
        self.prev_btn = _tbtn("◀", "Previous frame (,)", lambda: self.step(-1))
        self.play_btn = QPushButton("Play")
        self.play_btn.setToolTip("Play / pause (Space)")
        self.play_btn.setEnabled(False)
        self.play_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.play_btn.clicked.connect(self.toggle)
        self.next_btn = _tbtn("▶", "Next frame (.)", lambda: self.step(1))
        self.end_btn = _tbtn("⏭", "Jump to end (End)", self.go_end)

        self.loop_chk = QCheckBox("Loop")
        self.loop_chk.setToolTip("Repeat playback from the start")
        self.loop_chk.toggled.connect(self._set_loop)

        self.frame_lbl = QLabel("0 / 0")
        for w in (self.start_btn, self.prev_btn, self.play_btn,
                  self.next_btn, self.end_btn):
            controls.addWidget(w)
        controls.addSpacing(8)
        controls.addWidget(self.loop_chk)
        controls.addStretch(1)
        controls.addWidget(self.frame_lbl)
        layout.addLayout(controls)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._advance)

    def _current_fraction(self) -> Optional[float]:
        n = len(self._frames)
        return (self._idx / (n - 1)) if n > 1 else None

    def _set_transport_enabled(self, on: bool) -> None:
        for b in (self.start_btn, self.prev_btn, self.play_btn,
                  self.next_btn, self.end_btn):
            b.setEnabled(on)

    def _set_loop(self, on: bool) -> None:
        self._loop = bool(on)

    def _timecode(self, frame: int) -> str:
        fps = max(1.0, self._fps)
        per_sec = max(1, round(fps))
        frame = max(0, int(frame))
        secs, ff = divmod(frame, per_sec)
        return f"{secs // 60:02d}:{secs % 60:02d}:{ff:02d}"

    def _update_frame_label(self) -> None:
        n = len(self._frames)
        last = max(0, n - 1)
        self.frame_lbl.setText(
            f"{self._idx} / {last}   ·   "
            f"{self._timecode(self._idx)} / {self._timecode(last)}")

    # -- public transport (also driven by window shortcuts) ----------------- #

    def toggle(self) -> None:
        if self.timer.isActive():
            self.timer.stop()
            self.play_btn.setText("Play")
        elif self._frames:
            self.timer.start(int(1000 / max(1.0, self._fps)))
            self.play_btn.setText("Pause")

    def step(self, delta: int) -> None:
        if not self._frames:
            return
        self.timer.stop()
        self.play_btn.setText("Play")
        self.slider.setValue(max(0, min(self._idx + int(delta),
                                        len(self._frames) - 1)))

    def go_start(self) -> None:
        if self._frames:
            self.slider.setValue(0)

    def go_end(self) -> None:
        if self._frames:
            self.slider.setValue(len(self._frames) - 1)

    def current_index(self) -> int:
        return self._idx

    def current_image(self) -> Optional[QImage]:
        return self._frames[self._idx] if self._frames else None

    def set_frames(self, frames: List[QImage], fps: float) -> None:
        """Replace all frames at once, keeping the scrub position if possible."""
        frac = self._current_fraction()
        self.timer.stop()
        self.play_btn.setText("Play")
        self._frames = frames
        self._fps = fps or 30.0
        has = bool(frames)
        self.slider.setEnabled(has)
        self._set_transport_enabled(has)
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
        self._set_transport_enabled(False)

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
        self._set_transport_enabled(True)
        if was_empty:
            self._show(0)                                # show something at once
        else:
            self._update_frame_label()

    def end_stream(self) -> None:
        self._streaming = False
        if not self._frames:
            self.slider.setEnabled(False)
            self._set_transport_enabled(False)
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
        self._update_frame_label()
        self.frameChanged.emit(idx)

    def _on_slider(self, value: int) -> None:
        if value != self._idx:
            self._show(value)

    def _advance(self) -> None:
        if not self._frames:
            return
        if self._idx + 1 >= len(self._frames):       # reached the end
            if self._loop:
                self.slider.setValue(0)
            else:
                self.timer.stop()
                self.play_btn.setText("Play")
            return
        self.slider.setValue(self._idx + 1)          # triggers _show via _on_slider

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self._frames:
            self._show(self._idx)


# --------------------------------------------------------------------------- #
# Inspector (schema-driven)
# --------------------------------------------------------------------------- #

class InspectorPanel(QWidget):
    effectAddRequested = Signal(str, dict, object)        # mode, params, region
    effectUpdateRequested = Signal(str, str, dict, object)  # op_id, mode, params, region
    effectRemoveRequested = Signal(str)            # op_id
    effectMoveRequested = Signal(str, int)         # op_id, delta (-1 up / +1 down)
    effectEnabledChanged = Signal(str, bool)       # op_id, enabled
    presetSaveRequested = Signal()                 # save the current stack
    presetApplyRequested = Signal(str)             # preset name
    presetDeleteRequested = Signal(str)            # preset name
    pixelFxAddRequested = Signal(str)              # pixel mode name
    pixelFxRemoveRequested = Signal(int)           # index in the clip's pixel list
    pixelFxParamsChanged = Signal(int, dict)       # index, params
    bakeRequested = Signal()
    revertRequested = Signal()
    clipPropsChanged = Signal(dict)                # speed/reverse/fades/transition

    def __init__(self):
        super().__init__()
        self._getters: Dict[str, Callable] = {}
        self._param_widgets: Dict[str, QWidget] = {}
        self._clip_ref_combos: List[QComboBox] = []
        self._motion_labels: List[str] = []
        self._clip_id: Optional[str] = None
        self._populating = False
        self._effects: List[dict] = []
        self._selected_op: Optional[str] = None
        self._pixel_fx: List[dict] = []
        self._pixel_getters: Dict[str, Callable] = {}
        self._pixel_sel = -1

        layout = QVBoxLayout(self)
        layout.addWidget(_heading("Inspector"))

        self.clip_lbl = QLabel("Select a clip on the main track.")
        self.clip_lbl.setWordWrap(True)
        layout.addWidget(self.clip_lbl)
        layout.addWidget(self._build_clip_group())
        layout.addWidget(self._build_pixel_group())

        layout.addWidget(_heading("Effects (top → bottom)"))
        self.effect_list = QListWidget()
        self.effect_list.setMaximumHeight(108)
        self.effect_list.setToolTip("This clip's effect stack, applied top to "
                                    "bottom. Toggle the checkbox to enable/disable.")
        self.effect_list.currentRowChanged.connect(self._on_effect_row)
        self.effect_list.itemChanged.connect(self._on_effect_item_changed)
        layout.addWidget(self.effect_list)

        stack_btns = QHBoxLayout()
        self.add_btn = QPushButton("+ Add")
        self.add_btn.setToolTip("Add the effect configured below to the stack")
        self.add_btn.clicked.connect(self._emit_add)
        self.remove_btn = QPushButton("− Remove")
        self.remove_btn.clicked.connect(self._emit_remove)
        self.up_btn = QPushButton("↑")
        self.up_btn.setMaximumWidth(32)
        self.up_btn.clicked.connect(lambda: self._emit_move(-1))
        self.down_btn = QPushButton("↓")
        self.down_btn.setMaximumWidth(32)
        self.down_btn.clicked.connect(lambda: self._emit_move(1))
        for b in (self.add_btn, self.remove_btn, self.up_btn, self.down_btn):
            stack_btns.addWidget(b)
        layout.addLayout(stack_btns)

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

        layout.addWidget(self._build_region_row())

        editor_row = QHBoxLayout()
        self.apply_btn = QPushButton("Apply to selected effect")
        self.apply_btn.clicked.connect(self._emit_update)
        self.random_btn = QPushButton("🎲")
        self.random_btn.setMaximumWidth(36)
        self.random_btn.setToolTip("Randomise the parameters below")
        self.random_btn.clicked.connect(self._randomize_editor)
        editor_row.addWidget(self.apply_btn)
        editor_row.addWidget(self.random_btn)
        layout.addLayout(editor_row)

        preset_row = QHBoxLayout()
        self.preset_combo = QComboBox()
        self.preset_combo.setToolTip("Saved effect-stack presets")
        self.preset_save_btn = QPushButton("Save…")
        self.preset_save_btn.setToolTip("Save this clip's effect stack as a preset")
        self.preset_save_btn.clicked.connect(lambda: self.presetSaveRequested.emit())
        self.preset_apply_btn = QPushButton("Apply")
        self.preset_apply_btn.setToolTip("Replace this clip's stack with the preset")
        self.preset_apply_btn.clicked.connect(self._emit_apply_preset)
        self.preset_del_btn = QPushButton("✕")
        self.preset_del_btn.setMaximumWidth(30)
        self.preset_del_btn.setToolTip("Delete the selected preset")
        self.preset_del_btn.clicked.connect(self._emit_delete_preset)
        preset_row.addWidget(QLabel("Preset"))
        preset_row.addWidget(self.preset_combo, 1)
        preset_row.addWidget(self.preset_save_btn)
        preset_row.addWidget(self.preset_apply_btn)
        preset_row.addWidget(self.preset_del_btn)
        layout.addLayout(preset_row)

        row = QHBoxLayout()
        self.bake_btn = QPushButton("Bake stack")
        self.bake_btn.clicked.connect(lambda: self.bakeRequested.emit())
        self.revert_btn = QPushButton("Revert bake")
        self.revert_btn.clicked.connect(lambda: self.revertRequested.emit())
        row.addWidget(self.bake_btn)
        row.addWidget(self.revert_btn)
        layout.addLayout(row)
        layout.addStretch(1)

        self.set_enabled_for_clip(None, None)
        self._rebuild_params(self.mode_combo.currentText())

    # -- clip finishing (speed / reverse / fades / crossfade) --------------- #

    def _build_clip_group(self) -> QWidget:
        group = QWidget()
        form = QFormLayout(group)
        form.setContentsMargins(0, 2, 0, 6)

        self.speed_spin = QDoubleSpinBox()
        self.speed_spin.setRange(0.1, 8.0)
        self.speed_spin.setSingleStep(0.25)
        self.speed_spin.setValue(1.0)
        self.speed_spin.setToolTip("Playback speed (2 = twice as fast, 0.5 = half)")
        self.reverse_chk = QCheckBox("Reverse")
        self.fadein_spin = QSpinBox()
        self.fadein_spin.setRange(0, 600)
        self.fadein_spin.setSuffix(" f")
        self.fadeout_spin = QSpinBox()
        self.fadeout_spin.setRange(0, 600)
        self.fadeout_spin.setSuffix(" f")
        self.xfade_spin = QSpinBox()
        self.xfade_spin.setRange(0, 600)
        self.xfade_spin.setSuffix(" f")
        self.xfade_spin.setToolTip("Crossfade in from the previous clip over this "
                                   "many frames (0 = hard cut)")

        form.addRow("Speed ×", self.speed_spin)
        form.addRow("", self.reverse_chk)
        form.addRow("Fade in", self.fadein_spin)
        form.addRow("Fade out", self.fadeout_spin)
        form.addRow("Crossfade ⟵", self.xfade_spin)

        self.speed_spin.valueChanged.connect(self._emit_clip_props)
        self.reverse_chk.toggled.connect(self._emit_clip_props)
        self.fadein_spin.valueChanged.connect(self._emit_clip_props)
        self.fadeout_spin.valueChanged.connect(self._emit_clip_props)
        self.xfade_spin.valueChanged.connect(self._emit_clip_props)
        self._clip_group = group
        return group

    def _emit_clip_props(self) -> None:
        if self._populating:                  # don't echo back our own population
            return
        self.clipPropsChanged.emit({
            "speed": self.speed_spin.value(),
            "reverse": self.reverse_chk.isChecked(),
            "fade_in": self.fadein_spin.value(),
            "fade_out": self.fadeout_spin.value(),
            "transition_in": self.xfade_spin.value(),
        })

    def _populate_clip_props(self, clip) -> None:
        self._populating = True
        self.speed_spin.setValue(float(getattr(clip, "speed", 1.0)))
        self.reverse_chk.setChecked(bool(getattr(clip, "reverse", False)))
        self.fadein_spin.setValue(int(getattr(clip, "fade_in", 0)))
        self.fadeout_spin.setValue(int(getattr(clip, "fade_out", 0)))
        self.xfade_spin.setValue(int(getattr(clip, "transition_in", 0)))
        self._populating = False

    # -- pixel FX (clip finishing) ------------------------------------------ #

    def _build_pixel_group(self) -> QWidget:
        from ..modes import available_pixel_modes
        group = QWidget()
        v = QVBoxLayout(group)
        v.setContentsMargins(0, 0, 0, 4)
        v.setSpacing(3)
        v.addWidget(_heading("Pixel FX"))

        add_row = QHBoxLayout()
        self.pixel_add_combo = QComboBox()
        self.pixel_add_combo.addItems(available_pixel_modes())
        self.pixel_add_btn = QPushButton("+ Add")
        self.pixel_add_btn.clicked.connect(self._emit_pixel_add)
        add_row.addWidget(self.pixel_add_combo, 1)
        add_row.addWidget(self.pixel_add_btn)
        v.addLayout(add_row)

        self.pixel_list = QListWidget()
        self.pixel_list.setMaximumHeight(72)
        self.pixel_list.setToolTip("Pixel filters, applied after the mosh stack "
                                   "and the speed/fade finishing.")
        self.pixel_list.currentRowChanged.connect(self._on_pixel_row)
        v.addWidget(self.pixel_list)

        self._pixel_form_host = QWidget()
        self._pixel_form = QFormLayout(self._pixel_form_host)
        self._pixel_form.setContentsMargins(0, 2, 0, 2)
        v.addWidget(self._pixel_form_host)

        btns = QHBoxLayout()
        self.pixel_apply_btn = QPushButton("Apply pixel FX")
        self.pixel_apply_btn.clicked.connect(self._emit_pixel_params)
        self.pixel_remove_btn = QPushButton("− Remove")
        self.pixel_remove_btn.clicked.connect(self._emit_pixel_remove)
        btns.addWidget(self.pixel_apply_btn)
        btns.addWidget(self.pixel_remove_btn)
        v.addLayout(btns)
        self._pixel_group = group
        return group

    def set_clip_pixel_fx(self, pfx: List[dict]) -> None:
        self._pixel_fx = [dict(p) for p in (pfx or [])]
        prev = self._pixel_sel
        self._populating = True
        self.pixel_list.clear()
        for pe in self._pixel_fx:
            self.pixel_list.addItem(pe.get("name", ""))
        self._populating = False
        row = (prev if 0 <= prev < len(self._pixel_fx)
               else (len(self._pixel_fx) - 1 if self._pixel_fx else -1))
        if row >= 0:
            self.pixel_list.setCurrentRow(row)    # -> _on_pixel_row builds the form
        else:
            self._pixel_sel = -1
            self._rebuild_pixel_params(None)
            self._update_pixel_buttons()

    def _on_pixel_row(self, row: int) -> None:
        if self._populating:
            return
        if 0 <= row < len(self._pixel_fx):
            self._pixel_sel = row
            pe = self._pixel_fx[row]
            self._rebuild_pixel_params(pe.get("name"), pe.get("params") or {})
        else:
            self._pixel_sel = -1
            self._rebuild_pixel_params(None)
        self._update_pixel_buttons()

    def _rebuild_pixel_params(self, name, params=None) -> None:
        while self._pixel_form.rowCount():
            self._pixel_form.removeRow(0)
        self._pixel_getters = {}
        if not name:
            return
        from ..modes import get_pixel_mode
        self._populating = True
        for p in get_pixel_mode(name).params:
            widget, getter = build_param_widget(p)
            if p.help:
                widget.setToolTip(p.help)
            self._pixel_form.addRow(p.label or p.name, widget)
            self._pixel_getters[p.name] = getter
            if params and p.name in params:
                _set_param_value(getattr(getter, "__self__", None), params[p.name])
        self._populating = False

    def _update_pixel_buttons(self) -> None:
        has_sel = (self._clip_id is not None
                   and 0 <= self._pixel_sel < len(self._pixel_fx))
        self.pixel_remove_btn.setEnabled(has_sel)
        self.pixel_apply_btn.setEnabled(has_sel)

    def _emit_pixel_add(self) -> None:
        name = self.pixel_add_combo.currentText()
        if name and self._clip_id is not None:
            self.pixelFxAddRequested.emit(name)

    def _emit_pixel_remove(self) -> None:
        if 0 <= self._pixel_sel < len(self._pixel_fx):
            self.pixelFxRemoveRequested.emit(self._pixel_sel)

    def _emit_pixel_params(self) -> None:
        if not (0 <= self._pixel_sel < len(self._pixel_fx)):
            return
        params = {name: getter() for name, getter in self._pixel_getters.items()}
        self.pixelFxParamsChanged.emit(self._pixel_sel, params)

    # -- effect region (apply to a frame range) ----------------------------- #

    def _build_region_row(self) -> QWidget:
        host = QWidget()
        row = QHBoxLayout(host)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(4)
        self.region_chk = QCheckBox("Limit to frames")
        self.region_chk.setToolTip("Apply this effect to only a frame range of "
                                   "its input (the clip, for the first effect).")
        self.region_start = QSpinBox()
        self.region_start.setRange(0, 1_000_000)
        self.region_end = QSpinBox()
        self.region_end.setRange(0, 1_000_000)
        self.region_end.setSpecialValueText("end")     # 0 shows as "end" (= None)
        self.region_chk.toggled.connect(self._sync_region)
        row.addWidget(self.region_chk)
        row.addWidget(self.region_start)
        row.addWidget(QLabel("–"))
        row.addWidget(self.region_end)
        row.addStretch(1)
        self._region_host = host
        return host

    def _sync_region(self, *_a) -> None:
        on = self.region_chk.isChecked()
        self.region_start.setEnabled(on)
        self.region_end.setEnabled(on)

    def _editor_region(self):
        if not self.region_chk.isChecked():
            return None
        end = self.region_end.value()
        return [self.region_start.value(), (None if end == 0 else end)]

    def _populate_region(self, region) -> None:
        if region:
            self.region_chk.setChecked(True)
            self.region_start.setValue(int(region[0]))
            self.region_end.setValue(0 if region[1] is None else int(region[1]))
        else:
            self.region_chk.setChecked(False)
            self.region_start.setValue(0)
            self.region_end.setValue(0)
        self._sync_region()

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
                             clip=None, effects: Optional[List[dict]] = None) -> None:
        self._clip_id = clip_id
        on = clip_id is not None
        self.mode_combo.setEnabled(on)
        self.bake_btn.setEnabled(on)
        self.random_btn.setEnabled(on)
        self.preset_save_btn.setEnabled(on)
        self.preset_apply_btn.setEnabled(on)
        self._form_host.setEnabled(on)
        self._region_host.setEnabled(on)
        self._clip_group.setEnabled(on)
        self._pixel_group.setEnabled(on)
        self.effect_list.setEnabled(on)
        if on:
            self.clip_lbl.setText(f"Clip: <b>{label}</b>")
            self._populate_clip_props(clip if clip is not None else object())
            self.set_clip_pixel_fx(getattr(clip, "pixel_effects", []))
            self.set_clip_effects(effects or [])
        else:
            self.clip_lbl.setText("Select a clip on the main track.")
            self._populate_clip_props(object())        # reset to defaults
            self.set_clip_pixel_fx([])
            self.set_clip_effects([])
        self._update_stack_buttons()

    # -- effect stack ------------------------------------------------------- #

    def set_clip_effects(self, effects: List[dict]) -> None:
        """Populate the stack list. *effects*: [{id, mode, params, enabled}]."""
        self._effects = list(effects or [])
        prev = self._selected_op
        self._populating = True
        self.effect_list.clear()
        for e in self._effects:
            item = QListWidgetItem(e["mode"])
            item.setData(Qt.ItemDataRole.UserRole, e["id"])
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            enabled = e.get("enabled", True)
            item.setCheckState(Qt.CheckState.Checked if enabled
                               else Qt.CheckState.Unchecked)
            if not enabled:
                item.setForeground(QColor("#6a7280"))
            self.effect_list.addItem(item)
        row = next((i for i, e in enumerate(self._effects) if e["id"] == prev), -1)
        if row < 0 and self._effects:
            row = len(self._effects) - 1          # default to the newest
        self._populating = False
        if row >= 0:
            self.effect_list.setCurrentRow(row)   # -> _on_effect_row populates editor
        else:
            self._selected_op = None
            self._populate_region(None)
            self._update_stack_buttons()

    def _on_effect_row(self, row: int) -> None:
        if self._populating:
            return
        if 0 <= row < len(self._effects):
            e = self._effects[row]
            self._selected_op = e["id"]
            self._load_effect(e["mode"], e["params"])
            self._populate_region(e.get("region"))
        else:
            self._selected_op = None
            self._populate_region(None)
        self._update_stack_buttons()

    def _load_effect(self, mode: str, params: dict) -> None:
        self._populating = True
        if mode and mode != self.mode_combo.currentText():
            self.mode_combo.setCurrentText(mode)   # triggers _rebuild_params
        else:
            self._rebuild_params(self.mode_combo.currentText())
        if params:
            self._apply_values(params)
        self._populating = False

    def _on_effect_item_changed(self, item: QListWidgetItem) -> None:
        if self._populating:
            return
        self.effectEnabledChanged.emit(
            item.data(Qt.ItemDataRole.UserRole),
            item.checkState() == Qt.CheckState.Checked)

    def _update_stack_buttons(self) -> None:
        has_clip = self._clip_id is not None
        has_sel = self._selected_op is not None and has_clip
        self.add_btn.setEnabled(has_clip)
        self.apply_btn.setEnabled(has_sel)
        self.remove_btn.setEnabled(has_sel)
        idx = next((i for i, e in enumerate(self._effects)
                    if e["id"] == self._selected_op), -1)
        self.up_btn.setEnabled(has_sel and idx > 0)
        self.down_btn.setEnabled(has_sel and 0 <= idx < len(self._effects) - 1)

    # -- params form -------------------------------------------------------- #

    def _rebuild_params(self, mode_name: str) -> None:
        while self._form.rowCount():
            self._form.removeRow(0)
        self._getters = {}
        self._param_widgets = {}
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
            self._param_widgets[param.name] = widget

    # -- presets & randomiser ----------------------------------------------- #

    def set_presets(self, names: List[str]) -> None:
        cur = self.preset_combo.currentText()
        self.preset_combo.blockSignals(True)
        self.preset_combo.clear()
        self.preset_combo.addItems(names)
        if cur in names:
            self.preset_combo.setCurrentText(cur)
        self.preset_combo.blockSignals(False)

    def _emit_apply_preset(self) -> None:
        name = self.preset_combo.currentText()
        if name:
            self.presetApplyRequested.emit(name)

    def _emit_delete_preset(self) -> None:
        name = self.preset_combo.currentText()
        if name:
            self.presetDeleteRequested.emit(name)

    def _randomize_editor(self) -> None:
        """Roll random values into the editor's numeric/choice params."""
        import random
        for p in get_mode(self.mode_combo.currentText()).params:
            w = self._param_widgets.get(p.name)
            has_range = p.lo is not None and p.hi is not None
            rand_num = (random.randint(int(p.lo), int(p.hi)) if p.kind == "int"
                        else round(random.uniform(float(p.lo), float(p.hi)), 2)
                        ) if has_range else None
            if isinstance(w, AutoParamWidget):
                if has_range:
                    w.auto_chk.setChecked(False)
                    w.start.setValue(rand_num)
            elif isinstance(w, (QSpinBox, QDoubleSpinBox)):
                if has_range:
                    w.setValue(rand_num)
            elif isinstance(w, QCheckBox):
                w.setChecked(random.random() < 0.5)
            elif isinstance(w, QComboBox) and p.kind == "choice" and p.choices:
                w.setCurrentText(str(random.choice(p.choices)))

    def _apply_values(self, params: dict) -> None:
        # Reflect an existing op's params back into the controls (best effort).
        for name, getter in self._getters.items():
            if name in params:
                _set_param_value(getattr(getter, "__self__", None), params[name])

    def _editor_mode_params(self):
        """Current editor mode + params, or (None, None) if a required motion
        source is missing (with a hint shown)."""
        params = {name: getter() for name, getter in self._getters.items()}
        mode = self.mode_combo.currentText()
        for param in get_mode(mode).params:
            if param.kind == "clip_ref" and not params.get(param.name):
                self.clip_lbl.setText(
                    "<span style='color:#ff5470'>Import a motion clip and add "
                    "it to the motion track first.</span>")
                return None, None
        return mode, params

    def _emit_add(self) -> None:
        mode, params = self._editor_mode_params()
        if mode is not None:
            self.effectAddRequested.emit(mode, params, self._editor_region())

    def _emit_update(self) -> None:
        if not self._selected_op:
            return
        mode, params = self._editor_mode_params()
        if mode is not None:
            self.effectUpdateRequested.emit(self._selected_op, mode, params,
                                            self._editor_region())

    def _emit_remove(self) -> None:
        if self._selected_op:
            self.effectRemoveRequested.emit(self._selected_op)

    def _emit_move(self, delta: int) -> None:
        if self._selected_op:
            self.effectMoveRequested.emit(self._selected_op, delta)


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
