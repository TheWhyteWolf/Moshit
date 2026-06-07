"""Moshit -- a standalone, FFmpeg-backed datamoshing engine.

The engine transcodes inputs to a moshable MPEG-4 Part 2 / AVI intermediate,
performs the mosh as byte surgery on discrete frame chunks, then transcodes out.
Effects are modular plugins; editing is non-destructive at the project level.

No third-party Python dependencies -- only a working ffmpeg/ffprobe on PATH.
"""
from __future__ import annotations

__version__ = "0.1.0"

from .avi import AviVideo, Frame, classify_vop, parse_avi, write_avi
from .engine import EngineConfig, MoshEngine
from .ffmpeg import Capabilities, FFmpeg, FFmpegError
from .modes import (
    MoshContext,
    MoshMode,
    Param,
    available_modes,
    get_mode,
    load_modes,
)
from .project import BakeRecord, Clip, MediaItem, MoshOp, Project

__all__ = [
    "__version__",
    "AviVideo", "Frame", "classify_vop", "parse_avi", "write_avi",
    "EngineConfig", "MoshEngine",
    "Capabilities", "FFmpeg", "FFmpegError",
    "MoshContext", "MoshMode", "Param",
    "available_modes", "get_mode", "load_modes",
    "Project", "MediaItem", "Clip", "MoshOp", "BakeRecord",
]
