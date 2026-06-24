"""RAW DATA - AUDIO: CDP sound-transforms wrapped as raw video effects.

Each entry below maps a CDP (Composer Desktop Project) program + mode to a
:class:`~moshit.modes.raw.RawMode` under the category ``"RAW DATA - AUDIO"``,
exposing *every* CDP parameter as a controllable :class:`Param`. The effect runs
the clip's pixels through CDP as audio (see :mod:`moshit.audio_bend`) -- vivid,
unpredictable databending with real sound-design tools.

The modes are only registered when CDP binaries are actually present (bundled
``CDP8/NewRelease`` or ``$MOSHIT_CDP_DIR``), so they never show up as dead
controls on a machine without CDP. Adding a program is just adding a descriptor
to ``_DESCRIPTORS`` -- no new classes, no GUI changes (controls are generated
from the param schema).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List

from .. import audio_bend
from .base import Param
from .raw import RawMode


@dataclass
class _Arg:
    """One CDP command-line argument, bound to a user-facing Param."""
    param: Param
    role: str = "positional"          # "positional" | "flag" | "vflag"
    flag: str = ""                    # e.g. "-s" (for flag / vflag)


def _fmt(param: Param, value) -> str:
    if param.kind == "int":
        return str(int(round(float(value))))
    if param.kind == "float":
        return f"{float(value):g}"
    return str(value)


class _CDPMode(RawMode):
    """Base for generated CDP effects; subclasses set program/cdp_mode/spec."""
    category = "RAW DATA - AUDIO"
    program: str = ""
    cdp_mode: str = ""
    spec: List[_Arg] = []

    def apply(self, frames: List[bytes], *, width: int, height: int,
              fps: float, **params) -> List[bytes]:
        positionals: List[str] = []
        flags: List[str] = []
        for a in self.spec:
            v = params.get(a.param.name, a.param.default)
            if a.role == "positional":
                positionals.append(_fmt(a.param, v))
            elif a.role == "flag":
                if bool(v):
                    flags.append(a.flag)
            elif a.role == "vflag":
                flags.append(f"{a.flag}{_fmt(a.param, v)}")
        return audio_bend.bend(frames, width, height, program=self.program,
                               mode=self.cdp_mode, positionals=positionals,
                               flags=flags)


@dataclass
class _Descriptor:
    name: str
    program: str
    cdp_mode: str
    description: str
    spec: List[_Arg] = field(default_factory=list)


def _register(d: _Descriptor) -> type:
    """Build and register a RawMode subclass for descriptor *d*."""
    return type(f"CDP_{d.name}", (_CDPMode,), {
        "name": d.name,
        "description": d.description,
        "program": d.program,
        "cdp_mode": d.cdp_mode,
        "spec": d.spec,
        "params": [a.param for a in d.spec],
    })


# --------------------------------------------------------------------------- #
# The CDP "distort" family -- waveset operations, ideal for pixel databending.
# --------------------------------------------------------------------------- #

_DESCRIPTORS: List[_Descriptor] = [
    _Descriptor(
        "cdp_distort_multiply", "distort", "multiply",
        "CDP distort/multiply: multiply each waveset's frequency (harsh, bright).",
        [_Arg(Param("n", "int", 2, lo=2, hi=16, label="Multiplier",
                    help="Times to multiply each waveset (2-16).")),
         _Arg(Param("smooth", "bool", False, label="Smoothing",
                    help="Smooth glitches between wavesets."), "flag", "-s")]),
    _Descriptor(
        "cdp_distort_divide", "distort", "divide",
        "CDP distort/divide: divide each waveset's frequency (octave-down grind).",
        [_Arg(Param("n", "int", 2, lo=2, hi=16, label="Divider",
                    help="Times to divide each waveset (2-16).")),
         _Arg(Param("interp", "bool", False, label="Interpolate",
                    help="Waveform interpolation: slower but cleaner."),
              "flag", "-i")]),
    _Descriptor(
        "cdp_distort_repeat", "distort", "repeat",
        "CDP distort/repeat: repeat groups of wavesets (stuttering buzz).",
        [_Arg(Param("multiplier", "int", 2, lo=1, hi=32, label="Repeats",
                    help="Times each waveset group repeats.")),
         _Arg(Param("cyclecnt", "int", 1, lo=1, hi=64, label="Cycles/group",
                    help="Wavesets per repeated group."), "vflag", "-c"),
         _Arg(Param("skipcycles", "int", 0, lo=0, hi=256, label="Skip cycles",
                    help="Wavesets to skip at the start."), "vflag", "-s")]),
    _Descriptor(
        "cdp_distort_interpolate", "distort", "interpolate",
        "CDP distort/interpolate: morph between successive wavesets (smear).",
        [_Arg(Param("multiplier", "int", 2, lo=1, hi=32, label="Multiplier",
                    help="Interpolation steps per waveset.")),
         _Arg(Param("skipcycles", "int", 0, lo=0, hi=256, label="Skip cycles",
                    help="Wavesets to skip at the start."), "vflag", "-s")]),
    _Descriptor(
        "cdp_distort_telescope", "distort", "telescope",
        "CDP distort/telescope: collapse runs of wavesets into one (time-crush).",
        [_Arg(Param("cyclecnt", "int", 4, lo=2, hi=128, label="Cycles",
                    help="Wavesets telescoped into one.")),
         _Arg(Param("skipcycles", "int", 0, lo=0, hi=256, label="Skip cycles",
                    help="Wavesets to skip at the start."), "vflag", "-s"),
         _Arg(Param("average", "bool", False, label="Average length",
                    help="Telescope to the average cycle length (else longest)."),
              "flag", "-a")]),
    _Descriptor(
        "cdp_distort_reverse", "distort", "reverse",
        "CDP distort/reverse: reverse groups of wavesets (granular backwards).",
        [_Arg(Param("cyclecnt", "int", 4, lo=1, hi=128, label="Cycles/group",
                    help="Wavesets per reversed group."))]),
    _Descriptor(
        "cdp_distort_omit", "distort", "omit",
        "CDP distort/omit: silence A of every B wavesets (rhythmic dropouts).",
        [_Arg(Param("a", "int", 1, lo=1, hi=64, label="Omit (A)",
                    help="Wavesets silenced out of every B (must be < B).")),
         _Arg(Param("b", "int", 4, lo=2, hi=128, label="Out of (B)",
                    help="Group size; A of every B are silenced."))]),
]


# Register only when CDP is actually installed (no dead controls otherwise).
if audio_bend.cdp_dir() is not None:
    _REGISTERED = [_register(d) for d in _DESCRIPTORS]
