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

    # -- process helpers ---------------------------------------------------- #

    def _run(self, args: List[str], desc: str) -> str:
        proc = subprocess.run([self.ffmpeg, "-hide_banner", *args],
                              capture_output=True, text=True)
        if proc.returncode != 0:
            tail = "\n".join(proc.stderr.strip().splitlines()[-12:])
            raise FFmpegError(f"{desc} failed (exit {proc.returncode}):\n{tail}")
        return proc.stderr

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

    def export(self, src_avi, dst, profile: str, *,
               hwaccel: Optional[str] = None) -> None:
        """Transcode a moshed/baked AVI to a delivery format.

        Transcoding bakes the corruption in as real pixels regardless of the
        target codec. ``hwaccel`` (e.g. 'vaapi') is honoured for H.264/H.265
        only and silently ignored elsewhere.
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

        self._run([*pre, "-i", str(src_avi), "-an", *args, str(dst)],
                  f"export {profile}")
