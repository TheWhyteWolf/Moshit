"""Shared pytest fixtures.

The dependency-light checks (and ``cli selftest``) run anywhere; the integration
tests are gated on ffmpeg/ffprobe and skipped when they are not on PATH. GUI
tests additionally need a usable Qt platform and skip otherwise.
"""
from __future__ import annotations

import os
import shutil
import subprocess

import pytest

# Any GUI test runs headless.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
requires_ffmpeg = pytest.mark.skipif(
    not HAVE_FFMPEG, reason="ffmpeg/ffprobe not on PATH")


@pytest.fixture
def engine(tmp_path):
    from moshit.engine import EngineConfig, MoshEngine
    from moshit.ffmpeg import FFmpeg
    from moshit.modes import load_modes
    load_modes()
    cfg = EngineConfig(width=160, height=120, fps=24.0, gop=8,
                       work_dir=str(tmp_path / "work"))
    eng = MoshEngine(cfg, FFmpeg())
    yield eng
    eng.cleanup()


@pytest.fixture
def project(engine, tmp_path):
    from moshit.project import Project
    return Project(name="t", config=engine.config,
                   assets_dir=str(tmp_path / "assets"))


@pytest.fixture
def make_clip(tmp_path):
    """Factory: ``make_clip("a.mp4", color="red", audio=440, dur=1.0)`` -> path."""
    from moshit.ffmpeg import FFmpeg

    def _make(name, *, color=None, audio=None, dur=1.0, rate=24, size="160x120"):
        ff = FFmpeg()
        path = tmp_path / name
        vsrc = (f"color=c={color}:s={size}:r={rate}:d={dur}" if color
                else f"testsrc=size={size}:rate={rate}:duration={dur}")
        args = ["-f", "lavfi", "-i", vsrc]
        if audio is not None:
            args += ["-f", "lavfi", "-i",
                     f"sine=frequency={audio}:duration={dur}", "-shortest"]
        args += ["-pix_fmt", "yuv420p", "-y", str(path)]
        ff._run(args, f"make {name}")
        return path

    return _make


class _Probe:
    @staticmethod
    def _ffprobe(path, stream, entry):
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", f"{stream}:0",
             "-show_entries", entry, "-of", "csv=p=0", str(path)],
            capture_output=True, text=True).stdout.strip()
        return out

    @classmethod
    def nframes(cls, path):
        out = cls._ffprobe(path, "v", "stream=nb_read_frames")
        if not out.isdigit():
            out = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "v:0",
                 "-count_frames", "-show_entries", "stream=nb_read_frames",
                 "-of", "csv=p=0", str(path)],
                capture_output=True, text=True).stdout.strip()
        return int(out) if out.isdigit() else None

    @classmethod
    def vdur(cls, path):
        out = cls._ffprobe(path, "v", "stream=duration")
        return float(out) if out else None

    @classmethod
    def adur(cls, path):
        out = cls._ffprobe(path, "a", "stream=duration")
        return float(out) if out else None

    @classmethod
    def has_audio(cls, path):
        return bool(cls._ffprobe(path, "a", "stream=codec_type"))

    @classmethod
    def dims(cls, path):
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x",
             str(path)], capture_output=True, text=True).stdout.strip()
        return out

    @staticmethod
    def pixel(path, idx, x, y, w=160, h=120):
        """RGB tuple at (x, y) of frame *idx*, decoded via ffmpeg (no PIL)."""
        data = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", str(path),
             "-vf", f"select=eq(n\\,{int(idx)})", "-frames:v", "1",
             "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
            capture_output=True).stdout
        off = (y * w + x) * 3
        return tuple(data[off:off + 3]) if len(data) >= off + 3 else None


@pytest.fixture
def probe():
    return _Probe
