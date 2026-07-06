"""Non-destructive project model.

Nothing here mutates source media, and every operation is recorded so it can be
undone:

* :class:`MediaItem` is an immutable reference to a source file plus its cached
  moshable intermediate (or, for a precomp, a sequence's rendered output).
* :class:`Sequence` is a timeline of :class:`Track` lanes; :class:`Clip` is a
  *view* into a MediaItem placed on a track (free position + in/out trim).
* :class:`MoshOp` is a *recipe* -- a mode plus parameters targeting a clip -- not
  a baked result.
* ``render`` materialises a sequence read-only: a single contiguous video track
  takes the codec/flat path; multiple tracks, free positions/overlaps, or
  opacity/blend composite in the pixel domain. A sequence used as a clip (a
  precomp) is rendered to cached, moshable media.
* ``bake`` freezes one mosh op into a new clip but **archives** (never deletes)
  the originals and writes a :class:`BakeRecord`, so ``revert_bake`` fully
  restores the prior state.
"""
from __future__ import annotations

import collections
import json
import shutil
import threading
import uuid
from dataclasses import dataclass, field, fields as _dc_fields
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from . import avi
from .avi import AviVideo, Frame
from .engine import EngineConfig, MoshEngine

ROOT_SEQ_ID = "root"
MAIN_TRACK_ID = "main"             # root sequence's first video track (legacy id)
MOTION_TRACK_ID = "motion"         # root sequence's motion source pool (legacy id)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def _only_fields(cls, d: Dict) -> Dict:
    """Keep just the keys that are fields of *cls* (tolerant deserialisation)."""
    names = {f.name for f in _dc_fields(cls)}
    return {k: v for k, v in d.items() if k in names}


# --------------------------------------------------------------------------- #
# Entities
# --------------------------------------------------------------------------- #

@dataclass
class MediaItem:
    id: str
    source_path: str               # original file (never modified)
    label: str
    role: str                      # "main" | "motion"
    intermediate_path: str         # cached moshable AVI
    width: int = 0
    height: int = 0
    fps: float = 0.0
    nb_frames: int = 0
    derived: bool = False          # True for baked media
    sequence_id: Optional[str] = None  # set when this media is a rendered precomp
    digest: str = ""               # content hash of the source sequence (cache key)
    alpha_path: Optional[str] = None   # grayscale alpha map (source-file alpha matte)

    def to_dict(self) -> Dict:
        return self.__dict__.copy()

    @classmethod
    def from_dict(cls, d: Dict) -> "MediaItem":
        return cls(**_only_fields(cls, d))


@dataclass
class Clip:
    id: str
    media_id: str
    track: str                     # track id (e.g. "main"/"motion" or a generated id)
    start: int = 0                 # timeline position, in frames
    in_point: int = 0              # trim start within media (frames)
    out_point: Optional[int] = None  # exclusive; None = end of media
    enabled: bool = True
    archived: bool = False
    seq_id: str = ROOT_SEQ_ID      # which sequence this clip lives in
    opacity: float = 1.0           # 0..1 layer opacity (compositing)
    blend_mode: str = "normal"     # normal | screen | multiply | add | ...
    gain: float = 1.0              # audio gain for this clip when tracks mix
    # -- clean-edit finishing (pixel-domain; applied in the render finish pass).
    # Defaults are inert, so a clip with all defaults takes the fast path.
    speed: float = 1.0             # 2.0 = twice as fast, 0.5 = half speed
    reverse: bool = False          # play the clip backwards
    fade_in: int = 0               # frames to fade up from black at the head
    fade_out: int = 0              # frames to fade to black at the tail
    transition_in: int = 0         # crossfade frames from the previous main clip
    pixel_effects: List = field(default_factory=list)  # [{name, params}] FFmpeg FX
    raw_effects: List = field(default_factory=list)     # [{name, params}] numpy FX
    flow_transfer: Optional[Dict] = None   # live optical-flow warp (see render)
    layer_mask: Optional[Dict] = None      # compositing matte (luma/alpha/motion)
    fx_mask: Optional[Dict] = None         # matte gating this clip's pixel + raw FX

    def has_finish(self) -> bool:
        """True if this clip needs the pixel-domain finish pass."""
        return (self.speed != 1.0 or self.reverse or self.fade_in > 0
                or self.fade_out > 0 or self.transition_in > 0
                or bool(self.pixel_effects) or bool(self.raw_effects)
                or self.flow_transfer is not None)

    def to_dict(self) -> Dict:
        return self.__dict__.copy()

    @classmethod
    def from_dict(cls, d: Dict) -> "Clip":
        return cls(**_only_fields(cls, d))


@dataclass
class MoshOp:
    id: str
    mode: str
    params: Dict
    target_clip_id: str
    enabled: bool = True
    archived: bool = False
    region_start: int = 0              # apply only to frames [start, end) of its
    region_end: Optional[int] = None   # input; None end = through the last frame

    def to_dict(self) -> Dict:
        return {"id": self.id, "mode": self.mode, "params": self.params,
                "target_clip_id": self.target_clip_id,
                "enabled": self.enabled, "archived": self.archived,
                "region_start": self.region_start, "region_end": self.region_end}

    @classmethod
    def from_dict(cls, d: Dict) -> "MoshOp":
        return cls(**_only_fields(cls, d))


@dataclass
class Track:
    """A lane within a sequence. Video tracks composite top-to-bottom by
    ``index`` (0 = bottom); a motion track is a non-composited source pool."""
    id: str
    seq_id: str
    name: str
    index: int                     # compositing order within the sequence
    role: str = "video"            # "video" | "motion"
    enabled: bool = True

    def to_dict(self) -> Dict:
        return self.__dict__.copy()

    @classmethod
    def from_dict(cls, d: Dict) -> "Track":
        return cls(**_only_fields(cls, d))


@dataclass
class Sequence:
    """A timeline of tracks. The root sequence renders to the output; any other
    sequence can be used as a clip (a precomp) and is rendered to cached media."""
    id: str
    name: str = "Sequence"
    width: int = 0                 # 0 = inherit the project config
    height: int = 0
    fps: float = 0.0

    def to_dict(self) -> Dict:
        return self.__dict__.copy()

    @classmethod
    def from_dict(cls, d: Dict) -> "Sequence":
        return cls(**_only_fields(cls, d))


@dataclass
class BakeRecord:
    id: str
    baked_media_id: str
    baked_clip_id: str
    replaced_clip_ids: List[str]
    consumed_mosh_op_ids: List[str]
    created: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))

    def to_dict(self) -> Dict:
        return self.__dict__.copy()

    @classmethod
    def from_dict(cls, d: Dict) -> "BakeRecord":
        return cls(**_only_fields(cls, d))


# --------------------------------------------------------------------------- #
# Project
# --------------------------------------------------------------------------- #

class Project:
    VERSION = 1

    def __init__(self, name: str = "untitled",
                 config: Optional[EngineConfig] = None,
                 assets_dir: Optional[str] = None):
        self.name = name
        self.config = config or EngineConfig()
        self.assets_dir = Path(assets_dir) if assets_dir else None
        if self.assets_dir:
            self.assets_dir.mkdir(parents=True, exist_ok=True)
        self.media: Dict[str, MediaItem] = {}
        self.clips: List[Clip] = []
        self.mosh_ops: List[MoshOp] = []
        self.bake_records: List[BakeRecord] = []
        self.sequences: List[Sequence] = []
        self.tracks: List[Track] = []
        self.root_seq_id = ROOT_SEQ_ID
        # Parsed-media LRU: coded frames in RAM, bounded by total bytes so a
        # long session with many media doesn't pin them all forever (P12).
        # Evicted entries simply re-parse on next use.
        self._parsed: "collections.OrderedDict[str, AviVideo]" = \
            collections.OrderedDict()
        self._parsed_bytes: Dict[str, int] = {}
        self._parsed_total = 0
        self._parsed_budget = 1 * 1024 ** 3        # ~1 GB of coded frames
        self._parsed_lock = threading.Lock()
        self._seg_n: Dict[str, int] = {}           # seg-cache key -> frame count
        self._tmp_assets: Optional[Path] = None    # precomp cache when no assets_dir
        self._seg_locks: Dict[str, threading.Lock] = {}   # per-key in-flight locks
        self._seg_locks_mutex = threading.Lock()
        self._ensure_default_structure()

    def _ensure_default_structure(self) -> None:
        """Guarantee a root sequence with its legacy main/motion tracks exist."""
        if not any(s.id == self.root_seq_id for s in self.sequences):
            self.sequences.insert(0, Sequence(id=self.root_seq_id, name="Main"))
        have = {t.id for t in self.tracks}
        if MAIN_TRACK_ID not in have:
            self.tracks.append(Track(id=MAIN_TRACK_ID, seq_id=self.root_seq_id,
                                     name="Video 1", index=0, role="video"))
        if MOTION_TRACK_ID not in have:
            self.tracks.append(Track(id=MOTION_TRACK_ID, seq_id=self.root_seq_id,
                                     name="Motion", index=0, role="motion"))

    # -- lookups ------------------------------------------------------------ #

    def clip(self, clip_id: str) -> Clip:
        for c in self.clips:
            if c.id == clip_id:
                return c
        raise KeyError(f"no clip '{clip_id}'")

    def op(self, op_id: str) -> MoshOp:
        for o in self.mosh_ops:
            if o.id == op_id:
                return o
        raise KeyError(f"no mosh op '{op_id}'")

    def sequence(self, seq_id: str) -> Sequence:
        for s in self.sequences:
            if s.id == seq_id:
                return s
        raise KeyError(f"no sequence '{seq_id}'")

    def track(self, track_id: str) -> Track:
        for t in self.tracks:
            if t.id == track_id:
                return t
        raise KeyError(f"no track '{track_id}'")

    def tracks_for(self, seq_id: str, role: Optional[str] = None) -> List[Track]:
        """A sequence's tracks (optionally by role), in compositing order."""
        return sorted((t for t in self.tracks if t.seq_id == seq_id
                       and (role is None or t.role == role)),
                      key=lambda t: t.index)

    def video_tracks(self, seq_id: str) -> List[Track]:
        return [t for t in self.tracks_for(seq_id, "video") if t.enabled]

    def clips_for_track(self, track_id: str) -> List[Clip]:
        """Enabled, non-archived clips on a track, ordered by start."""
        return sorted((c for c in self.clips if c.track == track_id
                       and c.enabled and not c.archived),
                      key=lambda c: c.start)

    def _parsed_media(self, media_id: str) -> AviVideo:
        with self._parsed_lock:
            av = self._parsed.get(media_id)
            if av is not None:
                self._parsed.move_to_end(media_id)
                return av
        av = avi.parse_avi(self.media[media_id].intermediate_path)
        self._cache_parsed(media_id, av)
        return av

    def _cache_parsed(self, media_id: str, av: AviVideo) -> None:
        """Insert/refresh a parse-cache entry; evicts LRU over the byte budget
        (never the entry just inserted). Borrowed AviVideo references stay
        valid after eviction -- only the cache's own reference is dropped."""
        size = sum(f.size for f in av.frames)
        with self._parsed_lock:
            self._parsed_total += size - self._parsed_bytes.get(media_id, 0)
            self._parsed[media_id] = av
            self._parsed.move_to_end(media_id)
            self._parsed_bytes[media_id] = size
            while (self._parsed_total > self._parsed_budget
                   and len(self._parsed) > 1):
                old_id, _old = self._parsed.popitem(last=False)
                self._parsed_total -= self._parsed_bytes.pop(old_id, 0)

    def _uncache_parsed(self, media_id: str) -> None:
        with self._parsed_lock:
            if self._parsed.pop(media_id, None) is not None:
                self._parsed_total -= self._parsed_bytes.pop(media_id, 0)

    def _ops_for_clip(self, clip_id: str) -> List[MoshOp]:
        return [o for o in self.mosh_ops
                if o.target_clip_id == clip_id and o.enabled and not o.archived]

    def _flow_motion_media(self, source):
        """Resolve a flow effect's motion source (a media id or label)."""
        if source in self.media:
            return self.media[source]
        return next((m for m in self.media.values() if m.label == source), None)

    def _pixel_filters(self, clip: Clip, *, nframes: int = 0) -> List[str]:
        """FFmpeg filter strings for a clip's pixel effects (skips unknown ones).

        *nframes* is the clip's (post-mosh, pre-speed) frame count; motion modes
        use it to animate across the exact clip length."""
        from .modes import get_pixel_mode
        fps = self.config.fps or 30.0
        w, h = int(self.config.width or 0), int(self.config.height or 0)
        out: List[str] = []
        for pe in (clip.pixel_effects or []):
            try:
                mode = get_pixel_mode(pe["name"])
            except KeyError:
                continue
            params = mode.resolve(pe.get("params") or {})
            f = (mode.filter_ctx(params, fps=fps, nframes=int(nframes),
                                 width=w, height=h)
                 if getattr(mode, "needs_ctx", False)
                 else mode.filter(**params))
            if f:
                out.append(f)
        return out

    def _raw_specs(self, clip: Clip) -> List[Dict]:
        """A clip's known raw-frame effects, in order (skips unknown ones)."""
        from .modes import is_raw_mode
        return [{"name": re["name"], "params": re.get("params") or {}}
                for re in (clip.raw_effects or []) if is_raw_mode(re.get("name"))]

    def _flow_args(self, clip: Clip, nframes: int):
        """``(flow_kwargs, motion_intermediate_path)`` for a clip's flow transfer,
        or ``(None, None)`` if it has none / its motion source is missing. Shared
        by the per-clip render paths so the flow region math lives in one place."""
        ft = clip.flow_transfer
        if not ft:
            return None, None
        motion_m = self._flow_motion_media(ft.get("source"))
        if motion_m is None:
            return None, None
        n = int(nframes)
        rs = max(0, int(ft.get("region_start", 0) or 0))
        re = ft.get("region_end")
        region = ((rs, n if re is None else min(int(re), n))
                  if (rs > 0 or re is not None) else None)
        return ({"hold": ft.get("hold", True),
                 "accumulate": ft.get("accumulate", True),
                 "strength": float(ft.get("strength", 1.0)),
                 "preset": ft.get("preset", "fast"), "out_len": n,
                 "region": region}, motion_m.intermediate_path)

    def _clip_pixels(self, engine: MoshEngine, clip: Clip, seg, nframes: int):
        """Fused flow + raw-FX pixel stage for one clip's segment AVI *seg*;
        returns the (possibly new) segment path and consumes *seg* if replaced."""
        flow_args, motion_src = self._flow_args(clip, nframes)
        out = engine.apply_clip_pixels(
            seg, engine._tmp(".avi"), flow_args=flow_args, motion_src=motion_src,
            raw_specs=self._raw_specs(clip), mask=clip.fx_mask)
        if str(out) != str(seg):
            try:
                Path(seg).unlink()
            except OSError:
                pass
        return out

    def _media_state(self):
        """Each media's identity + on-disk state, as ``{id: (label, digest,
        mtime)}`` (one stat per media). Render loops compute this once and pass
        it down; :meth:`_clip_seg_key` folds in only the entries a clip actually
        depends on, so re-imported footage or a re-rendered precomp invalidates
        exactly the segments that read it — and nothing else."""
        state = {}
        for m in self.media.values():
            try:
                mt = Path(m.intermediate_path).stat().st_mtime
            except OSError:
                mt = 0.0
            state[m.id] = (m.label, getattr(m, "digest", "") or "", mt)
        return state

    def _clip_media_deps(self, clip: Clip) -> Optional[List[str]]:
        """Media ids this clip's segment reads: its own media, its flow-transfer
        motion source, and any media referenced by a mosh op's ``clip_ref``
        param. Returns None when an op's mode is unregistered (it could read any
        motion clip), meaning "depend on everything"."""
        from .modes import get_mode
        deps = {clip.media_id}
        ft = clip.flow_transfer
        if ft:
            mm = self._flow_motion_media(ft.get("source"))
            if mm:
                deps.add(mm.id)
        for op in self._ops_for_clip(clip.id):
            try:
                mode = get_mode(op.mode)
            except Exception:
                return None                    # unknown mode: assume anything
            for p in getattr(mode, "params", []):
                if getattr(p, "kind", None) != "clip_ref":
                    continue
                src = (op.params or {}).get(p.name)
                m = self._flow_motion_media(src) if src else None
                if m:
                    deps.add(m.id)
        return sorted(deps)

    def _clip_seg_key(self, engine: MoshEngine, clip: Clip,
                      media_state=None) -> str:
        """Cache key for a clip's post-mosh, post-pixel segment. Captures only what
        the *segment* depends on -- the codec mosh (media/trim/ops) plus the flow
        and raw-FX pixel stages -- not the finish-pass speed/fade/pixel filters,
        which are cheap to re-apply over a cached segment. Render loops pass a
        precomputed *media_state* so it isn't re-stat'ed once per clip; only the
        clip's own dependency subset is folded in (see :meth:`_clip_media_deps`),
        so touching one media no longer busts every other clip's segment."""
        import hashlib
        state = media_state if media_state is not None else self._media_state()
        deps = self._clip_media_deps(clip)
        if deps is None:                               # conservative: everything
            media_sig = sorted((mid, *v) for mid, v in state.items())
        else:
            media_sig = [(mid, *state[mid]) for mid in deps if mid in state]
        payload = {
            "geom": engine._pixel_geom(),                  # preview vs full-res
            "media_id": clip.media_id,
            "trim": [clip.in_point, clip.out_point],
            "ops": [o.to_dict() for o in self._ops_for_clip(clip.id)],
            "flow": clip.flow_transfer,
            "raw": clip.raw_effects,
            # fx_mask only changes the segment when it gates raw FX (pixel-FX
            # masking happens later, in the finish pass).
            "fx_mask": clip.fx_mask if clip.raw_effects else None,
            "media_sig": media_sig,
        }
        blob = json.dumps(payload, sort_keys=True, default=str)
        return hashlib.sha1(blob.encode()).hexdigest()

    def _clip_seg(self, engine: MoshEngine, clip: Clip, frames, media,
                  media_state=None) -> Tuple[Path, int]:
        """A clip's post-mosh, post-pixel segment AVI plus its frame count,
        served from the engine's per-clip cache when its codec+pixel state is
        unchanged -- so editing one clip reuses the others' expensive output
        instead of redoing it. *frames* may be a thunk; it is only called on a
        cache miss, so a hit also skips the codec-domain mosh entirely.

        A per-key lock serialises concurrent builders of the *same* segment
        (e.g. duplicated clips rendered by parallel workers): the first one
        computes, the rest wait and take the cache hit."""
        key = self._clip_seg_key(engine, clip, media_state)
        with self._seg_lock_for(key):
            cached = engine.seg_cache_get(key)
            n = self._seg_n.get(key)
            if cached is not None and n is not None:
                return cached, n
            frames = frames() if callable(frames) else frames
            self._seg_n[key] = len(frames)
            if cached is not None:                 # AVI cached, count forgotten
                return cached, len(frames)
            seg = engine.write_moshed(frames, media, engine._tmp(".avi"))
            seg = self._clip_pixels(engine, clip, seg, len(frames))
            return engine.seg_cache_put(key, seg), len(frames)

    def _seg_lock_for(self, key: str) -> threading.Lock:
        with self._seg_locks_mutex:
            if len(self._seg_locks) > 512 and key not in self._seg_locks:
                self._seg_locks.clear()            # bound the registry
            return self._seg_locks.setdefault(key, threading.Lock())

    def _parallel_segments(self, engine: MoshEngine, tasks, *, progress=None,
                           steps=None, noun: str = "clip") -> List:
        """Run per-clip segment builders and return their results in order.

        The heavy stages are ffmpeg subprocesses (which release the GIL), so
        with ``engine.seg_workers > 1`` the tasks fan out across a thread pool;
        the engine's tmp counter, segment cache and motion-RGB cache are locked
        for exactly this. Falls back to the serial loop for one worker/task.
        The first failing task aborts the render (its exception propagates)."""
        n = len(tasks)
        total = steps if steps is not None else n + 1
        workers = min(int(getattr(engine, "seg_workers", 1) or 1), n)
        if workers <= 1:
            out = []
            for i, task in enumerate(tasks):
                if progress:
                    progress(i, total, f"Rendering {noun} {i + 1}/{n}…")
                out.append(task())
            return out
        from concurrent.futures import ThreadPoolExecutor, as_completed
        if progress:
            progress(0, total, f"Rendering {n} {noun}s ({workers} workers)…")
        results: List = [None] * n
        done = 0
        with ThreadPoolExecutor(max_workers=workers,
                                thread_name_prefix="moshit-seg") as ex:
            futs = {ex.submit(task): i for i, task in enumerate(tasks)}
            try:
                for f in as_completed(futs):
                    results[futs[f]] = f.result()
                    done += 1
                    if progress:
                        progress(done, total, f"Rendered {noun} {done}/{n}…")
            except BaseException:
                for f in futs:                     # drop work that hasn't started
                    f.cancel()
                raise
        return results

    def _alpha_matte_segment(self, engine: MoshEngine, clip: Clip, motion=None):
        """An aligned grayscale alpha-map segment for *clip*, or None.

        The clip's source-file alpha map is trimmed to the clip and put through
        the *same codec-domain mosh ops* as the picture. Because the alpha map is
        encoded with the same GOP structure (keyframes only on GOP boundaries,
        see :meth:`FFmpeg.normalize`), the op chain transforms it frame-for-frame
        identically -- so the transparency blooms and smears in sympathy with the
        glitch and stays aligned. Only the finish-stage transforms that re-time
        the picture out from under the map -- speed, reverse, optical-flow -- still
        fall back to an opaque matte. Returns a temp segment the caller consumes.
        """
        item = self.media.get(clip.media_id)
        if not item or not getattr(item, "alpha_path", None):
            return None
        if not Path(item.alpha_path).exists():
            return None
        if clip.speed != 1.0 or clip.reverse or clip.flow_transfer is not None:
            return None
        media = self._parsed_media(clip.media_id)
        in_pt = self._snapped_in_point(media, clip.in_point)
        out_pt = clip.out_point if clip.out_point is not None else len(media.frames)
        w, h = self.config.width, self.config.height
        ops = self._ops_for_clip(clip.id)
        if not ops:                                # clean placement: decode + slice
            frames = list(engine.ff.decode_rgb_raw(item.alpha_path, w, h))[in_pt:out_pt]
            if not frames:
                return None
            return engine.ff.encode_rgb_raw(
                frames, engine._tmp(".avi"), width=w, height=h, fps=self.config.fps,
                qscale=self.config.qscale, gop=self.config.gop)
        # moshed: run the alpha map's own coded frames through the identical op
        # chain (same regions, same motion sources) so it tracks the picture.
        alpha_av = avi.parse_avi(item.alpha_path)
        aframes = list(alpha_av.frames[in_pt:out_pt])
        if not aframes:
            return None
        if motion is None:
            motion = self._motion_frames()
        for op in ops:
            aframes = engine.mosh(
                _as_avivideo(aframes, alpha_av), op.mode, op.params,
                motion_clips=_wrap_motion(motion),
                region=self._op_region(op, len(aframes)))
        return engine.write_moshed(aframes, alpha_av, engine._tmp(".avi"))

    @staticmethod
    def _op_region(op: MoshOp, n: int):
        """The frame ``range`` an op applies to within its *n*-frame input, or
        None for the whole thing (clamped; an empty/full span means None)."""
        start = max(0, min(int(op.region_start or 0), n))
        end = n if op.region_end is None else max(start, min(int(op.region_end), n))
        if start >= end or (start == 0 and end == n):
            return None
        return range(start, end)

    def _motion_frames(self) -> Dict[str, List[Frame]]:
        # Any imported clip can drive motion; keyed by its (unique) label. Skip
        # media with no intermediate on disk yet (e.g. an unrendered precomp).
        return {m.label: self._parsed_media(m.id).frames
                for m in self.media.values()
                if Path(m.intermediate_path).exists()}

    # -- building (all non-destructive) ------------------------------------- #

    def import_media(self, engine: MoshEngine, source_path, label: Optional[str] = None,
                     role: str = "main") -> MediaItem:
        """Normalise *source_path* and register it. The source is never altered."""
        label = label or Path(source_path).stem
        existing = {m.label for m in self.media.values()}
        if label in existing:                          # keep labels unique
            base, n = label, 2
            while f"{base} ({n})" in existing:
                n += 1
            label = f"{base} ({n})"
        clip = engine.normalize_clip(source_path, label=label,
                                     single_keyframe=(role == "motion"))
        media_id = _new_id("media")
        inter = Path(clip.source)                  # engine wrote the intermediate here
        if self.assets_dir:
            dest = self.assets_dir / f"{media_id}.avi"
            shutil.copy2(inter, dest)
            inter = dest
        item = MediaItem(
            id=media_id, source_path=str(source_path), label=label, role=role,
            intermediate_path=str(inter), width=clip.width, height=clip.height,
            fps=clip.fps, nb_frames=len(clip.frames))
        # capture source-file transparency (for alpha mattes) as a grayscale map
        if role != "motion" and engine.source_has_alpha(source_path):
            amap = (self.assets_dir / f"{media_id}.alpha.avi") if self.assets_dir \
                else Path(inter).with_suffix(".alpha.avi")
            try:
                engine.extract_alpha_map(source_path, amap)
                item.alpha_path = str(amap)
            except Exception:
                item.alpha_path = None          # alpha matte falls back to opaque
        self.media[media_id] = item
        self._cache_parsed(media_id, avi.parse_avi(item.intermediate_path))
        return item

    def relink_media(self, engine: MoshEngine, media_id: str,
                     source_path) -> MediaItem:
        """Rebuild an offline media item's cached intermediate from
        *source_path*. The id is kept, so clips and effects stay attached."""
        m = self.media[media_id]
        clip = engine.normalize_clip(source_path, label=m.label,
                                     single_keyframe=(m.role == "motion"))
        inter = Path(clip.source)                  # engine's fresh intermediate
        if self.assets_dir:
            self.assets_dir.mkdir(parents=True, exist_ok=True)
            dest = self.assets_dir / f"{media_id}.avi"
            shutil.copy2(inter, dest)
            inter = dest
        m.source_path = str(source_path)
        m.intermediate_path = str(inter)
        m.width, m.height = clip.width, clip.height
        m.fps, m.nb_frames = clip.fps, len(clip.frames)
        m.alpha_path = None                        # re-capture source alpha
        if m.role != "motion" and engine.source_has_alpha(source_path):
            amap = (self.assets_dir / f"{media_id}.alpha.avi") if self.assets_dir \
                else Path(inter).with_suffix(".alpha.avi")
            try:
                engine.extract_alpha_map(source_path, amap)
                m.alpha_path = str(amap)
            except Exception:
                m.alpha_path = None                # falls back to opaque
        self._cache_parsed(media_id, avi.parse_avi(m.intermediate_path))
        return m

    def add_clip(self, media_id: str, track: str = "main", *,
                 start: Optional[int] = None, in_point: int = 0,
                 out_point: Optional[int] = None) -> Clip:
        if media_id not in self.media:
            raise KeyError(f"no media '{media_id}'")
        if start is None:
            start = self._timeline_end(track)
        try:
            seq_id = self.track(track).seq_id      # keep the clip with its track
        except KeyError:
            seq_id = self.root_seq_id
        c = Clip(id=_new_id("clip"), media_id=media_id, track=track,
                 start=start, in_point=in_point, out_point=out_point,
                 seq_id=seq_id)
        self.clips.append(c)
        return c

    def add_track(self, seq_id: Optional[str] = None, *, role: str = "video",
                  name: Optional[str] = None) -> Track:
        """Add a track to a sequence (the root by default), on top of its peers."""
        seq_id = seq_id or self.root_seq_id
        idx = 1 + max((t.index for t in self.tracks_for(seq_id, role)), default=-1)
        t = Track(id=_new_id("track"), seq_id=seq_id,
                  name=name or f"Video {idx + 1}", index=idx, role=role)
        self.tracks.append(t)
        return t

    def add_sequence(self, name: str = "Precomp") -> Sequence:
        """Create a new sequence (precomp) with one empty video track."""
        s = Sequence(id=_new_id("seq"), name=name, width=self.config.width,
                     height=self.config.height, fps=self.config.fps)
        self.sequences.append(s)
        self.add_track(s.id, role="video", name="Video 1")
        return s

    def _precomp_dir(self) -> Path:
        if self.assets_dir:
            return self.assets_dir
        if self._tmp_assets is None:
            import tempfile
            self._tmp_assets = Path(tempfile.mkdtemp(prefix="moshit_precomp_"))
        return self._tmp_assets

    def sequence_media(self, seq_id: str) -> MediaItem:
        """The MediaItem that backs a sequence used as a clip, creating it (with
        a cache path for its rendered intermediate) the first time."""
        self.sequence(seq_id)                      # validate
        for m in self.media.values():
            if m.sequence_id == seq_id:
                return m
        mid = _new_id("media")
        item = MediaItem(
            id=mid, source_path="", label=self.sequence(seq_id).name,
            role="main", intermediate_path=str(self._precomp_dir() / f"{mid}.avi"),
            width=self.config.width, height=self.config.height,
            fps=self.config.fps, nb_frames=0, derived=True, sequence_id=seq_id)
        self.media[mid] = item
        return item

    def add_sequence_clip(self, track_id: str, seq_id: str, *,
                          start: Optional[int] = None) -> Clip:
        """Place a sequence (precomp) as a clip on *track_id* (cycle-checked at
        render time)."""
        media = self.sequence_media(seq_id)
        return self.add_clip(media.id, track_id, start=start)

    def add_mosh(self, mode: str, params: Dict, target_clip_id: str) -> MoshOp:
        self.clip(target_clip_id)                  # validate
        o = MoshOp(id=_new_id("op"), mode=mode, params=dict(params),
                   target_clip_id=target_clip_id)
        self.mosh_ops.append(o)
        return o

    def clip_ops(self, clip_id: str) -> List[MoshOp]:
        """All non-archived ops on a clip, in application order (the stack)."""
        return [o for o in self.mosh_ops
                if o.target_clip_id == clip_id and not o.archived]

    def remove_mosh(self, op_id: str) -> bool:
        before = len(self.mosh_ops)
        self.mosh_ops = [o for o in self.mosh_ops if o.id != op_id]
        return len(self.mosh_ops) != before

    def move_mosh(self, op_id: str, delta: int) -> bool:
        """Reorder an op within its clip's stack (delta -1 = earlier, +1 = later).

        Application order is the order in ``mosh_ops``, so a move swaps the op
        with the sibling ``delta`` away in the clip's own sub-sequence.
        """
        op = next((o for o in self.mosh_ops if o.id == op_id), None)
        if op is None:
            return False
        siblings = self.clip_ops(op.target_clip_id)
        idx = siblings.index(op)
        new = idx + delta
        if new < 0 or new >= len(siblings):
            return False
        other = siblings[new]
        i, j = self.mosh_ops.index(op), self.mosh_ops.index(other)
        self.mosh_ops[i], self.mosh_ops[j] = self.mosh_ops[j], self.mosh_ops[i]
        return True

    def split_clip(self, clip_id: str, offset: int) -> Optional[Clip]:
        """Split a clip *offset* frames from its start into two clips.

        The original keeps its id (and any mosh op) as the first half; a new
        clip becomes the second half. Returns the new clip, or None if the
        offset is out of range.
        """
        c = self.clip(clip_id)
        media = self.media[c.media_id]
        out = c.out_point if c.out_point is not None else media.nb_frames
        offset = int(offset)                       # timeline (post-speed) frames
        src_off = round(offset * c.speed) if c.speed else offset
        if offset <= 0 or c.in_point + src_off >= out:
            return None
        split = c.in_point + src_off
        # speed/reverse carry to both halves; the tail fade moves to the new one
        new = Clip(id=_new_id("clip"), media_id=c.media_id, track=c.track,
                   start=c.start + offset, in_point=split, out_point=out,
                   speed=c.speed, reverse=c.reverse, fade_out=c.fade_out)
        c.out_point = split
        c.fade_out = 0
        self.clips.insert(self.clips.index(c) + 1, new)
        return new

    def duplicate_clip(self, clip_id: str, *, copy_ops: bool = True) -> Clip:
        """Duplicate a clip in place, inserting the copy right after it.

        The copy shares the same media and trim, and (by default) the clip's
        active mosh ops are duplicated onto it too -- so duplicating a clip
        carries its effect, the way copy/paste does in an NLE. The main track is
        left for the caller to repack. Returns the new clip.
        """
        c = self.clip(clip_id)
        length = self._clip_length(c)
        # carry speed/reverse/fades; not transition_in (the copy hard-cuts in)
        new = Clip(id=_new_id("clip"), media_id=c.media_id, track=c.track,
                   start=c.start + length, in_point=c.in_point,
                   out_point=c.out_point, enabled=c.enabled, speed=c.speed,
                   reverse=c.reverse, fade_in=c.fade_in, fade_out=c.fade_out)
        self.clips.insert(self.clips.index(c) + 1, new)
        if copy_ops:
            for o in list(self.mosh_ops):
                if o.target_clip_id == clip_id and o.enabled and not o.archived:
                    self.mosh_ops.append(
                        MoshOp(id=_new_id("op"), mode=o.mode,
                               params=dict(o.params), target_clip_id=new.id))
        return new

    def _timeline_end(self, track: str) -> int:
        end = 0
        for c in self.clips:
            if c.track == track and not c.archived:
                length = self._clip_length(c)
                end = max(end, c.start + length)
        return end

    def _source_len(self, c: Clip) -> int:
        """Trimmed length in the clip's *source* frames (before any speed)."""
        media = self.media[c.media_id]
        out = c.out_point if c.out_point is not None else media.nb_frames
        return max(0, out - c.in_point)

    def _clip_length(self, c: Clip) -> int:
        """Timeline length in frames, after speed (reverse keeps the count).

        This is the clip's own extent; its timeline position comes from
        ``clip.start`` and any crossfade overlap is handled by ``track_layout``.
        """
        n = self._source_len(c)
        if n and c.speed and c.speed != 1.0:
            n = max(1, round(n / c.speed))
        return n

    # -- frame extraction --------------------------------------------------- #

    def _snapped_in_point(self, media: AviVideo, in_point: int) -> int:
        """The clip's in-point snapped back to the preceding keyframe.

        A clip must start on a keyframe to decode on its own, so trims align to
        the nearest preceding I-frame. Audio extraction uses the same snapped
        point so sound lines up with the frames actually shown.
        """
        if in_point <= 0:
            return 0
        keys = [i for i in media.iframe_indices if i <= in_point]
        return keys[-1] if keys else 0

    def _clip_frames(self, c: Clip, snap_to_keyframe: bool = True) -> List[Frame]:
        """The (trimmed) frames a clip contributes, keyframe-aligned at the head."""
        media = self._parsed_media(c.media_id)
        frames = media.frames
        in_pt = self._snapped_in_point(media, c.in_point) if snap_to_keyframe \
            else c.in_point
        out_pt = c.out_point if c.out_point is not None else len(frames)
        return list(frames[in_pt:out_pt])

    # -- render (read-only) ------------------------------------------------- #

    def main_clips(self) -> List[Clip]:
        return self.clips_for_track(MAIN_TRACK_ID)

    def track_layout(self, track_id: str) -> List[Tuple[Clip, int, int, int]]:
        """A track's clips as ``[(clip, start, length, trans), ...]`` in frames.

        Clips are placed at their own ``start`` (gaps allowed). ``trans`` is the
        crossfade = how many frames a clip overlaps the previous one: either set
        explicitly by positioning (``start`` < the previous clip's end), or, for a
        clip still butted up against the previous one, pulled back by its legacy
        ``transition_in``. Shared by the timeline drawing, the compositor, and
        audio-aligned features (e.g. beat sync).
        """
        out: List[Tuple[Clip, int, int, int]] = []
        prev_end = 0
        prev_len = 0
        for i, clip in enumerate(self.clips_for_track(track_id)):
            length = self._clip_length(clip)
            start = max(0, int(clip.start))
            if i > 0 and start >= prev_end and int(getattr(clip, "transition_in", 0)):
                start = max(0, prev_end
                            - min(int(clip.transition_in), length, prev_len))
            trans = min(max(0, prev_end - start), length, prev_len) if i > 0 else 0
            out.append((clip, start, length, trans))
            prev_end = start + length
            prev_len = length
        return out

    def main_layout(self) -> List[Tuple[Clip, int, int, int]]:
        """Overlap-aware layout of the root sequence's first video track."""
        return self.track_layout(MAIN_TRACK_ID)

    def render(self, engine: MoshEngine, out_avi, *,
               profile: Optional[str] = None, export_path=None,
               audio: bool = False, sequence_id: Optional[str] = None,
               progress: Optional[Callable[[int, int, str], None]] = None) -> Dict:
        """Materialise a sequence (the root by default). Does not mutate the
        project.

        A single video track of contiguous clips at full opacity and the
        ``normal`` blend takes the **flat** path (codec-domain fast path, or
        per-clip finish + crossfade fold). Otherwise — multiple tracks, free
        positions/overlaps, or any opacity/blend — its video tracks are
        **composited** bottom-to-top (opacity + blend mode + alpha) in the pixel
        domain. ``audio_plans`` carries one plan per audible video track; with a
        *profile* and *audio* they are summed (per-clip ``gain``) and the mix is
        muxed on export. ``audio_plan`` keeps the first track's plan for
        single-track callers.
        """
        seq_id = sequence_id or self.root_seq_id
        self._resolve_precomps(engine, seq_id)
        vtracks = self.video_tracks(seq_id)
        occupied = [t for t in vtracks if self.clips_for_track(t.id)]
        if not occupied:
            raise ValueError("no enabled video clips to render")
        clips = self.clips_for_track(occupied[0].id)
        simple = len(occupied) == 1 and self._is_contiguous(clips) and all(
            abs(getattr(c, "opacity", 1.0) - 1.0) < 1e-6
            and getattr(c, "blend_mode", "normal") == "normal"
            and getattr(c, "layer_mask", None) is None    # a matte needs compositing
            for c in clips)
        if simple:
            result = self._render_flat(
                engine, out_avi, clips,
                profile=profile, export_path=export_path, audio=audio,
                progress=progress)
        else:
            result = self._render_composite(
                engine, out_avi, vtracks,
                profile=profile, export_path=export_path, audio=audio,
                progress=progress)
        engine.seg_cache_trim()        # bound the cache now the fold is done
        return result

    def _is_contiguous(self, clips) -> bool:
        """True if clips butt up start-to-end with no free-positioned gap/overlap
        (so the codec/flat path, which concatenates in order, is faithful)."""
        cursor = 0
        for c in clips:
            if int(c.start) != cursor:
                return False
            cursor += self._clip_length(c)
        return True

    def _resolve_precomps(self, engine: MoshEngine, seq_id: str,
                          _visiting: Optional[List[str]] = None) -> None:
        """(Re)render every sequence-backed media this sequence uses, depth-first
        and cached by content digest, so a precomp clip decodes from a fresh
        intermediate. Raises on a sequence cycle."""
        _visiting = _visiting or []
        if seq_id in _visiting:
            chain = " -> ".join(_visiting + [seq_id])
            raise ValueError(f"sequence cycle: {chain}")
        stack = _visiting + [seq_id]
        for t in self.tracks_for(seq_id):
            for c in self.clips_for_track(t.id):
                m = self.media.get(c.media_id)
                if m and m.sequence_id:
                    self._resolve_precomps(engine, m.sequence_id, stack)
                    self._render_sequence_to_media(engine, m)

    def _sequence_digest(self, seq_id: str) -> str:
        """Content hash of a sequence (its tracks, clips, ops, and the digests of
        any precomps it nests) -- the cache key for its rendered intermediate."""
        import hashlib
        clip_ids = {c.id for c in self.clips if c.seq_id == seq_id}
        nested = sorted((m.id, m.digest) for m in self.media.values()
                        if m.sequence_id and m.id in {c.media_id for c in self.clips
                                                      if c.seq_id == seq_id})
        payload = {
            "tracks": [t.to_dict() for t in self.tracks_for(seq_id)],
            "clips": [c.to_dict() for c in self.clips if c.seq_id == seq_id],
            "ops": [o.to_dict() for o in self.mosh_ops
                    if o.target_clip_id in clip_ids],
            "geom": (self.config.width, self.config.height, self.config.fps),
            "nested": nested,
        }
        blob = json.dumps(payload, sort_keys=True, default=str)
        return hashlib.sha1(blob.encode()).hexdigest()

    def _render_sequence_to_media(self, engine: MoshEngine,
                                  media: MediaItem) -> None:
        out = Path(media.intermediate_path)
        digest = self._sequence_digest(media.sequence_id)
        if media.digest == digest and out.exists():
            return                                 # cache hit: nothing changed
        # A precomp's cached intermediate is shared with export, so always render
        # it at full geometry/quality even when the outer render is a fast preview.
        cap, ov = engine.preview_max_width, engine.flow_preset_override
        engine.preview_max_width, engine.flow_preset_override = None, None
        try:
            self.render(engine, out, sequence_id=media.sequence_id)
        finally:
            engine.preview_max_width, engine.flow_preset_override = cap, ov
        av = avi.parse_avi(str(out))
        media.nb_frames = len(av.frames)
        media.width = media.width or self.config.width
        media.height = media.height or self.config.height
        media.fps = media.fps or self.config.fps
        media.digest = digest
        self._cache_parsed(media.id, av)           # refresh the parse cache

    def _audio_seg(self, clip: Clip, mlen: int, prev_len: int, idx: int,
                   fps: float):
        """One audio-plan segment for *clip* (see build_audio_track). A clip's
        trim indexes its *source* media so source audio lines up; only baked
        (derived) media can't map back, so it stays silent. Speed/reverse/fades/
        crossfade mirror the video finish. Returns ``(segment, trans)``."""
        media = self._parsed_media(clip.media_id)
        item = self.media[clip.media_id]
        snapped = self._snapped_in_point(media, clip.in_point)
        trans = min(clip.transition_in, mlen, prev_len) if idx > 0 else 0
        return ({
            "source": item.source_path, "start": snapped / fps,
            "duration": mlen / fps, "silent": item.derived,
            "speed": clip.speed, "reverse": clip.reverse,
            "fade_in": clip.fade_in, "fade_out": clip.fade_out,
            "transition_in": trans, "gain": float(clip.gain),
        }, trans)

    def _export_result(self, engine: MoshEngine, out, audio_plans, frames,
                       n_clips, *, profile, export_path, audio) -> Dict:
        # audio_plans: one plan per (audible) track. The first is the canonical
        # single-track plan kept under "audio_plan" for back-compat/preview; the
        # full list under "audio_plans" drives multi-track mixing.
        audio_plans = audio_plans or [[]]
        result = {"moshed_avi": out, "frames": frames, "clips_rendered": n_clips,
                  "audio_plan": audio_plans[0], "audio_plans": audio_plans}
        if profile:
            fps = self.config.fps or 30.0
            audio_path = None
            if audio:
                audio_path = engine.mix_audio(
                    audio_plans, Path(out).with_suffix(".audio.wav"), fps=fps)
                if audio_path:
                    result["audio"] = audio_path
            ep = Path(export_path) if export_path else Path(out).with_suffix(
                {"h264_mp4": ".mp4", "h265_mp4": ".mp4", "prores_mov": ".mov",
                 "ffv1_mkv": ".mkv", "vp9_webm": ".webm"}.get(profile, ".mp4"))
            result["export"] = engine.export(out, ep, profile,
                                             audio_path=audio_path)
        return result

    def _clip_segment(self, engine: MoshEngine, clip: Clip, motion=None,
                      media_state=None):
        """Render one clip to a finished segment AVI at project geometry (mosh +
        flow + per-clip finish chain). Returns ``(path, post_speed_length)``."""
        media = self._parsed_media(clip.media_id)
        if motion is None:
            motion = self._motion_frames()

        def moshed():
            frames = self._clip_frames(clip)
            for op in self._ops_for_clip(clip.id):
                frames = engine.mosh(
                    _as_avivideo(frames, media), op.mode, op.params,
                    motion_clips=_wrap_motion(motion),
                    region=self._op_region(op, len(frames)))
            return frames

        seg, n = self._clip_seg(engine, clip, moshed, media, media_state)
        meta = [{"n": n, "speed": clip.speed, "reverse": clip.reverse,
                 "fade_in": clip.fade_in, "fade_out": clip.fade_out,
                 "transition_in": 0,
                 "pixel": self._pixel_filters(clip, nframes=n),
                 "fx_mask": clip.fx_mask}]
        # The finished (post-speed/fade/pixel-filter) segment is cached too, so
        # a composite re-render only re-runs the finish for clips whose state
        # actually changed -- repositioning/opacity/blend edits skip it (P11).
        fin_key = "fin_" + self._blob_key(
            {"seg": self._clip_seg_key(engine, clip, media_state), "meta": meta,
             "geom": engine._pixel_geom(),
             "enc": [self.config.fps, self.config.gop, self.config.qscale]})
        with self._seg_lock_for(fin_key):
            finished = engine.seg_cache_get(fin_key)
            if finished is None:
                finished = engine.finish_clips([seg], meta, engine._tmp(".avi"))
                finished = engine.seg_cache_put(fin_key, finished)
        # seg + finished are cache-owned (LRU-evicted); leave them for reuse.
        length = (max(1, round(n / clip.speed))
                  if (clip.speed and clip.speed != 1.0) else n)
        return finished, length

    @staticmethod
    def _blob_key(payload) -> str:
        import hashlib
        blob = json.dumps(payload, sort_keys=True, default=str)
        return hashlib.sha1(blob.encode()).hexdigest()

    def _render_flat(self, engine: MoshEngine, out_avi, clips, *,
                     profile=None, export_path=None, audio=False,
                     progress=None) -> Dict:
        fps = self.config.fps or 30.0
        motion = self._motion_frames()
        media_state = self._media_state()          # one stat per media
        needs_finish = any(c.has_finish() for c in clips)

        def moshed(c, media):
            """The clip's codec-domain frame list (the pure-Python mosh)."""
            frames = self._clip_frames(c)
            for op in self._ops_for_clip(c.id):
                frames = engine.mosh(
                    _as_avivideo(frames, media), op.mode, op.params,
                    motion_clips=_wrap_motion(motion),
                    region=self._op_region(op, len(frames)))
            return frames

        # Codec-domain stage. On the finish path the mosh runs lazily inside
        # _clip_seg, so a segment-cache hit skips it entirely; the fast path
        # concatenates coded frames directly, so it always needs them.
        steps = len(clips) + 1                     # per-clip work + assembly
        entries = [(c, self._parsed_media(c.media_id)) for c in clips]  # serial
        template: Optional[AviVideo] = entries[0][1] if entries else None
        if needs_finish:
            # Segment building is dominated by ffmpeg subprocess work, so fan
            # the clips out across the engine's worker threads (P17).
            tasks = [
                (lambda c=c, media=media: self._clip_seg(
                    engine, c, lambda: moshed(c, media), media, media_state))
                for c, media in entries]
            results = self._parallel_segments(engine, tasks,
                                              progress=progress, steps=steps)
            segs = [(c, media, seg, n)                 # (clip, media, seg, n)
                    for (c, media), (seg, n) in zip(entries, results)]
        else:                                      # pure-Python concat: serial
            segs = []
            for k, (c, media) in enumerate(entries):
                if progress:
                    progress(k, steps, f"Rendering clip {k + 1}/{len(clips)}…")
                frames = moshed(c, media)
                segs.append((c, media, frames, len(frames)))

        # Per-clip finished (post-speed) length; crossfade overlap handled below.
        finished = [(max(1, round(n / c.speed))
                     if (c.speed and c.speed != 1.0) else n)
                    for c, _, _, n in segs]

        audio_plan: List[Dict] = []
        total = 0
        prev_len = 0
        for idx, ((c, _, _, _), mlen) in enumerate(zip(segs, finished)):
            seg, trans = self._audio_seg(c, mlen, prev_len, idx, fps)
            audio_plan.append(seg)
            total += mlen - trans
            prev_len = mlen

        if progress:
            progress(len(clips), steps, "Assembling sequence…")
        if needs_finish:
            meta = [{"n": n, "speed": c.speed, "reverse": c.reverse,
                     "fade_in": c.fade_in, "fade_out": c.fade_out,
                     "transition_in": c.transition_in,
                     "pixel": self._pixel_filters(c, nframes=n),
                     "fx_mask": c.fx_mask}
                    for c, _, _, n in segs]
            # The whole fold is cached on (ordered seg keys + finish meta):
            # a render whose video is unchanged (undo/redo hop, audio-only
            # edit, manual refresh) copies the cached AVI instead of
            # re-encoding the sequence (P11).
            fold_key = "fold_" + self._blob_key(
                {"segs": [self._clip_seg_key(engine, c, media_state)
                          for c, _, _, _ in segs],
                 "meta": meta, "geom": engine._pixel_geom(),
                 "enc": [self.config.fps, self.config.gop, self.config.qscale]})
            cached = engine.seg_cache_get(fold_key)
            if cached is not None:
                shutil.copyfile(cached, out_avi)
                out = Path(out_avi)
            else:
                out = engine.finish_clips([s for _, _, s, _ in segs], meta,
                                          out_avi)
                # cache a copy -- the caller owns (and later rewrites) out_avi
                engine.seg_cache_put(fold_key,
                                     shutil.copy2(out, engine._tmp(".avi")))
            # segments are cache-owned (LRU-evicted); leave them for reuse.
        else:                                      # fast path: concat coded chunks
            sequence: List[Frame] = []
            for _, _, frames, _ in segs:
                sequence.extend(frames)
            out = engine.write_moshed(sequence, template, out_avi)

        return self._export_result(engine, out, [audio_plan], total, len(clips),
                                   profile=profile, export_path=export_path,
                                   audio=audio)

    def _render_composite(self, engine: MoshEngine, out_avi, vtracks, *,
                          profile=None, export_path=None, audio=False,
                          progress=None) -> Dict:
        fps = self.config.fps or 30.0
        layouts = {t.id: self.track_layout(t.id) for t in vtracks}
        total = max((start + length
                     for lay in layouts.values() for _c, start, length, _t in lay),
                    default=0)
        if total <= 0:
            raise ValueError("empty composition")

        motion = self._motion_frames()             # shared by every clip below
        media_state = self._media_state()
        pairs = [(t, clip, int(start), length, int(trans))
                 for t in vtracks                  # bottom -> top by index
                 for clip, start, length, trans in layouts[t.id]]
        steps = len(pairs) + 1                     # per-clip work + composite

        def seg_task(clip):
            """Segment + (optional) aligned alpha matte for one layer."""
            seg, seglen = self._clip_segment(engine, clip, motion, media_state)
            lm = clip.layer_mask
            mask_input = (self._alpha_matte_segment(engine, clip, motion)
                          if lm and lm.get("source") == "alpha" else None)
            return seg, seglen, mask_input

        results = self._parallel_segments(
            engine, [(lambda clip=clip: seg_task(clip))
                     for _t, clip, _s, _l, _tr in pairs],
            progress=progress, steps=steps, noun="layer")

        layers: List[Dict] = []
        by_track: Dict[str, List] = {}             # track: [(clip,start,len,trans)]
        for (t, clip, start, _length, trans), (seg, seglen, mask_input) \
                in zip(pairs, results):
            layers.append({"input": seg, "start": start,
                           "length": seglen, "opacity": float(clip.opacity),
                           "blend": clip.blend_mode, "head_fade": trans,
                           "mask": clip.layer_mask,
                           "mask_input": str(mask_input) if mask_input else None})
            by_track.setdefault(t.id, []).append((clip, start, seglen, trans))
        track_seqs = [by_track[t.id] for t in vtracks if t.id in by_track]
        if progress:
            progress(len(pairs), steps, "Compositing layers…")
        out = engine.composite(layers, out_avi, total_frames=total)
        # layer inputs are cache-owned finished segments (LRU-evicted between
        # renders); only the per-render alpha mattes are consumed here
        for lay in layers:
            if lay.get("mask_input"):
                try:
                    Path(lay["mask_input"]).unlink()
                except OSError:
                    pass

        # Audio: every enabled video track becomes a full-length plan (clips at
        # their absolute positions, gap silence, crossfade head-trims); the
        # tracks are summed in the mix. Single-track comps just pass one plan.
        audio_plans = [self._composite_audio_plan(seq, fps) for seq in track_seqs]
        return self._export_result(engine, out, audio_plans, total, len(layers),
                                   profile=profile, export_path=export_path,
                                   audio=audio)

    def _composite_audio_plan(self, track_seq, fps) -> List[Dict]:
        plan: List[Dict] = []
        prev_end = 0
        prev_len = 0
        for idx, (clip, start, length, trans) in enumerate(track_seq):
            gap = start - prev_end
            if gap > 0:                            # silence fills a free-position gap
                plan.append({"source": None, "start": 0.0, "duration": gap / fps,
                             "silent": True, "speed": 1.0, "reverse": False,
                             "fade_in": 0, "fade_out": 0, "transition_in": 0})
            seg, _ = self._audio_seg(clip, length, prev_len, idx, fps)
            seg["transition_in"] = int(trans)      # the actual overlap, not legacy
            plan.append(seg)
            prev_end = start + length
            prev_len = length
        return plan

    # -- bake (reversible) -------------------------------------------------- #

    def bake_op(self, engine: MoshEngine, op_id: str) -> BakeRecord:
        """Freeze a single mosh op into a baked clip; archive the originals."""
        op = self.op(op_id)
        target = self.clip(op.target_clip_id)
        media = self._parsed_media(target.media_id)
        frames = self._clip_frames(target)
        motion = self._motion_frames()
        moshed = engine.mosh(_as_avivideo(frames, media), op.mode, op.params,
                             motion_clips=_wrap_motion(motion),
                             region=self._op_region(op, len(frames)))

        # write moshed region, then re-encode (bake) to a clean moshable clip
        raw = engine.write_moshed(moshed, media, engine._tmp(".avi"))
        baked_id = _new_id("media")
        baked_path = (self.assets_dir / f"{baked_id}.avi") if self.assets_dir \
            else engine._tmp(".avi")
        baked_clip_av = engine.bake(raw, baked_path)

        baked_media = MediaItem(
            id=baked_id, source_path=self.media[target.media_id].source_path,
            label=f"baked_{target.id}", role="main",
            intermediate_path=str(baked_path), width=baked_clip_av.width,
            height=baked_clip_av.height, fps=baked_clip_av.fps,
            nb_frames=len(baked_clip_av.frames), derived=True)
        self.media[baked_id] = baked_media
        self._cache_parsed(baked_id, baked_clip_av)

        new_clip = Clip(id=_new_id("clip"), media_id=baked_id, track="main",
                        start=target.start, in_point=0,
                        out_point=len(baked_clip_av.frames))
        self.clips.append(new_clip)

        # archive (do not delete) the originals
        target.enabled = False
        target.archived = True
        op.enabled = False
        op.archived = True

        record = BakeRecord(
            id=_new_id("bake"), baked_media_id=baked_id, baked_clip_id=new_clip.id,
            replaced_clip_ids=[target.id], consumed_mosh_op_ids=[op.id])
        self.bake_records.append(record)
        return record

    def bake_clip(self, engine: MoshEngine, clip_id: str) -> BakeRecord:
        """Freeze a clip's whole effect stack into one baked clip.

        Applies every enabled op in order, re-encodes to a clean clip, and
        archives the original clip and all the ops it consumed. The clip's
        finishing (speed/reverse/fades/crossfade) is *not* baked in -- it carries
        over to the baked clip so it stays editable.
        """
        target = self.clip(clip_id)
        media = self._parsed_media(target.media_id)
        frames = self._clip_frames(target)
        motion = self._motion_frames()
        ops = self._ops_for_clip(clip_id)
        for op in ops:
            frames = engine.mosh(_as_avivideo(frames, media), op.mode, op.params,
                                 motion_clips=_wrap_motion(motion),
                                 region=self._op_region(op, len(frames)))

        raw = engine.write_moshed(frames, media, engine._tmp(".avi"))
        baked_id = _new_id("media")
        baked_path = (self.assets_dir / f"{baked_id}.avi") if self.assets_dir \
            else engine._tmp(".avi")
        baked_av = engine.bake(raw, baked_path)

        baked_media = MediaItem(
            id=baked_id, source_path=self.media[target.media_id].source_path,
            label=f"baked_{target.id}", role="main",
            intermediate_path=str(baked_path), width=baked_av.width,
            height=baked_av.height, fps=baked_av.fps,
            nb_frames=len(baked_av.frames), derived=True)
        self.media[baked_id] = baked_media
        self._cache_parsed(baked_id, baked_av)

        new_clip = Clip(id=_new_id("clip"), media_id=baked_id, track="main",
                        start=target.start, in_point=0,
                        out_point=len(baked_av.frames),
                        speed=target.speed, reverse=target.reverse,
                        fade_in=target.fade_in, fade_out=target.fade_out,
                        transition_in=target.transition_in,
                        pixel_effects=[dict(pe) for pe in target.pixel_effects],
                        raw_effects=[dict(re) for re in target.raw_effects],
                        layer_mask=dict(target.layer_mask) if target.layer_mask
                        else None,
                        fx_mask=dict(target.fx_mask) if target.fx_mask else None)
        self.clips.append(new_clip)

        target.enabled = False
        target.archived = True
        consumed = []
        for op in ops:
            op.enabled = False
            op.archived = True
            consumed.append(op.id)

        record = BakeRecord(
            id=_new_id("bake"), baked_media_id=baked_id, baked_clip_id=new_clip.id,
            replaced_clip_ids=[target.id], consumed_mosh_op_ids=consumed)
        self.bake_records.append(record)
        return record

    def apply_optical_flow(self, engine: MoshEngine, base_clip_id: str,
                           motion_media_id: str, **params) -> BakeRecord:
        """Replace a clip with an appearance-free, flow-warped version of its
        footage driven by *motion_media_id*. Reversible (a :class:`BakeRecord`,
        like :meth:`bake_clip`); the original clip is archived, not deleted."""
        target = self.clip(base_clip_id)
        base_media = self.media[target.media_id]
        motion_media = self.media[motion_media_id]

        warped_id = _new_id("media")
        warped_path = (self.assets_dir / f"{warped_id}.avi") if self.assets_dir \
            else engine._tmp(".avi")
        engine.optical_flow_transfer(base_media.intermediate_path,
                                     motion_media.intermediate_path,
                                     warped_path, **params)
        warped_av = avi.parse_avi(warped_path)

        derived = MediaItem(
            id=warped_id, source_path=base_media.source_path,
            label=f"flow_{target.id}", role="main",
            intermediate_path=str(warped_path), width=warped_av.width,
            height=warped_av.height, fps=warped_av.fps,
            nb_frames=len(warped_av.frames), derived=True)
        self.media[warped_id] = derived
        self._cache_parsed(warped_id, warped_av)

        new_clip = Clip(id=_new_id("clip"), media_id=warped_id, track="main",
                        start=target.start, in_point=0,
                        out_point=len(warped_av.frames),
                        speed=target.speed, reverse=target.reverse,
                        fade_in=target.fade_in, fade_out=target.fade_out,
                        transition_in=target.transition_in,
                        pixel_effects=[dict(pe) for pe in target.pixel_effects],
                        raw_effects=[dict(re) for re in target.raw_effects],
                        layer_mask=dict(target.layer_mask) if target.layer_mask
                        else None,
                        fx_mask=dict(target.fx_mask) if target.fx_mask else None)
        self.clips.append(new_clip)

        target.enabled = False
        target.archived = True
        record = BakeRecord(
            id=_new_id("bake"), baked_media_id=warped_id, baked_clip_id=new_clip.id,
            replaced_clip_ids=[target.id], consumed_mosh_op_ids=[])
        self.bake_records.append(record)
        return record

    def revert_bake(self, bake_record_id: str) -> None:
        """Undo a bake: restore archived clips/ops, drop the baked clip+media."""
        rec = next((r for r in self.bake_records if r.id == bake_record_id), None)
        if rec is None:
            raise KeyError(f"no bake record '{bake_record_id}'")

        for cid in rec.replaced_clip_ids:
            c = self.clip(cid)
            c.enabled = True
            c.archived = False
        for oid in rec.consumed_mosh_op_ids:
            o = self.op(oid)
            o.enabled = True
            o.archived = False

        self.clips = [c for c in self.clips if c.id != rec.baked_clip_id]
        baked = self.media.pop(rec.baked_media_id, None)
        self._uncache_parsed(rec.baked_media_id)
        if baked and baked.derived:
            p = Path(baked.intermediate_path)
            if self.assets_dir and p.exists() and p.parent == self.assets_dir:
                p.unlink(missing_ok=True)
        self.bake_records = [r for r in self.bake_records if r.id != bake_record_id]

    # -- persistence -------------------------------------------------------- #

    def to_dict(self) -> Dict:
        cfg = self.config.__dict__.copy()
        return {
            "version": self.VERSION, "name": self.name, "config": cfg,
            "assets_dir": str(self.assets_dir) if self.assets_dir else None,
            "root_seq_id": self.root_seq_id,
            "sequences": [s.to_dict() for s in self.sequences],
            "tracks": [t.to_dict() for t in self.tracks],
            "media": [m.to_dict() for m in self.media.values()],
            "clips": [c.to_dict() for c in self.clips],
            "mosh_ops": [o.to_dict() for o in self.mosh_ops],
            "bake_records": [r.to_dict() for r in self.bake_records],
        }

    def save(self, path) -> Path:
        path = Path(path)
        path.write_text(json.dumps(self.to_dict(), indent=2))
        return path

    @classmethod
    def from_dict(cls, d: Dict) -> "Project":
        cfg = EngineConfig(**d.get("config", {}))
        p = cls(name=d.get("name", "untitled"), config=cfg,
                assets_dir=d.get("assets_dir"))
        p.root_seq_id = d.get("root_seq_id", ROOT_SEQ_ID)
        # Pre-v-compositing saves have no sequences/tracks; _ensure_default_structure
        # (run in __init__) already created the root sequence + main/motion tracks,
        # and legacy clips carry track="main"/"motion" with seq_id defaulting to root.
        if d.get("sequences"):
            p.sequences = [Sequence.from_dict(s) for s in d["sequences"]]
        if d.get("tracks"):
            p.tracks = [Track.from_dict(t) for t in d["tracks"]]
        for m in d.get("media", []):
            item = MediaItem.from_dict(m)
            p.media[item.id] = item
        p.clips = [Clip.from_dict(c) for c in d.get("clips", [])]
        p.mosh_ops = [MoshOp.from_dict(o) for o in d.get("mosh_ops", [])]
        p.bake_records = [BakeRecord.from_dict(r) for r in d.get("bake_records", [])]
        p._ensure_default_structure()              # backfill anything still missing
        return p

    @classmethod
    def load(cls, path) -> "Project":
        p = cls.from_dict(json.loads(Path(path).read_text()))
        p._repair_media_paths(Path(path))
        return p

    def _repair_media_paths(self, project_path: Path) -> None:
        """Recover from a moved project folder: saved paths are absolute, so
        relocating ``proj.json`` + ``proj_assets/`` together leaves them stale
        even though every asset is right there next to the file."""
        assets = project_path.parent / f"{project_path.stem}_assets"
        if assets.is_dir():
            # saves always place assets in this sibling dir, so it wins over
            # the recorded path (which __init__ may have re-created, empty)
            self.assets_dir = assets
        for m in self.media.values():
            for attr in ("intermediate_path", "alpha_path"):
                val = getattr(m, attr)
                if val and not Path(val).exists():
                    cand = assets / Path(val).name
                    if cand.exists():
                        setattr(m, attr, str(cand))

    def missing_media(self) -> List[MediaItem]:
        """Media whose cached intermediate AVI is gone (offline). Precomp
        media are excluded — they re-render from their sequence."""
        return [m for m in self.media.values()
                if not m.sequence_id and not Path(m.intermediate_path).exists()]


# --------------------------------------------------------------------------- #
# Small adapters so render/bake can reuse MoshEngine.mosh
# --------------------------------------------------------------------------- #

def _as_avivideo(frames: List[Frame], template: AviVideo) -> AviVideo:
    """Wrap a frame list in an AviVideo that borrows *template*'s header."""
    return AviVideo(
        hdrl=template.hdrl, frames=frames, width=template.width,
        height=template.height, fps=template.fps, video_ckid=template.video_ckid,
        _avih_total_off=template._avih_total_off,
        _strh_len_off=template._strh_len_off, source=template.source)


def _wrap_motion(motion: Dict[str, List[Frame]]) -> Dict[str, AviVideo]:
    wrapped = {}
    for label, frames in motion.items():
        wrapped[label] = AviVideo(
            hdrl=bytearray(), frames=frames, width=0, height=0, fps=0.0,
            video_ckid=b"00dc", _avih_total_off=-1, _strh_len_off=-1, source=label)
    return wrapped
