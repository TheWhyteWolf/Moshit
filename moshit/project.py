"""Non-destructive project model.

Nothing here mutates source media, and every operation is recorded so it can be
undone:

* :class:`MediaItem` is an immutable reference to a source file plus its cached
  moshable intermediate.
* :class:`Clip` is a *view* into a MediaItem (track, position, in/out trim).
* :class:`MoshOp` is a *recipe* -- a mode plus parameters targeting a clip -- not
  a baked result.
* ``render`` materialises the current timeline read-only.
* ``bake`` freezes one mosh op into a new clip but **archives** (never deletes)
  the originals and writes a :class:`BakeRecord`, so ``revert_bake`` fully
  restores the prior state.

v1 timeline semantics: the main track is a sequence of non-overlapping clips; a
mosh op targets a single main-track clip and may pull motion from a clip on the
motion track. Overlapping/compositing tracks are future work.
"""
from __future__ import annotations

import json
import shutil
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from . import avi
from .avi import AviVideo, Frame
from .engine import EngineConfig, MoshEngine


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


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

    def to_dict(self) -> Dict:
        return self.__dict__.copy()

    @classmethod
    def from_dict(cls, d: Dict) -> "MediaItem":
        return cls(**d)


@dataclass
class Clip:
    id: str
    media_id: str
    track: str                     # "main" | "motion"
    start: int = 0                 # timeline position, in frames
    in_point: int = 0              # trim start within media (frames)
    out_point: Optional[int] = None  # exclusive; None = end of media
    enabled: bool = True
    archived: bool = False
    # -- clean-edit finishing (pixel-domain; applied in the render finish pass).
    # Defaults are inert, so a clip with all defaults takes the fast path.
    speed: float = 1.0             # 2.0 = twice as fast, 0.5 = half speed
    reverse: bool = False          # play the clip backwards
    fade_in: int = 0               # frames to fade up from black at the head
    fade_out: int = 0              # frames to fade to black at the tail
    transition_in: int = 0         # crossfade frames from the previous main clip

    def has_finish(self) -> bool:
        """True if this clip needs the pixel-domain finish pass."""
        return (self.speed != 1.0 or self.reverse or self.fade_in > 0
                or self.fade_out > 0 or self.transition_in > 0)

    def to_dict(self) -> Dict:
        return self.__dict__.copy()

    @classmethod
    def from_dict(cls, d: Dict) -> "Clip":
        return cls(**d)


@dataclass
class MoshOp:
    id: str
    mode: str
    params: Dict
    target_clip_id: str
    enabled: bool = True
    archived: bool = False

    def to_dict(self) -> Dict:
        return {"id": self.id, "mode": self.mode, "params": self.params,
                "target_clip_id": self.target_clip_id,
                "enabled": self.enabled, "archived": self.archived}

    @classmethod
    def from_dict(cls, d: Dict) -> "MoshOp":
        return cls(**d)


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
        return cls(**d)


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
        self._parsed: Dict[str, AviVideo] = {}     # media_id -> AviVideo (cache)

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

    def _parsed_media(self, media_id: str) -> AviVideo:
        if media_id not in self._parsed:
            self._parsed[media_id] = avi.parse_avi(self.media[media_id].intermediate_path)
        return self._parsed[media_id]

    def _ops_for_clip(self, clip_id: str) -> List[MoshOp]:
        return [o for o in self.mosh_ops
                if o.target_clip_id == clip_id and o.enabled and not o.archived]

    def _motion_frames(self) -> Dict[str, List[Frame]]:
        # Any imported clip can drive motion; keyed by its (unique) label.
        return {m.label: self._parsed_media(m.id).frames
                for m in self.media.values()}

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
        self.media[media_id] = item
        self._parsed[media_id] = avi.parse_avi(item.intermediate_path)
        return item

    def add_clip(self, media_id: str, track: str = "main", *,
                 start: Optional[int] = None, in_point: int = 0,
                 out_point: Optional[int] = None) -> Clip:
        if media_id not in self.media:
            raise KeyError(f"no media '{media_id}'")
        if start is None:
            start = self._timeline_end(track)
        c = Clip(id=_new_id("clip"), media_id=media_id, track=track,
                 start=start, in_point=in_point, out_point=out_point)
        self.clips.append(c)
        return c

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

        Crossfade overlap is a junction property and is not subtracted here, so
        the timeline lays clips out contiguously; the rendered video is the one
        that overlaps and is therefore shorter.
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
        return sorted((c for c in self.clips
                       if c.track == "main" and c.enabled and not c.archived),
                      key=lambda c: c.start)

    def render(self, engine: MoshEngine, out_avi, *,
               profile: Optional[str] = None, export_path=None,
               audio: bool = False) -> Dict:
        """Materialise the current timeline. Does not mutate the project.

        Always returns an ``audio_plan`` (one segment per main clip, keyed to the
        rendered video's duration). When *audio* is set and an export *profile*
        is given, the plan is assembled into a track and muxed into the export.
        Clean and moshed clips keep their source audio (padded/trimmed to the
        rendered length so the track stays in sync); baked clips are silent.
        """
        clips = self.main_clips()
        if not clips:
            raise ValueError("no enabled main-track clips to render")

        fps = self.config.fps or 30.0
        motion = self._motion_frames()
        needs_finish = any(c.has_finish() for c in clips)

        # Codec-domain stage: each clip's moshed frame list (pure Python).
        segs: List = []                            # (clip, media, frames)
        template: Optional[AviVideo] = None
        for c in clips:
            media = self._parsed_media(c.media_id)
            if template is None:
                template = media
            frames = self._clip_frames(c)
            for op in self._ops_for_clip(c.id):
                frames = engine.mosh(
                    _as_avivideo(frames, media), op.mode, op.params,
                    motion_clips=_wrap_motion(motion))
            segs.append((c, media, frames))

        # Per-clip finished (post-speed) length; crossfade overlap handled below.
        finished = [(max(1, round(len(f) / c.speed))
                     if (c.speed and c.speed != 1.0) else len(f))
                    for c, _, f in segs]

        audio_plan: List[Dict] = []
        total = 0
        prev_len = 0
        for idx, ((c, media, frames), mlen) in enumerate(zip(segs, finished)):
            item = self.media[c.media_id]
            snapped = self._snapped_in_point(media, c.in_point)
            # The first clip can't crossfade from a previous one; the video
            # finish ignores its transition_in, so the audio plan must too.
            trans = min(c.transition_in, mlen, prev_len) if idx > 0 else 0
            audio_plan.append({
                "source": item.source_path, "start": snapped / fps,
                "duration": mlen / fps,
                # A clip's trim indexes its *source* media, so source audio lines
                # up; only baked (derived) media -- re-encoded frames -- can't map
                # back, so it stays silent. Speed/reverse/fades/crossfade mirror
                # the video finish (see build_audio_track).
                "silent": item.derived,
                "speed": c.speed, "reverse": c.reverse,
                "fade_in": c.fade_in, "fade_out": c.fade_out,
                "transition_in": trans,
            })
            total += mlen - trans
            prev_len = mlen

        if needs_finish:
            seg_avis = [engine.write_moshed(frames, media, engine._tmp(".avi"))
                        for c, media, frames in segs]
            meta = [{"n": len(frames), "speed": c.speed, "reverse": c.reverse,
                     "fade_in": c.fade_in, "fade_out": c.fade_out,
                     "transition_in": c.transition_in}
                    for c, _, frames in segs]
            out = engine.finish_clips(seg_avis, meta, out_avi)
            for s in seg_avis:               # consumed; don't pile up across renders
                try:
                    Path(s).unlink()
                except OSError:
                    pass
        else:                                      # fast path: concat coded chunks
            sequence: List[Frame] = []
            for _, _, frames in segs:
                sequence.extend(frames)
            out = engine.write_moshed(sequence, template, out_avi)

        result = {"moshed_avi": out, "frames": total,
                  "clips_rendered": len(clips), "audio_plan": audio_plan}
        if profile:
            audio_path = None
            if audio:
                audio_path = engine.build_audio(
                    audio_plan, Path(out).with_suffix(".audio.wav"), fps=fps)
                if audio_path:
                    result["audio"] = audio_path
            ep = Path(export_path) if export_path else out.with_suffix(
                {"h264_mp4": ".mp4", "h265_mp4": ".mp4", "prores_mov": ".mov",
                 "ffv1_mkv": ".mkv", "vp9_webm": ".webm"}.get(profile, ".mp4"))
            result["export"] = engine.export(out, ep, profile,
                                             audio_path=audio_path)
        return result

    # -- bake (reversible) -------------------------------------------------- #

    def bake_op(self, engine: MoshEngine, op_id: str) -> BakeRecord:
        """Freeze a single mosh op into a baked clip; archive the originals."""
        op = self.op(op_id)
        target = self.clip(op.target_clip_id)
        media = self._parsed_media(target.media_id)
        frames = self._clip_frames(target)
        motion = self._motion_frames()
        moshed = engine.mosh(_as_avivideo(frames, media), op.mode, op.params,
                             motion_clips=_wrap_motion(motion))

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
        self._parsed[baked_id] = baked_clip_av

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
                                 motion_clips=_wrap_motion(motion))

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
        self._parsed[baked_id] = baked_av

        new_clip = Clip(id=_new_id("clip"), media_id=baked_id, track="main",
                        start=target.start, in_point=0,
                        out_point=len(baked_av.frames),
                        speed=target.speed, reverse=target.reverse,
                        fade_in=target.fade_in, fade_out=target.fade_out,
                        transition_in=target.transition_in)
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
        self._parsed.pop(rec.baked_media_id, None)
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
        for m in d.get("media", []):
            item = MediaItem.from_dict(m)
            p.media[item.id] = item
        p.clips = [Clip.from_dict(c) for c in d.get("clips", [])]
        p.mosh_ops = [MoshOp.from_dict(o) for o in d.get("mosh_ops", [])]
        p.bake_records = [BakeRecord.from_dict(r) for r in d.get("bake_records", [])]
        return p

    @classmethod
    def load(cls, path) -> "Project":
        return cls.from_dict(json.loads(Path(path).read_text()))


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
