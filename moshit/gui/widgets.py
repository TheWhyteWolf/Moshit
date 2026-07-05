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
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout,
    QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMenu, QMessageBox, QPushButton, QScrollArea, QSizePolicy, QSlider, QSpinBox,
    QVBoxLayout, QWidget,
)

try:
    from PySide6.QtCore import QUrl
    from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
    _HAVE_QT_AUDIO = True
except Exception:                      # QtMultimedia not built / no backend
    _HAVE_QT_AUDIO = False

from ..modes.base import Param, _build_evaluator


# --------------------------------------------------------------------------- #
# Schema -> widget
# --------------------------------------------------------------------------- #

class _SpinBox(QSpinBox):
    """A spin box that selects its text on focus, so clicking in and typing
    *replaces* the value instead of appending to it (which clamped/garbled
    multi-digit entry, e.g. you couldn't cleanly type a new larger number)."""
    def focusInEvent(self, e):                  # noqa: N802 (Qt signature)
        super().focusInEvent(e)
        QTimer.singleShot(0, self.selectAll)


class _DoubleSpinBox(QDoubleSpinBox):
    def focusInEvent(self, e):                  # noqa: N802 (Qt signature)
        super().focusInEvent(e)
        QTimer.singleShot(0, self.selectAll)


def _make_spin(param: Param):
    if param.kind == "int":
        s = _SpinBox()
        s.setRange(int(param.lo) if param.lo is not None else -1_000_000,
                   int(param.hi) if param.hi is not None else 1_000_000)
    else:
        s = _DoubleSpinBox()
        s.setDecimals(2)
        s.setSingleStep(0.1)
        s.setRange(float(param.lo) if param.lo is not None else -1e6,
                   float(param.hi) if param.hi is not None else 1e6)
    return s


class _CurvePreview(QWidget):
    """Tiny live plot of an automation curve (used by the keyframe dialog)."""

    def __init__(self, get_spec: Callable, lo, hi):
        super().__init__()
        self._get = get_spec
        self._lo, self._hi = lo, hi
        self.setMinimumHeight(90)

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#14171c"))
        spec = self._get()
        keys = spec.get("keys", [])
        if not keys:
            p.end()
            return
        vals = [k[1] for k in keys]
        lo = self._lo if self._lo is not None else min(vals)
        hi = self._hi if self._hi is not None else max(vals)
        if hi <= lo:
            hi = lo + 1.0
        ox, oy = 5, 5
        w, h = self.width() - 10, self.height() - 10
        ev = _build_evaluator(spec)

        def xy(pos, val):
            return (int(ox + max(0.0, min(1.0, pos)) * w),
                    int(oy + h - (val - lo) / (hi - lo) * h))

        poly = QPolygon([QPoint(*xy(i / (w - 1), ev(i / (w - 1))))
                         for i in range(max(2, w))])
        p.setPen(QColor("#9fb4d6"))
        p.drawPolyline(poly)
        p.setPen(QColor("#ff5470"))
        p.setBrush(QColor("#ff5470"))
        for k in keys:
            p.drawEllipse(QPoint(*xy(k[0], k[1])), 3, 3)
        p.end()


class KeyframeDialog(QDialog):
    """Edit an automation curve: any number of keyframes plus an easing mode,
    with a live preview."""

    def __init__(self, parent, param: Param, spec: dict):
        super().__init__(parent)
        self.setWindowTitle(f"Automate: {param.label or param.name}")
        self._param = param
        self._is_int = param.kind == "int"
        self._lo = param.lo
        self._hi = param.hi

        v = QVBoxLayout(self)
        self.preview = _CurvePreview(self._collect, self._lo, self._hi)
        v.addWidget(self.preview)

        top = QHBoxLayout()
        top.addWidget(QLabel("Keyframes  (pos · value · ease→next)"))
        top.addStretch(1)
        add_btn = QPushButton("+ Keyframe")
        add_btn.clicked.connect(lambda: self._add_row(0.5, self._mid(), "linear"))
        top.addWidget(add_btn)
        v.addLayout(top)

        self._rows_host = QWidget()
        self._rows = QVBoxLayout(self._rows_host)
        self._rows.setContentsMargins(0, 0, 0, 0)
        self._rows.setSpacing(2)
        v.addWidget(self._rows_host)

        default = spec.get("interp", "linear")
        keys = sorted(spec.get("keys") or [[0.0, self._mid()], [1.0, self._mid()]],
                      key=lambda k: k[0])
        for k in keys:
            self._add_row(k[0], k[1], k[2] if len(k) > 2 else default)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                   | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        v.addWidget(buttons)

    def _mid(self):
        if self._lo is not None and self._hi is not None:
            m = (self._lo + self._hi) / 2
            return int(m) if self._is_int else round(m, 2)
        return 0

    def _add_row(self, pos, val, ease="linear") -> None:
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        ps = QDoubleSpinBox()
        ps.setRange(0.0, 1.0)
        ps.setSingleStep(0.05)
        ps.setDecimals(2)
        ps.setValue(float(pos))
        vs = _make_spin(self._param)
        vs.setValue(int(val) if self._is_int else float(val))
        es = QComboBox()
        es.addItems(["linear", "smooth", "hold"])
        es.setCurrentText(ease if ease in ("linear", "smooth", "hold") else "linear")
        es.setToolTip("Easing from this keyframe to the next")
        rm = QPushButton("✕")
        rm.setMaximumWidth(28)
        ps.valueChanged.connect(lambda *_: self.preview.update())
        vs.valueChanged.connect(lambda *_: self.preview.update())
        es.currentTextChanged.connect(lambda *_: self.preview.update())
        rm.clicked.connect(lambda: self._remove_row(row))
        h.addWidget(ps)
        h.addWidget(vs, 1)
        h.addWidget(es)
        h.addWidget(rm)
        row._pos, row._val, row._ease = ps, vs, es
        self._rows.addWidget(row)
        self.preview.update()

    def _remove_row(self, row) -> None:
        if self._rows.count() <= 2:           # keep at least two keyframes
            return
        row.setParent(None)
        row.deleteLater()
        self.preview.update()

    def _collect(self) -> dict:
        keys = []
        for i in range(self._rows.count()):
            row = self._rows.itemAt(i).widget()
            if row is not None:
                keys.append([row._pos.value(), row._val.value(),
                             row._ease.currentText()])
        # interp mirrors the first key's easing for any curve-level consumer
        return {"__auto__": True, "interp": keys[0][2] if keys else "linear",
                "keys": keys}

    def values(self) -> dict:
        spec = self._collect()
        spec["keys"] = sorted(spec["keys"], key=lambda k: k[0])
        return spec


class AutoParamWidget(QWidget):
    """A numeric control with an **A**(utomate) toggle. Off, it's a plain value;
    on, it reports a keyframe spec (edited via **Curve…** -- any number of
    keyframes with linear/hold/smooth easing)."""

    beatsRequested = Signal()                 # fill keyframes from the audio beats
    changed = Signal()                        # any value/automation edit (live preview)

    def __init__(self, param: Param):
        super().__init__()
        self._param = param
        self._is_int = param.kind == "int"
        default = param.default if param.default is not None else 0
        self._default = int(default) if self._is_int else float(default)
        self._spec = None                     # set when automation is enabled

        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(3)
        self.auto_chk = QCheckBox("A")
        self.auto_chk.setToolTip("Automate this value across the clip")
        self.value = _make_spin(param)
        self.value.setValue(self._default)
        self.value.valueChanged.connect(self._on_value)
        self.curve_btn = QPushButton("Curve…")
        self.curve_btn.setMaximumWidth(60)
        self.curve_btn.setToolTip("Edit keyframes (multi-point + easing)")
        self.curve_btn.clicked.connect(self._edit_curve)
        self.beats_btn = QPushButton("♪")
        self.beats_btn.setMaximumWidth(26)
        self.beats_btn.setToolTip("Pulse this value on the audio beats")
        self.beats_btn.clicked.connect(self.beatsRequested)
        for w in (self.auto_chk, self.value, self.curve_btn, self.beats_btn):
            lay.addWidget(w)
        self.auto_chk.toggled.connect(self._on_toggle)
        self._sync()

    def _ramp(self, value):
        return {"__auto__": True, "interp": "linear",
                "keys": [[0.0, value], [1.0, value]]}

    def _sync(self) -> None:
        on = self.auto_chk.isChecked()
        self.curve_btn.setVisible(on)
        self.beats_btn.setVisible(on)

    def beat_range(self):
        """(low, high, is_int) the beat pulse should swing between."""
        p = self._param
        low = p.lo if p.lo is not None else 0
        high = p.hi if p.hi is not None else max(self._default, low + 1)
        return low, high, self._is_int

    def apply_curve(self, spec) -> None:
        """Set an automation spec (e.g. a beat pulse) and turn automation on."""
        if spec and spec.get("keys"):
            self.set_value(spec)

    def _on_toggle(self, on: bool) -> None:
        if on and self._spec is None:
            self._spec = self._ramp(self.value.value())
        self._sync()
        self.changed.emit()

    def _on_value(self, v) -> None:
        if self.auto_chk.isChecked() and self._spec and self._spec["keys"]:
            self._spec["keys"][0][1] = v      # inline spin edits the first key
        self.changed.emit()

    def _edit_curve(self) -> None:
        if self._spec is None:
            self._spec = self._ramp(self.value.value())
        dlg = KeyframeDialog(self, self._param, self._spec)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._spec = dlg.values()
            keys = self._spec.get("keys")
            if keys:
                self.value.blockSignals(True)
                self.value.setValue(int(keys[0][1]) if self._is_int
                                    else float(keys[0][1]))
                self.value.blockSignals(False)
            self.changed.emit()

    def get_value(self):
        if not self.auto_chk.isChecked():
            return self.value.value()
        return {"__auto__": True, "interp": self._spec.get("interp", "linear"),
                "keys": [list(k) for k in self._spec.get("keys", [])]}

    def set_value(self, value) -> None:
        coerce = int if self._is_int else float
        if isinstance(value, dict) and value.get("__auto__"):
            self._spec = {
                "__auto__": True, "interp": value.get("interp", "linear"),
                "keys": [([float(k[0]), coerce(k[1])]
                          + ([k[2]] if len(k) > 2 else []))
                         for k in sorted(value.get("keys", []), key=lambda k: k[0])]}
            self.auto_chk.setChecked(True)
            first = self._spec["keys"][0][1] if self._spec["keys"] else self._default
            self.value.blockSignals(True)
            self.value.setValue(first)
            self.value.blockSignals(False)
        else:
            self.auto_chk.setChecked(False)
            self._spec = None
            self.value.blockSignals(True)
            self.value.setValue(coerce(value))
            self.value.blockSignals(False)
        self._sync()
        self.changed.emit()


def build_param_widget(param: Param) -> Tuple[QWidget, Callable]:
    """Return (control, getter) for a mode parameter. getter() -> current value.

    The parameter's ``help`` text becomes the control's tooltip, so the inspector
    surfaces the same guidance ``moshit modes`` prints on the CLI.
    """
    w, getter = _build_param_control(param)
    if getattr(param, "help", ""):
        w.setToolTip(param.help)
    return w, getter


def _build_param_control(param: Param) -> Tuple[QWidget, Callable]:
    if param.kind in ("int", "float") and getattr(param, "automatable", False):
        w = AutoParamWidget(param)
        return w, w.get_value
    if param.kind == "bool":
        w = QCheckBox()
        w.setChecked(bool(param.default))
        return w, w.isChecked
    if param.kind in ("int", "float"):
        w = _make_spin(param)                   # select-all-on-focus spin box
        w.setValue(int(param.default or 0) if param.kind == "int"
                   else float(param.default or 0.0))
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
# Effect add menu (grouped by corruption type) + per-effect parameter dialog
# --------------------------------------------------------------------------- #

# Presentation-only grouping for the codec mosh modes' "+ Add" menu; unknown
# modes fall into "Other". Pixel/raw modes group by their own metadata.
_MOSH_CATEGORY = {
    "pframe_duplicate": "P-frame", "pframe_drop": "P-frame", "pframe_echo": "P-frame",
    "pframe_reverse": "P-frame", "pframe_shuffle": "P-frame",
    "pframe_stutter": "P-frame", "pingpong": "P-frame",
    "iframe_removal": "I-frame / GOP", "iframe_pulse": "I-frame / GOP",
    "gop_scramble": "I-frame / GOP",
    "motion_splice": "Motion", "motion_weave": "Motion", "motion_gain": "Motion",
    "momentum": "Motion", "surge": "Motion",
    "bitrot": "Corruption",
}
_MOSH_CAT_ORDER = ["P-frame", "I-frame / GOP", "Motion", "Corruption", "Other"]


def _mode_for(kind: str, name: str):
    from ..modes import get_mode, get_pixel_mode, get_raw_mode
    if kind == "pixel":
        return get_pixel_mode(name)
    if kind == "raw":
        return get_raw_mode(name)
    return get_mode(name)


def grouped_modes(kind: str) -> List[Tuple[str, List[str]]]:
    """``[(category, [mode_name, ...]), ...]`` for an add menu, in display order."""
    from ..modes import (available_modes, available_pixel_modes,
                         available_raw_modes, get_raw_mode)
    groups: Dict[str, List[str]] = {}
    if kind == "mosh":
        for n in available_modes():
            groups.setdefault(_MOSH_CATEGORY.get(n, "Other"), []).append(n)
        order = _MOSH_CAT_ORDER
    elif kind == "pixel":
        motion = {"zoom", "pan", "rotate", "shake"}
        for n in available_pixel_modes():
            groups.setdefault("Motion injection" if n in motion else "Pixel FX",
                              []).append(n)
        order = ["Pixel FX", "Motion injection"]
    else:                                          # raw
        for n in available_raw_modes():
            groups.setdefault(getattr(get_raw_mode(n), "category", "Raw FX"),
                              []).append(n)
        order = ["Raw FX", "RAW DATA - AUDIO"]
    out = [(c, sorted(groups[c])) for c in order if c in groups]
    out += [(c, sorted(v)) for c, v in groups.items() if c not in order]
    return out


def effect_add_menu(parent, kind: str, on_pick: Callable[[str], None]) -> QMenu:
    """A QMenu of a kind's effects, grouped into submenus by category."""
    menu = QMenu(parent)
    for category, names in grouped_modes(kind):
        sub = menu.addMenu(category)
        for name in names:
            act = sub.addAction(name)
            act.triggered.connect(lambda _checked=False, n=name: on_pick(n))
    return menu


class EffectParamDialog(QDialog):
    """Modal editor exposing all of one effect's parameters.

    Opened both when adding an effect (its defaults) and when double-clicking one
    in a stack (its current values). Works for any effect *kind* -- ``"mosh"``,
    ``"pixel"`` or ``"raw"`` -- building controls from the mode's ``Param``
    schema; mosh effects also get a frame-range (region) row, and automatable
    params carry the full automation / curve / beats controls.

    In *live* mode the dialog is non-modal and emits :sig:`paramsEdited` on every
    control change, so the host can re-render the preview as sliders move; Ok /
    Cancel then mean commit / revert rather than gating the values.
    """

    paramsEdited = Signal(dict, object)       # (params, region) on any live change

    def __init__(self, parent, *, kind: str, mode_name: str,
                 params: Optional[dict] = None, region=None,
                 motion_labels: Optional[List[str]] = None,
                 beat_provider: Optional[Callable] = None,
                 clip_id: Optional[str] = None, verb: str = "Edit",
                 live: bool = False):
        super().__init__(parent)
        self._kind = kind
        self.mode_name = mode_name
        self._beat_provider = beat_provider
        self._clip_id = clip_id
        self._live = live
        self._getters: Dict[str, Callable] = {}
        self._param_widgets: Dict[str, QWidget] = {}
        self._clip_ref_names: List[str] = []
        mode = _mode_for(kind, mode_name)
        self.setWindowTitle(f"{verb}: {mode_name}")
        self.setModal(not live)

        v = QVBoxLayout(self)
        if getattr(mode, "description", ""):
            d = QLabel(mode.description)
            d.setWordWrap(True)
            d.setStyleSheet("color:#8a92a6;")
            v.addWidget(d)

        form_host = QWidget()
        form = QFormLayout(form_host)
        form.setContentsMargins(0, 4, 0, 4)
        for param in mode.params:
            widget, getter = build_param_widget(param)
            if param.kind == "clip_ref":
                widget.addItems(list(motion_labels or []))
                self._clip_ref_names.append(param.name)
            if isinstance(widget, AutoParamWidget) and beat_provider is not None:
                widget.beatsRequested.connect(lambda w=widget: self._fill_beats(w))
            form.addRow(param.label or param.name, widget)
            self._getters[param.name] = getter
            self._param_widgets[param.name] = widget
        v.addWidget(form_host)
        if params:
            for name, widget in self._param_widgets.items():
                if name in params:
                    _set_param_value(widget, params[name])

        self._region_chk = None
        if kind == "mosh":
            self._region_chk = QCheckBox("Limit to a frame range")
            self._region_start = _make_spin(Param("s", "int", 0, lo=0, hi=1_000_000))
            self._region_end = _make_spin(Param("e", "int", 0, lo=0, hi=1_000_000))
            self._region_end.setSpecialValueText("end")   # 0 shows as "end" (= None)
            rrow = QHBoxLayout()
            rrow.addWidget(self._region_chk)
            rrow.addWidget(self._region_start)
            rrow.addWidget(QLabel("–"))
            rrow.addWidget(self._region_end)
            rrow.addStretch(1)
            v.addLayout(rrow)
            self._region_chk.toggled.connect(self._sync_region)
            if region:
                self._region_chk.setChecked(True)
                self._region_start.setValue(int(region[0]))
                self._region_end.setValue(0 if region[1] is None else int(region[1]))
            self._sync_region(bool(region))

        bar = QHBoxLayout()
        rnd = QPushButton("🎲 Randomise")
        rnd.setToolTip("Roll random values into the parameters")
        rnd.clicked.connect(self._randomise)
        bar.addWidget(rnd)
        bar.addStretch(1)
        v.addLayout(bar)

        # Ok commits (keeps the live-applied values); Cancel reverts to the
        # values the editor opened with -- the host restores its pre-edit snapshot.
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                              | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self._on_accept)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)

        if live:
            self._wire_live()

    def _wire_live(self) -> None:
        """Emit :sig:`paramsEdited` whenever any control changes, so the host can
        live-render. Connected only in live mode, after the initial values are
        populated, so seeding the controls doesn't fire spurious edits."""
        for w in self._param_widgets.values():
            if isinstance(w, AutoParamWidget):
                w.changed.connect(self._emit_live)
            elif isinstance(w, QCheckBox):
                w.toggled.connect(lambda *_: self._emit_live())
            elif isinstance(w, (QSpinBox, QDoubleSpinBox)):
                w.valueChanged.connect(lambda *_: self._emit_live())
            elif isinstance(w, QComboBox):
                w.currentIndexChanged.connect(lambda *_: self._emit_live())
            elif isinstance(w, QLineEdit):
                w.textChanged.connect(lambda *_: self._emit_live())
        if self._region_chk is not None:
            self._region_chk.toggled.connect(lambda *_: self._emit_live())
            self._region_start.valueChanged.connect(lambda *_: self._emit_live())
            self._region_end.valueChanged.connect(lambda *_: self._emit_live())

    def _emit_live(self) -> None:
        params = self.result_params()
        # A clip_ref mode is unrenderable until a motion source is picked; hold
        # off live-updating (the OK-time validation still guards the commit).
        if any(not params.get(n) for n in self._clip_ref_names):
            return
        self.paramsEdited.emit(params, self.result_region())

    def result_params(self) -> dict:
        return {name: g() for name, g in self._getters.items()}

    def result_region(self):
        if not self._region_chk or not self._region_chk.isChecked():
            return None
        end = self._region_end.value()
        return [self._region_start.value(), (None if end == 0 else end)]

    def _sync_region(self, on) -> None:
        self._region_start.setEnabled(bool(on))
        self._region_end.setEnabled(bool(on))

    def _on_accept(self) -> None:
        vals = self.result_params()
        if any(not vals.get(n) for n in self._clip_ref_names):
            QMessageBox.warning(self, "Motion source needed",
                                "Pick a motion source first (import a clip and add "
                                "it to the motion track).")
            return
        self.accept()

    def _randomise(self) -> None:
        import random
        for p in _mode_for(self._kind, self.mode_name).params:
            w = self._param_widgets.get(p.name)
            has_range = p.lo is not None and p.hi is not None
            num = (random.randint(int(p.lo), int(p.hi)) if p.kind == "int"
                   else round(random.uniform(float(p.lo), float(p.hi)), 2)
                   ) if has_range else None
            if isinstance(w, AutoParamWidget):
                if has_range:
                    w.set_value(num)
            elif isinstance(w, (QSpinBox, QDoubleSpinBox)):
                if has_range:
                    w.setValue(num)
            elif isinstance(w, QCheckBox):
                w.setChecked(random.random() < 0.5)
            elif isinstance(w, QComboBox) and p.kind == "choice" and p.choices:
                w.setCurrentText(str(random.choice(p.choices)))

    def _fill_beats(self, widget) -> None:
        from .. import beats
        if not self._beat_provider or not self._clip_id:
            QMessageBox.information(self, "No beats yet",
                                    "Render a preview with audio (unmute) first so "
                                    "the beats can be detected.")
            return
        positions = self._beat_provider(self._clip_id)
        if not positions:
            QMessageBox.information(self, "No beats", "No beats found in this clip.")
            return
        low, high, is_int = widget.beat_range()
        widget.apply_curve(beats.pulse_curve(positions, low, high, is_int=is_int))


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

    def add_media(self, item, offline: bool = False) -> None:
        text = f"{item.label}  ·  {item.nb_frames}f"
        if offline:
            text += "   ⚠ offline"
        entry = QListWidgetItem(text)
        entry.setData(Qt.ItemDataRole.UserRole, item.id)
        if offline:
            entry.setForeground(QColor("#e07a5f"))
            entry.setToolTip("Missing cached video:\n"
                             f"{item.intermediate_path}\n"
                             "Use File → Relink offline media… to restore it.")
        self.list.addItem(entry)
        self.list.setCurrentItem(entry)

    def selected_media_id(self) -> Optional[str]:
        it = self.list.currentItem()
        return it.data(Qt.ItemDataRole.UserRole) if it else None


# --------------------------------------------------------------------------- #
# Timeline
# --------------------------------------------------------------------------- #

class TimelineWidget(QWidget):
    """Interactive multi-track timeline for the current sequence.

    Video tracks stack top-to-bottom (the topmost lane composites over the ones
    below); a motion lane sits at the bottom. Click a clip to select it, drag its
    body to move it in time (free positioning; overlapping clips on a track
    cross-dissolve), drag either edge to trim, and press Delete to remove it.
    Right-click an empty lane for track actions (add / remove / move / enable).
    A frame ruler and playhead run across the top.
    """

    clipSelected = Signal(str)
    moveRequested = Signal(str, int)               # clip_id, new start frame
    trimRequested = Signal(str, int, int)          # clip_id, in|-1, out|-1
    removeRequested = Signal(str)
    seekRequested = Signal(float)                  # scrub position, 0..1
    splitRequested = Signal(str, int)              # clip_id, offset frames
    duplicateRequested = Signal(str)               # clip_id
    addTrackRequested = Signal()                   # new video track
    removeTrackRequested = Signal(str)             # track_id
    reorderTrackRequested = Signal(str, int)       # track_id, delta (-1 up/+1 down)
    trackEnabledToggled = Signal(str, bool)        # track_id, enabled
    addClipToTrackRequested = Signal(str)          # track_id (uses library selection)
    enterSequenceRequested = Signal(str)           # seq_id (double-clicked a precomp)
    zoomStepRequested = Signal(float, int)         # wheel steps, anchor x (widget px)
    zoomFitRequested = Signal()
    panRequested = Signal(int, int)                # dx, dy in px
    playheadMoved = Signal(int)                    # playhead x (widget px)

    RULER_H = 20
    WAVE_H = 22                                     # audio waveform strip
    LANE_H = 46
    PAD = 8
    LABEL_W = 56
    EDGE = 6                                        # px hot zone for edge-trim

    def __init__(self):
        super().__init__()
        self.setMinimumHeight(self.RULER_H + self.WAVE_H + 2 * self.LANE_H
                              + 3 * self.PAD + 6)
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
        self._waveform: Optional[List[float]] = None      # audio peak envelope
        self._seq_id: Optional[str] = None          # sequence shown (None = root)

    def set_project(self, project) -> None:
        self._project = project
        if self._seq_id is None or not any(s.id == self._seq_id
                                           for s in project.sequences):
            self._seq_id = project.root_seq_id
        self._total = self._seq_total()
        self._update_height()
        self.update()

    def set_sequence(self, seq_id) -> None:
        self._seq_id = seq_id
        if self._project:
            self._total = self._seq_total()
            self._update_height()
        self.update()

    def set_play_fraction(self, frac: float) -> None:
        self._play_frac = max(0.0, min(1.0, float(frac)))
        x0, x1 = self._track_x()
        self.playheadMoved.emit(int(x0 + self._play_frac * max(1, x1 - x0)))
        self.update()

    def set_waveform(self, peaks) -> None:
        self._waveform = list(peaks) if peaks else None
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

    def _main_layout(self):
        """Overlap-aware main-track layout (see ``Project.main_layout``)."""
        return self._project.main_layout() if self._project else []

    def _main_length(self) -> int:
        return max((start + length for _c, start, length, _t in self._main_layout()),
                   default=0)

    def _seq(self) -> str:
        return self._seq_id or (self._project.root_seq_id if self._project else "")

    def _video_tracks(self):
        return self._project.tracks_for(self._seq(), "video") if self._project else []

    def _motion_tracks(self):
        return self._project.tracks_for(self._seq(), "motion") if self._project else []

    def _lanes(self):
        """Tracks top-to-bottom: video tracks (top layer first), then motion."""
        return list(reversed(self._video_tracks())) + self._motion_tracks()

    def _lane_index(self, track_id: str) -> int:
        for i, t in enumerate(self._lanes()):
            if t.id == track_id:
                return i
        return 0

    def _track_total(self, track) -> int:
        if track.role == "video":
            return max((start + length for _c, start, length, _t
                        in self._project.track_layout(track.id)), default=0)
        return sum(self._project._clip_length(c)
                   for c in self._project.clips_for_track(track.id))

    def _seq_total(self) -> int:
        if not self._project:
            return 1
        return max(1, max((self._track_total(t) for t in self._lanes()), default=0))

    def _update_height(self) -> None:
        n = max(2, len(self._lanes()))
        self.setMinimumHeight(self.RULER_H + self.WAVE_H
                              + n * (self.LANE_H + self.PAD) + self.PAD + 6)

    def _track_x(self) -> Tuple[int, int]:
        return self.PAD + self.LABEL_W, self.width() - self.PAD

    def _ppf(self) -> float:
        x0, x1 = self._track_x()
        return max(1, x1 - x0) / max(1, self._total)

    def _tick_step(self, ppf: float) -> int:
        """Smallest 1/2/5×10^k frame step that keeps ruler labels ~70px apart,
        so tick density stays sane at any zoom level."""
        target = 70.0 / max(ppf, 1e-9)
        k = 1
        while True:
            for m in (1, 2, 5):
                if m * k >= target:
                    return m * k
            k *= 10

    def _visible(self) -> QRect:
        """Visible portion of the widget (the whole rect when unscrolled or
        rendered off-screen) — lets paint skip off-screen work when zoomed."""
        vis = self.visibleRegion().boundingRect()
        return self.rect() if vis.isEmpty() else vis

    def _lane_y(self, lane: int) -> int:
        return (self.RULER_H + self.WAVE_H + self.PAD
                + lane * (self.LANE_H + self.PAD))

    # -- painting ----------------------------------------------------------- #

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        p.fillRect(self.rect(), QColor("#1c1f24"))
        self._hits = []
        x0, x1 = self._track_x()
        track_w = max(1, x1 - x0)
        ppf = self._ppf()
        vis = self._visible()

        font = QFont()
        font.setPointSize(8)
        p.setFont(font)

        # ruler ticks + labels, with a scrub track underneath (only the
        # visible span — when zoomed the widget is far wider than the view)
        p.setPen(QColor("#4a5160"))
        step = self._tick_step(ppf)
        f = max(0, int((vis.left() - x0) / ppf / step)) * step
        while f <= self._total:
            tx = int(x0 + f * ppf)
            if tx > vis.right():
                break
            p.drawLine(tx, 2, tx, 6)
            p.drawText(tx + 2, 12, str(f))
            f += step
        p.setPen(QColor("#3a414c"))
        p.drawLine(x0, self.RULER_H - 2, x1, self.RULER_H - 2)

        # audio waveform strip (spans the full rendered timeline)
        self._draw_waveform(p, x0, track_w, vis)

        # lanes (video tracks top-to-bottom, motion at the bottom)
        bands = []
        for lane, t in enumerate(self._lanes()):
            y = self._lane_y(lane)
            on = getattr(t, "enabled", True)
            p.fillRect(QRect(x0, y, track_w, self.LANE_H),
                       QColor("#262b33") if on else QColor("#202329"))
            if t.role == "video":
                for clip, start, length, trans in self._project.track_layout(t.id):
                    rect = QRect(int(x0 + start * ppf), y,
                                 max(2, int(length * ppf) - 2), self.LANE_H)
                    self._draw_clip(p, rect, clip, length)
                    self._hits.append((rect, clip.id, t.id))
                    if trans > 0:
                        bands.append(QRect(int(x0 + start * ppf), y,
                                           max(2, int(trans * ppf)), self.LANE_H))
            else:                                     # motion: contiguous pool
                cursor = 0
                for clip in self._project.clips_for_track(t.id):
                    length = self._project._clip_length(clip)
                    rect = QRect(int(x0 + cursor * ppf), y,
                                 max(2, int(length * ppf) - 2), self.LANE_H)
                    self._draw_clip(p, rect, clip, length, motion=True)
                    self._hits.append((rect, clip.id, t.id))
                    cursor += length
        for band in bands:                            # overlay after all clips
            self._draw_xfade_band(p, band)

        # lane labels — stick to the visible left edge when scrolled, drawn
        # over the clips on a translucent chip so they stay readable
        scrolled = vis.left() > 0
        lx = self.PAD + max(0, vis.left())
        for lane, t in enumerate(self._lanes()):
            y = self._lane_y(lane)
            on = getattr(t, "enabled", True)
            if scrolled:
                p.fillRect(QRect(lx - 4, y, self.LABEL_W, self.LANE_H),
                           QColor(28, 31, 36, 215))
            p.setPen(QColor("#8a92a6") if on else QColor("#565d6b"))
            name = t.name if len(t.name) <= 8 else t.name[:7] + "…"
            p.drawText(lx, y + 18, name)
            if not on:
                p.drawText(lx, y + 32, "(off)")

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
        if abs(getattr(clip, "opacity", 1.0) - 1.0) > 1e-6:
            badges.append(f"{clip.opacity * 100:.0f}%")
        if getattr(clip, "blend_mode", "normal") != "normal":
            badges.append(clip.blend_mode)
        suffix = ("   " + " ".join(badges)) if badges else ""
        p.setPen(QColor("#eef1f6"))
        p.drawText(rect.adjusted(5, 0, -3, 0),
                   Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                   f"{label}  {length}f{suffix}")

    def _draw_waveform(self, p, x0, track_w, vis) -> None:
        top = self.RULER_H + 2
        h = self.WAVE_H - 3
        p.fillRect(QRect(x0, top, track_w, h), QColor("#171a1f"))
        peaks = self._waveform
        if not peaks:
            return
        mid = top + h / 2.0
        amp = h / 2.0 - 1
        n = len(peaks)
        # only paint the visible columns — when zoomed, track_w can be tens
        # of thousands of pixels wide
        c0 = max(0, vis.left() - x0)
        c1 = min(track_w, vis.right() - x0 + 1)
        p.setPen(QColor("#3aa6a0"))
        for col in range(c0, c1):
            v = peaks[min(n - 1, col * n // track_w)]
            ph = v * amp
            x = x0 + col
            p.drawLine(x, int(mid - ph), x, int(mid + ph))
        p.setPen(QColor("#2a3038"))                   # centre baseline
        p.drawLine(x0 + c0, int(mid), x0 + c1, int(mid))

    def _draw_xfade_band(self, p, rect) -> None:
        """Shade the crossfade overlap region with a hatched translucent band."""
        p.save()
        p.setClipRect(rect)
        p.fillRect(rect, QColor(255, 209, 102, 70))   # translucent yellow wash
        p.setPen(QColor(255, 209, 102, 150))          # diagonal hatch
        h = rect.height()
        x = rect.left() - h
        while x < rect.right():
            p.drawLine(x, rect.bottom(), x + h, rect.top())
            x += 6
        p.setPen(QColor("#ffd166"))                   # crisp edges
        p.drawLine(rect.left(), rect.top(), rect.left(), rect.bottom())
        p.drawLine(rect.right(), rect.top(), rect.right(), rect.bottom())
        p.restore()

    # -- hit testing -------------------------------------------------------- #

    def _hit(self, pos) -> Optional[Tuple[QRect, str, str]]:
        # Where crossfading clips overlap, a clip near one of its trim edges wins
        # (so both handles stay reachable); otherwise the topmost (last-drawn) clip.
        edge_hit = None
        body_hit = None
        for entry in self._hits:
            rect = entry[0]
            if not rect.contains(pos):
                continue
            body_hit = entry
            if edge_hit is None and (abs(pos.x() - rect.left()) <= self.EDGE
                                     or abs(pos.x() - rect.right()) <= self.EDGE):
                edge_hit = entry
        return edge_hit or body_hit

    def _is_video(self, track_id: str) -> bool:
        try:
            return self._project.track(track_id).role == "video"
        except (KeyError, AttributeError):
            return False

    def _lane_at(self, y: int):
        for i, t in enumerate(self._lanes()):
            ly = self._lane_y(i)
            if ly <= y <= ly + self.LANE_H:
                return t
        return None

    def request_split_at_playhead(self) -> None:
        """Split the video clip under the playhead at its current frame.

        The standard NLE 'split at the cursor' action: mirrors a Cut-tool click
        at the playhead. Prefers the selected clip when it's under the playhead.
        """
        if not self._project or not self._hits:
            return
        x0, x1 = self._track_x()
        px = int(x0 + self._play_frac * max(1, x1 - x0))
        under = [(rect, cid) for rect, cid, tr in self._hits
                 if self._is_video(tr) and rect.left() <= px <= rect.right()]
        if not under:
            return
        rect, cid = next((u for u in under if u[1] == self._selected), under[0])
        offset = round((px - rect.left()) / self._ppf())
        if offset > 0:
            self.splitRequested.emit(cid, offset)

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
        x0, _x1 = self._track_x()
        disp_start = round((rect.left() - x0) / self._ppf())   # current timeline pos
        self._drag = {"id": clip_id, "track": track, "mode": mode,
                      "press_x": pos.x(), "in": clip.in_point, "out": orig_out,
                      "ppf": self._ppf(), "start": disp_start}
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
            if self._is_video(d["track"]) and dframes != 0:    # free positioning
                self.moveRequested.emit(d["id"], max(0, d["start"] + dframes))
        elif d["mode"] == "trim_l" and dframes != 0:
            self.trimRequested.emit(d["id"], max(0, d["in"] + dframes), -1)
        elif d["mode"] == "trim_r" and dframes != 0:
            self.trimRequested.emit(d["id"], -1, d["out"] + dframes)
        self.update()

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton or not self._project:
            return
        hit = self._hit(event.position().toPoint())
        if not hit:
            return
        clip = self._project.clip(hit[1])
        media = self._project.media.get(clip.media_id)
        if media and getattr(media, "sequence_id", None):   # a precomp clip
            self.enterSequenceRequested.emit(media.sequence_id)

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace) and self._selected:
            self.removeRequested.emit(self._selected)
        else:
            super().keyPressEvent(event)

    def wheelEvent(self, event) -> None:
        """Ctrl+wheel zooms around the cursor; plain wheel pans horizontally
        (the timeline's primary axis); Shift+wheel pans vertically."""
        delta = event.angleDelta()
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            steps = (delta.y() or delta.x()) / 120.0
            if steps:
                self.zoomStepRequested.emit(steps, int(event.position().x()))
        elif event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            self.panRequested.emit(0, -(delta.y() or delta.x()))
        else:
            self.panRequested.emit(-(delta.x() or delta.y()), 0)
        event.accept()

    def contextMenuEvent(self, event) -> None:
        from PySide6.QtWidgets import QMenu
        hit = self._hit(event.pos())
        if hit:
            clip_id, track = hit[1], hit[2]
            self._selected = clip_id
            self.clipSelected.emit(clip_id)
            self.update()
            menu = QMenu(self)
            act_dup = menu.addAction("Duplicate clip")
            act_split = (menu.addAction("Split at playhead")
                         if self._is_video(track) else None)
            menu.addSeparator()
            act_remove = menu.addAction("Remove from timeline")
            chosen = menu.exec(event.globalPos())
            if chosen == act_dup:
                self.duplicateRequested.emit(clip_id)
            elif act_split is not None and chosen == act_split:
                self.request_split_at_playhead()
            elif chosen == act_remove:
                self.removeRequested.emit(clip_id)
            return
        # empty area: act on the lane (track) under the cursor
        t = self._lane_at(event.pos().y())
        menu = QMenu(self)
        act_add = menu.addAction("Add video track")
        act_clip = act_up = act_down = act_toggle = act_rm = None
        if t is not None and t.role == "video":
            act_clip = menu.addAction("Add selected media here")
            menu.addSeparator()
            act_up = menu.addAction("Move track up")
            act_down = menu.addAction("Move track down")
            act_toggle = menu.addAction("Disable track" if t.enabled
                                        else "Enable track")
            act_rm = menu.addAction("Remove track")
        chosen = menu.exec(event.globalPos())
        if chosen is None:
            return
        if chosen == act_add:
            self.addTrackRequested.emit()
        elif chosen == act_clip:
            self.addClipToTrackRequested.emit(t.id)
        elif chosen == act_up:                        # up = toward the top layer
            self.reorderTrackRequested.emit(t.id, +1)
        elif chosen == act_down:
            self.reorderTrackRequested.emit(t.id, -1)
        elif chosen == act_toggle:
            self.trackEnabledToggled.emit(t.id, not t.enabled)
        elif chosen == act_rm:
            self.removeTrackRequested.emit(t.id)


class TimelinePane(QScrollArea):
    """Zoomable, scrollable host for the :class:`TimelineWidget`.

    Zoom stretches the timeline widget horizontally (1× = fit to width, the
    historical behaviour) and the scroll area supplies the scrollbars, so all
    of the timeline's coordinate math keeps working unchanged. Ctrl+wheel
    zooms around the cursor, plain wheel pans horizontally, Shift+wheel pans
    vertically (useful once tracks overflow the pane).
    """

    MAX_ZOOM = 64.0
    zoomChanged = Signal(float)

    def __init__(self, timeline: TimelineWidget):
        super().__init__()
        self._timeline = timeline
        self._zoom = 1.0
        self.setWidget(timeline)
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        timeline.zoomStepRequested.connect(self._on_zoom_step)
        timeline.zoomFitRequested.connect(self.zoom_fit)
        timeline.panRequested.connect(self._on_pan)
        timeline.playheadMoved.connect(self._follow_playhead)

    # -- zoom --------------------------------------------------------------- #

    def zoom(self) -> float:
        return self._zoom

    def set_zoom(self, zoom: float, anchor_x: Optional[int] = None) -> None:
        """Set the zoom factor, keeping ``anchor_x`` (timeline-widget px)
        fixed on screen; defaults to the centre of the viewport."""
        zoom = max(1.0, min(self.MAX_ZOOM, float(zoom)))
        if abs(zoom - self._zoom) < 1e-9:
            return
        hbar = self.horizontalScrollBar()
        if anchor_x is None:
            anchor_x = hbar.value() + self.viewport().width() // 2
        view_x = anchor_x - hbar.value()          # viewport column to keep fixed
        t = self._timeline
        x0 = t.PAD + t.LABEL_W                    # frame x scales with the track
        old_track = max(1, t.width() - x0 - t.PAD)          # area, not the widget
        self._zoom = zoom
        new_w = self._apply_zoom_width()
        scale = max(1, new_w - x0 - t.PAD) / old_track
        hbar.setValue(int(round(x0 + (anchor_x - x0) * scale - view_x)))
        self.zoomChanged.emit(zoom)

    def zoom_in(self) -> None:
        self.set_zoom(self._zoom * 1.5)

    def zoom_out(self) -> None:
        self.set_zoom(self._zoom / 1.5)

    def zoom_fit(self) -> None:
        self.set_zoom(1.0)
        self.horizontalScrollBar().setValue(0)

    def _apply_zoom_width(self) -> int:
        """Sync the timeline widget's width to viewport × zoom."""
        vw = max(1, self.viewport().width())
        w = max(vw, int(round(vw * self._zoom)))
        t = self._timeline
        t.setMinimumWidth(w if self._zoom > 1.0 else 0)
        t.resize(w, max(t.height(), t.minimumHeight()))
        # the scroll range only refreshes on the next layout pass; set it now
        # so an immediate setValue() isn't clamped against the stale range
        self.horizontalScrollBar().setRange(0, max(0, w - vw))
        return w

    def resizeEvent(self, event) -> None:          # noqa: N802 (Qt signature)
        super().resizeEvent(event)
        self._apply_zoom_width()

    # -- scrolling ---------------------------------------------------------- #

    def _on_zoom_step(self, steps: float, anchor_x: int) -> None:
        self.set_zoom(self._zoom * (1.25 ** steps), anchor_x)

    def _on_pan(self, dx: int, dy: int) -> None:
        if dx:
            h = self.horizontalScrollBar()
            h.setValue(h.value() + dx)
        if dy:
            v = self.verticalScrollBar()
            v.setValue(v.value() + dy)

    def _follow_playhead(self, px: int) -> None:
        """Page the view when playback carries the playhead off-screen, so it
        re-enters near the left edge (no continuous chase-scrolling)."""
        hbar = self.horizontalScrollBar()
        vw = self.viewport().width()
        if hbar.maximum() > 0 and (px < hbar.value() or px > hbar.value() + vw):
            hbar.setValue(max(0, min(hbar.maximum(), px - vw // 6)))


# --------------------------------------------------------------------------- #
# Preview + transport
# --------------------------------------------------------------------------- #

class PreviewWidget(QWidget):
    frameChanged = Signal(int)
    muteToggled = Signal(bool)

    def __init__(self):
        super().__init__()
        self._frames: List[QImage] = []
        self._fps = 30.0
        self._idx = 0
        self._restore_frac: Optional[float] = None
        self._streaming = False
        self._loop = False
        self._audio_path: Optional[str] = None
        self._muted = False
        self._advancing = False               # distinguish auto-advance from scrubs
        if _HAVE_QT_AUDIO:
            self._audio_out = QAudioOutput()
            self._player = QMediaPlayer()
            self._player.setAudioOutput(self._audio_out)
        else:
            self._audio_out = self._player = None

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

        self.mute_btn = QPushButton("🔊")
        self.mute_btn.setMaximumWidth(34)
        self.mute_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.mute_btn.setToolTip("Preview audio on/off")
        self.mute_btn.clicked.connect(self._toggle_mute)
        self.mute_btn.setVisible(_HAVE_QT_AUDIO)

        self.frame_lbl = QLabel("0 / 0")
        for w in (self.start_btn, self.prev_btn, self.play_btn,
                  self.next_btn, self.end_btn):
            controls.addWidget(w)
        controls.addSpacing(8)
        controls.addWidget(self.loop_chk)
        controls.addWidget(self.mute_btn)
        controls.addStretch(1)
        controls.addWidget(self.frame_lbl)
        layout.addLayout(controls)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._advance)

        # busy badge over the image: the preview keeps showing the previous
        # (stale) frames while a re-render runs, so say that it's updating
        self._busy_badge = QLabel("Rendering…", self.view)
        self._busy_badge.setStyleSheet(
            "background: rgba(13, 15, 18, 190); color: #ffd166;"
            "padding: 4px 10px; border-radius: 4px; font-weight: bold;")
        self._busy_badge.hide()

    def set_rendering(self, active: bool, text: str = "Rendering…") -> None:
        if active:
            self._busy_badge.setText(text or "Rendering…")
            self._busy_badge.adjustSize()
            self._place_badge()
            self._busy_badge.show()
            self._busy_badge.raise_()
        else:
            self._busy_badge.hide()

    def _place_badge(self) -> None:
        self._busy_badge.move(
            self.view.width() - self._busy_badge.width() - 10, 10)

    def _current_fraction(self) -> Optional[float]:
        n = len(self._frames)
        return (self._idx / (n - 1)) if n > 1 else None

    def _set_transport_enabled(self, on: bool) -> None:
        for b in (self.start_btn, self.prev_btn, self.play_btn,
                  self.next_btn, self.end_btn):
            b.setEnabled(on)

    def _set_loop(self, on: bool) -> None:
        self._loop = bool(on)

    # -- audio (synced to the frame stepper) -------------------------------- #

    def set_audio(self, path) -> None:
        """Set the preview's audio track (or None to clear)."""
        self._audio_path = path
        if not self._player:
            return
        self._player.setSource(QUrl.fromLocalFile(path) if path else QUrl())
        if self.timer.isActive():             # resync if it arrives mid-playback
            self._audio_start()

    def _audio_ms(self, idx: int) -> int:
        return int(idx / max(1.0, self._fps) * 1000)

    def _audio_start(self) -> None:
        if self._player and self._audio_path and not self._muted:
            self._player.setPosition(self._audio_ms(self._idx))
            self._player.play()

    def _audio_stop(self) -> None:
        if self._player:
            self._player.pause()

    def _audio_resync(self) -> None:
        if (self._player and not self._muted and self._player.playbackState()
                == QMediaPlayer.PlaybackState.PlayingState):
            self._player.setPosition(self._audio_ms(self._idx))

    def _toggle_mute(self) -> None:
        self._muted = not self._muted
        self.mute_btn.setText("🔇" if self._muted else "🔊")
        if self._audio_out:
            self._audio_out.setMuted(self._muted)
        if self._muted:
            self._audio_stop()
        elif self.timer.isActive():
            self._audio_start()
        self.muteToggled.emit(self._muted)

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
            self._audio_stop()
        elif self._frames:
            self.timer.start(int(1000 / max(1.0, self._fps)))
            self.play_btn.setText("Pause")
            self._audio_start()

    def step(self, delta: int) -> None:
        if not self._frames:
            return
        self.timer.stop()
        self.play_btn.setText("Play")
        self._audio_stop()
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

    def cancel_stream(self) -> None:
        """Finalize a render that was cancelled mid-stream, so the transport isn't
        left disabled (keeps whatever frames had already arrived)."""
        if self._streaming:
            self.end_stream()

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
            if not self._advancing:                  # user scrub -> resync audio
                self._audio_resync()

    def _advance(self) -> None:
        if not self._frames:
            return
        if self._idx + 1 >= len(self._frames):       # reached the end
            if self._loop:
                self._advancing = True
                self.slider.setValue(0)
                self._advancing = False
                self._audio_resync()                 # restart audio at the top
            else:
                self.timer.stop()
                self.play_btn.setText("Play")
                self._audio_stop()
            return
        self._advancing = True
        self.slider.setValue(self._idx + 1)          # triggers _show via _on_slider
        self._advancing = False

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self._frames:
            self._show(self._idx)
        if self._busy_badge.isVisible():
            self._place_badge()


# --------------------------------------------------------------------------- #
# Inspector (schema-driven)
# --------------------------------------------------------------------------- #

class _CollapsibleSection(QWidget):
    """A titled inspector section whose body collapses when its header is clicked.

    Lets the inspector show many groups without becoming a wall of controls --
    the user expands only what they're working on.
    """

    def __init__(self, title: str, content: QWidget, *, expanded: bool = True):
        super().__init__()
        self._title = title
        self._content = content
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        self._btn = QPushButton()
        self._btn.setCheckable(True)
        self._btn.setChecked(expanded)
        self._btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn.setStyleSheet(
            "QPushButton { text-align:left; padding:4px 6px; border:0;"
            " border-top:1px solid #3a414c; background:#1c1f24;"
            " color:#cdd3df; font-weight:bold; }"
            "QPushButton:hover { background:#222831; }")
        self._btn.toggled.connect(self._on_toggled)
        v.addWidget(self._btn)
        v.addWidget(content)
        content.setVisible(expanded)
        self._render()

    def _render(self) -> None:
        self._btn.setText(f"{'▾' if self._btn.isChecked() else '▸'}  {self._title}")

    def _on_toggled(self, on: bool) -> None:
        self._content.setVisible(on)
        self._render()


class _HintListWidget(QListWidget):
    """A list that shows centered placeholder text while it holds no items, so an
    empty stack reads as "nothing here yet" rather than a blank box."""

    def __init__(self, hint: str):
        super().__init__()
        self._hint = hint

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        if self.count() == 0:
            painter = QPainter(self.viewport())
            painter.setPen(QColor("#6a7280"))
            painter.drawText(self.viewport().rect(),
                             int(Qt.AlignmentFlag.AlignCenter), self._hint)
            painter.end()


class InspectorPanel(QWidget):
    effectAddBegin = Signal(str)                   # mode -> host creates the op live
    effectEditBegin = Signal(str)                  # op_id -> open a live edit session
    effectLiveUpdate = Signal(str, str, dict, object)  # op_id, mode, params, region
    effectEditEnd = Signal(str, bool)              # op_id, committed
    effectRemoveRequested = Signal(str)            # op_id
    effectMoveRequested = Signal(str, int)         # op_id, delta (-1 up / +1 down)
    effectEnabledChanged = Signal(str, bool)       # op_id, enabled
    presetSaveRequested = Signal()                 # save the current stack
    presetApplyRequested = Signal(str)             # preset name
    presetDeleteRequested = Signal(str)            # preset name
    pixelFxAddBegin = Signal(str)                  # name -> host appends + opens editor
    pixelFxEditBegin = Signal(int)                 # index -> open a live edit session
    pixelFxLiveUpdate = Signal(int, dict)          # index, params
    pixelFxEditEnd = Signal(int, bool)             # index, committed
    pixelFxRemoveRequested = Signal(int)           # index in the clip's pixel list
    rawFxAddBegin = Signal(str)                    # name -> host appends + opens editor
    rawFxEditBegin = Signal(int)                   # index -> open a live edit session
    rawFxLiveUpdate = Signal(int, dict)            # index, params
    rawFxEditEnd = Signal(int, bool)               # index, committed
    rawFxRemoveRequested = Signal(int)             # index in the clip's raw list
    maskChanged = Signal(str, object)              # kind ("layer"|"fx"), spec|None
    flowTransferRequested = Signal()               # optical-flow transfer (bake)
    flowChanged = Signal(object)                    # live flow_transfer dict / None
    bakeRequested = Signal()
    revertRequested = Signal()
    clipPropsChanged = Signal(dict)                # speed/reverse/fades/transition

    def __init__(self):
        super().__init__()
        self._motion_labels: List[str] = []
        self._clip_id: Optional[str] = None
        self._populating = False
        self._effects: List[dict] = []
        self._selected_op: Optional[str] = None
        self._pixel_fx: List[dict] = []
        self._pixel_sel = -1
        self._raw_fx: List[dict] = []
        self._raw_sel = -1
        self._mask_editors: Dict[str, dict] = {}
        self._beat_provider: Optional[Callable] = None    # clip_id -> [pos, ...]
        self._live_dlg: Optional[EffectParamDialog] = None  # open live param editor

        outer = QVBoxLayout(self)
        outer.addWidget(_heading("Inspector"))

        self.clip_lbl = QLabel("Select a clip on the main track.")
        self.clip_lbl.setWordWrap(True)
        outer.addWidget(self.clip_lbl)

        # Everything below the heading lives in a body container that stays
        # hidden until a clip is selected, so the panel is blank otherwise.
        self._body = QWidget()
        outer.addWidget(self._body)
        outer.addStretch(1)
        layout = QVBoxLayout(self._body)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        # Each group is collapsible so the inspector isn't a wall of controls;
        # the primary stacks (Clip, Effects) start open, the rest folded away.
        layout.addWidget(_CollapsibleSection(
            "Clip", self._build_clip_group(), expanded=True))
        layout.addWidget(_CollapsibleSection(
            "Effects (top → bottom)", self._build_effects_group(), expanded=True))
        layout.addWidget(_CollapsibleSection(
            "Pixel FX", self._build_pixel_group(), expanded=False))
        layout.addWidget(_CollapsibleSection(
            "Raw FX (numpy)", self._build_raw_group(), expanded=False))
        layout.addWidget(_CollapsibleSection(
            "Masks", self._build_mask_group(), expanded=False))
        layout.addWidget(_CollapsibleSection(
            "Flow FX (motion warp)", self._build_flow_group(), expanded=False))
        layout.addWidget(self._build_clip_actions())   # always-visible actions
        layout.addStretch(1)

        self.set_enabled_for_clip(None, None)

    def _build_effects_group(self) -> QWidget:
        """The mosh effect stack (list + add/remove/reorder) and presets."""
        group = QWidget()
        v = QVBoxLayout(group)
        v.setContentsMargins(0, 2, 0, 6)
        v.setSpacing(3)

        self.effect_list = _HintListWidget("No effects yet — use “+ Add ▾”")
        self.effect_list.setMaximumHeight(140)
        self.effect_list.setToolTip("This clip's effect stack, applied top to "
                                    "bottom. Double-click an effect to edit it; "
                                    "toggle the checkbox to enable/disable.")
        self.effect_list.currentRowChanged.connect(self._on_effect_row)
        self.effect_list.itemChanged.connect(self._on_effect_item_changed)
        self.effect_list.itemDoubleClicked.connect(self._edit_effect_item)
        v.addWidget(self.effect_list)

        stack_btns = QHBoxLayout()
        self.add_btn = QPushButton("+ Add ▾")
        self.add_btn.setToolTip("Add a corruption effect (grouped by type); its "
                                "parameters open in a pop-up.")
        self.add_btn.clicked.connect(self._show_add_menu)
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
        v.addLayout(stack_btns)

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
        v.addLayout(preset_row)
        return group

    def _build_clip_actions(self) -> QWidget:
        """Clip-level actions kept always visible below the collapsible groups."""
        group = QWidget()
        v = QVBoxLayout(group)
        v.setContentsMargins(0, 6, 0, 0)
        v.setSpacing(3)
        self.flow_btn = QPushButton("Optical-flow transfer…")
        self.flow_btn.setToolTip("Warp this clip's pixels by another clip's motion "
                                 "(appearance-free; GPU via OpenCV/OpenCL)")
        self.flow_btn.clicked.connect(lambda: self.flowTransferRequested.emit())
        v.addWidget(self.flow_btn)
        row = QHBoxLayout()
        self.bake_btn = QPushButton("Bake stack")
        self.bake_btn.clicked.connect(lambda: self.bakeRequested.emit())
        self.revert_btn = QPushButton("Revert bake")
        self.revert_btn.clicked.connect(lambda: self.revertRequested.emit())
        row.addWidget(self.bake_btn)
        row.addWidget(self.revert_btn)
        v.addLayout(row)
        return group

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
        self.opacity_spin = QDoubleSpinBox()
        self.opacity_spin.setRange(0.0, 1.0)
        self.opacity_spin.setSingleStep(0.05)
        self.opacity_spin.setValue(1.0)
        self.opacity_spin.setToolTip("Layer opacity when compositing over the "
                                     "tracks below (1 = opaque)")
        from ..ffmpeg import BLEND_MODES
        self.blend_combo = QComboBox()
        self.blend_combo.addItems(["normal"] + sorted(BLEND_MODES))
        self.blend_combo.setToolTip("How this clip blends with the tracks below")
        self.gain_spin = QDoubleSpinBox()
        self.gain_spin.setRange(0.0, 4.0)
        self.gain_spin.setSingleStep(0.1)
        self.gain_spin.setValue(1.0)
        self.gain_spin.setToolTip("Audio gain for this clip when tracks are "
                                  "mixed (1 = unchanged, 0 = silent)")

        form.addRow("Speed ×", self.speed_spin)
        form.addRow("", self.reverse_chk)
        form.addRow("Fade in", self.fadein_spin)
        form.addRow("Fade out", self.fadeout_spin)
        form.addRow("Crossfade ⟵", self.xfade_spin)
        form.addRow("Opacity", self.opacity_spin)
        form.addRow("Blend", self.blend_combo)
        form.addRow("Gain", self.gain_spin)

        self.speed_spin.valueChanged.connect(self._emit_clip_props)
        self.reverse_chk.toggled.connect(self._emit_clip_props)
        self.fadein_spin.valueChanged.connect(self._emit_clip_props)
        self.fadeout_spin.valueChanged.connect(self._emit_clip_props)
        self.xfade_spin.valueChanged.connect(self._emit_clip_props)
        self.opacity_spin.valueChanged.connect(self._emit_clip_props)
        self.blend_combo.currentTextChanged.connect(self._emit_clip_props)
        self.gain_spin.valueChanged.connect(self._emit_clip_props)
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
            "opacity": self.opacity_spin.value(),
            "blend_mode": self.blend_combo.currentText(),
            "gain": self.gain_spin.value(),
        })

    def _populate_clip_props(self, clip) -> None:
        self._populating = True
        self.speed_spin.setValue(float(getattr(clip, "speed", 1.0)))
        self.reverse_chk.setChecked(bool(getattr(clip, "reverse", False)))
        self.fadein_spin.setValue(int(getattr(clip, "fade_in", 0)))
        self.fadeout_spin.setValue(int(getattr(clip, "fade_out", 0)))
        self.xfade_spin.setValue(int(getattr(clip, "transition_in", 0)))
        self.opacity_spin.setValue(float(getattr(clip, "opacity", 1.0)))
        self.blend_combo.setCurrentText(getattr(clip, "blend_mode", "normal"))
        self.gain_spin.setValue(float(getattr(clip, "gain", 1.0)))
        self._populating = False

    # -- pixel FX (clip finishing) ------------------------------------------ #

    def _build_pixel_group(self) -> QWidget:
        group = QWidget()
        v = QVBoxLayout(group)
        v.setContentsMargins(0, 0, 0, 4)
        v.setSpacing(3)

        self.pixel_list = _HintListWidget("No pixel FX")
        self.pixel_list.setMaximumHeight(72)
        self.pixel_list.setToolTip("Pixel filters, applied after the mosh stack "
                                   "and the speed/fade finishing. Double-click to "
                                   "edit one.")
        self.pixel_list.currentRowChanged.connect(self._on_pixel_row)
        self.pixel_list.itemDoubleClicked.connect(self._edit_pixel_item)
        v.addWidget(self.pixel_list)

        btns = QHBoxLayout()
        self.pixel_add_btn = QPushButton("+ Add ▾")
        self.pixel_add_btn.setToolTip("Add a pixel filter; its parameters open "
                                      "in a pop-up.")
        self.pixel_add_btn.clicked.connect(self._show_pixel_add_menu)
        self.pixel_remove_btn = QPushButton("− Remove")
        self.pixel_remove_btn.clicked.connect(self._emit_pixel_remove)
        btns.addWidget(self.pixel_add_btn)
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
            self.pixel_list.setCurrentRow(row)
        else:
            self._pixel_sel = -1
        self._update_pixel_buttons()

    def _on_pixel_row(self, row: int) -> None:
        if self._populating:
            return
        self._pixel_sel = row if 0 <= row < len(self._pixel_fx) else -1
        self._update_pixel_buttons()

    def _update_pixel_buttons(self) -> None:
        has_sel = (self._clip_id is not None
                   and 0 <= self._pixel_sel < len(self._pixel_fx))
        self.pixel_remove_btn.setEnabled(has_sel)

    def _show_pixel_add_menu(self) -> None:
        """Grouped '+ Add' menu of pixel filters; a pick opens its dialog."""
        if self._clip_id is None:
            return
        menu = effect_add_menu(self, "pixel", self._add_pixel_fx)
        menu.exec(self.pixel_add_btn.mapToGlobal(
            self.pixel_add_btn.rect().bottomLeft()))

    def _add_pixel_fx(self, name: str) -> None:
        # The host appends the FX with its defaults, then calls
        # open_pixel_live_editor() so its params tune with a live preview.
        self.pixelFxAddBegin.emit(name)

    def _edit_pixel_item(self, item: QListWidgetItem) -> None:
        self.open_pixel_live_editor(self.pixel_list.row(item))

    def open_pixel_live_editor(self, index: int) -> None:
        if not (0 <= index < len(self._pixel_fx)):
            return
        pe = self._pixel_fx[index]
        self._launch_live_editor(
            "pixel", pe.get("name", ""), pe.get("params") or {}, None,
            on_begin=lambda: self.pixelFxEditBegin.emit(index),
            on_update=lambda p, _r: self.pixelFxLiveUpdate.emit(index, p),
            on_end=lambda ok: self.pixelFxEditEnd.emit(index, ok))

    def _emit_pixel_remove(self) -> None:
        if 0 <= self._pixel_sel < len(self._pixel_fx):
            self.pixelFxRemoveRequested.emit(self._pixel_sel)

    # -- raw FX (numpy frame processors: pixel sort, ...) ------------------- #

    def _build_raw_group(self) -> QWidget:
        group = QWidget()
        v = QVBoxLayout(group)
        v.setContentsMargins(0, 0, 0, 4)
        v.setSpacing(3)

        self.raw_list = _HintListWidget("No raw FX")
        self.raw_list.setMaximumHeight(72)
        self.raw_list.setToolTip("Per-pixel effects applied to decoded frames "
                                 "(needs numpy); run before the FFmpeg pixel FX. "
                                 "Double-click to edit one.")
        self.raw_list.currentRowChanged.connect(self._on_raw_row)
        self.raw_list.itemDoubleClicked.connect(self._edit_raw_item)
        v.addWidget(self.raw_list)

        btns = QHBoxLayout()
        self.raw_add_btn = QPushButton("+ Add ▾")
        self.raw_add_btn.setToolTip("Add a raw (numpy) effect, grouped by type "
                                    "(incl. RAW DATA - AUDIO); parameters open in "
                                    "a pop-up.")
        self.raw_add_btn.clicked.connect(self._show_raw_add_menu)
        self.raw_remove_btn = QPushButton("− Remove")
        self.raw_remove_btn.clicked.connect(self._emit_raw_remove)
        btns.addWidget(self.raw_add_btn)
        btns.addWidget(self.raw_remove_btn)
        v.addLayout(btns)
        self._raw_group = group
        return group

    def set_clip_raw_fx(self, rfx: List[dict]) -> None:
        self._raw_fx = [dict(p) for p in (rfx or [])]
        prev = self._raw_sel
        self._populating = True
        self.raw_list.clear()
        for re in self._raw_fx:
            self.raw_list.addItem(re.get("name", ""))
        self._populating = False
        row = (prev if 0 <= prev < len(self._raw_fx)
               else (len(self._raw_fx) - 1 if self._raw_fx else -1))
        if row >= 0:
            self.raw_list.setCurrentRow(row)
        else:
            self._raw_sel = -1
        self._update_raw_buttons()

    def _on_raw_row(self, row: int) -> None:
        if self._populating:
            return
        self._raw_sel = row if 0 <= row < len(self._raw_fx) else -1
        self._update_raw_buttons()

    def _update_raw_buttons(self) -> None:
        has_sel = (self._clip_id is not None
                   and 0 <= self._raw_sel < len(self._raw_fx))
        self.raw_remove_btn.setEnabled(has_sel)

    def _show_raw_add_menu(self) -> None:
        """Grouped '+ Add' menu of raw effects; a pick opens its dialog."""
        if self._clip_id is None:
            return
        menu = effect_add_menu(self, "raw", self._add_raw_fx)
        menu.exec(self.raw_add_btn.mapToGlobal(
            self.raw_add_btn.rect().bottomLeft()))

    def _add_raw_fx(self, name: str) -> None:
        self.rawFxAddBegin.emit(name)

    def _edit_raw_item(self, item: QListWidgetItem) -> None:
        self.open_raw_live_editor(self.raw_list.row(item))

    def open_raw_live_editor(self, index: int) -> None:
        if not (0 <= index < len(self._raw_fx)):
            return
        re = self._raw_fx[index]
        self._launch_live_editor(
            "raw", re.get("name", ""), re.get("params") or {}, None,
            on_begin=lambda: self.rawFxEditBegin.emit(index),
            on_update=lambda p, _r: self.rawFxLiveUpdate.emit(index, p),
            on_end=lambda ok: self.rawFxEditEnd.emit(index, ok))

    def _emit_raw_remove(self) -> None:
        if 0 <= self._raw_sel < len(self._raw_fx):
            self.rawFxRemoveRequested.emit(self._raw_sel)

    # -- masks (layer matte + FX matte, keyed by luma/alpha/motion) ---------- #

    def _build_mask_group(self) -> QWidget:
        group = QWidget()
        v = QVBoxLayout(group)
        v.setContentsMargins(0, 0, 0, 4)
        v.setSpacing(3)
        self._mask_editors = {}
        for kind, label, tip in (
                ("layer", "Layer matte",
                 "Show this clip through a matte when compositing "
                 "(reveals the track below where the matte is dark)."),
                ("fx", "FX matte",
                 "Apply this clip's pixel + raw FX only where the matte is "
                 "bright.")):
            self._mask_editors[kind] = self._build_mask_editor(v, kind, label, tip)
        self._mask_group = group
        return group

    def _build_mask_editor(self, parent: QVBoxLayout, kind: str, label: str,
                           tip: str) -> dict:
        from ..ffmpeg import MASK_SOURCES, MASK_MODES
        enable = QCheckBox(label)
        enable.setToolTip(tip)
        parent.addWidget(enable)
        host = QWidget()
        form = QFormLayout(host)
        form.setContentsMargins(12, 0, 0, 2)
        form.setSpacing(2)
        source = QComboBox()
        source.addItems(MASK_SOURCES)
        key = QLineEdit("#00ff00")
        key.setToolTip("Key color to remove (chroma source); #rrggbb or a name")
        key_row = QWidget()
        _kr = QHBoxLayout(key_row)
        _kr.setContentsMargins(0, 0, 0, 0)
        _kr.addWidget(key)
        key_row.setVisible(False)
        lo = QDoubleSpinBox()
        lo.setRange(0.0, 1.0)
        lo.setSingleStep(0.05)
        hi = QDoubleSpinBox()
        hi.setRange(0.0, 1.0)
        hi.setSingleStep(0.05)
        hi.setValue(1.0)
        invert = QCheckBox("Invert")
        feather = QSpinBox()
        feather.setRange(0, 50)
        feather.setSuffix(" px")
        form.addRow("Source", source)
        form.addRow("Key", key_row)
        form.addRow("Threshold lo", lo)
        form.addRow("Threshold hi", hi)
        form.addRow("", invert)
        form.addRow("Feather", feather)
        mode = None
        if kind == "fx":                           # confine vs overspill (FX only)
            mode = QComboBox()
            mode.addItems(MASK_MODES)
            mode.setToolTip("confine = effect stays inside the matte; "
                            "source = effect generated from the matte, free to "
                            "spill out")
            form.addRow("Mode", mode)
        parent.addWidget(host)
        host.setVisible(False)
        ed = {"enable": enable, "host": host, "source": source, "key": key,
              "key_row": key_row, "lo": lo, "hi": hi, "invert": invert,
              "feather": feather, "mode": mode}
        enable.toggled.connect(host.setVisible)
        source.currentTextChanged.connect(
            lambda s=None, e=ed: e["key_row"].setVisible(
                e["source"].currentText() == "chroma"))
        for w in (enable, invert):
            w.toggled.connect(lambda _=False, k=kind: self._emit_mask(k))
        source.currentTextChanged.connect(lambda _=None, k=kind: self._emit_mask(k))
        key.textChanged.connect(lambda _=None, k=kind: self._emit_mask(k))
        if mode is not None:
            mode.currentTextChanged.connect(lambda _=None, k=kind: self._emit_mask(k))
        for sp in (lo, hi, feather):
            sp.valueChanged.connect(lambda _=None, k=kind: self._emit_mask(k))
        return ed

    def _emit_mask(self, kind: str) -> None:
        if self._populating or self._clip_id is None:
            return
        self.maskChanged.emit(kind, self._read_mask(kind))

    def _read_mask(self, kind: str):
        ed = self._mask_editors[kind]
        if not ed["enable"].isChecked():
            return None
        spec = {"source": ed["source"].currentText(),
                "lo": round(ed["lo"].value(), 4), "hi": round(ed["hi"].value(), 4),
                "invert": ed["invert"].isChecked(),
                "feather": ed["feather"].value()}
        if spec["source"] == "chroma":
            spec["key"] = ed["key"].text().strip() or "#00ff00"
        if ed["mode"] is not None:
            spec["mode"] = ed["mode"].currentText()
        return spec

    def set_clip_masks(self, layer_spec, fx_spec) -> None:
        self._populating = True
        for kind, spec in (("layer", layer_spec), ("fx", fx_spec)):
            ed = self._mask_editors[kind]
            on = bool(spec)
            ed["enable"].setChecked(on)
            ed["host"].setVisible(on)
            spec = spec or {}
            src = str(spec.get("source", "luma"))
            ed["source"].setCurrentText(src)
            ed["key_row"].setVisible(src == "chroma")
            ed["key"].setText(str(spec.get("key", "#00ff00")))
            ed["lo"].setValue(float(spec.get("lo", 0.0)))
            ed["hi"].setValue(float(spec.get("hi", 1.0)))
            ed["invert"].setChecked(bool(spec.get("invert", False)))
            ed["feather"].setValue(int(spec.get("feather", 0)))
            if ed["mode"] is not None:
                ed["mode"].setCurrentText(str(spec.get("mode", "confine")))
        self._populating = False

    # -- flow FX (live optical-flow warp) ----------------------------------- #

    def _build_flow_group(self) -> QWidget:
        group = QWidget()
        v = QVBoxLayout(group)
        v.setContentsMargins(0, 0, 0, 4)
        v.setSpacing(3)

        r1 = QHBoxLayout()
        self.flow_source = QComboBox()
        self.flow_source.setToolTip("Warp this clip by another clip's motion "
                                    "((none) = off; appearance-free)")
        self.flow_strength = QDoubleSpinBox()
        self.flow_strength.setRange(0.1, 5.0)
        self.flow_strength.setSingleStep(0.1)
        self.flow_strength.setValue(1.0)
        r1.addWidget(QLabel("Motion"))
        r1.addWidget(self.flow_source, 1)
        r1.addWidget(QLabel("×"))
        r1.addWidget(self.flow_strength)
        v.addLayout(r1)

        r2 = QHBoxLayout()
        self.flow_hold = QCheckBox("Hold")
        self.flow_hold.setChecked(True)
        self.flow_accum = QCheckBox("Accumulate")
        self.flow_accum.setChecked(True)
        self.flow_preset = QComboBox()
        self.flow_preset.addItems(["ultrafast", "fast", "medium"])
        self.flow_preset.setCurrentText("fast")
        r2.addWidget(self.flow_hold)
        r2.addWidget(self.flow_accum)
        r2.addWidget(self.flow_preset)
        r2.addStretch(1)
        v.addLayout(r2)

        r3 = QHBoxLayout()
        self.flow_region_chk = QCheckBox("Limit to frames")
        self.flow_region_start = QSpinBox()
        self.flow_region_start.setRange(0, 1_000_000)
        self.flow_region_end = QSpinBox()
        self.flow_region_end.setRange(0, 1_000_000)
        self.flow_region_end.setSpecialValueText("end")
        r3.addWidget(self.flow_region_chk)
        r3.addWidget(self.flow_region_start)
        r3.addWidget(QLabel("–"))
        r3.addWidget(self.flow_region_end)
        r3.addStretch(1)
        v.addLayout(r3)

        self.flow_source.currentIndexChanged.connect(self._emit_flow)
        self.flow_strength.valueChanged.connect(self._emit_flow)
        self.flow_hold.toggled.connect(self._emit_flow)
        self.flow_accum.toggled.connect(self._emit_flow)
        self.flow_preset.currentTextChanged.connect(self._emit_flow)
        self.flow_region_chk.toggled.connect(self._sync_flow_region)
        self.flow_region_chk.toggled.connect(self._emit_flow)
        self.flow_region_start.valueChanged.connect(self._emit_flow)
        self.flow_region_end.valueChanged.connect(self._emit_flow)
        self._flow_group = group
        return group

    def set_flow_sources(self, choices) -> None:
        cur = self.flow_source.currentData()
        self._populating = True
        self.flow_source.clear()
        self.flow_source.addItem("(none)", None)
        for label, media_id in choices:
            self.flow_source.addItem(label, media_id)
        idx = self.flow_source.findData(cur)
        self.flow_source.setCurrentIndex(idx if idx >= 0 else 0)
        self._populating = False

    def _sync_flow_region(self, *_a) -> None:
        on = self.flow_region_chk.isChecked()
        self.flow_region_start.setEnabled(on)
        self.flow_region_end.setEnabled(on)

    def _emit_flow(self, *_a) -> None:
        if self._populating:
            return
        media_id = self.flow_source.currentData()
        if not media_id:
            self.flowChanged.emit(None)
            return
        ft = {"source": media_id, "strength": self.flow_strength.value(),
              "hold": self.flow_hold.isChecked(),
              "accumulate": self.flow_accum.isChecked(),
              "preset": self.flow_preset.currentText()}
        if self.flow_region_chk.isChecked():
            ft["region_start"] = self.flow_region_start.value()
            end = self.flow_region_end.value()
            ft["region_end"] = None if end == 0 else end
        self.flowChanged.emit(ft)

    def _populate_flow(self, ft) -> None:
        self._populating = True
        if ft:
            idx = self.flow_source.findData(ft.get("source"))
            self.flow_source.setCurrentIndex(idx if idx >= 0 else 0)
            self.flow_strength.setValue(float(ft.get("strength", 1.0)))
            self.flow_hold.setChecked(bool(ft.get("hold", True)))
            self.flow_accum.setChecked(bool(ft.get("accumulate", True)))
            self.flow_preset.setCurrentText(ft.get("preset", "fast"))
            has_region = "region_start" in ft or ft.get("region_end") is not None
            self.flow_region_chk.setChecked(has_region)
            self.flow_region_start.setValue(int(ft.get("region_start", 0) or 0))
            self.flow_region_end.setValue(0 if ft.get("region_end") is None
                                          else int(ft["region_end"]))
        else:
            self.flow_source.setCurrentIndex(0)
            self.flow_strength.setValue(1.0)
            self.flow_hold.setChecked(True)
            self.flow_accum.setChecked(True)
            self.flow_preset.setCurrentText("fast")
            self.flow_region_chk.setChecked(False)
            self.flow_region_start.setValue(0)
            self.flow_region_end.setValue(0)
        self._sync_flow_region()
        self._populating = False

    # -- effect region (apply to a frame range) ----------------------------- #

    # -- external state ----------------------------------------------------- #

    def set_motion_labels(self, labels: List[str]) -> None:
        # stored for the effect dialog's clip_ref (motion-source) pickers
        self._motion_labels = list(labels)

    def set_enabled_for_clip(self, clip_id: Optional[str], label: Optional[str],
                             clip=None, effects: Optional[List[dict]] = None) -> None:
        self._clip_id = clip_id
        on = clip_id is not None
        self._body.setVisible(on)           # blank panel until a clip is selected
        self.bake_btn.setEnabled(on)
        self.flow_btn.setEnabled(on)
        self.preset_save_btn.setEnabled(on)
        self.preset_apply_btn.setEnabled(on)
        self._clip_group.setEnabled(on)
        self._pixel_group.setEnabled(on)
        self._raw_group.setEnabled(on)
        self._mask_group.setEnabled(on)
        self._flow_group.setEnabled(on)
        self.effect_list.setEnabled(on)
        if on:
            self.clip_lbl.setText(f"Clip: <b>{label}</b>")
            self._populate_clip_props(clip if clip is not None else object())
            self.set_clip_pixel_fx(getattr(clip, "pixel_effects", []))
            self.set_clip_raw_fx(getattr(clip, "raw_effects", []))
            self.set_clip_masks(getattr(clip, "layer_mask", None),
                                getattr(clip, "fx_mask", None))
            self._populate_flow(getattr(clip, "flow_transfer", None))
            self.set_clip_effects(effects or [])
        else:
            self.clip_lbl.setText("Select a clip on the main track.")
            self._populate_clip_props(object())        # reset to defaults
            self.set_clip_pixel_fx([])
            self.set_clip_raw_fx([])
            self.set_clip_masks(None, None)
            self._populate_flow(None)
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
            self.effect_list.setCurrentRow(row)   # -> _on_effect_row selects it
        else:
            self._selected_op = None
            self._update_stack_buttons()

    def _on_effect_row(self, row: int) -> None:
        if self._populating:
            return
        self._selected_op = (self._effects[row]["id"]
                             if 0 <= row < len(self._effects) else None)
        self._update_stack_buttons()

    def _show_add_menu(self) -> None:
        """Grouped '+ Add' menu of mosh corruption types; a pick opens its dialog."""
        if self._clip_id is None:
            return
        menu = effect_add_menu(self, "mosh", self._add_effect)
        menu.exec(self.add_btn.mapToGlobal(self.add_btn.rect().bottomLeft()))

    def _add_effect(self, mode_name: str) -> None:
        # The host creates the effect with its defaults, then calls
        # open_live_editor() so the parameters can be tuned with a live preview.
        self.effectAddBegin.emit(mode_name)

    def _edit_effect_item(self, item: QListWidgetItem) -> None:
        self.open_live_editor(item.data(Qt.ItemDataRole.UserRole))

    def _launch_live_editor(self, kind: str, name: str, params, region, *,
                            on_begin, on_update, on_end) -> None:
        """Open a non-modal live parameter editor for any effect *kind* and wire
        its begin/live-update/finished signals through the supplied callbacks.
        Only one editor is open at a time; a new one commits the last."""
        if self._live_dlg is not None:                 # commit the one it replaces
            self._live_dlg.accept()
        dlg = EffectParamDialog(
            self, kind=kind, mode_name=name, params=params or {}, region=region,
            motion_labels=self._motion_labels, beat_provider=self._beat_provider,
            clip_id=self._clip_id, verb="Edit", live=True)
        self._live_dlg = dlg
        on_begin()
        dlg.paramsEdited.connect(lambda p, r: on_update(p, r))

        def _done(code, _dlg=dlg):
            if self._live_dlg is _dlg:
                self._live_dlg = None
            on_end(code == QDialog.DialogCode.Accepted)
        dlg.finished.connect(_done)
        dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        dlg.show()
        dlg.raise_()

    def open_live_editor(self, op_id: str) -> None:
        """Open the non-modal live parameter editor for a mosh effect in the
        stack. Called on double-click and just after a live add."""
        e = next((x for x in self._effects if x["id"] == op_id), None)
        if e is None:
            return
        mode = e["mode"]
        self._launch_live_editor(
            "mosh", mode, e.get("params") or {}, e.get("region"),
            on_begin=lambda: self.effectEditBegin.emit(op_id),
            on_update=lambda p, r: self.effectLiveUpdate.emit(op_id, mode, p, r),
            on_end=lambda ok: self.effectEditEnd.emit(op_id, ok))

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
        self.remove_btn.setEnabled(has_sel)
        idx = next((i for i, e in enumerate(self._effects)
                    if e["id"] == self._selected_op), -1)
        self.up_btn.setEnabled(has_sel and idx > 0)
        self.down_btn.setEnabled(has_sel and 0 <= idx < len(self._effects) - 1)

    def set_beat_provider(self, provider) -> None:
        """Inject a callable ``clip_id -> [normalised beat positions]`` (used by
        the effect dialog's automatable params)."""
        self._beat_provider = provider

    # -- presets ------------------------------------------------------------ #

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
