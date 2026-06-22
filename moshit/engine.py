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

import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

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
        load_modes()               # ensure built-in + user modes are registered

    # -- workspace ---------------------------------------------------------- #

    def _tmp(self, suffix: str) -> Path:
        self._n += 1
        return self._work / f"_dm{self._n:04d}{suffix}"

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

    def optical_flow_transfer(self, base_src, motion_src, out_avi, *,
                              hold: bool = True, accumulate: bool = True,
                              strength: float = 1.0, preset: str = "fast") -> Path:
        """Warp *base_src*'s pixels by the optical flow of *motion_src* into a
        fresh moshable AVI (appearance-free motion transfer).

        Both inputs should already be at the project geometry (i.e. normalised
        intermediates). Needs the optional ``flow`` extra (OpenCV + numpy)."""
        from . import flow
        if not flow.available():
            raise FlowUnavailable(
                "Optical-flow transfer needs OpenCV + numpy. Install them with: "
                "pip install 'moshit[flow]'")
        w, h = self.config.width, self.config.height
        base = list(self.ff.decode_rgb_raw(base_src, w, h))
        motion = list(self.ff.decode_rgb_raw(motion_src, w, h))
        warped = flow.transfer_raw(base, motion, w, h, hold=hold,
                                   accumulate=accumulate, strength=strength,
                                   preset=preset)
        return self.ff.encode_rgb_raw(warped, out_avi, width=w, height=h,
                                      fps=self.config.fps,
                                      qscale=self.config.qscale, gop=self.config.gop)

    def finish_clips(self, segments, meta, dst):
        """Pixel-domain finish pass: apply per-clip speed/reverse/fade/pixel-FX
        and fold clips with crossfade/hard-cut. Returns the finished AVI path."""
        return self.ff.finish_video(segments, meta, dst, fps=self.config.fps,
                                    gop=self.config.gop, qscale=self.config.qscale,
                                    width=self.config.width, height=self.config.height)

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
