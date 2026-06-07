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


class AppController(QObject):
    media_added = Signal(object)          # MediaItem
    project_changed = Signal()
    busy = Signal(bool, str)              # (is_busy, message)
    error = Signal(str)
    status = Signal(str)
    preview_ready = Signal(list, float)   # (frames, fps)

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
        # Safety net: clean the temp dir even if the window's closeEvent never
        # fires (e.g. the process is interrupted from the terminal).
        atexit.register(self.cleanup)

    # -- export profiles available on this ffmpeg --------------------------- #

    def export_profiles(self) -> List[str]:
        return self.ff.capabilities().available_export_profiles()

    def motion_labels(self) -> List[str]:
        return [m.label for m in self.project.media.values() if m.role == "motion"]

    # -- the actual work (synchronous; directly testable) ------------------- #

    def _do_import(self, path, role):
        return self.project.import_media(self.engine, path, role=role)

    def _do_render_preview(self):
        out = self._dir / "preview.avi"
        self.project.render(self.engine, out)
        frames, fps, _ = self.decoder.decode(out, max_width=720)
        return frames, fps

    def _do_export(self, profile, path):
        out = self._dir / "export_src.avi"
        self.project.render(self.engine, out)
        return self.engine.export(out, path, profile)

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

    def import_media(self, path, role: str) -> None:
        def done(item):
            self.media_added.emit(item)
            self.project_changed.emit()
            self.status.emit(f"Imported {item.label} ({item.nb_frames} frames)")
        self._run(lambda: self._do_import(path, role), done,
                  f"Importing {Path(path).name}…")

    def refresh_preview(self) -> None:
        if not self.project.main_clips():
            self.error.emit("Add a clip to the main track first.")
            return

        def done(res):
            frames, fps = res
            self.preview_ready.emit(frames, fps)
            self.status.emit(f"Preview: {len(frames)} frames @ {fps:.0f} fps")
        self._run(self._do_render_preview, done, "Rendering preview…")

    def export(self, profile: str, path) -> None:
        def done(p):
            self.status.emit(f"Exported → {p}")
        self._run(lambda: self._do_export(profile, path), done,
                  f"Exporting {profile}…")

    def bake(self, op_id: str) -> None:
        def done(_rec):
            self.project_changed.emit()
            self.status.emit("Baked (revertible).")
        self._run(lambda: self._do_bake(op_id), done, "Baking…")

    # -- instant model edits ------------------------------------------------ #

    def add_clip_for_media(self, media_id: str):
        media = self.project.media[media_id]
        track = "motion" if media.role == "motion" else "main"
        clip = self.project.add_clip(media_id, track)
        self.project_changed.emit()
        self.status.emit(f"Added {media.label} to the {track} track")
        return clip

    def set_mosh(self, clip_id: str, mode: str, params: dict):
        """Set (or update in place) the mosh op on a clip."""
        for op in self.project.mosh_ops:
            if op.target_clip_id == clip_id and op.enabled and not op.archived:
                op.mode = mode
                op.params = dict(params)
                self.project_changed.emit()
                return op
        op = self.project.add_mosh(mode, params, clip_id)
        self.project_changed.emit()
        return op

    def active_op_for_clip(self, clip_id: str):
        for op in self.project.mosh_ops:
            if op.target_clip_id == clip_id and op.enabled and not op.archived:
                return op
        return None

    def revert_last_bake(self) -> None:
        if not self.project.bake_records:
            self.error.emit("Nothing to revert.")
            return
        self.project.revert_bake(self.project.bake_records[-1].id)
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
        self.project.clips = [x for x in self.project.clips if x.id != clip_id]
        self.project.mosh_ops = [o for o in self.project.mosh_ops
                                 if o.target_clip_id != clip_id]
        if c.track == "main":
            self._repack_main()
        self.project_changed.emit()

    def split_clip(self, clip_id: str, offset: int) -> None:
        new = self.project.split_clip(clip_id, offset)
        if new is None:
            return
        if new.track == "main":
            self._repack_main()
        self.project_changed.emit()
        self.status.emit("Split clip.")

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
        self.project_changed.emit()
        self.status.emit(f"Opened project: {Path(path).name}")
        return proj

    def new_project(self) -> None:
        self.project = Project(name="untitled", config=self.config,
                               assets_dir=str(self._dir / "assets"))
        self.project_changed.emit()
        self.status.emit("New project.")

    # -- lifecycle ---------------------------------------------------------- #

    def cleanup(self) -> None:
        if self._cleaned:
            return
        self._cleaned = True
        self.engine.cleanup()
        shutil.rmtree(self._dir, ignore_errors=True)
