"""Pixel-domain effects: clean looks built from FFmpeg video filters.

Unlike :class:`MoshMode` (which does byte surgery on coded frames), a
:class:`PixelMode` returns an FFmpeg *filter string* that is spliced into the
render's pixel finish pass. They are therefore clip finishing -- pixel-domain,
re-encoded, applied after the codec mosh and the speed/fade transforms -- and
carry a familiar ``Param`` schema so the GUI builds controls for them too.

Dependency-free: an effect just formats a filter string.
"""
from __future__ import annotations

from typing import Any, Dict, List

from .base import Param

_PIXEL_REGISTRY: Dict[str, type] = {}


class PixelMode:
    """Base class for pixel-domain effects. Subclass and implement :meth:`filter`."""

    name: str = ""
    description: str = ""
    params: List[Param] = []

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        if cls.name:
            _PIXEL_REGISTRY[cls.name] = cls

    def defaults(self) -> Dict[str, Any]:
        return {p.name: p.default for p in self.params}

    def resolve(self, overrides) -> Dict[str, Any]:
        values = self.defaults()
        known = {p.name for p in self.params}
        for key, val in (overrides or {}).items():
            if key in known:
                values[key] = val
        return values

    def filter(self, **params) -> str:
        """Return the FFmpeg filter(graph) string for these params."""
        raise NotImplementedError


def available_pixel_modes() -> List[str]:
    return sorted(_PIXEL_REGISTRY)


def get_pixel_mode(name: str) -> PixelMode:
    if name not in _PIXEL_REGISTRY:
        raise KeyError(f"unknown pixel effect '{name}'. "
                       f"Available: {available_pixel_modes()}")
    return _PIXEL_REGISTRY[name]()


def is_pixel_mode(name: str) -> bool:
    return name in _PIXEL_REGISTRY


# --------------------------------------------------------------------------- #
# Built-in pixel effects
# --------------------------------------------------------------------------- #

class RGBShift(PixelMode):
    name = "rgb_shift"
    description = "Offset the red and blue channels (chromatic-aberration fringing)."
    params = [Param("amount", "int", 4, lo=0, hi=40, label="Shift px")]

    def filter(self, *, amount: int = 4) -> str:
        a = max(0, int(amount))
        return f"rgbashift=rh={a}:bh={-a}:rv={a // 2}:bv={-(a // 2)}"


class HueRotate(PixelMode):
    name = "hue_rotate"
    description = "Rotate hue and push saturation."
    params = [Param("degrees", "int", 90, lo=-180, hi=180, label="Hue°"),
              Param("saturation", "float", 1.4, lo=0.0, hi=3.0, label="Saturation")]

    def filter(self, *, degrees: int = 90, saturation: float = 1.4) -> str:
        return f"hue=h={int(degrees)}:s={float(saturation):.2f}"


class Pixelate(PixelMode):
    name = "pixelate"
    description = "Mosaic blocks (downscale then nearest-neighbour upscale)."
    params = [Param("block", "int", 8, lo=2, hi=64, label="Block px")]

    def filter(self, *, block: int = 8) -> str:
        b = max(2, int(block))
        # the finish pass restores exact geometry afterwards, so rounding is fine
        return (f"scale=iw/{b}:ih/{b}:flags=neighbor,"
                f"scale=iw*{b}:ih*{b}:flags=neighbor")


class Noise(PixelMode):
    name = "noise"
    description = "Add animated grain / static."
    params = [Param("amount", "int", 20, lo=0, hi=100, label="Amount")]

    def filter(self, *, amount: int = 20) -> str:
        return f"noise=alls={max(0, int(amount))}:allf=t"


class Echo(PixelMode):
    name = "echo"
    description = "Temporal blend of recent frames (ghosting / smear)."
    params = [Param("frames", "int", 3, lo=2, hi=16, label="Frames")]

    def filter(self, *, frames: int = 3) -> str:
        return f"tmix=frames={max(2, int(frames))}"


class Trails(PixelMode):
    name = "trails"
    description = "Bright-pixel trails / light streaks."
    params = [Param("decay", "float", 0.95, lo=0.0, hi=1.0, label="Decay")]

    def filter(self, *, decay: float = 0.95) -> str:
        return f"lagfun=decay={max(0.0, min(1.0, float(decay))):.3f}"
