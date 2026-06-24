"""FFmpeg / FFprobe orchestration.

The engine never decodes or encodes video itself; it shells out to FFmpeg for
the three transcoding stages (normalise in, re-encode on bake, export out) and
keeps the byte-level mosh in pure Python. The encoder of the moshable
intermediate is always the *software* ``mpeg4`` encoder: hardware encoders
produce H.264/H.265/AV1, none of which yield the simple MPEG-4 Part 2 stream the
byte surgery depends on. Hardware acceleration is therefore offered only for the
final export.

This module has no third-party dependencies.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional


class FFmpegError(RuntimeError):
    """Raised when an ffmpeg/ffprobe invocation fails."""


# Clip blend_mode -> ffmpeg `blend` filter all_mode. "normal" is handled
# separately (plain alpha-over via overlay), so it isn't listed here.
BLEND_MODES = {
    "screen": "screen", "multiply": "multiply", "overlay": "overlay",
    "add": "addition", "subtract": "subtract", "difference": "difference",
    "darken": "darken", "lighten": "lighten", "hardlight": "hardlight",
    "softlight": "softlight",
}

# Matte sources for layer/effect masks.
MASK_SOURCES = ("luma", "alpha", "motion")


def mask_chain(spec: Dict) -> str:
    """FFmpeg filter string turning a video into a grayscale matte per *spec*.

    *spec* is ``{source, lo, hi, invert, feather}``: ``source`` is ``luma`` (the
    picture's brightness), ``alpha`` (its transparency -- opaque where the source
    carries no alpha) or ``motion`` (frame-to-frame difference). ``lo``/``hi``
    (0..1) are a soft threshold ramp -- below ``lo`` the matte is black, above
    ``hi`` white, linear between (so the band doubles as feathering). ``invert``
    flips it; ``feather`` blurs the edges by that many pixels (sigma)."""
    source = str(spec.get("source", "luma"))
    lo = max(0.0, min(1.0, float(spec.get("lo", 0.0))))
    hi = max(0.0, min(1.0, float(spec.get("hi", 1.0))))
    invert = bool(spec.get("invert", False))
    feather = max(0, int(spec.get("feather", 0)))
    if source == "alpha":
        chain = ["format=yuva420p", "alphaextract"]   # opaque -> white if no alpha
    elif source == "motion":
        chain = ["tblend=all_mode=difference", "format=gray"]
    else:                                             # luma
        chain = ["format=gray"]
    lo255 = lo * 255.0
    span = max(1.0, (hi - lo) * 255.0)
    chain.append(f"lutyuv=y='clip((val-{lo255:.3f})/{span:.3f}*255,0,255)'")
    if invert:
        chain.append("lutyuv=y='255-val'")
    if feather > 0:
        chain.append(f"gblur=sigma={feather}")
    return ",".join(chain)


def _atempo_chain(speed: float) -> List[str]:
    """`atempo` filters whose product is *speed* (each stage stays in 0.5..2.0)."""
    s = float(speed)
    if s <= 0 or s == 1.0:
        return []
    out: List[str] = []
    while s > 2.0:
        out.append("atempo=2.0")
        s /= 2.0
    while s < 0.5:
        out.append("atempo=0.5")
        s /= 0.5
    out.append(f"atempo={s:.6f}")
    return out


# --------------------------------------------------------------------------- #
# Capability detection
# --------------------------------------------------------------------------- #

# Export profiles -> the encoder they require. Probed against the live build so
# the UI can disable what is unavailable instead of failing at render time.
_PROFILE_ENCODER = {
    "h264_mp4": "libx264",
    "h265_mp4": "libx265",
    "prores_mov": "prores_ks",
    "ffv1_mkv": "ffv1",
    "vp9_webm": "libvpx-vp9",
}


@dataclass
class Capabilities:
    encoders: set = field(default_factory=set)
    hwaccels: set = field(default_factory=set)

    @property
    def can_make_intermediate(self) -> bool:
        return "mpeg4" in self.encoders

    def has_encoder(self, name: str) -> bool:
        return name in self.encoders

    def available_export_profiles(self) -> List[str]:
        return [p for p, enc in _PROFILE_ENCODER.items() if enc in self.encoders]

    def report(self) -> str:
        lines = ["FFmpeg capabilities"]
        lines.append("  intermediate (mpeg4): "
                     + ("yes" if self.can_make_intermediate else "MISSING"))
        lines.append("  export profiles:")
        for profile, enc in _PROFILE_ENCODER.items():
            ok = enc in self.encoders
            lines.append(f"    {'[x]' if ok else '[ ]'} {profile:<12} "
                         f"({enc})" + ("" if ok else "  - encoder not built"))
        accels = ", ".join(sorted(self.hwaccels)) or "none"
        lines.append(f"  hwaccels (compiled): {accels}")
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Wrapper
# --------------------------------------------------------------------------- #

class FFmpeg:
    """Thin wrapper over the ffmpeg/ffprobe binaries."""

    def __init__(self, ffmpeg: Optional[str] = None, ffprobe: Optional[str] = None):
        self.ffmpeg = ffmpeg or os.environ.get("FFMPEG_BIN", "ffmpeg")
        self.ffprobe = ffprobe or os.environ.get("FFPROBE_BIN", "ffprobe")
        if shutil.which(self.ffmpeg) is None and not Path(self.ffmpeg).exists():
            raise FFmpegError(
                f"ffmpeg not found at '{self.ffmpeg}'. Install it (Arch: "
                f"`pacman -S ffmpeg`) or set FFMPEG_BIN to its path."
            )
        self._caps: Optional[Capabilities] = None
        self._audio_cache: Dict[str, bool] = {}

    # -- process helpers ---------------------------------------------------- #

    def _run(self, args: List[str], desc: str) -> str:
        proc = subprocess.run([self.ffmpeg, "-hide_banner", *args],
                              capture_output=True, text=True)
        if proc.returncode != 0:
            tail = "\n".join(proc.stderr.strip().splitlines()[-12:])
            raise FFmpegError(f"{desc} failed (exit {proc.returncode}):\n{tail}")
        return proc.stderr

    # detailed, time-static texture so the encoder sees only the transform's
    # motion (a flat source would yield no motion vectors to transfer)
    _PATTERN = ("geq=lum='128+60*sin(X/6)+60*cos(Y/7)+50*sin((X+Y)/11)'"
                ":cb=128:cr=128")
    TRANSFORMS = ("zoom_in", "zoom_out", "pan_x", "pan_y", "rotate")

    def synthesize_transform(self, kind: str, dst, *, width: int, height: int,
                             fps: float, frames: int, speed: float = 1.0) -> None:
        """Render a clip whose *motion* is a geometric transform of a static
        texture, to be used as a motion source for splice/weave effects."""
        if kind not in self.TRANSFORMS:
            raise FFmpegError(f"unknown transform '{kind}'; "
                              f"choose from {', '.join(self.TRANSFORMS)}")
        W, H, F = int(width), int(height), float(fps)
        n = max(2, int(frames))
        sp = max(0.1, float(speed))
        dst = Path(dst)

        # texture is oversized for pan/rotate so the moving window stays filled
        if kind == "pan_x":
            pw, ph = W * 2, H
        elif kind == "pan_y":
            pw, ph = W, H * 2
        elif kind == "rotate":
            pw, ph = int(W * 1.5), int(H * 1.5)
        else:
            pw, ph = W, H

        tex = dst.with_suffix(".texture.png")
        self._run(["-f", "lavfi", "-i", f"color=c=black:s={pw}x{ph}",
                   "-vf", self._PATTERN, "-frames:v", "1", "-y", str(tex)],
                  "transform texture")
        try:
            zrate = 0.012 * sp
            last = n - 1
            if kind == "zoom_in":
                vf = (f"zoompan=z='min(1+{zrate}*on\\,2.6)':d=1:"
                      f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={W}x{H}:fps={F}")
            elif kind == "zoom_out":
                vf = (f"zoompan=z='max(2.6-{zrate}*on\\,1)':d=1:"
                      f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={W}x{H}:fps={F}")
            elif kind == "pan_x":
                vf = f"crop={W}:{H}:x='(in_w-{W})*n/{last}':y=0"
            elif kind == "pan_y":
                vf = f"crop={W}:{H}:x=0:y='(in_h-{H})*n/{last}'"
            else:  # rotate
                vf = f"rotate='{0.5 * sp}*t':c=black,crop={W}:{H}"
            self._run(["-loop", "1", "-framerate", str(F), "-i", str(tex),
                       "-vf", vf, "-frames:v", str(n),
                       "-c:v", "libx264", "-pix_fmt", "yuv420p", "-y", str(dst)],
                      f"transform {kind}")
        finally:
            try:
                tex.unlink()
            except OSError:
                pass

    # -- capabilities ------------------------------------------------------- #

    def capabilities(self, refresh: bool = False) -> Capabilities:
        if self._caps is not None and not refresh:
            return self._caps
        enc = subprocess.run([self.ffmpeg, "-hide_banner", "-encoders"],
                             capture_output=True, text=True).stdout
        encoders = set()
        for line in enc.splitlines():
            parts = line.split()
            # data rows look like: " V....D name  Description"
            if len(parts) >= 2 and len(parts[0]) == 6 and parts[0][0] in "VAS":
                encoders.add(parts[1])
        hw = subprocess.run([self.ffmpeg, "-hide_banner", "-hwaccels"],
                            capture_output=True, text=True).stdout
        hwaccels = {ln.strip() for ln in hw.splitlines()[1:] if ln.strip()}
        self._caps = Capabilities(encoders=encoders, hwaccels=hwaccels)
        return self._caps

    def probe_hwaccel(self, name: str) -> bool:
        """Confirm a hwaccel actually initialises at runtime (not just compiled)."""
        if name not in self.capabilities().hwaccels:
            return False
        args = ["-f", "lavfi", "-i", "color=c=black:s=64x64:d=0.1"]
        if name == "vaapi":
            dev = os.environ.get("VAAPI_DEVICE", "/dev/dri/renderD128")
            args = ["-vaapi_device", dev, *args,
                    "-vf", "format=nv12,hwupload", "-c:v", "h264_vaapi"]
        else:
            args += ["-c:v", "h264_" + name]
        args += ["-f", "null", "-"]
        try:
            subprocess.run([self.ffmpeg, "-hide_banner", "-loglevel", "error", *args],
                           capture_output=True, text=True, timeout=20, check=True)
            return True
        except Exception:
            return False

    # -- probing media ------------------------------------------------------ #

    def probe_video(self, path) -> Dict:
        """Return {width,height,fps,nb_frames,codec} using ffprobe (with fallback)."""
        if shutil.which(self.ffprobe) or Path(self.ffprobe).exists():
            proc = subprocess.run(
                [self.ffprobe, "-v", "error", "-select_streams", "v:0",
                 "-show_entries",
                 "stream=width,height,avg_frame_rate,nb_frames,codec_name",
                 "-of", "json", str(path)],
                capture_output=True, text=True)
            if proc.returncode == 0:
                streams = json.loads(proc.stdout).get("streams", [])
                if streams:
                    s = streams[0]
                    num, _, den = (s.get("avg_frame_rate") or "0/1").partition("/")
                    fps = (float(num) / float(den)) if den and float(den) else 0.0
                    return {
                        "width": int(s.get("width", 0)),
                        "height": int(s.get("height", 0)),
                        "fps": fps,
                        "nb_frames": int(s["nb_frames"]) if s.get("nb_frames",
                                                                  "N/A").isdigit() else None,
                        "codec": s.get("codec_name", ""),
                    }
        return {"width": 0, "height": 0, "fps": 0.0, "nb_frames": None, "codec": ""}

    def has_audio(self, path) -> bool:
        """True if *path* has at least one audio stream (probed once, cached)."""
        key = str(path)
        if key in self._audio_cache:
            return self._audio_cache[key]
        ok = False
        if shutil.which(self.ffprobe) or Path(self.ffprobe).exists():
            proc = subprocess.run(
                [self.ffprobe, "-v", "error", "-select_streams", "a:0",
                 "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(path)],
                capture_output=True, text=True)
            ok = proc.returncode == 0 and "audio" in proc.stdout
        self._audio_cache[key] = ok
        return ok

    # -- audio assembly (for clean-edit passthrough) ------------------------ #

    def build_audio_track(self, plan: List[Dict], dst, *, fps: float = 30.0
                          ) -> Optional[Path]:
        """Assemble one WAV that matches the rendered video's duration.

        *plan* is the render's audio plan: an ordered list of segments, one per
        main-track clip. Each carries ``{"source", "start", "duration",
        "silent"}`` and optional finishing fields ``{"speed", "reverse",
        "fade_in", "fade_out", "transition_in"}`` (frame counts). A non-silent
        segment whose source has audio contributes that audio -- retimed
        (``atempo``/``areverse``) and faded (``afade``) to mirror the video --
        while everything else contributes matching-length silence so the track
        stays locked to the video. A crossfade (``transition_in``) trims that
        many frames off the segment's head so the total length matches the
        overlapping video (the audio itself hard-cuts at the seam). Returns
        *dst*, or ``None`` if no real audio was placed (so callers can skip
        muxing a pure-silence track).
        """
        dst = Path(dst)
        work = dst.parent
        segs: List[Path] = []
        listfile = work / f"{dst.stem}_concat.txt"
        any_real = False
        try:
            for i, seg in enumerate(plan):
                full = max(0.0, float(seg.get("duration", 0.0)))
                trans = max(0, int(seg.get("transition_in", 0))) / fps
                dur = full - trans                 # net contribution after overlap
                if dur <= 0.0:
                    continue
                seg_path = work / f"{dst.stem}_seg{i:04d}.wav"
                source = seg.get("source")
                real = (not seg.get("silent")) and source and self.has_audio(source)
                if real:
                    speed = float(seg.get("speed", 1.0)) or 1.0
                    # start after the crossfaded-away head, in source time
                    start = max(0.0, float(seg.get("start", 0.0)) + trans * speed)
                    src_dur = dur * speed          # atempo brings this back to dur
                    af = ["aresample=48000"]
                    if seg.get("reverse"):
                        af.append("areverse")
                    af.extend(_atempo_chain(speed))
                    fi = int(seg.get("fade_in", 0)) / fps
                    fo = int(seg.get("fade_out", 0)) / fps
                    if fi > 0:
                        af.append(f"afade=t=in:st=0:d={fi:.6f}")
                    if fo > 0:
                        af.append(f"afade=t=out:st={max(0.0, dur - fo):.6f}:d={fo:.6f}")
                    gain = float(seg.get("gain", 1.0))
                    if abs(gain - 1.0) > 1e-6:
                        af.append(f"volume={gain:.6f}")
                    af.append("apad")              # pad short audio to exactly `dur`
                    self._run(
                        ["-y", "-ss", f"{start:.6f}", "-t", f"{src_dur:.6f}",
                         "-i", str(source), "-vn", "-af", ",".join(af),
                         "-ac", "2", "-ar", "48000", "-t", f"{dur:.6f}",
                         "-c:a", "pcm_s16le", str(seg_path)],
                        f"audio segment {i}")
                    any_real = True
                else:
                    self._run(
                        ["-y", "-f", "lavfi", "-i",
                         "anullsrc=channel_layout=stereo:sample_rate=48000",
                         "-t", f"{dur:.6f}", "-c:a", "pcm_s16le", str(seg_path)],
                        f"silent segment {i}")
                segs.append(seg_path)

            if not any_real or not segs:
                return None
            listfile.write_text("".join(f"file '{p}'\n" for p in segs))
            self._run(["-y", "-f", "concat", "-safe", "0", "-i", str(listfile),
                       "-c:a", "pcm_s16le", "-ar", "48000", "-ac", "2", str(dst)],
                      "audio concat")
            return dst
        finally:
            for p in segs:
                try:
                    p.unlink()
                except OSError:
                    pass
            try:
                listfile.unlink()
            except OSError:
                pass

    def mix_audio_tracks(self, plans: List[List[Dict]], dst, *,
                         fps: float = 30.0) -> Optional[Path]:
        """Build one WAV per track plan and sum them into *dst*.

        Each plan is assembled via :meth:`build_audio_track` (so each track is
        already laid out full-length with gap silence and per-clip finishing),
        then the audible tracks are combined with ``amix`` (``normalize=0`` so
        they sum rather than attenuate; per-clip ``gain`` controls levels).
        Returns *dst*, or ``None`` if no track carried real audio. A single
        audible track is returned as-is (no needless mix pass)."""
        dst = Path(dst)
        plans = [p for p in (plans or []) if p]
        if not plans:
            return None
        if len(plans) == 1:
            return self.build_audio_track(plans[0], dst, fps=fps)
        tracks: List[Path] = []
        try:
            for i, plan in enumerate(plans):
                tp = dst.with_name(f"{dst.stem}_track{i:02d}.wav")
                res = self.build_audio_track(plan, tp, fps=fps)
                if res is not None:
                    tracks.append(res)
            if not tracks:
                return None
            if len(tracks) == 1:
                os.replace(tracks[0], dst)
                tracks = []
                return dst
            inputs: List[str] = []
            for t in tracks:
                inputs += ["-i", str(t)]
            self._run(["-y", *inputs, "-filter_complex",
                       f"amix=inputs={len(tracks)}:normalize=0:duration=longest",
                       "-c:a", "pcm_s16le", "-ar", "48000", "-ac", "2", str(dst)],
                      "audio mix")
            return dst
        finally:
            for t in tracks:
                try:
                    t.unlink()
                except OSError:
                    pass

    def finish_video(self, segments: List, meta: List[Dict], dst, *,
                     fps: float, gop: int = 250, qscale: int = 3,
                     width: int = 0, height: int = 0) -> Path:
        """Assemble per-clip moshed segment AVIs into one finished video.

        Each clip gets a pixel-domain chain -- ``reverse``, ``setpts`` (speed),
        ``fps``, ``fade`` in/out, then any ``pixel`` filter strings -- and
        adjacent clips are folded together with ``xfade`` (crossfade) where
        ``transition_in`` is set, else ``concat`` (a hard cut). ``meta[i]`` is
        ``{n, speed, reverse, fade_in, fade_out, transition_in, pixel}`` (frame
        counts plus a list of filter strings), and an optional ``fx_mask`` matte
        spec that gates the pixel filters through ``maskedmerge`` (the FX show
        only where the matte is bright). ``settb`` pins a common timebase so
        ``xfade`` accepts the concat output; pixel filters are followed by a
        ``scale`` back to ``width x height`` so size-changing ones stay foldable.
        """
        tb = int(round(fps))
        parts: List[str] = []
        lens: List[int] = []                       # post-speed frame count per clip
        for i, m in enumerate(meta):
            n = int(m["n"])
            speed = float(m.get("speed", 1.0)) or 1.0
            mlen = max(1, round(n / speed)) if speed != 1.0 else n
            head: List[str] = []                   # common: retime + fades
            if m.get("reverse"):
                head.append("reverse")
            if speed != 1.0:
                head.append(f"setpts={1.0 / speed:.6f}*PTS")
            head.append(f"fps={fps:g}")
            fi, fo = int(m.get("fade_in", 0)), int(m.get("fade_out", 0))
            if fi > 0:
                head.append(f"fade=t=in:st=0:d={fi / fps:.6f}")
            if fo > 0:
                head.append(f"fade=t=out:st={max(0.0, (mlen - fo) / fps):.6f}:"
                            f"d={fo / fps:.6f}")
            pixel = m.get("pixel") or []
            fx: List[str] = list(pixel)
            if pixel and width and height:         # restore exact geometry for the fold
                fx.append(f"scale={int(width)}:{int(height)}:flags=bicubic")
            fx_mask = m.get("fx_mask")
            if pixel and fx_mask:                  # gate the FX through a matte
                # alpha the FX branch by the matte and overlay it on the original,
                # so only the matte's bright areas take the effect (overlay's
                # single-channel alpha avoids maskedmerge's per-plane chroma bug)
                mc = mask_chain(fx_mask)
                parts.append(f"[{i}:v]" + ",".join(head) + f"[hd{i}]")
                parts.append(f"[hd{i}]split=3[fo{i}][ff{i}][fm{i}]")
                parts.append(f"[ff{i}]" + ",".join(fx) + f",format=yuva420p[fxc{i}]")
                parts.append(f"[fm{i}]{mc}[fmk{i}]")
                parts.append(f"[fxc{i}][fmk{i}]alphamerge[fxm{i}]")
                parts.append(f"[fo{i}]format=yuv420p[forig{i}]")
                parts.append(f"[forig{i}][fxm{i}]overlay=eof_action=pass,"
                             f"format=yuv420p,settb=1/{tb}[v{i}]")
            else:
                chain = head + fx + ["format=yuv420p", f"settb=1/{tb}"]
                parts.append(f"[{i}:v]" + ",".join(chain) + f"[v{i}]")
            lens.append(mlen)

        acc, acc_len = "[v0]", lens[0]
        for i in range(1, len(meta)):
            trans = max(0, int(meta[i].get("transition_in", 0)))
            out = f"[x{i}]"
            if trans > 0:
                d = min(trans, acc_len, lens[i])
                offset = max(0.0, (acc_len - d) / fps)
                parts.append(f"{acc}[v{i}]xfade=transition=fade:duration={d / fps:.6f}"
                             f":offset={offset:.6f},settb=1/{tb}{out}")
                acc_len += lens[i] - d
            else:
                parts.append(f"{acc}[v{i}]concat=n=2:v=1:a=0,settb=1/{tb}{out}")
                acc_len += lens[i]
            acc = out

        inputs: List[str] = []
        for s in segments:
            inputs += ["-i", str(s)]
        self._run([*inputs, "-filter_complex", ";".join(parts), "-map", acc,
                   "-c:v", "mpeg4", "-q:v", str(qscale), "-bf", "0",
                   "-g", str(max(1, gop)), "-pix_fmt", "yuv420p", "-y", str(dst)],
                  "finish video")
        return Path(dst)

    def composite_video(self, layers: List[Dict], dst, *, total_frames: int,
                        width: int, height: int, fps: float,
                        gop: int = 250, qscale: int = 3) -> Path:
        """Composite positioned layers onto a black canvas, bottom-to-top.

        *layers* is ordered bottom→top; each is ``{"input", "start", "length",
        "opacity", "blend", "head_fade"}`` (frame counts) plus an optional
        ``"mask"`` matte spec (see :func:`mask_chain`) multiplied into the layer's
        alpha so it shows through only where the matte is bright. Each input becomes a
        full-length layer (transparent outside its [start, start+length] window),
        scaled to ``width x height`` with its alpha set by ``opacity`` and ramped
        in over its first ``head_fade`` frames (the crossfade with whatever is
        beneath it). ``normal`` composites with a plain alpha-over (``overlay``);
        any other ``blend`` maps through :data:`BLEND_MODES`, applied only inside
        the window via the layer's alpha (``blend`` → ``alphamerge`` →
        ``overlay``), so opacity and alpha stay correct. Output is a moshable
        MPEG-4 AVI like the finish pass.
        """
        tb = int(round(fps))
        dur = max(1, int(total_frames)) / fps
        w, h = int(width), int(height)
        base = (f"color=c=black:s={w}x{h}:r={fps:g}:d={dur:.6f},"
                f"format=yuv420p,settb=1/{tb}")
        parts: List[str] = [f"{base}[acc0]"]
        for i, lay in enumerate(layers):
            op = max(0.0, min(1.0, float(lay.get("opacity", 1.0))))
            start = max(0, int(lay.get("start", 0)))
            head = max(0, int(lay.get("head_fade", 0)))   # crossfade-in frames
            startT = start / fps
            chain = [f"fps={fps:g}", f"scale={w}:{h}:flags=bicubic",
                     "format=yuva420p"]
            if head > 0:                                   # dissolve in over the overlap
                chain.append(f"fade=t=in:st=0:d={head / fps:.6f}:alpha=1")
            if op < 1.0:
                chain.append(f"colorchannelmixer=aa={op:.4f}")
            chain.append(f"settb=1/{tb}")
            if startT > 0:
                chain.append(f"setpts=PTS+{startT:.6f}/TB")
            parts.append(f"[{i}:v]" + ",".join(chain) + f"[c{i}]")
            # optional layer matte: derive a grayscale mask from the clip itself
            # and multiply it into the clip's alpha (preserving opacity/head_fade)
            clip_lbl = f"[c{i}]"
            mask = lay.get("mask")
            if mask:
                mc = mask_chain(mask)
                parts.append(f"[c{i}]split=3[cb{i}][cm{i}][ca{i}]")
                parts.append(f"[cm{i}]{mc}[mk{i}]")
                parts.append(f"[ca{i}]alphaextract[caa{i}]")
                parts.append(f"[caa{i}][mk{i}]blend=all_mode=multiply[cmm{i}]")
                parts.append(f"[cb{i}][cmm{i}]alphamerge[cmsk{i}]")
                clip_lbl = f"[cmsk{i}]"
            # place the (delayed) clip on a full-length transparent canvas
            parts.append(f"color=c=black@0:s={w}x{h}:r={fps:g}:d={dur:.6f},"
                         f"format=yuva420p,settb=1/{tb}[t{i}]")
            parts.append(f"[t{i}]{clip_lbl}overlay=eof_action=pass:repeatlast=0[lay{i}]")
            mode = str(lay.get("blend", "normal"))
            if mode == "normal" or mode not in BLEND_MODES:
                parts.append(f"[acc{i}][lay{i}]overlay=eof_action=pass[acc{i + 1}]")
            else:
                fm = BLEND_MODES[mode]
                parts.append(f"[lay{i}]split[lz{i}][la{i}]")
                parts.append(f"[la{i}]alphaextract[am{i}]")
                parts.append(f"[lz{i}]format=yuv420p[lr{i}]")
                parts.append(f"[acc{i}][lr{i}]blend=all_mode={fm}[bl{i}]")
                parts.append(f"[bl{i}][am{i}]alphamerge[bm{i}]")
                parts.append(f"[acc{i}][bm{i}]overlay=eof_action=pass[acc{i + 1}]")
        final = f"[acc{len(layers)}]"
        inputs: List[str] = []
        for lay in layers:
            inputs += ["-i", str(lay["input"])]
        self._run([*inputs, "-filter_complex", ";".join(parts), "-map", final,
                   "-c:v", "mpeg4", "-q:v", str(qscale), "-bf", "0",
                   "-g", str(max(1, gop)), "-pix_fmt", "yuv420p", "-y", str(dst)],
                  "composite video")
        return Path(dst)

    def _audio_args(self, profile: str) -> List[str]:
        """Delivery audio codec for muxing a passthrough track into an export."""
        if profile == "vp9_webm":
            enc = "libopus" if self.capabilities().has_encoder("libopus") \
                else "libvorbis"
            return ["-c:a", enc, "-b:a", "160k"]
        if profile in ("prores_mov", "ffv1_mkv"):
            return ["-c:a", "pcm_s16le"]          # lossless containers
        return ["-c:a", "aac", "-b:a", "192k"]    # h264_mp4, h265_mp4

    # -- transcoding stages ------------------------------------------------- #

    def normalize(self, src, dst, *, width: int, height: int, fps: float,
                  gop: int, qscale: int = 3, single_keyframe: bool = False,
                  keep_aspect: bool = True) -> None:
        """Transcode *src* to a moshable MPEG-4 Part 2 AVI at fixed geometry/fps.

        All clips that will mosh into one another must share width, height, fps
        and codec settings, so this is where they are normalised. B-frames are
        disabled so every inter frame is a clean, self-contained P-frame.
        """
        if keep_aspect:
            vf = (f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
                  f"pad={width}:{height}:-1:-1:color=black,setsar=1,fps={fps}")
        else:
            vf = f"scale={width}:{height},setsar=1,fps={fps}"
        g = 10_000_000 if single_keyframe else max(1, gop)
        self._run(
            ["-y", "-i", str(src), "-an", "-sn", "-map", "0:v:0",
             "-vf", vf, "-c:v", "mpeg4", "-q:v", str(qscale), "-bf", "0",
             "-g", str(g), "-pix_fmt", "yuv420p", str(dst)],
            f"normalise {Path(src).name}")

    def reencode_intermediate(self, src_avi, dst_avi, *, gop: int = 250,
                              qscale: int = 3) -> None:
        """Bake: decode a moshed AVI (glitches become real pixels) and re-encode
        to a fresh, well-formed moshable clip with a leading keyframe."""
        self._run(
            ["-y", "-i", str(src_avi), "-an", "-sn",
             "-c:v", "mpeg4", "-q:v", str(qscale), "-bf", "0",
             "-g", str(max(1, gop)), "-pix_fmt", "yuv420p", str(dst_avi)],
            f"bake {Path(src_avi).name}")

    def decode_rgb_raw(self, src, width: int, height: int):
        """Yield each frame of *src* as raw RGB24 bytes at *width* x *height*.

        Frames are scaled to the requested geometry, so the byte length is always
        ``width*height*3``. Kept numpy-free -- callers (e.g. :mod:`moshit.flow`)
        wrap the bytes in arrays themselves.
        """
        w, h = int(width), int(height)
        frame_bytes = w * h * 3
        proc = subprocess.Popen(
            [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(src),
             "-vf", f"scale={w}:{h}", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        try:
            while True:
                buf = b""
                while len(buf) < frame_bytes:
                    chunk = proc.stdout.read(frame_bytes - len(buf))
                    if not chunk:
                        break
                    buf += chunk
                if len(buf) < frame_bytes:
                    break
                yield buf
        finally:
            if proc.stdout:
                proc.stdout.close()
            proc.wait()

    def encode_rgb_raw(self, frames, dst, *, width: int, height: int, fps: float,
                       qscale: int = 3, gop: int = 250) -> Path:
        """Encode an iterable of RGB24 byte frames to a moshable MPEG-4 AVI.

        stdout/stderr are redirected (not inherited) -- otherwise, when this runs
        on a Qt ``QThreadPool`` worker (e.g. the GUI's flow transfer), the
        inherited fds break the encoder's pipe mid-write.
        """
        import tempfile
        errlog = tempfile.TemporaryFile()
        proc = subprocess.Popen(
            [self.ffmpeg, "-hide_banner", "-loglevel", "error",
             "-f", "rawvideo", "-pix_fmt", "rgb24",
             "-s", f"{int(width)}x{int(height)}", "-r", str(fps), "-i", "-",
             "-c:v", "mpeg4", "-q:v", str(qscale), "-bf", "0",
             "-g", str(max(1, int(gop))), "-pix_fmt", "yuv420p", "-y", str(dst)],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=errlog)
        try:
            for f in frames:
                proc.stdin.write(f)
        except BrokenPipeError:
            pass                              # encoder exited early; surfaced below
        finally:
            if proc.stdin:
                try:
                    proc.stdin.close()
                except OSError:
                    pass
            proc.wait()
        if proc.returncode not in (0, None):
            errlog.seek(0)
            tail = "\n".join(errlog.read().decode("utf-8", "replace")
                             .strip().splitlines()[-8:])
            errlog.close()
            raise FFmpegError(f"rgb encode failed (exit {proc.returncode}):\n{tail}")
        errlog.close()
        return Path(dst)

    def snapshot(self, src, dst, frame_index: int) -> None:
        """Write a single decoded frame (*frame_index*, 0-based) as an image.

        The format follows *dst*'s extension (e.g. .png/.jpg). Decoding is linear
        from the start because a moshed stream has no reliable seek points.
        """
        self._run(
            ["-y", "-i", str(src),
             "-vf", f"select=eq(n\\,{max(0, int(frame_index))})",
             "-frames:v", "1", str(dst)],
            f"snapshot frame {frame_index}")

    def export(self, src_avi, dst, profile: str, *,
               hwaccel: Optional[str] = None, audio_path=None) -> None:
        """Transcode a moshed/baked AVI to a delivery format.

        Transcoding bakes the corruption in as real pixels regardless of the
        target codec. ``hwaccel`` (e.g. 'vaapi') is honoured for H.264/H.265
        only and silently ignored elsewhere. If *audio_path* is given (a WAV
        built to match the video's duration), it is muxed in and encoded with a
        container-appropriate codec.
        """
        if profile not in _PROFILE_ENCODER:
            raise FFmpegError(f"unknown export profile '{profile}'. "
                              f"Choose from {sorted(_PROFILE_ENCODER)}.")
        enc = _PROFILE_ENCODER[profile]
        if not self.capabilities().has_encoder(enc):
            raise FFmpegError(
                f"export profile '{profile}' needs encoder '{enc}', which this "
                f"ffmpeg build does not have.")

        pre: List[str] = ["-y"]
        args: List[str]
        if profile == "h264_mp4":
            if hwaccel == "vaapi":
                dev = os.environ.get("VAAPI_DEVICE", "/dev/dri/renderD128")
                pre = ["-y", "-vaapi_device", dev]
                args = ["-vf", "format=nv12,hwupload", "-c:v", "h264_vaapi",
                        "-movflags", "+faststart"]
            else:
                args = ["-c:v", "libx264", "-crf", "18", "-preset", "medium",
                        "-pix_fmt", "yuv420p", "-movflags", "+faststart"]
        elif profile == "h265_mp4":
            if hwaccel == "vaapi":
                dev = os.environ.get("VAAPI_DEVICE", "/dev/dri/renderD128")
                pre = ["-y", "-vaapi_device", dev]
                args = ["-vf", "format=nv12,hwupload", "-c:v", "hevc_vaapi",
                        "-tag:v", "hvc1", "-movflags", "+faststart"]
            else:
                args = ["-c:v", "libx265", "-crf", "20", "-preset", "medium",
                        "-tag:v", "hvc1", "-pix_fmt", "yuv420p",
                        "-movflags", "+faststart"]
        elif profile == "prores_mov":
            args = ["-c:v", "prores_ks", "-profile:v", "3",
                    "-pix_fmt", "yuv422p10le"]          # ProRes 422 HQ, ~visually lossless
        elif profile == "ffv1_mkv":
            args = ["-c:v", "ffv1", "-level", "3", "-g", "1"]  # mathematically lossless
        elif profile == "vp9_webm":
            args = ["-c:v", "libvpx-vp9", "-crf", "24", "-b:v", "0",
                    "-pix_fmt", "yuv420p"]
        else:  # pragma: no cover - guarded above
            raise FFmpegError(f"unhandled profile '{profile}'")

        inputs = ["-i", str(src_avi)]
        if audio_path:
            inputs += ["-i", str(audio_path)]
            tail = ["-map", "0:v:0", "-map", "1:a:0", *self._audio_args(profile)]
        else:
            tail = ["-an"]
        self._run([*pre, *inputs, *args, *tail, str(dst)], f"export {profile}")
