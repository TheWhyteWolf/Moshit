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


# --------------------------------------------------------------------------- #
# Matte (numpy mirror of ffmpeg.mask_chain, so an fx_mask gates raw effects too)
# --------------------------------------------------------------------------- #

def _box_blur(m, radius: int):
    """Separable moving-average blur (edge-padded) -- a numpy stand-in for the
    matte's ``gblur`` feather, no scipy needed."""
    import numpy as np
    r = int(radius)
    if r <= 0:
        return m
    k = 2 * r + 1

    def blur1d(a):
        pad = np.pad(a, ((r, r), (0, 0)), mode="edge")
        cs = np.cumsum(pad, axis=0)
        cs = np.concatenate([np.zeros((1,) + a.shape[1:], cs.dtype), cs], axis=0)
        return (cs[k:] - cs[:-k]) / k

    m = blur1d(m)                              # rows
    m = blur1d(m.T).T                          # cols
    return m


_NAMED_COLORS = {
    "black": (0, 0, 0), "white": (255, 255, 255), "red": (255, 0, 0),
    "green": (0, 128, 0), "lime": (0, 255, 0), "blue": (0, 0, 255),
    "cyan": (0, 255, 255), "magenta": (255, 0, 255), "yellow": (255, 255, 0),
}


def _parse_color(value):
    """A color (``#rrggbb`` / ``0xrrggbb`` / name) -> (r, g, b) floats in 0..1."""
    import numpy as np
    v = str(value or "#00ff00").strip().lower()
    if v in _NAMED_COLORS:
        rgb = _NAMED_COLORS[v]
    else:
        h = v[1:] if v.startswith("#") else (v[2:] if v.startswith("0x") else v)
        try:
            rgb = (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
        except (ValueError, IndexError):
            rgb = (0, 255, 0)
    return np.array(rgb, np.float32) / 255.0


def mask_frames(frames, width: int, height: int, spec: Dict):
    """Per-frame grayscale mattes (float32, 0..1) for *frames* per *spec*.

    Mirrors :func:`moshit.ffmpeg.mask_chain`: ``source`` luma / alpha / motion /
    chroma (distance from the ``key`` color), a soft ``lo``/``hi`` ramp,
    ``invert`` and ``feather``. RGB frames carry no alpha, so an ``alpha`` matte
    is fully opaque (1.0) here too."""
    import numpy as np
    H, W = int(height), int(width)
    source = str(spec.get("source", "luma"))
    lo = max(0.0, min(1.0, float(spec.get("lo", 0.0))))
    hi = max(0.0, min(1.0, float(spec.get("hi", 1.0))))
    invert = bool(spec.get("invert", False))
    feather = max(0, int(spec.get("feather", 0)))
    span = max(1e-6, hi - lo)
    key = _parse_color(spec.get("key", "#00ff00")) if source == "chroma" else None
    arrs = [np.frombuffer(f, np.uint8).reshape(H, W, 3).astype(np.float32) / 255.0
            for f in frames]
    out = []
    for i, arr in enumerate(arrs):
        if source == "alpha":
            base = np.ones((H, W), np.float32)
        elif source == "motion":
            if len(arrs) < 2:
                base = np.zeros((H, W), np.float32)
            else:
                ref = arrs[i - 1] if i > 0 else arrs[1]
                base = np.abs(arr - ref).mean(2)
        elif source == "chroma":                  # distance from the key color
            diff = arr - key
            base = np.sqrt((diff * diff).sum(2) / 3.0)
        else:
            base = _luma(arr)
        m = np.clip((base - lo) / span, 0.0, 1.0)
        if invert:
            m = 1.0 - m
        if feather > 0:
            m = _box_blur(m, feather)
        out.append(m.astype(np.float32))
    return out


def gate_island(frames, width: int, height: int, spec: Dict):
    """Black out everything outside the matte (the *source*-mode FX input)."""
    import numpy as np
    H, W = int(height), int(width)
    masks = mask_frames(frames, width, height, spec)
    out = []
    for f, m in zip(frames, masks):
        a = np.frombuffer(f, np.uint8).reshape(H, W, 3).astype(np.float32)
        isl = a * m[..., None]
        out.append(np.ascontiguousarray(
            np.clip(isl, 0, 255).astype(np.uint8)).tobytes())
    return out


def blend_masked(original, processed, width: int, height: int, spec: Dict):
    """Blend *processed* over *original* per :func:`mask_frames` -- the *confine*
    raw-FX matte (effect shows where the matte is bright, original elsewhere)."""
    import numpy as np
    H, W = int(height), int(width)
    masks = mask_frames(original, width, height, spec)
    out = []
    for ob, pb, m in zip(original, processed, masks):
        o = np.frombuffer(ob, np.uint8).reshape(H, W, 3).astype(np.float32)
        p = np.frombuffer(pb, np.uint8).reshape(H, W, 3).astype(np.float32)
        m3 = m[..., None]
        blended = o * (1.0 - m3) + p * m3
        out.append(np.ascontiguousarray(
            np.clip(blended, 0, 255).astype(np.uint8)).tobytes())
    return out


def overlay_spill(original, processed, width: int, height: int, spec: Dict):
    """*source*-mode raw-FX matte: *processed* (the effect run on the matte-cut
    island) overlays *original* wherever the matte is bright or the effect spilled
    non-black content beyond it -- so glitches are free to overspill the matte."""
    import numpy as np
    H, W = int(height), int(width)
    masks = mask_frames(original, width, height, spec)
    out = []
    for ob, pb, m in zip(original, processed, masks):
        o = np.frombuffer(ob, np.uint8).reshape(H, W, 3).astype(np.float32)
        p = np.frombuffer(pb, np.uint8).reshape(H, W, 3).astype(np.float32)
        show = (m > 0.5) | (p.max(2) > 6)         # in-matte, or spilled content
        res = np.where(show[..., None], p, o)
        out.append(np.ascontiguousarray(
            np.clip(res, 0, 255).astype(np.uint8)).tobytes())
    return out


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
