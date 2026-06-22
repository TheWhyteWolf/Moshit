"""Controller layer: the bridge between the Qt UI and the headless engine.

Owns the :class:`Project` and :class:`MoshEngine`. Slow operations (import,
preview render, export, bake) run on a worker thread; their results are
delivered back on the main thread via a QObject slot (so the UI is only ever
touched from the main thread). Instant model edits (add clip, set mosh op) run
synchronously.

The ``_do_*`` methods are the actual work and are callable directly, which keeps
the whole pipeline testable without a running event loop.
"""
from __future__ import annotations

import atexit
import copy
import os
import shutil
import tempfile
import traceback
from pathlib import Path
from typing import Callable, List, Optional

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot

from ..engine import EngineConfig, MoshEngine
from ..ffmpeg import FFmpeg
from ..modes import load_modes
from ..project import Project
from .preview import PreviewDecoder


class _WorkerSignals(QObject):
    finished = Signal(object)
    error = Signal(str)


class _Worker(QRunnable):
    def __init__(self, fn: Callable):
        super().__init__()
        self.fn = fn
        self.signals = _WorkerSignals()

    @Slot()
    def run(self):
        try:
            self.signals.finished.emit(self.fn())
        except Exception as exc:                  # surfaced to the UI
            traceback.print_exc()
            self.signals.error.emit(str(exc))


class _StreamSignals(QObject):
    begin = Signal(int, float)            # total_frames (estimate), fps
    batch = Signal(list)                  # list[QImage]
    done = Signal()
    error = Signal(str)


class _StreamWorker(QRunnable):
    """Runs a render+decode that reports frames progressively."""

    def __init__(self, fn: Callable):
        super().__init__()
        self.fn = fn                       # fn(emit_begin, emit_batch)
        self.signals = _StreamSignals()

    @Slot()
    def run(self):
        try:
            self.fn(self.signals.begin.emit, self.signals.batch.emit)
            self.signals.done.emit()
        except Exception as exc:
            traceback.print_exc()
            self.signals.error.emit(str(exc))


class AppController(QObject):
    media_added = Signal(object)          # MediaItem
    project_changed = Signal()
    busy = Signal(bool, str)              # (is_busy, message)
    error = Signal(str)
    status = Signal(str)
    preview_begin = Signal(int, float)    # (total_frames, fps) — stream start
    preview_batch = Signal(list)          # list[QImage] — a chunk of frames
    preview_done = Signal()               # stream complete

    def __init__(self, config: Optional[EngineConfig] = None,
                 ffmpeg_bin: Optional[str] = None,
                 ffprobe_bin: Optional[str] = None):
        super().__init__()
        load_modes()
        self._dir = Path(tempfile.mkdtemp(prefix="moshit_gui_"))
        self.ff = FFmpeg(ffmpeg=ffmpeg_bin, ffprobe=ffprobe_bin)
        self.config = config or EngineConfig(work_dir=str(self._dir / "work"))
        self.engine = MoshEngine(self.config, self.ff)
        self.project = Project(name="untitled", config=self.config,
                               assets_dir=str(self._dir / "assets"))
        self.decoder = PreviewDecoder(self.ff.ffmpeg)
        self.pool = QThreadPool.globalInstance()
        self._busy = False
        self._pending: Optional[Callable] = None
        self._cleaned = False
        self._undo: List = []                 # snapshots of (clips, mosh_ops)
        self._redo: List = []
        self._undo_limit = 64
        # Safety net: clean the temp dir even if the window's closeEvent never
        # fires (e.g. the process is interrupted from the terminal).
        atexit.register(self.cleanup)

    # -- export profiles available on this ffmpeg --------------------------- #

    @property
    def is_busy(self) -> bool:
        return self._busy

    def export_profiles(self) -> List[str]:
        return self.ff.capabilities().available_export_profiles()

    def motion_labels(self) -> List[str]:
        return [m.label for m in self.project.media.values()]

    def set_project_config(self, *, width: int, height: int, fps: float,
                           gop: Optional[int] = None,
                           qscale: Optional[int] = None) -> bool:
        """Change the sequence geometry/fps. Only allowed before any media is
        imported, since every clip is normalised to these on import.

        ``self.config`` is shared by the engine and the project, so mutating it
        in place updates both. Returns False (with an error) if media exists.
        """
        if self.project.media:
            self.error.emit("Project settings can only change before importing "
                            "media. Start a new project to change them.")
            return False
        self.config.width = max(2, int(width))
        self.config.height = max(2, int(height))
        self.config.fps = max(1.0, float(fps))
        if gop is not None:
            self.config.gop = max(1, int(gop))
        if qscale is not None:
            self.config.qscale = max(1, int(qscale))
        self.project_changed.emit()
        self.status.emit(f"Sequence: {self.config.width}x{self.config.height} "
                         f"@ {self.config.fps:g}fps")
        return True

    @property
    def has_media(self) -> bool:
        return bool(self.project.media)

    # -- the actual work (synchronous; directly testable) ------------------- #

    def _do_import(self, path, role):
        return self.project.import_media(self.engine, path, role=role)

    def _do_render_preview(self):
        out = self._dir / "preview.avi"
        self.project.render(self.engine, out)
        frames, fps, _ = self.decoder.decode(out, max_width=720)
        return frames, fps

    def _do_export(self, profile, path, audio=True):
        out = self._dir / "export_src.avi"
        r = self.project.render(self.engine, out, profile=profile,
                                export_path=str(path), audio=audio)
        return r["export"]

    def _do_bake(self, op_id):
        return self.project.bake_op(self.engine, op_id)

    # -- off-thread dispatch ------------------------------------------------ #

    def _run(self, fn: Callable, on_done: Callable, message: str) -> None:
        if self._busy:
            self.error.emit("Still working on the previous task - please wait.")
            return
        self._busy = True
        self._pending = on_done
        self.busy.emit(True, message)
        worker = _Worker(fn)
        worker.signals.finished.connect(self._on_finished)   # queued -> main thread
        worker.signals.error.connect(self._on_error)
        self.pool.start(worker)

    @Slot(object)
    def _on_finished(self, result):
        self._busy = False
        self.busy.emit(False, "")
        cb, self._pending = self._pending, None
        if cb:
            cb(result)

    @Slot(str)
    def _on_error(self, message: str):
        self._busy = False
        self._pending = None
        self.busy.emit(False, "")
        self.error.emit(message)

    # -- public async operations -------------------------------------------- #

    def import_media(self, path, role: str = "any") -> None:
        def done(item):
            self.media_added.emit(item)
            self.project_changed.emit()
            self.status.emit(f"Imported {item.label} ({item.nb_frames} frames)")
        self._run(lambda: self._do_import(path, role), done,
                  f"Importing {Path(path).name}…")

    _TRANSFORM_LABELS = {"zoom_in": "zoom-in", "zoom_out": "zoom-out",
                         "pan_x": "pan-x", "pan_y": "pan-y", "rotate": "rotate"}

    def add_transform_source(self, kind: str, *, frames: int = 120,
                             speed: float = 1.0) -> None:
        """Generate a zoom/pan/rotate motion source and add it to the motion
        track (its motion can then drive a base clip via motion_splice)."""
        label = self._TRANSFORM_LABELS.get(kind, kind)

        def work():
            fd, src = tempfile.mkstemp(prefix=f"gen_{kind}_", suffix=".mp4",
                                       dir=str(self._dir))
            os.close(fd)
            self.engine.render_transform(kind, src, frames=frames, speed=speed)
            return self.project.import_media(self.engine, src, label=label)

        def done(item):
            self.media_added.emit(item)
            self.add_clip_for_media(item.id, "motion")   # emits project_changed
            self.status.emit(f"Generated {label} motion source")
        self._run(work, done, f"Generating {label} motion source…")

    def refresh_preview(self) -> None:
        if self._busy:
            return
        if not self.project.main_clips():
            self.error.emit("Add a clip to the main track first.")
            return
        self._busy = True
        self.busy.emit(True, "Rendering preview…")
        out = self._dir / "preview.avi"

        def work(emit_begin, emit_batch):
            self.project.render(self.engine, out)
            self.decoder.decode_stream(out, emit_begin, emit_batch, max_width=720)

        worker = _StreamWorker(work)
        worker.signals.begin.connect(self._on_preview_begin)   # queued -> main
        worker.signals.batch.connect(self._on_preview_batch)
        worker.signals.done.connect(self._on_preview_done)
        worker.signals.error.connect(self._on_error)
        self.pool.start(worker)

    @Slot(int, float)
    def _on_preview_begin(self, total: int, fps: float):
        self.preview_begin.emit(total, fps)

    @Slot(list)
    def _on_preview_batch(self, frames: list):
        self.preview_batch.emit(frames)

    @Slot()
    def _on_preview_done(self):
        self._busy = False
        self.busy.emit(False, "")
        self.preview_done.emit()
        self.status.emit("Preview updated.")

    def export(self, profile: str, path, audio: bool = True) -> None:
        def done(p):
            self.status.emit(f"Exported → {p}")
        self._run(lambda: self._do_export(profile, path, audio), done,
                  f"Exporting {profile}…")

    def export_frame(self, frame_index: int, path) -> None:
        """Save one frame of the current preview render as a full-resolution
        image. Requires a preview to have been rendered (``preview.avi``)."""
        src = self._dir / "preview.avi"
        if not src.exists():
            self.error.emit("Render a preview first, then save a frame.")
            return

        def done(p):
            self.status.emit(f"Saved frame → {p}")
        self._run(lambda: (self.ff.snapshot(src, path, frame_index), Path(path))[1],
                  done, "Saving frame…")

    def bake(self, op_id: str) -> None:
        def done(_rec):
            self._clear_undo()
            self.project_changed.emit()
            self.status.emit("Baked (revertible).")
        self._run(lambda: self._do_bake(op_id), done, "Baking…")

    def bake_clip(self, clip_id: str) -> None:
        def done(_rec):
            self._clear_undo()
            self.project_changed.emit()
            self.status.emit("Baked effect stack (revertible).")
        self._run(lambda: self.project.bake_clip(self.engine, clip_id), done,
                  "Baking…")

    # -- optical-flow motion transfer --------------------------------------- #

    def flow_available(self) -> bool:
        from ..flow import available
        return available()

    def flow_backend(self) -> str:
        from ..flow import backend
        return backend()

    def media_choices(self):
        """(label, media_id) for every imported clip -- candidate flow drivers."""
        return [(m.label, m.id) for m in self.project.media.values()]

    def apply_optical_flow(self, base_clip_id: str, motion_media_id: str,
                           **params) -> None:
        if not self.flow_available():
            self.error.emit("Optical-flow transfer needs OpenCV + numpy: "
                            "pip install 'moshit[flow]'")
            return

        def work():
            return self.project.apply_optical_flow(
                self.engine, base_clip_id, motion_media_id, **params)

        def done(_rec):
            self._clear_undo()
            self.project_changed.emit()
            self.status.emit(f"Optical-flow transfer applied "
                             f"({self.flow_backend()}); revertible.")
        self._run(work, done, "Optical-flow transfer…")

    # -- undo / redo (snapshots of the editable timeline state) ------------- #

    def _snapshot(self):
        return (copy.deepcopy(self.project.clips),
                copy.deepcopy(self.project.mosh_ops))

    def _commit_undo(self, snap) -> None:
        self._undo.append(snap)
        if len(self._undo) > self._undo_limit:
            self._undo.pop(0)
        self._redo.clear()

    def _push_undo(self) -> None:
        """Record current state so the edit about to happen can be undone."""
        self._commit_undo(self._snapshot())

    def _restore(self, snap) -> None:
        self.project.clips = copy.deepcopy(snap[0])
        self.project.mosh_ops = copy.deepcopy(snap[1])

    def _clear_undo(self) -> None:
        self._undo.clear()
        self._redo.clear()

    @property
    def can_undo(self) -> bool:
        return bool(self._undo)

    @property
    def can_redo(self) -> bool:
        return bool(self._redo)

    def undo(self) -> None:
        if not self._undo:
            return
        self._redo.append(self._snapshot())
        self._restore(self._undo.pop())
        self.project_changed.emit()
        self.status.emit("Undo")

    def redo(self) -> None:
        if not self._redo:
            return
        self._undo.append(self._snapshot())
        self._restore(self._redo.pop())
        self.project_changed.emit()
        self.status.emit("Redo")

    # -- instant model edits ------------------------------------------------ #

    def add_clip_for_media(self, media_id: str, track: str = "main"):
        media = self.project.media[media_id]
        track = "motion" if track == "motion" else "main"
        self._push_undo()
        clip = self.project.add_clip(media_id, track)
        self.project_changed.emit()
        self.status.emit(f"Added {media.label} to the {track} track")
        return clip

    # -- pixel FX (clip finishing) ------------------------------------------ #

    def clip_pixel_fx(self, clip_id: str) -> List[dict]:
        try:
            return [dict(pe) for pe in self.project.clip(clip_id).pixel_effects]
        except KeyError:
            return []

    def add_pixel_fx(self, clip_id: str, name: str):
        from ..modes import get_pixel_mode
        try:
            c = self.project.clip(clip_id)
            mode = get_pixel_mode(name)
        except KeyError:
            return None
        self._push_undo()
        c.pixel_effects.append({"name": name, "params": mode.defaults()})
        self.project_changed.emit()
        self.status.emit(f"Added pixel FX: {name}")
        return c

    def remove_pixel_fx(self, clip_id: str, index: int) -> None:
        try:
            c = self.project.clip(clip_id)
        except KeyError:
            return
        if not (0 <= index < len(c.pixel_effects)):
            return
        self._push_undo()
        c.pixel_effects.pop(index)
        self.project_changed.emit()
        self.status.emit("Removed pixel FX")

    def update_pixel_fx(self, clip_id: str, index: int, params: dict) -> None:
        try:
            c = self.project.clip(clip_id)
        except KeyError:
            return
        if not (0 <= index < len(c.pixel_effects)):
            return
        if c.pixel_effects[index].get("params") == params:
            return
        self._push_undo()
        c.pixel_effects[index]["params"] = dict(params)
        self.project_changed.emit()

    # -- effect stack ------------------------------------------------------- #

    @staticmethod
    def _region_tuple(region):
        """Normalise a GUI region (None or [start, end]) to (start, end|None)."""
        if not region:
            return (0, None)
        start, end = region
        return (max(0, int(start)), None if end is None else int(end))

    def clip_effects(self, clip_id: str) -> List[dict]:
        """The clip's effect stack as plain dicts for the inspector."""
        out = []
        for o in self.project.clip_ops(clip_id):
            region = ((o.region_start, o.region_end)
                      if (o.region_start or o.region_end is not None) else None)
            out.append({"id": o.id, "mode": o.mode, "params": dict(o.params),
                        "enabled": o.enabled, "region": region})
        return out

    def add_effect(self, clip_id: str, mode: str, params: dict, region=None):
        self._push_undo()
        op = self.project.add_mosh(mode, params, clip_id)
        op.region_start, op.region_end = self._region_tuple(region)
        self.project_changed.emit()
        self.status.emit(f"Added {mode} to the effect stack")
        return op

    def update_effect(self, op_id: str, mode: str, params: dict, region=None):
        try:
            op = self.project.op(op_id)
        except KeyError:
            return None
        new_region = self._region_tuple(region)
        if (op.mode == mode and op.params == params
                and (op.region_start, op.region_end) == new_region):
            return op
        self._push_undo()
        op.mode = mode
        op.params = dict(params)
        op.region_start, op.region_end = new_region
        self.project_changed.emit()
        return op

    def remove_effect(self, op_id: str) -> None:
        self._push_undo()
        if not self.project.remove_mosh(op_id):
            self._undo.pop()                       # nothing removed; drop the snapshot
            return
        self.project_changed.emit()
        self.status.emit("Removed effect")

    def move_effect(self, op_id: str, delta: int) -> None:
        snap = self._snapshot()
        if not self.project.move_mosh(op_id, delta):
            return
        self._commit_undo(snap)
        self.project_changed.emit()

    def set_effect_enabled(self, op_id: str, on: bool) -> None:
        try:
            op = self.project.op(op_id)
        except KeyError:
            return
        if op.enabled == on:
            return
        self._push_undo()
        op.enabled = on
        self.project_changed.emit()
        self.status.emit(("Enabled " if on else "Disabled ") + op.mode)

    # -- effect-stack presets ----------------------------------------------- #

    def preset_names(self) -> List[str]:
        from ..presets import preset_names
        return preset_names()

    def save_stack_as_preset(self, clip_id: str, name: str) -> bool:
        from ..presets import save_preset
        effects = []
        for o in self.project.clip_ops(clip_id):
            region = ((o.region_start, o.region_end)
                      if (o.region_start or o.region_end is not None) else None)
            effects.append({"mode": o.mode, "params": dict(o.params),
                            "region": list(region) if region else None,
                            "enabled": o.enabled})
        if not effects:
            self.error.emit("This clip has no effects to save as a preset.")
            return False
        save_preset(name, effects)
        self.status.emit(f"Saved preset '{name}'")
        return True

    def apply_preset(self, clip_id: str, name: str, *, replace: bool = True) -> None:
        from ..presets import load_presets
        effects = load_presets().get(name)
        if not effects:
            self.error.emit(f"Preset '{name}' is missing or empty.")
            return
        self._push_undo()
        if replace:
            self.project.mosh_ops = [o for o in self.project.mosh_ops
                                     if o.target_clip_id != clip_id or o.archived]
        for e in effects:
            op = self.project.add_mosh(e["mode"], dict(e.get("params") or {}),
                                       clip_id)
            op.region_start, op.region_end = self._region_tuple(e.get("region"))
            op.enabled = bool(e.get("enabled", True))
        self.project_changed.emit()
        self.status.emit(f"Applied preset '{name}'")

    def delete_preset(self, name: str) -> None:
        from ..presets import delete_preset
        if delete_preset(name):
            self.status.emit(f"Deleted preset '{name}'")

    def set_clip_props(self, clip_id: str, props: dict):
        """Set a main clip's finishing properties (speed/reverse/fades/crossfade).

        No-ops (and records no undo) when nothing actually changes, so simply
        re-selecting a clip never dirties the project.
        """
        try:
            c = self.project.clip(clip_id)
        except KeyError:
            return None
        speed = max(0.1, min(8.0, float(props.get("speed", c.speed))))
        reverse = bool(props.get("reverse", c.reverse))
        fade_in = max(0, int(props.get("fade_in", c.fade_in)))
        fade_out = max(0, int(props.get("fade_out", c.fade_out)))
        trans = max(0, int(props.get("transition_in", c.transition_in)))
        if (speed, reverse, fade_in, fade_out, trans) == (
                c.speed, c.reverse, c.fade_in, c.fade_out, c.transition_in):
            return c
        self._push_undo()
        c.speed, c.reverse = speed, reverse
        c.fade_in, c.fade_out, c.transition_in = fade_in, fade_out, trans
        if c.track == "main":
            self._repack_main()        # speed changes length -> reposition the rest
        self.project_changed.emit()
        self.status.emit("Clip updated.")
        return c

    def revert_last_bake(self) -> None:
        if not self.project.bake_records:
            self.error.emit("Nothing to revert.")
            return
        self.project.revert_bake(self.project.bake_records[-1].id)
        self._clear_undo()
        self.project_changed.emit()
        self.status.emit("Reverted last bake.")

    # -- timeline editing --------------------------------------------------- #

    def _visible_main(self):
        clips = [c for c in self.project.clips
                 if c.track == "main" and c.enabled and not c.archived]
        clips.sort(key=lambda c: c.start)
        return clips

    def _repack_main(self) -> None:
        """Pack visible main-track clips contiguously by their current order."""
        cursor = 0
        for c in self._visible_main():
            c.start = cursor
            cursor += self.project._clip_length(c)

    def reorder_main_clip(self, clip_id: str, new_index: int) -> None:
        ordered = self._visible_main()
        ids = [c.id for c in ordered]
        if clip_id not in ids:
            return
        self._push_undo()
        ids.remove(clip_id)
        new_index = max(0, min(int(new_index), len(ids)))
        ids.insert(new_index, clip_id)
        by_id = {c.id: c for c in ordered}
        cursor = 0
        for cid in ids:
            c = by_id[cid]
            c.start = cursor
            cursor += self.project._clip_length(c)
        self.project_changed.emit()

    def trim_clip(self, clip_id: str, in_point=None, out_point=None) -> None:
        c = self.project.clip(clip_id)
        media = self.project.media[c.media_id]
        self._push_undo()
        cur_out = c.out_point if c.out_point is not None else media.nb_frames
        if in_point is not None:
            c.in_point = max(0, min(int(in_point), cur_out - 1))
        if out_point is not None:
            c.out_point = max(c.in_point + 1, min(int(out_point), media.nb_frames))
        if c.track == "main":
            self._repack_main()
        self.project_changed.emit()

    def remove_clip(self, clip_id: str) -> None:
        try:
            c = self.project.clip(clip_id)
        except KeyError:
            return
        if c.archived:                 # archived clips are bake history; leave them
            return
        self._push_undo()
        self.project.clips = [x for x in self.project.clips if x.id != clip_id]
        self.project.mosh_ops = [o for o in self.project.mosh_ops
                                 if o.target_clip_id != clip_id]
        if c.track == "main":
            self._repack_main()
        self.project_changed.emit()

    def split_clip(self, clip_id: str, offset: int) -> None:
        snap = self._snapshot()
        new = self.project.split_clip(clip_id, offset)
        if new is None:
            return
        self._commit_undo(snap)
        if new.track == "main":
            self._repack_main()
        self.project_changed.emit()
        self.status.emit("Split clip.")

    def duplicate_clip(self, clip_id: str):
        try:
            c = self.project.clip(clip_id)
        except KeyError:
            return None
        if c.archived:
            return None
        self._push_undo()
        new = self.project.duplicate_clip(clip_id)
        if new.track == "main":
            self._repack_main()
        self.project_changed.emit()
        self.status.emit("Duplicated clip.")
        return new

    # -- project persistence ------------------------------------------------ #

    def save_project(self, path) -> None:
        path = Path(path)
        assets = path.parent / f"{path.stem}_assets"
        assets.mkdir(parents=True, exist_ok=True)
        for m in self.project.media.values():
            src = Path(m.intermediate_path)
            dst = assets / f"{m.id}.avi"
            if src.exists() and src.resolve() != dst.resolve():
                shutil.copy2(src, dst)
            m.intermediate_path = str(dst)
        self.project.assets_dir = assets
        self.project.save(path)
        self.status.emit(f"Saved project → {path}")

    def open_project(self, path):
        proj = Project.load(path)
        self.config = proj.config
        self.config.work_dir = str(self._dir / "work_open")
        self.engine = MoshEngine(self.config, self.ff)
        self.project = proj
        self._clear_undo()
        self.project_changed.emit()
        self.status.emit(f"Opened project: {Path(path).name}")
        return proj

    def new_project(self) -> None:
        self.project = Project(name="untitled", config=self.config,
                               assets_dir=str(self._dir / "assets"))
        self._clear_undo()
        self.project_changed.emit()
        self.status.emit("New project.")

    # -- lifecycle ---------------------------------------------------------- #

    def cleanup(self) -> None:
        if self._cleaned:
            return
        self._cleaned = True
        self.engine.cleanup()
        shutil.rmtree(self._dir, ignore_errors=True)
