"""The mosh engine: glue between FFmpeg, the AVI codec and the mode system.

Pipeline, with the byte surgery sandwiched between two transcodes:

    source clip ──normalize──▶ MPEG-4/AVI ──parse──▶ frames
                                                       │
                                              mode.apply (pure Python)
                                                       ▼
                                            write moshed AVI ──┬─▶ bake (re-encode)
                                                               └─▶ export (delivery)

The engine is independent of any GUI and is fully exercisable from the CLI.
"""
from __future__ import annotations

import collections
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import avi
from .avi import AviVideo, Frame
from .ffmpeg import FFmpeg
from .modes import MoshContext, get_mode, load_modes, resolve_automation


class FlowUnavailable(RuntimeError):
    """Raised when optical-flow transfer is used without the optional deps."""


@dataclass
class EngineConfig:
    """Project-wide normalisation target. All clips are coerced to these."""

    width: int = 1280
    height: int = 720
    fps: float = 30.0
    gop: int = 250                 # base-clip keyframe interval
    qscale: int = 3                # mpeg4 quality (lower = better, 2..5 sane)
    keep_aspect: bool = True
    work_dir: Optional[str] = None


class MoshEngine:
    def __init__(self, config: Optional[EngineConfig] = None,
                 ffmpeg: Optional[FFmpeg] = None):
        self.config = config or EngineConfig()
        self.ff = ffmpeg or FFmpeg()
        # Own (and later clean up) the work dir only if we created it ourselves.
        self._owns_work = self.config.work_dir is None
        self._work = Path(self.config.work_dir or tempfile.mkdtemp(prefix="moshit_"))
        self._work.mkdir(parents=True, exist_ok=True)
        self._n = 0
        # Preview tuning (left at defaults for full-quality export/bake):
        #   preview_max_width -- cap the *pixel*-stage long edge (flow/raw/finish/
        #     composite) so previews run the per-pixel work at display size, not
        #     full res. The codec-domain mosh still runs on full-res frames, so the
        #     glitch structure is unchanged; only re-rendered pixels shrink.
        #   flow_preset_override -- force a DIS optical-flow preset (e.g. the
        #     cheapest one for live previews) regardless of the clip's setting.
        self.preview_max_width: Optional[int] = None
        self.flow_preset_override: Optional[str] = None
        # Per-clip segment cache: an edit to one clip can reuse the unchanged
        # others' expensive (flow / raw-FX) segments instead of re-rendering them.
        # Keyed by the caller (project) on the clip's codec+pixel state; LRU-bound.
        self._seg_cache: "collections.OrderedDict[str, Path]" = collections.OrderedDict()
        self._seg_cache_limit = 128
        # Decoded-RGB cache for flow motion sources: the same driver clip is
        # typically reused by several clips and every preview, and its decode is
        # a full ffmpeg run. Small LRU -- entries are whole decoded clips.
        self._motion_rgb_cache: "collections.OrderedDict[tuple, List[bytes]]" = \
            collections.OrderedDict()
        self._motion_rgb_bytes = 0
        self._motion_rgb_budget = 512 * 1024 * 1024   # ~512 MB of decoded drivers
        load_modes()               # ensure built-in + user modes are registered

    # -- workspace ---------------------------------------------------------- #

    def _tmp(self, suffix: str) -> Path:
        self._n += 1
        return self._work / f"_dm{self._n:04d}{suffix}"

    def _pixel_geom(self) -> Tuple[int, int]:
        """Geometry for the pixel-domain stages: full project geometry, unless
        ``preview_max_width`` caps the long edge (even dimensions for yuv420p)."""
        w, h = int(self.config.width), int(self.config.height)
        cap = self.preview_max_width
        if cap and w > int(cap):
            s = int(cap) / w
            w = max(2, (int(round(w * s)) // 2) * 2)
            h = max(2, (int(round(h * s)) // 2) * 2)
        return w, h

    def _motion_rgb(self, motion_src, w: int, h: int) -> List[bytes]:
        """Decoded RGB frames of a flow motion source, cached across renders.

        The same driver clip is typically reused by several clips and by every
        preview, and its decode is a full ffmpeg run, so caching it is a real
        win. Keyed on (path, mtime_ns, size, geometry) so re-imported footage or
        a re-rendered precomp invalidates; frames are immutable bytes, safe to
        share. The cache is bounded by total *bytes* (not entry count): a single
        long full-res driver decodes to gigabytes, so a count-only bound would
        pin multiple GB for the engine's lifetime."""
        p = Path(motion_src)
        try:
            st = p.stat()
            key = (str(p), st.st_mtime_ns, st.st_size, int(w), int(h))
        except OSError:
            key = None
        if key is not None:
            hit = self._motion_rgb_cache.get(key)
            if hit is not None:
                self._motion_rgb_cache.move_to_end(key)
                return hit
        frames = list(self.ff.decode_rgb_raw(motion_src, w, h))
        if key is None:
            return frames
        self._motion_rgb_cache[key] = frames
        self._motion_rgb_bytes += sum(len(f) for f in frames)
        # Evict LRU while over budget, but always keep the just-inserted entry
        # (an oversized single driver stays until the next decode displaces it).
        while (self._motion_rgb_bytes > self._motion_rgb_budget
               and len(self._motion_rgb_cache) > 1):
            _k, old = self._motion_rgb_cache.popitem(last=False)
            self._motion_rgb_bytes -= sum(len(f) for f in old)
        return frames

    # -- per-clip segment cache --------------------------------------------- #

    def seg_cache_get(self, key: str) -> Optional[Path]:
        """The cached segment AVI for *key*, or None. Refreshes its LRU position."""
        p = self._seg_cache.get(key)
        if p is not None and Path(p).exists():
            self._seg_cache.move_to_end(key)
            return p
        if p is not None:                          # recorded but the file vanished
            self._seg_cache.pop(key, None)
        return None

    def seg_cache_put(self, key: str, seg) -> Path:
        """Move segment AVI *seg* into the cache under *key* and return its new
        path. Does not evict -- a single render may need more than the limit and
        must keep them all live until its fold; call :meth:`seg_cache_trim`
        between renders to bound the cache."""
        cache_dir = self._work / "segcache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        dst = cache_dir / f"{key}.avi"
        src = Path(seg)
        if src.resolve() != dst.resolve():
            try:
                src.replace(dst)                   # atomic move within the work dir
            except OSError:
                shutil.copyfile(src, dst)
        self._seg_cache[key] = dst
        self._seg_cache.move_to_end(key)
        return dst

    def seg_cache_trim(self) -> None:
        """Evict least-recently-used segments down to the limit. Safe only when no
        render is mid-fold, so the project calls it once a render completes."""
        while len(self._seg_cache) > self._seg_cache_limit:
            _old, old_path = self._seg_cache.popitem(last=False)
            try:
                Path(old_path).unlink()
            except OSError:
                pass

    def cleanup(self) -> None:
        """Remove the scratch work dir if the engine created it.

        A no-op when an explicit ``work_dir`` was supplied -- the caller owns
        those files (e.g. a project's cached intermediates).
        """
        if self._owns_work and self._work.exists():
            shutil.rmtree(self._work, ignore_errors=True)

    def __enter__(self) -> "MoshEngine":
        return self

    def __exit__(self, *exc) -> None:
        self.cleanup()

    # -- stage 1: normalise ------------------------------------------------- #

    def normalize_clip(self, src, label: Optional[str] = None, *,
                       single_keyframe: bool = False) -> AviVideo:
        """Transcode *src* to the moshable intermediate and parse it.

        Use ``single_keyframe=True`` for motion sources so the clip is one
        keyframe followed by an unbroken run of P-frames.
        """
        label = label or Path(src).stem
        dst = self._tmp(".avi")
        self.ff.normalize(
            src, dst, width=self.config.width, height=self.config.height,
            fps=self.config.fps, gop=self.config.gop, qscale=self.config.qscale,
            single_keyframe=single_keyframe, keep_aspect=self.config.keep_aspect)
        clip = avi.parse_avi(dst)
        clip.source = str(dst)          # path to the moshable intermediate
        for f in clip.frames:
            f.source = label            # human provenance label, used by modes
        return clip

    def source_has_alpha(self, src) -> bool:
        """True if *src* carries an alpha channel (for source-file alpha mattes)."""
        return self.ff.has_alpha(src)

    def extract_alpha_map(self, src, dst) -> Path:
        """Render *src*'s alpha as a grayscale moshable AVI at project geometry,
        frame-aligned with the normalised clip so it can matte it."""
        return self.ff.extract_alpha(
            src, dst, width=self.config.width, height=self.config.height,
            fps=self.config.fps, gop=self.config.gop, qscale=self.config.qscale,
            keep_aspect=self.config.keep_aspect)

    def render_transform(self, kind: str, dst, *, frames: int = 120,
                         speed: float = 1.0):
        """Render a geometric-transform motion source (zoom/pan/rotate) at the
        project's geometry. Returns the written file path."""
        self.ff.synthesize_transform(
            kind, dst, width=self.config.width, height=self.config.height,
            fps=self.config.fps, frames=frames, speed=speed)
        return dst

    # -- stage 2: mosh (pure Python) ---------------------------------------- #

    def mosh(self, base: AviVideo, mode_name: str,
             params: Optional[Dict] = None, *,
             motion_clips: Optional[Dict[str, AviVideo]] = None,
             region: Optional[range] = None) -> List[Frame]:
        """Apply *mode_name* to *base* (optionally only a frame ``region``)."""
        mode = get_mode(mode_name)
        values = mode.resolve(params)
        clips = {lbl: clip.frames for lbl, clip in (motion_clips or {}).items()}
        body = list(base.frames if region is None
                    else base.frames[region.start:region.stop])
        automation = resolve_automation(values)    # mutates values -> start scalars
        ctx = MoshContext(fps=base.fps, width=base.width, height=base.height,
                          clips=clips, log=lambda m: None,
                          automation=automation, n_frames=len(body))
        if region is None:
            return mode.apply(body, ctx, **values)
        head = list(base.frames[:region.start])
        tail = list(base.frames[region.stop:])
        return head + mode.apply(body, ctx, **values) + tail

    # -- stage 3: write / bake / export ------------------------------------- #

    def write_moshed(self, frames: List[Frame], template: AviVideo,
                     out_avi) -> Path:
        avi.write_avi(out_avi, frames, template)
        return Path(out_avi)

    def bake(self, moshed_avi, out_avi=None) -> AviVideo:
        """Re-encode a moshed AVI into a fresh, well-formed moshable clip.

        The decode realises the glitches as pixels; the encode gives the result
        a leading keyframe so it can itself be moshed again (one generation of
        MPEG-4 recompression -- the accepted cost of chaining).
        """
        out_avi = Path(out_avi) if out_avi else self._tmp(".avi")
        self.ff.reencode_intermediate(moshed_avi, out_avi,
                                      gop=self.config.gop, qscale=self.config.qscale)
        return avi.parse_avi(out_avi)

    def export(self, src_avi, out_path, profile: str,
               hwaccel: Optional[str] = None, audio_path=None) -> Path:
        self.ff.export(src_avi, out_path, profile, hwaccel=hwaccel,
                       audio_path=audio_path)
        return Path(out_path)

    def build_audio(self, plan, dst, *, fps: Optional[float] = None):
        """Assemble a passthrough audio track from a render's audio plan.

        Returns the written path, or None if there was no real audio to place.
        """
        return self.ff.build_audio_track(plan, dst, fps=fps or self.config.fps)

    def mix_audio(self, plans, dst, *, fps: Optional[float] = None):
        """Assemble and sum one audio track per plan into a single WAV.

        Returns the written path, or None if no plan carried real audio.
        """
        return self.ff.mix_audio_tracks(plans, dst, fps=fps or self.config.fps)

    def optical_flow_transfer(self, base_src, motion_src, out_avi, *,
                              hold: bool = True, accumulate: bool = True,
                              strength: float = 1.0, preset: str = "fast",
                              out_len=None, region=None) -> Path:
        """Warp *base_src*'s pixels by the optical flow of *motion_src* into a
        fresh moshable AVI (appearance-free motion transfer).

        Both inputs should already be at the project geometry (i.e. normalised
        intermediates). ``out_len`` (default = motion length) and ``region`` make
        it usable as a length-preserving, region-scoped clip effect. Needs the
        optional ``flow`` extra (OpenCV + numpy)."""
        from . import flow
        if not flow.available():
            raise FlowUnavailable(
                "Optical-flow transfer needs OpenCV + numpy. Install them with: "
                "pip install 'moshit[flow]'")
        w, h = self._pixel_geom()
        base = list(self.ff.decode_rgb_raw(base_src, w, h))
        motion = self._motion_rgb(motion_src, w, h)
        warped = flow.transfer_raw(base, motion, w, h, hold=hold,
                                   accumulate=accumulate, strength=strength,
                                   preset=self.flow_preset_override or preset,
                                   out_len=out_len, region=region)
        return self.ff.encode_rgb_raw(warped, out_avi, width=w, height=h,
                                      fps=self.config.fps,
                                      qscale=self.config.qscale, gop=self.config.gop)

    def apply_raw_effects(self, src_avi, specs, out_avi, *, mask=None) -> Path:
        """Run numpy raw-frame effects over *src_avi* into a fresh moshable AVI.

        *specs* is an ordered list of ``{"name", "params"}``; each decoded RGB
        frame stack is passed through the named :class:`RawMode` in turn, then
        re-encoded. Geometry/length are preserved. With a *mask* spec, the result
        is blended back over the originals through that matte (the FX show only
        where the matte is bright). Needs numpy (the ``flow`` extra); returns
        *src_avi* unchanged if it (or every spec) is unavailable.
        """
        from .modes import raw as _raw
        if not _raw.available():
            return Path(src_avi)
        usable = [s for s in (specs or []) if _raw.is_raw_mode(s.get("name"))]
        if not usable:
            return Path(src_avi)
        w, h = self._pixel_geom()
        frames = self._raw_frames(list(self.ff.decode_rgb_raw(src_avi, w, h)),
                                  usable, w, h, mask=mask)
        return self.ff.encode_rgb_raw(frames, out_avi, width=w, height=h,
                                      fps=self.config.fps,
                                      qscale=self.config.qscale, gop=self.config.gop)

    def _raw_frames(self, frames, specs, w: int, h: int, *, mask=None):
        """Run raw-FX *specs* over in-memory RGB *frames* at ``w x h`` (the shared
        core of :meth:`apply_raw_effects` and :meth:`apply_clip_pixels`)."""
        from .modes import raw as _raw, get_raw_mode
        usable = [s for s in (specs or []) if _raw.is_raw_mode(s.get("name"))]
        if not usable:
            return frames
        original = list(frames)
        matte_mode = str((mask or {}).get("mode", "confine"))
        # The matte is computed once and shared: "source" mode needs it both to
        # cut the FX input island and to overlay the spill afterwards.
        mattes = _raw.mask_frames(original, w, h, mask) if mask else None
        # "source" mode feeds the effects the matte-cut island so their output is
        # free to spill; "confine" runs them on the full frame and limits output.
        out = (_raw.gate_island(original, w, h, mask, masks=mattes)
               if mask and matte_mode == "source" else list(original))
        for spec in usable:
            mode = get_raw_mode(spec["name"])
            params = mode.resolve(spec.get("params") or {})
            out = mode.apply(out, width=w, height=h, fps=self.config.fps, **params)
        if mask:
            out = (_raw.overlay_spill(original, out, w, h, mask, masks=mattes)
                   if matte_mode == "source"
                   else _raw.blend_masked(original, out, w, h, mask, masks=mattes))
        return out

    def apply_clip_pixels(self, seg, out_avi, *, flow_args=None, motion_src=None,
                          raw_specs=None, mask=None) -> Path:
        """Fused per-clip pixel stage: decode *seg* once, optionally warp it by the
        optical flow of *motion_src*, optionally run raw FX, then encode once.

        Folding the flow and raw stages into one decode/encode saves a full
        MPEG-4 round-trip versus running them back-to-back. Returns ``Path(seg)``
        unchanged when there is nothing to do (or numpy/OpenCV is unavailable), so
        callers can cheaply detect the no-op."""
        from .modes import raw as _raw
        from . import flow
        do_flow = bool(flow_args and motion_src) and flow.available()
        usable = [s for s in (raw_specs or []) if _raw.is_raw_mode(s.get("name"))]
        do_raw = bool(usable) and _raw.available()
        if not do_flow and not do_raw:
            return Path(seg)
        w, h = self._pixel_geom()
        frames = list(self.ff.decode_rgb_raw(seg, w, h))
        if do_flow:
            motion = self._motion_rgb(motion_src, w, h)
            frames = flow.transfer_raw(
                frames, motion, w, h,
                hold=flow_args.get("hold", True),
                accumulate=flow_args.get("accumulate", True),
                strength=float(flow_args.get("strength", 1.0)),
                preset=self.flow_preset_override or flow_args.get("preset", "fast"),
                out_len=flow_args.get("out_len"), region=flow_args.get("region"))
        if do_raw:
            frames = self._raw_frames(frames, usable, w, h, mask=mask)
        return self.ff.encode_rgb_raw(frames, out_avi, width=w, height=h,
                                      fps=self.config.fps,
                                      qscale=self.config.qscale, gop=self.config.gop)

    def finish_clips(self, segments, meta, dst):
        """Pixel-domain finish pass: apply per-clip speed/reverse/fade/pixel-FX
        and fold clips with crossfade/hard-cut. Returns the finished AVI path."""
        w, h = self._pixel_geom()
        return self.ff.finish_video(segments, meta, dst, fps=self.config.fps,
                                    gop=self.config.gop, qscale=self.config.qscale,
                                    width=w, height=h)

    def composite(self, layers, dst, *, total_frames):
        """Composite positioned video layers (opacity + blend + alpha) into one
        moshable AVI. Returns the written path."""
        w, h = self._pixel_geom()
        return self.ff.composite_video(
            layers, dst, total_frames=total_frames, fps=self.config.fps,
            width=w, height=h, gop=self.config.gop, qscale=self.config.qscale)

    # -- convenience: end-to-end two-clip mosh ------------------------------ #

    def mosh_two_clips(self, base_src, motion_src, out_avi, *,
                       mode_name: str = "motion_splice",
                       params: Optional[Dict] = None,
                       motion_label: str = "motion",
                       export_profile: Optional[str] = None,
                       export_path=None) -> Dict:
        """Normalise a base + motion clip, mosh, write, and optionally export.

        This is the headless 'eyeball it' path. Returns a dict of artefacts and
        stats.
        """
        base = self.normalize_clip(base_src, label="base")
        motion = self.normalize_clip(motion_src, label=motion_label,
                                     single_keyframe=True)
        merged = dict(params or {})
        merged.setdefault("source", motion_label)
        frames = self.mosh(base, mode_name, merged, motion_clips={motion_label: motion})
        out = self.write_moshed(frames, base, out_avi)

        result = {
            "base": base, "motion": motion, "moshed_avi": out,
            "moshed_frames": frames,
            "base_summary": base.summary(),
            "motion_summary": motion.summary(),
            "moshed_iframes": sum(1 for f in frames if f.is_iframe),
            "moshed_pframes": sum(1 for f in frames if f.is_pframe),
            "moshed_total": len(frames),
        }
        if export_profile:
            ep = Path(export_path) if export_path else out.with_suffix(
                _ext_for_profile(export_profile))
            result["export"] = self.export(out, ep, export_profile)
        return result


def _ext_for_profile(profile: str) -> str:
    return {
        "h264_mp4": ".mp4", "h265_mp4": ".mp4", "prores_mov": ".mov",
        "ffv1_mkv": ".mkv", "vp9_webm": ".webm",
    }.get(profile, ".mp4")
