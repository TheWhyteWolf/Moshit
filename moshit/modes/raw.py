"""Raw-frame effects: numpy pixel processors run in the render's finish stage.

Where a :class:`~moshit.modes.pixel.PixelMode` emits an FFmpeg filter string,
a :class:`RawMode` transforms the *decoded* frames directly (RGB24 bytes ->
numpy -> bytes). That makes per-pixel algorithms ffmpeg can't express -- pixel
sorting first -- straightforward, at the cost of a decode/re-encode round trip
(the same one optical-flow transfer already pays).

numpy is the one dependency (the optional ``flow`` extra). When it is missing
the render skips raw effects with a note rather than failing, mirroring flow.
"""
from __future__ import annotations

from typing import Any, Dict, List

from .base import Param

_RAW_REGISTRY: Dict[str, type] = {}


def available() -> bool:
    """True if numpy is importable (raw effects can run)."""
    try:
        import numpy  # noqa: F401
        return True
    except Exception:
        return False


class RawMode:
    """Base class for raw-frame effects. Subclass and implement :meth:`apply`."""

    name: str = ""
    description: str = ""
    params: List[Param] = []

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        if cls.name:
            _RAW_REGISTRY[cls.name] = cls

    def defaults(self) -> Dict[str, Any]:
        return {p.name: p.default for p in self.params}

    def resolve(self, overrides) -> Dict[str, Any]:
        values = self.defaults()
        known = {p.name for p in self.params}
        for key, val in (overrides or {}).items():
            if key in known:
                values[key] = val
        return values

    def apply(self, frames: List[bytes], *, width: int, height: int,
              fps: float, **params) -> List[bytes]:
        """Return the processed RGB24 frames (one ``width*height*3`` byte string
        each). Must preserve the frame count and geometry."""
        raise NotImplementedError


def available_raw_modes() -> List[str]:
    return sorted(_RAW_REGISTRY)


def get_raw_mode(name: str) -> RawMode:
    if name not in _RAW_REGISTRY:
        raise KeyError(f"unknown raw effect '{name}'. "
                       f"Available: {available_raw_modes()}")
    return _RAW_REGISTRY[name]()


def is_raw_mode(name: str) -> bool:
    return name in _RAW_REGISTRY


# --------------------------------------------------------------------------- #
# Pixel sorting
# --------------------------------------------------------------------------- #

def _luma(arr):
    # arr: (H, W, 3) float32 in 0..1
    return 0.299 * arr[..., 0] + 0.587 * arr[..., 1] + 0.114 * arr[..., 2]


def _saturation(arr):
    mx = arr.max(2)
    mn = arr.min(2)
    import numpy as np
    return np.where(mx > 1e-6, (mx - mn) / np.maximum(mx, 1e-6), 0.0)


def _hue(arr):
    import numpy as np
    r, g, b = arr[..., 0], arr[..., 1], arr[..., 2]
    mx = arr.max(2)
    mn = arr.min(2)
    d = mx - mn
    h = np.zeros_like(mx)
    nz = d > 1e-6
    rm = nz & (mx == r)
    gm = nz & (mx == g) & ~rm
    bm = nz & (mx == b) & ~rm & ~gm
    h[rm] = (((g - b)[rm] / d[rm]) % 6.0)
    h[gm] = ((b - r)[gm] / d[gm]) + 2.0
    h[bm] = ((r - g)[bm] / d[bm]) + 4.0
    return h / 6.0                         # 0..1


_KEYS = {"brightness": _luma, "saturation": _saturation, "hue": _hue}


def _sort_frame(buf: bytes, width: int, height: int, *, vertical: bool,
                by: str, lo: float, hi: float, descending: bool) -> bytes:
    """Sort contiguous threshold-banded spans of each line (row, or column when
    *vertical*) by *by*, leaving out-of-band pixels anchored in place.

    Fully vectorised: a per-pixel run id (one per contiguous in-band span, never
    crossing a line boundary) plus a single ``lexsort`` of ``(key, run)`` orders
    every span at once, then a scatter writes the sorted pixels back."""
    import numpy as np

    img = np.frombuffer(buf, np.uint8).reshape(int(height), int(width), 3)
    work = img.transpose(1, 0, 2) if vertical else img      # sort along rows
    H, W = work.shape[:2]
    arr = work.astype(np.float32) / 255.0

    luma = _luma(arr)
    key = _KEYS.get(by, _luma)(arr)
    mask = (luma >= float(lo)) & (luma <= float(hi))         # (H, W) in-band

    flat = work.reshape(-1, 3)
    m = mask.reshape(-1)
    P = np.flatnonzero(m)                                    # in-band positions
    if P.size < 2:
        return work.transpose(1, 0, 2).tobytes() if vertical else work.tobytes()

    cols = P % W
    prev_in = m[np.clip(P - 1, 0, None)]                    # in-band at f-1?
    new_run = (cols == 0) | ~prev_in                        # span starts here
    run = np.cumsum(new_run)                                # 1..G along P

    k = key.reshape(-1)[P]
    if descending:
        k = -k
    order = np.lexsort((k, run))                            # by run, then key
    out = flat.copy()
    out[P] = flat[P[order]]                                 # scatter spans back
    out = out.reshape(H, W, 3)
    out = out.transpose(1, 0, 2) if vertical else out
    return np.ascontiguousarray(out, np.uint8).tobytes()


class PixelSort(RawMode):
    name = "pixel_sort"
    description = ("Sort pixels within brightness-banded spans of each row/column "
                   "(the classic datamosh-adjacent glitch).")
    params = [
        Param("axis", "choice", "horizontal",
              choices=("horizontal", "vertical"), label="Axis",
              help="Sort within rows (horizontal) or columns (vertical)."),
        Param("by", "choice", "brightness",
              choices=("brightness", "hue", "saturation"), label="Sort by"),
        Param("lo", "float", 0.25, lo=0.0, hi=1.0, label="Threshold lo",
              help="Only pixels whose brightness is in [lo, hi] get sorted."),
        Param("hi", "float", 0.80, lo=0.0, hi=1.0, label="Threshold hi"),
        Param("order", "choice", "ascending",
              choices=("ascending", "descending"), label="Order"),
    ]

    def apply(self, frames, *, width, height, fps, axis="horizontal",
              by="brightness", lo=0.25, hi=0.80, order="ascending"):
        lo, hi = float(lo), float(hi)
        if hi < lo:
            lo, hi = hi, lo
        vertical = (axis == "vertical")
        descending = (order == "descending")
        return [_sort_frame(f, width, height, vertical=vertical, by=by,
                            lo=lo, hi=hi, descending=descending)
                for f in frames]
