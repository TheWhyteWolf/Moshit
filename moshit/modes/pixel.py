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

    # Set on modes whose filter depends on the clip's geometry / fps / length
    # (e.g. motion injection animates across the exact clip duration). The
    # render passes that context to :meth:`filter_ctx`; plain modes ignore it.
    needs_ctx: bool = False

    def filter(self, **params) -> str:
        """Return the FFmpeg filter(graph) string for these params."""
        raise NotImplementedError

    def filter_ctx(self, params: Dict[str, Any], *, fps: float, nframes: int,
                   width: int, height: int) -> str:
        """Build the filter with render context. Defaults to the context-free
        :meth:`filter`; motion modes override this to animate over the clip."""
        return self.filter(**params)


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
    params = [Param("amount", "int", 4, lo=0, hi=40, label="Shift px",
                    help="How far (px) to pull red and blue apart; the vertical "
                         "split is half this. 0 is off.")]

    def filter(self, *, amount: int = 4) -> str:
        a = max(0, int(amount))
        return f"rgbashift=rh={a}:bh={-a}:rv={a // 2}:bv={-(a // 2)}"


class HueRotate(PixelMode):
    name = "hue_rotate"
    description = "Rotate hue and push saturation."
    params = [Param("degrees", "int", 90, lo=-180, hi=180, label="Hue°",
                    help="Degrees to rotate the colour wheel (±180 is a full "
                         "hue inversion; 0 leaves hue alone)."),
              Param("saturation", "float", 1.4, lo=0.0, hi=3.0, label="Saturation",
                    help="Saturation multiplier: 1.0 unchanged, 0 greyscale, "
                         ">1 pushes colours harder.")]

    def filter(self, *, degrees: int = 90, saturation: float = 1.4) -> str:
        return f"hue=h={int(degrees)}:s={float(saturation):.2f}"


class Pixelate(PixelMode):
    name = "pixelate"
    description = "Mosaic blocks (downscale then nearest-neighbour upscale)."
    params = [Param("block", "int", 8, lo=2, hi=64, label="Block px",
                    help="Mosaic cell size in pixels — bigger blocks are "
                         "coarser/chunkier.")]

    def filter(self, *, block: int = 8) -> str:
        b = max(2, int(block))
        # the finish pass restores exact geometry afterwards, so rounding is fine
        return (f"scale=iw/{b}:ih/{b}:flags=neighbor,"
                f"scale=iw*{b}:ih*{b}:flags=neighbor")


class Noise(PixelMode):
    name = "noise"
    description = "Add animated grain / static."
    params = [Param("amount", "int", 20, lo=0, hi=100, label="Amount",
                    help="Grain strength (0 = clean, 100 = heavy static); the "
                         "grain re-rolls every frame.")]

    def filter(self, *, amount: int = 20) -> str:
        return f"noise=alls={max(0, int(amount))}:allf=t"


class Echo(PixelMode):
    name = "echo"
    description = "Temporal blend of recent frames (ghosting / smear)."
    params = [Param("frames", "int", 3, lo=2, hi=16, label="Frames",
                    help="How many recent frames to average together — more "
                         "frames = longer ghost/smear trails.")]

    def filter(self, *, frames: int = 3) -> str:
        return f"tmix=frames={max(2, int(frames))}"


class Trails(PixelMode):
    name = "trails"
    description = "Bright-pixel trails / light streaks."
    params = [Param("decay", "float", 0.95, lo=0.0, hi=1.0, label="Decay",
                    help="How slowly bright pixels fade (0 = none, near 1 = long "
                         "persistent light streaks).")]

    def filter(self, *, decay: float = 0.95) -> str:
        return f"lagfun=decay={max(0.0, min(1.0, float(decay))):.3f}"


# --------------------------------------------------------------------------- #
# Motion injection -- synthetic camera moves animated across the clip.
# These need the clip's geometry/fps/length, so they implement filter_ctx and
# set needs_ctx; the render hands them the exact frame count to interpolate over.
# --------------------------------------------------------------------------- #

def _ramp(expr_from: float, expr_to: float, last: int) -> str:
    """An ffmpeg expression ramping ``expr_from``->``expr_to`` over ``last``+1
    frames using the per-frame index ``on``/``n`` (caller picks the var name via
    ``{n}`` substitution). ``last`` <= 0 yields a constant."""
    if last <= 0 or abs(expr_to - expr_from) < 1e-9:
        return f"{expr_from:.6f}"
    return f"({expr_from:.6f}+({expr_to - expr_from:.6f})*{{n}}/{last})"


class Zoom(PixelMode):
    name = "zoom"
    description = "Push in / pull out -- magnify, optionally animated across the clip."
    needs_ctx = True
    params = [Param("start", "float", 1.0, lo=1.0, hi=8.0, label="Start ×",
                    help="Magnification at the clip's first frame (1.0 = no "
                         "zoom)."),
              Param("end", "float", 1.5, lo=1.0, hi=8.0, label="End ×",
                    help="Magnification at the last frame; the zoom ramps evenly "
                         "from Start to End. Equal values = a static zoom.")]

    def filter(self, *, start: float = 1.0, end: float = 1.5) -> str:
        # context-free fallback: a static zoom at the start magnification
        return self.filter_ctx({"start": start, "end": end},
                               fps=25.0, nframes=1, width=0, height=0)

    def filter_ctx(self, params, *, fps, nframes, width, height) -> str:
        start = max(1.0, float(params.get("start", 1.0)))
        end = max(1.0, float(params.get("end", 1.5)))
        last = max(0, int(nframes) - 1)
        z = _ramp(start, end, last).replace("{n}", "on")
        size = f":s={int(width)}x{int(height)}" if width and height else ""
        # d=1 -> exactly one output frame per input frame (length preserved)
        return (f"zoompan=z='{z}':d=1{size}:fps={fps:g}"
                f":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'")


class Pan(PixelMode):
    name = "pan"
    description = "Drift the frame by a pixel offset across the clip (with headroom)."
    needs_ctx = True
    params = [Param("dx", "int", 0, lo=-400, hi=400, label="Δx px",
                    help="Total horizontal drift over the clip (px; + right, "
                         "- left)."),
              Param("dy", "int", 0, lo=-400, hi=400, label="Δy px",
                    help="Total vertical drift over the clip (px; + down, - up)."),
              Param("zoom", "float", 1.2, lo=1.0, hi=3.0, label="Headroom ×",
                    help="Pre-zoom so the drift doesn't expose blank edges — "
                         "raise it if a big pan reaches the frame border.")]

    def filter(self, *, dx: int = 0, dy: int = 0, zoom: float = 1.2) -> str:
        return self.filter_ctx({"dx": dx, "dy": dy, "zoom": zoom},
                               fps=25.0, nframes=1, width=0, height=0)

    def filter_ctx(self, params, *, fps, nframes, width, height) -> str:
        dx, dy = int(params.get("dx", 0)), int(params.get("dy", 0))
        zoom = max(1.0, float(params.get("zoom", 1.2)))
        last = max(0, int(nframes) - 1)
        xr = _ramp(0.0, float(dx), last).replace("{n}", "n")
        yr = _ramp(0.0, float(dy), last).replace("{n}", "n")
        return (f"scale=iw*{zoom:.4f}:ih*{zoom:.4f},"
                f"crop=in_w/{zoom:.4f}:in_h/{zoom:.4f}"
                f":x='(in_w-out_w)/2+{xr}':y='(in_h-out_h)/2+{yr}'")


class Rotate(PixelMode):
    name = "rotate"
    description = "Rotate / spin the frame (static angle plus optional spin over the clip)."
    needs_ctx = True
    params = [Param("angle", "float", 0.0, lo=-180.0, hi=180.0, label="Angle°",
                    help="Starting rotation in degrees (held for the whole clip "
                         "unless Spin is set)."),
              Param("spin", "float", 0.0, lo=-1440.0, hi=1440.0, label="Spin° total",
                    help="Extra degrees swept over the clip on top of Angle "
                         "(360 = one full turn; ± sets direction).")]

    def filter(self, *, angle: float = 0.0, spin: float = 0.0) -> str:
        return self.filter_ctx({"angle": angle, "spin": spin},
                               fps=25.0, nframes=1, width=0, height=0)

    def filter_ctx(self, params, *, fps, nframes, width, height) -> str:
        import math
        a0 = math.radians(float(params.get("angle", 0.0)))
        a1 = a0 + math.radians(float(params.get("spin", 0.0)))
        last = max(0, int(nframes) - 1)
        a = _ramp(a0, a1, last).replace("{n}", "n")
        return f"rotate=a='{a}':ow=iw:oh=ih:c=black@0"


class Shake(PixelMode):
    name = "shake"
    description = "Hand-held camera jitter (deterministic sinusoidal wobble)."
    needs_ctx = True
    params = [Param("amount", "int", 8, lo=0, hi=80, label="Amplitude px",
                    help="How far the frame wobbles (px). 0 is off; the frame "
                         "is pre-zoomed to hide the shaking edges."),
              Param("speed", "float", 1.0, lo=0.1, hi=6.0, label="Speed",
                    help="How fast the jitter oscillates — higher is a more "
                         "frantic shake.")]

    def filter(self, *, amount: int = 8, speed: float = 1.0) -> str:
        return self.filter_ctx({"amount": amount, "speed": speed},
                               fps=25.0, nframes=1, width=0, height=0)

    def filter_ctx(self, params, *, fps, nframes, width, height) -> str:
        amp = max(0, int(params.get("amount", 8)))
        w = max(0.1, float(params.get("speed", 1.0)))
        if amp <= 0:
            return "null"
        zoom = 1.0 + min(0.5, amp / 200.0)         # headroom hides the jitter edges
        xj = f"{amp}*(sin(n*{1.7 * w:.4f})+0.5*sin(n*{4.3 * w:.4f}))"
        yj = f"{amp}*(cos(n*{2.1 * w:.4f})+0.5*sin(n*{3.9 * w:.4f}))"
        return (f"scale=iw*{zoom:.4f}:ih*{zoom:.4f},"
                f"crop=in_w/{zoom:.4f}:in_h/{zoom:.4f}"
                f":x='(in_w-out_w)/2+{xj}':y='(in_h-out_h)/2+{yj}'")
