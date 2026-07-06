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
import collections
import copy
import dataclasses
import os
import shutil
import tempfile
import traceback
from pathlib import Path
from typing import Callable, List, Optional

# Previews are rendered (and decoded) at this display width: the pixel-domain
# stages run at preview size rather than full project resolution, which is the
# single biggest speed-up on the interactive edit loop.
PREVIEW_MAX_WIDTH = 720

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot

from ..engine import EngineConfig, MoshEngine
from ..ffmpeg import FFmpeg, FFmpegError
from ..modes import load_modes
from ..project import Project, ROOT_SEQ_ID, MAIN_TRACK_ID, MOTION_TRACK_ID
from .. import beats, waveform
from .preview import PreviewDecoder


class _WorkerSignals(QObject):
    finished = Signal(object)
    error = Signal(str)


def _friendly_error(exc: Exception) -> str:
    """Map common failures to actionable messages instead of raw exception
    text (tracebacks still go to the terminal for debugging)."""
    if isinstance(exc, FileNotFoundError):
        missing = exc.filename or str(exc)
        return (f"A file is missing:\n{missing}\n"
                "If it's project media, use File → Relink offline media…; "
                "otherwise re-import the source.")
    msg = str(exc)
    if isinstance(exc, FFmpegError):
        # already descriptive (step + stderr tail); add a hint when the cause
        # is clearly a vanished input file
        if "No such file or directory" in msg:
            msg += ("\n\nAn input file has moved or was deleted — try "
                    "File → Relink offline media…")
        return msg
    if isinstance(exc, PermissionError):
        return (f"Permission denied:\n{exc.filename or msg}\n"
                "Pick a different location or check the file's permissions.")
    if isinstance(exc, OSError) and getattr(exc, "errno", None) == 28:
        return "The disk is full — free some space and try again."
    return msg


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
            self.signals.error.emit(_friendly_error(exc))


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
            self.signals.error.emit(_friendly_error(exc))


class AppController(QObject):
    media_added = Signal(object)          # MediaItem
    media_relinked = Signal(object)       # list[MediaItem] (offline → restored)
    progress = Signal(int, int, str)      # done steps, total steps, label
    project_changed = Signal()
    busy = Signal(bool, str)              # (is_busy, message)
    error = Signal(str)
    status = Signal(str)
    preview_begin = Signal(int, float)    # (total_frames, fps) — stream start
    preview_batch = Signal(list)          # list[QImage] — a chunk of frames
    preview_done = Signal()               # stream complete
    preview_audio = Signal(object)        # path to the preview's audio, or None
    preview_waveform = Signal(object)     # list[float] peak envelope, or None
    sequence_changed = Signal()           # the shown sequence (current_seq_id) changed

    def __init__(self, config: Optional[EngineConfig] = None,
                 ffmpeg_bin: Optional[str] = None,
                 ffprobe_bin: Optional[str] = None):
        super().__init__()
        load_modes()
        self._dir = Path(tempfile.mkdtemp(prefix="moshit_gui_"))
        self.ff = FFmpeg(ffmpeg=ffmpeg_bin, ffprobe=ffprobe_bin)
        self.config = config or EngineConfig(work_dir=str(self._dir / "work"))
        self.engine = MoshEngine(self.config, self.ff)
        self.preview_engine = self._make_preview_engine()
        self.project = Project(name="untitled", config=self.config,
                               assets_dir=str(self._dir / "assets"))
        self.decoder = PreviewDecoder(self.ff.ffmpeg)
        self.pool = QThreadPool.globalInstance()
        self._busy = False
        self._job_gen = 0                      # bumped on cancel; stale results ignored
        self._draining = False                 # a cancelled worker may still be running
        self._pending: Optional[Callable] = None
        self._active_worker = None             # keep the running QRunnable alive
        self._preview_muted = False            # build + play preview audio
        self._audio_plan_cache = None          # rebuild audio only when it changes
        self._audio_path_cache = None
        self._preview_audio = None
        self._preview_waveform = None          # peak envelope for the timeline
        # A/B compare: its own FFmpeg (cancel() kills self.ff's processes and
        # must not take a hold-to-compare fetch with it) + a small frame cache.
        self._ab_ff = FFmpeg(ffmpeg=ffmpeg_bin, ffprobe=ffprobe_bin)
        self._ab_cache: "collections.OrderedDict" = collections.OrderedDict()
        self._cleaned = False
        self.current_seq_id = ROOT_SEQ_ID      # sequence the timeline is showing
        self.easy_mode = False                 # added clips melt into the previous one
        self._undo: List = []                 # snapshots (clips, ops, tracks, seqs)
        self._redo: List = []
        self._undo_limit = 64
        self._live = None                     # in-flight live effect-edit session
        # Safety net: clean the temp dir even if the window's closeEvent never
        # fires (e.g. the process is interrupted from the terminal).
        atexit.register(self.cleanup)

    def _make_preview_engine(self) -> MoshEngine:
        """A second engine for previews: same geometry as the full-res engine, but
        its pixel stages (flow / raw FX / finish / composite) run at preview width
        and use the cheapest optical-flow preset. Export/bake stay on the full-res
        engine. Rebuilt whenever the project geometry changes (e.g. open_project)."""
        base = self.config.work_dir or str(self._dir / "work")
        preview_cfg = dataclasses.replace(self.config, work_dir=base + "_preview")
        eng = MoshEngine(preview_cfg, self.ff)
        eng.preview_max_width = PREVIEW_MAX_WIDTH
        eng.flow_preset_override = "ultrafast"
        return eng

    # -- export profiles available on this ffmpeg --------------------------- #

    @property
    def is_busy(self) -> bool:
        # Draining counts as busy: a cancelled worker may still mutate state, so
        # callers (auto-refresh) must wait for it to finish before the next job.
        return self._busy or self._draining

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

    def _do_export(self, profile, path, audio=True, progress=None):
        out = self._dir / "export_src.avi"
        r = self.project.render(self.engine, out, profile=profile,
                                export_path=str(path), audio=audio,
                                progress=progress)
        return r["export"]

    def _progress_cb(self, gen):
        """A render progress callback (worker thread) that forwards to the
        ``progress`` signal, dropping reports from a cancelled job."""
        def cb(done: int, total: int, label: str) -> None:
            if gen == self._job_gen:
                self.progress.emit(done, total, label)
        return cb

    def _do_bake(self, op_id):
        return self.project.bake_op(self.engine, op_id)

    # -- off-thread dispatch ------------------------------------------------ #

    def _run(self, fn: Callable, on_done: Callable, message: str) -> None:
        if self.is_busy:
            self.error.emit("Still working on the previous task - please wait."
                            if self._busy else
                            "Finishing the cancelled task - try again in a moment.")
            return
        self._busy = True
        self._pending = on_done
        gen = self._job_gen
        self.ff.reset_abort()                  # a prior cancel must not block us
        self.busy.emit(True, message)
        worker = _Worker(fn)
        # gen is captured so a result that arrives after a cancel is dropped.
        worker.signals.finished.connect(
            lambda r, g=gen: self._on_finished(r, g))        # queued -> main thread
        worker.signals.error.connect(lambda m, g=gen: self._on_error(m, g))
        # Retain a Python reference: QThreadPool owns the C++ runnable, but
        # without this the Python wrapper (and its signals) can be GC'd mid-run
        # -- corrupting long tasks like the flow transfer and losing the result.
        self._active_worker = worker
        self.pool.start(worker)

    def cancel(self) -> None:
        """Cancel the in-flight task: kill its ffmpeg subprocesses and drop its
        result (callbacks tagged with the old generation are ignored)."""
        if not self._busy:
            return
        self._job_gen += 1
        self._busy = False
        self._draining = True                  # block new jobs until the worker drains
        self._pending = None
        self.ff.terminate_active()
        self.decoder.terminate()
        self.busy.emit(False, "")
        self.status.emit("Cancelled.")

    def _on_finished(self, result, gen):
        if gen != self._job_gen:               # superseded by a cancel
            self._draining = False             # the cancelled worker has now drained
            return
        self._busy = False
        self.busy.emit(False, "")
        cb, self._pending = self._pending, None
        if cb:
            cb(result)

    def _on_error(self, message: str, gen=None):
        if gen is not None and gen != self._job_gen:    # cancelled job's failure
            self._draining = False
            return
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

    def import_media_batch(self, paths, role: str = "any") -> None:
        """Import several files as ONE background job (e.g. dropped files).

        Per-file failures are collected and surfaced once at the end; the
        files that did import still land. Like single imports, this is not an
        undoable edit (media isn't part of the undo snapshots)."""
        paths = [str(p) for p in paths]
        if not paths:
            return
        if len(paths) == 1:                    # keep the single-file status text
            return self.import_media(paths[0], role)
        gen = self._job_gen

        def work():
            cb = self._progress_cb(gen)
            items, errors = [], []
            for i, p in enumerate(paths):
                cb(i, len(paths), f"Importing {Path(p).name}…")
                try:
                    items.append(self._do_import(p, role))
                except Exception as exc:       # keep going; report at the end
                    errors.append(f"{Path(p).name}: {_friendly_error(exc)}")
            return items, errors

        def done(result):
            items, errors = result
            for item in items:
                self.media_added.emit(item)
            if items:
                self.project_changed.emit()
                self.status.emit(f"Imported {len(items)} file(s).")
            if errors:
                shown = errors[:3] + (["…"] if len(errors) > 3 else [])
                self.error.emit("Some imports failed:\n" + "\n".join(shown))

        self._run(work, done, f"Importing {len(paths)} files…")

    def missing_media(self) -> list:
        """Offline media items (their cached intermediate AVI is gone)."""
        return self.project.missing_media()

    def is_media_offline(self, media) -> bool:
        return (not media.sequence_id
                and not Path(media.intermediate_path).exists())

    def relink_media(self, mapping: dict) -> None:
        """Relink offline media: ``{media_id: new_source_path}``. Runs all
        relinks as one background job (re-normalizes each source)."""
        if not mapping:
            return

        def work():
            return [self.project.relink_media(self.engine, mid, src)
                    for mid, src in mapping.items()]

        def done(items):
            self.media_relinked.emit(items)
            self.project_changed.emit()
            self.status.emit(f"Relinked {len(items)} media file(s).")

        self._run(work, done, f"Relinking {len(mapping)} media file(s)…")

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
        if self.is_busy:                       # also waits out a draining cancel
            return
        seq_id = self.current_seq_id
        if not any(self.project.clips_for_track(t.id)
                   for t in self.project.video_tracks(seq_id)):
            self.error.emit("Add a clip to a video track first.")
            return
        self._busy = True
        gen = self._job_gen
        self.ff.reset_abort()                  # a prior cancel must not block us
        self.busy.emit(True, "Rendering preview…")
        out = self._dir / "preview.avi"

        def work(emit_begin, emit_batch):
            r = self.project.render(self.preview_engine, out, sequence_id=seq_id,
                                    progress=self._progress_cb(gen))
            self.decoder.decode_stream(out, emit_begin, emit_batch,
                                       max_width=PREVIEW_MAX_WIDTH)
            self._preview_audio = self._build_preview_audio(r.get("audio_plans"))

        worker = _StreamWorker(work)
        worker.signals.begin.connect(                          # queued -> main
            lambda t, f, g=gen: self._on_preview_begin(t, f, g))
        worker.signals.batch.connect(lambda fr, g=gen: self._on_preview_batch(fr, g))
        worker.signals.done.connect(lambda g=gen: self._on_preview_done(g))
        worker.signals.error.connect(lambda m, g=gen: self._on_error(m, g))
        self._active_worker = worker           # retain (see _run)
        self.pool.start(worker)

    def _build_preview_audio(self, audio_plans):
        """Assemble the preview's audio track (worker thread). Cached by plan, so
        edits that don't change the audio reuse the previous build. *audio_plans*
        is one plan per audible track; they are summed into the preview WAV."""
        if self._preview_muted or not audio_plans:
            self._preview_waveform = None
            return None
        if audio_plans == self._audio_plan_cache and self._audio_path_cache:
            return self._audio_path_cache         # waveform cache stays valid too
        path = self.engine.mix_audio(audio_plans, self._dir / "preview.wav",
                                     fps=self.config.fps)
        self._audio_plan_cache = audio_plans
        self._audio_path_cache = str(path) if path else None
        self._preview_waveform = (waveform.peaks(self._audio_path_cache)
                                  if self._audio_path_cache else None)
        return self._audio_path_cache

    def set_preview_muted(self, muted: bool) -> None:
        self._preview_muted = bool(muted)
        if not muted:
            self.refresh_preview()             # build the audio now

    # -- A/B compare: the clean source frame for a preview position ---------- #

    def source_frame_for(self, preview_idx: int):
        """Map a preview frame to ``(media_id, source_frame)`` on the current
        sequence's base video track, or None if nothing is there.

        Documented approximations: within a crossfade overlap the incoming
        (later) clip wins; keyframe-snapped trims are ignored, so a trimmed
        clip can be off by up to one GOP; derived (baked) media maps to its
        own intermediate — still the pre-effects picture, which is the point.
        """
        idx = max(0, int(preview_idx))
        tracks = self.project.video_tracks(self.current_seq_id)
        hit = None
        if tracks:                             # base (bottom) track only
            for entry in self.project.track_layout(tracks[0].id):
                clip, start, length, _trans = entry
                if start <= idx < start + length:
                    hit = (clip, start)
        if hit is None:
            return None
        clip, start = hit
        media = self.project.media.get(clip.media_id)
        if media is None or media.nb_frames <= 0:
            return None
        out = clip.out_point if clip.out_point is not None else media.nb_frames
        src_len = max(1, out - clip.in_point)
        off = int((idx - start) * (clip.speed or 1.0))
        off = max(0, min(off, src_len - 1))
        frame = (out - 1 - off) if clip.reverse else clip.in_point + off
        return media.id, max(0, min(frame, media.nb_frames - 1))

    def fetch_source_frame(self, media_id: str, frame: int):
        """One clean frame decoded from the media's normalised intermediate,
        as a QImage (blocking, ~100 ms; LRU-cached). None if unavailable."""
        from PySide6.QtGui import QImage
        key = (media_id, int(frame))
        img = self._ab_cache.get(key)
        if img is not None:
            self._ab_cache.move_to_end(key)
            return img
        media = self.project.media.get(media_id)
        if media is None or not Path(media.intermediate_path).exists():
            return None
        png = self._dir / "ab_frame.png"
        try:
            self._ab_ff.snapshot(media.intermediate_path, png, int(frame))
            img = QImage(str(png))
        except Exception:                      # fetch is best-effort
            return None
        if img.isNull():
            return None
        self._ab_cache[key] = img
        while len(self._ab_cache) > 32:
            self._ab_cache.popitem(last=False)
        return img

    def beat_positions(self, clip_id: str) -> List[float]:
        """Onsets in the preview audio that fall within *clip_id*'s span, as
        normalised 0..1 offsets within the clip (for beat-synced automation).
        Empty if there's no preview audio yet or the clip isn't on the main track.
        """
        wav = self._audio_path_cache
        if not wav:
            return []
        span = next(((start, length) for clip, start, length, _t
                     in self.project.main_layout() if clip.id == clip_id), None)
        if not span or span[1] <= 0:
            return []
        fps = self.config.fps or 30.0
        start_s, dur_s = span[0] / fps, span[1] / fps
        if dur_s <= 0:
            return []
        return [(t - start_s) / dur_s for t in beats.onsets(wav)
                if start_s <= t < start_s + dur_s]

    def _on_preview_begin(self, total: int, fps: float, gen):
        if gen != self._job_gen:               # cancelled before it started
            return
        self.preview_begin.emit(total, fps)

    def _on_preview_batch(self, frames: list, gen):
        if gen != self._job_gen:               # stale frames from a cancelled render
            return
        self.preview_batch.emit(frames)

    def _on_preview_done(self, gen):
        if gen != self._job_gen:               # superseded by a cancel
            self._draining = False             # the cancelled render has drained
            return
        self._busy = False
        self.busy.emit(False, "")
        self.preview_done.emit()
        self.preview_audio.emit(self._preview_audio)
        self.preview_waveform.emit(self._preview_waveform)
        self.status.emit("Preview updated.")

    def export(self, profile: str, path, audio: bool = True) -> None:
        def done(p):
            self.status.emit(f"Exported → {p}")
        gen = self._job_gen
        self._run(lambda: self._do_export(profile, path, audio,
                                          progress=self._progress_cb(gen)),
                  done, f"Exporting {profile}…")

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
        pre = self._snapshot()                 # pre-bake state, committed on success
        def done(_rec):
            self._commit_undo(pre, "Bake")
            self.project_changed.emit()
            self.status.emit("Baked (undoable).")
        self._run(lambda: self._do_bake(op_id), done, "Baking…")

    def bake_clip(self, clip_id: str) -> None:
        pre = self._snapshot()
        def done(_rec):
            self._commit_undo(pre, "Bake stack")
            self.project_changed.emit()
            self.status.emit("Baked effect stack (undoable).")
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

    def set_flow_transfer(self, clip_id: str, flow_transfer) -> None:
        """Set (or clear, with None) a clip's live optical-flow warp."""
        try:
            c = self.project.clip(clip_id)
        except KeyError:
            return
        if c.flow_transfer == flow_transfer:
            return
        if flow_transfer and not self.flow_available():
            self.error.emit("Optical-flow needs OpenCV + numpy: "
                            "pip install 'moshit[flow]'")
            return
        self._push_undo("Flow FX")
        c.flow_transfer = dict(flow_transfer) if flow_transfer else None
        self.project_changed.emit()
        self.status.emit("Flow FX updated." if flow_transfer else "Flow FX removed.")

    def apply_optical_flow(self, base_clip_id: str, motion_media_id: str,
                           **params) -> None:
        if not self.flow_available():
            self.error.emit("Optical-flow transfer needs OpenCV + numpy: "
                            "pip install 'moshit[flow]'")
            return

        pre = self._snapshot()                 # committed on success (undoable)

        def work():
            return self.project.apply_optical_flow(
                self.engine, base_clip_id, motion_media_id, **params)

        def done(_rec):
            self._commit_undo(pre, "Optical flow")
            self.project_changed.emit()
            self.status.emit(f"Optical-flow transfer applied "
                             f"({self.flow_backend()}); undoable.")
        self._run(work, done, "Optical-flow transfer…")

    # -- undo / redo (snapshots of the editable timeline state) ------------- #

    def _snapshot(self):
        # media + bake_records ride along so a bake/flow (which add derived media
        # and a record) can be undone; imported source media are preserved on
        # restore (see _restore), so undo never deletes footage you brought in.
        return (copy.deepcopy(self.project.clips),
                copy.deepcopy(self.project.mosh_ops),
                copy.deepcopy(self.project.tracks),
                copy.deepcopy(self.project.sequences),
                copy.deepcopy(self.project.media),
                copy.deepcopy(self.project.bake_records))

    def _commit_undo(self, snap, label: str = "") -> None:
        self._undo.append((label, snap))
        if len(self._undo) > self._undo_limit:
            self._undo.pop(0)
        self._redo.clear()

    def _push_undo(self, label: str = "") -> None:
        """Record current state so the edit about to happen can be undone."""
        self._commit_undo(self._snapshot(), label)

    def _restore(self, snap) -> None:
        self.project.clips = copy.deepcopy(snap[0])
        self.project.mosh_ops = copy.deepcopy(snap[1])
        if len(snap) > 2:                      # tracks + sequences (compositing)
            self.project.tracks = copy.deepcopy(snap[2])
            self.project.sequences = copy.deepcopy(snap[3])
        if len(snap) > 5:                      # media + bake records (bake/flow)
            media = copy.deepcopy(snap[4])
            # keep any *imported* (non-derived) media added since the snapshot —
            # undoing a later edit must not remove footage the user imported.
            for mid, item in self.project.media.items():
                if mid not in media and not getattr(item, "derived", False):
                    media[mid] = copy.deepcopy(item)
            self.project.media = media
            self.project.bake_records = copy.deepcopy(snap[5])
        if not any(s.id == self.current_seq_id for s in self.project.sequences):
            self.current_seq_id = self.project.root_seq_id   # undid into a gone seq

    def _clear_undo(self) -> None:
        self._undo.clear()
        self._redo.clear()

    @property
    def can_undo(self) -> bool:
        return bool(self._undo)

    @property
    def can_redo(self) -> bool:
        return bool(self._redo)

    @property
    def undo_label(self) -> str:
        return self._undo[-1][0] if self._undo else ""

    @property
    def redo_label(self) -> str:
        return self._redo[-1][0] if self._redo else ""

    def undo(self) -> None:
        if not self._undo:
            return
        label, snap = self._undo.pop()
        self._redo.append((label, self._snapshot()))
        self._restore(snap)
        self.project_changed.emit()
        self.status.emit(f"Undo {label}".strip())

    def redo(self) -> None:
        if not self._redo:
            return
        label, snap = self._redo.pop()
        self._undo.append((label, self._snapshot()))
        self._restore(snap)
        self.project_changed.emit()
        self.status.emit(f"Redo {label}".strip())

    # -- instant model edits ------------------------------------------------ #

    def set_easy_mode(self, on: bool) -> None:
        """Toggle Easy mode: every clip added after another one gets the
        keyframe at its cut deleted (see :meth:`add_clip_for_media`)."""
        on = bool(on)
        if on == self.easy_mode:
            return
        self.easy_mode = on
        self.status.emit("Easy mode on — added clips melt into the previous one "
                         "(keyframe deleted at the cut)." if on
                         else "Easy mode off — added clips cut normally.")

    def _add_melt_op(self, clip_id: str) -> None:
        """Attach the Easy-mode transition: an ordinary iframe_removal op whose
        region is the clip's first frame, deleting just the keyframe at the cut
        — visible, tweakable and removable in the effect stack like any op."""
        op = self.project.add_mosh(
            "iframe_removal", {"keep_first": False, "keep_every": 0}, clip_id)
        op.region_end = 1

    def add_clip_for_media(self, media_id: str, track: str = "main"):
        media = self.project.media[media_id]
        if track not in {t.id for t in self.project.tracks}:
            track = "motion" if track == "motion" else "main"   # legacy fallback
        # Easy mode: a clip placed after another one melts into it. The first
        # clip on a track keeps its clean opening.
        melt = (self.easy_mode and self.project.track(track).role == "video"
                and bool(self.project.clips_for_track(track)))
        self._push_undo("Add clip")
        clip = self.project.add_clip(media_id, track)
        if melt:
            self._add_melt_op(clip.id)
        self.project_changed.emit()
        self.status.emit(f"Added {media.label} to {self.project.track(track).name}"
                         + (" — the cut melts (Easy mode)" if melt else ""))
        return clip

    def place_clip_at(self, media_id: str, track_id: str, start: int):
        """Place a clip at an explicit timeline frame (library drag-and-drop).

        Easy mode applies the same melt rule as :meth:`add_clip_for_media`.
        A melted clip only chains across the cut while it stays butted against
        the previous one — the timeline's drop ghost snaps to make that the
        default; a deliberately gapped drop keeps its (removable) melt op.
        """
        media = self.project.media.get(media_id)
        try:
            track = self.project.track(track_id)
        except KeyError:
            track = None
        if media is None or track is None:
            return None
        if track.role != "video":              # motion pool plays in order
            return self.add_clip_for_media(media_id, track_id)
        melt = self.easy_mode and bool(self.project.clips_for_track(track_id))
        self._push_undo("Place clip")
        clip = self.project.add_clip(media_id, track_id,
                                     start=max(0, int(start)))
        if melt:
            self._add_melt_op(clip.id)
        self.project_changed.emit()
        self.status.emit(
            f"Placed {media.label} on {track.name} at frame {clip.start}"
            + (" — the cut melts (Easy mode)" if melt else ""))
        return clip

    # -- tracks (compositing) ----------------------------------------------- #

    def add_video_track(self, seq_id: Optional[str] = None):
        self._push_undo("Add track")
        t = self.project.add_track(seq_id or self.current_seq_id, role="video")
        self.project_changed.emit()
        self.status.emit(f"Added {t.name}")
        return t

    def remove_track(self, track_id: str) -> None:
        try:
            t = self.project.track(track_id)
        except KeyError:
            return
        if t.role != "video":
            return
        if len(self.project.video_tracks(t.seq_id)) <= 1:
            self.error.emit("Can't remove the only video track.")
            return
        self._push_undo("Remove track")
        cids = {c.id for c in self.project.clips if c.track == track_id}
        self.project.tracks = [x for x in self.project.tracks if x.id != track_id]
        self.project.clips = [c for c in self.project.clips if c.track != track_id]
        self.project.mosh_ops = [o for o in self.project.mosh_ops
                                 if o.target_clip_id not in cids]
        self.project_changed.emit()
        self.status.emit(f"Removed {t.name}")

    def reorder_track(self, track_id: str, delta: int) -> None:
        try:
            t = self.project.track(track_id)
        except KeyError:
            return
        sibs = self.project.tracks_for(t.seq_id, "video")
        idx = sibs.index(t)
        new = idx + int(delta)
        if not 0 <= new < len(sibs):
            return
        self._push_undo("Reorder track")
        other = sibs[new]
        t.index, other.index = other.index, t.index
        self.project_changed.emit()

    def set_track_enabled(self, track_id: str, enabled: bool) -> None:
        try:
            t = self.project.track(track_id)
        except KeyError:
            return
        if t.role != "video" or t.enabled == bool(enabled):
            return
        self._push_undo("Toggle track")
        t.enabled = bool(enabled)
        self.project_changed.emit()

    # -- sequences (precomps) ----------------------------------------------- #

    def set_current_sequence(self, seq_id: str) -> None:
        """Switch which sequence the timeline edits (no project mutation)."""
        if seq_id == self.current_seq_id:
            return
        if not any(s.id == seq_id for s in self.project.sequences):
            return
        self.current_seq_id = seq_id
        self.sequence_changed.emit()

    def precompose(self, clip_ids, name: str = "Precomp"):
        """Move the given clips into a new sequence and drop a precomp clip where
        they were (After-Effects 'precompose'). Returns the new Sequence."""
        valid = []
        for cid in clip_ids:
            try:
                c = self.project.clip(cid)
            except KeyError:
                continue
            if c.enabled and not c.archived and self.project.track(c.track).role \
                    == "video":
                valid.append(c)
        if not valid:
            return None
        host_track = valid[0].track
        insert_start = min(c.start for c in valid)
        self._push_undo("Precompose")
        seq = self.project.add_sequence(name)
        vt = self.project.video_tracks(seq.id)[0]
        cursor = 0
        for c in sorted(valid, key=lambda c: c.start):    # rehome onto the precomp
            c.seq_id, c.track, c.start = seq.id, vt.id, cursor
            cursor += self.project._clip_length(c)
        media = self.project.sequence_media(seq.id)
        media.nb_frames = cursor                          # provisional until rendered
        self.project.add_sequence_clip(host_track, seq.id, start=insert_start)
        self.project_changed.emit()
        self.status.emit(f"Precomposed {len(valid)} clip(s) into {name}.")
        return seq

    # -- pixel FX (clip finishing) ------------------------------------------ #

    def clip_pixel_fx(self, clip_id: str) -> List[dict]:
        try:
            return [dict(pe) for pe in self.project.clip(clip_id).pixel_effects]
        except KeyError:
            return []

    def add_pixel_fx(self, clip_id: str, name: str, params: Optional[dict] = None):
        from ..modes import get_pixel_mode
        try:
            c = self.project.clip(clip_id)
            mode = get_pixel_mode(name)
        except KeyError:
            return None
        self._push_undo("Add pixel FX")
        c.pixel_effects.append({"name": name,
                                "params": dict(params) if params else mode.defaults()})
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
        self._push_undo("Remove pixel FX")
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
        self._push_undo("Edit pixel FX")
        c.pixel_effects[index]["params"] = dict(params)
        self.project_changed.emit()

    # -- raw FX (numpy frame processors) ------------------------------------ #

    def clip_raw_fx(self, clip_id: str) -> List[dict]:
        try:
            return [dict(re) for re in self.project.clip(clip_id).raw_effects]
        except KeyError:
            return []

    def add_raw_fx(self, clip_id: str, name: str, params: Optional[dict] = None):
        from ..modes import get_raw_mode
        try:
            c = self.project.clip(clip_id)
            mode = get_raw_mode(name)
        except KeyError:
            return None
        self._push_undo("Add raw FX")
        c.raw_effects.append({"name": name,
                              "params": dict(params) if params else mode.defaults()})
        self.project_changed.emit()
        self.status.emit(f"Added raw FX: {name}")
        return c

    def remove_raw_fx(self, clip_id: str, index: int) -> None:
        try:
            c = self.project.clip(clip_id)
        except KeyError:
            return
        if not (0 <= index < len(c.raw_effects)):
            return
        self._push_undo("Remove raw FX")
        c.raw_effects.pop(index)
        self.project_changed.emit()
        self.status.emit("Removed raw FX")

    def update_raw_fx(self, clip_id: str, index: int, params: dict) -> None:
        try:
            c = self.project.clip(clip_id)
        except KeyError:
            return
        if not (0 <= index < len(c.raw_effects)):
            return
        if c.raw_effects[index].get("params") == params:
            return
        self._push_undo("Edit raw FX")
        c.raw_effects[index]["params"] = dict(params)
        self.project_changed.emit()

    def set_clip_mask(self, clip_id: str, kind: str, spec) -> None:
        """Set a clip's ``layer`` or ``fx`` matte (None clears it)."""
        try:
            c = self.project.clip(clip_id)
        except KeyError:
            return
        attr = {"layer": "layer_mask", "fx": "fx_mask"}.get(kind)
        if attr is None:
            return
        spec = dict(spec) if spec else None
        if getattr(c, attr) == spec:
            return
        self._push_undo("Matte")
        setattr(c, attr, spec)
        self.project_changed.emit()
        self.status.emit(f"{'Layer' if kind == 'layer' else 'FX'} matte updated.")

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
        self._push_undo("Add effect")
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
        self._push_undo("Edit effect")
        op.mode = mode
        op.params = dict(params)
        op.region_start, op.region_end = new_region
        self.project_changed.emit()
        return op

    # -- live effect editing (coalesced undo) ------------------------------- #
    #
    # A live edit -- the non-modal param editor dragging a slider -- fires many
    # updates in quick succession. These would each push_undo(), flooding the
    # history with one entry per tick. Instead a *session* holds a single
    # pre-edit snapshot aside and commits exactly one undo entry the first time a
    # value actually changes (or, for an add, when the session commits). Cancel
    # rolls back to the snapshot and drops that entry, so an abandoned edit
    # leaves no trace. One generic core serves the mosh stack, the pixel FX and
    # the raw FX; each identifies its session by a ``key`` tuple.

    def _begin_live(self, key, *, created: bool, pre=None, label: str = "") -> None:
        """Open a live session identified by *key*. A no-op if one for the same
        key is already open -- the live add flow opens the editor (which signals
        begin) over the session the add just created, whose pre-snapshot must
        survive. *pre* lets an add pass the snapshot it took before creating."""
        if self._live and self._live.get("key") == key:
            return
        self._live = {"key": key, "pre": pre if pre is not None else self._snapshot(),
                      "created": created, "committed": False, "label": label}

    def _live_commit_point(self) -> None:
        """Record the session's single undo entry on the first real change."""
        if self._live and not self._live["committed"]:
            self._commit_undo(self._live["pre"], self._live.get("label", ""))
            self._live["committed"] = True

    def _end_live(self, key, *, commit: bool) -> None:
        live = self._live
        if not live or live["key"] != key:
            self._live = None
            return
        self._live = None
        if commit:
            if live["created"] and not live["committed"]:
                self._commit_undo(live["pre"], live.get("label", ""))  # bare add
            return
        # cancel: undo the session entirely
        if live["committed"] and self._undo and self._undo[-1][1] is live["pre"]:
            self._undo.pop()
        self._restore(live["pre"])
        self.project_changed.emit()

    # -- mosh effect stack -------------------------------------------------- #

    def begin_effect_add(self, clip_id: str, mode: str, params: dict = None,
                         region=None):
        """Create an effect with its defaults inside a live session, then return
        it so the caller can open the live editor bound to it. The add is not
        undoable on its own until the session ends (or a value is changed)."""
        pre = self._snapshot()
        op = self.project.add_mosh(mode, params or {}, clip_id)
        op.region_start, op.region_end = self._region_tuple(region)
        self._begin_live(("mosh", op.id), created=True, pre=pre,
                         label=f"Add {mode}")
        self.project_changed.emit()
        return op

    def begin_effect_edit(self, op_id: str) -> None:
        """Open a live session over an existing effect, snapshotting its state."""
        try:
            op = self.project.op(op_id)
        except KeyError:
            return
        self._begin_live(("mosh", op_id), created=False, label=f"Edit {op.mode}")

    def live_update_effect(self, op_id: str, mode: str, params: dict,
                           region=None):
        """Apply a value during a live session. The first real change commits the
        session's single undo entry; later changes mutate in place."""
        live = self._live
        if not live or live["key"] != ("mosh", op_id):   # no session: atomic
            return self.update_effect(op_id, mode, params, region)
        try:
            op = self.project.op(op_id)
        except KeyError:
            return None
        new_region = self._region_tuple(region)
        if (op.mode == mode and op.params == params
                and (op.region_start, op.region_end) == new_region):
            return op                              # nothing changed
        self._live_commit_point()
        op.mode = mode
        op.params = dict(params)
        op.region_start, op.region_end = new_region
        self.project_changed.emit()
        return op

    def end_effect_edit(self, op_id: str, *, commit: bool = True) -> None:
        """Close a live session. ``commit`` keeps the edit (recording the bare
        add if nothing was tweaked); otherwise revert to the pre-edit snapshot."""
        self._end_live(("mosh", op_id), commit=commit)

    # -- pixel / raw FX (positional, keyed by clip + index) ----------------- #

    def _fx_list(self, kind: str, clip_id: str):
        c = self.project.clip(clip_id)             # may raise KeyError
        return c.pixel_effects if kind == "pixel" else c.raw_effects

    def begin_fx_add(self, kind: str, clip_id: str, name: str):
        """Append a pixel/raw FX with its defaults inside a live session; returns
        its index so the caller can open the live editor on it."""
        from ..modes import get_pixel_mode, get_raw_mode
        pre = self._snapshot()
        try:
            fx = self._fx_list(kind, clip_id)
            mode = (get_pixel_mode if kind == "pixel" else get_raw_mode)(name)
        except KeyError:
            return None
        fx.append({"name": name, "params": mode.defaults()})
        index = len(fx) - 1
        self._begin_live((kind, clip_id, index), created=True, pre=pre,
                         label=f"Add {name}")
        self.project_changed.emit()
        self.status.emit(f"Added {kind} FX: {name}")
        return index

    def begin_fx_edit(self, kind: str, clip_id: str, index: int) -> None:
        try:
            fx = self._fx_list(kind, clip_id)
        except KeyError:
            return
        if 0 <= index < len(fx):
            name = fx[index].get("name", f"{kind} FX")
            self._begin_live((kind, clip_id, index), created=False,
                             label=f"Edit {name}")

    def live_update_fx(self, kind: str, clip_id: str, index: int, params: dict):
        """Apply a pixel/raw FX value during a live session (atomic fallback if no
        session is open, mirroring live_update_effect)."""
        key = (kind, clip_id, index)
        live = self._live
        if not live or live["key"] != key:
            return (self.update_pixel_fx if kind == "pixel"
                    else self.update_raw_fx)(clip_id, index, params)
        try:
            fx = self._fx_list(kind, clip_id)
        except KeyError:
            return
        if not (0 <= index < len(fx)) or fx[index].get("params") == params:
            return
        self._live_commit_point()
        fx[index]["params"] = dict(params)
        self.project_changed.emit()

    def end_fx_edit(self, kind: str, clip_id: str, index: int, *,
                    commit: bool = True) -> None:
        self._end_live((kind, clip_id, index), commit=commit)

    def remove_effect(self, op_id: str) -> None:
        self._push_undo("Remove effect")
        if not self.project.remove_mosh(op_id):
            self._undo.pop()                       # nothing removed; drop the snapshot
            return
        self.project_changed.emit()
        self.status.emit("Removed effect")

    def randomise_effect(self, op_id: str) -> None:
        """Randomise a mosh op's parameters (one undo step). Values come from the
        mode's Param schema, so they stay in range and honour choices/bools."""
        from ..modes import get_mode
        from ..modes.base import random_params
        try:
            op = self.project.op(op_id)
            mode = get_mode(op.mode)
        except KeyError:
            return
        params = random_params(mode, op.params)
        region = ((op.region_start, op.region_end)
                  if (op.region_start or op.region_end is not None) else None)
        self.update_effect(op_id, op.mode, params, region)   # one undo step

    def move_effect(self, op_id: str, delta: int) -> None:
        snap = self._snapshot()
        if not self.project.move_mosh(op_id, delta):
            return
        self._commit_undo(snap, "Reorder effect")
        self.project_changed.emit()

    def set_effect_enabled(self, op_id: str, on: bool) -> None:
        try:
            op = self.project.op(op_id)
        except KeyError:
            return
        if op.enabled == on:
            return
        self._push_undo("Toggle effect")
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
        self._push_undo("Apply preset")
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
        opacity = max(0.0, min(1.0, float(props.get("opacity", c.opacity))))
        blend = str(props.get("blend_mode", c.blend_mode))
        gain = max(0.0, min(4.0, float(props.get("gain", c.gain))))
        if (speed, reverse, fade_in, fade_out, trans, opacity, blend, gain) == (
                c.speed, c.reverse, c.fade_in, c.fade_out, c.transition_in,
                c.opacity, c.blend_mode, c.gain):
            return c
        self._push_undo("Clip settings")
        c.speed, c.reverse = speed, reverse
        c.fade_in, c.fade_out, c.transition_in = fade_in, fade_out, trans
        c.opacity, c.blend_mode, c.gain = opacity, blend, gain
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

    def trim_clip(self, clip_id: str, in_point=None, out_point=None) -> None:
        c = self.project.clip(clip_id)
        media = self.project.media[c.media_id]
        self._push_undo("Trim clip")
        cur_out = c.out_point if c.out_point is not None else media.nb_frames
        if in_point is not None:
            c.in_point = max(0, min(int(in_point), cur_out - 1))
        if out_point is not None:
            c.out_point = max(c.in_point + 1, min(int(out_point), media.nb_frames))
        self.project_changed.emit()

    def move_clip(self, clip_id: str, new_start: int) -> None:
        """Move a clip in time (free positioning; gaps/overlaps allowed). Clears
        the legacy crossfade so the explicit position wins."""
        try:
            c = self.project.clip(clip_id)
        except KeyError:
            return
        new_start = max(0, int(new_start))
        if c.start == new_start and not c.transition_in:
            return
        self._push_undo("Move clip")
        c.start = new_start
        c.transition_in = 0                # overlap (if any) now comes from position
        self.project_changed.emit()

    def remove_clip(self, clip_id: str) -> None:
        try:
            c = self.project.clip(clip_id)
        except KeyError:
            return
        if c.archived:                 # archived clips are bake history; leave them
            return
        self._push_undo("Remove clip")
        self.project.clips = [x for x in self.project.clips if x.id != clip_id]
        self.project.mosh_ops = [o for o in self.project.mosh_ops
                                 if o.target_clip_id != clip_id]
        self.project_changed.emit()

    def split_clip(self, clip_id: str, offset: int) -> None:
        snap = self._snapshot()
        new = self.project.split_clip(clip_id, offset)
        if new is None:
            return
        self._commit_undo(snap, "Split clip")
        self.project_changed.emit()
        self.status.emit("Split clip.")

    def duplicate_clip(self, clip_id: str):
        try:
            c = self.project.clip(clip_id)
        except KeyError:
            return None
        if c.archived:
            return None
        self._push_undo("Duplicate clip")
        new = self.project.duplicate_clip(clip_id)
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
        self.preview_engine = self._make_preview_engine()
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
