"""Recursive RGB shift / swap -- compounding chromatic displacement.

A numpy raw effect that rolls the colour channels apart and permutes them, then
feeds the result back into itself ``iterations`` times. Each pass shifts red and
blue in opposite directions, swaps the channel order, and cross-fades over the
previous pass (``decay``), so the chromatic fringing accumulates into deep,
recursive colour trails. numpy is the only dependency (the ``flow`` extra);
without it the effect passes frames through untouched.
"""
from __future__ import annotations

from typing import List

from .base import Param
from .raw import RawMode

_PERMS = {"rgb": (0, 1, 2), "rbg": (0, 2, 1), "grb": (1, 0, 2),
          "gbr": (1, 2, 0), "brg": (2, 0, 1), "bgr": (2, 1, 0)}


class RGBRecurse(RawMode):
    name = "rgb_recurse"
    description = "Recursive RGB channel shift + swap (compounding colour trails)."
    params = [
        Param("iterations", "int", 4, lo=1, hi=64, label="Iterations",
              help="How many times the shift+swap feeds back into itself."),
        Param("shift_x", "int", 3, lo=-128, hi=128, label="Shift X",
              help="Horizontal pixels red/blue separate by, per iteration."),
        Param("shift_y", "int", 0, lo=-128, hi=128, label="Shift Y",
              help="Vertical pixels red/blue separate by, per iteration."),
        Param("swap", "choice", "rgb", choices=tuple(_PERMS), label="Channel swap",
              help="Channel permutation applied each iteration (rgb = none; "
                   "cyclic swaps like gbr spin the hues through the recursion)."),
        Param("decay", "float", 0.6, lo=0.0, hi=1.0, label="Feedback",
              help="Cross-fade of each pass over the last (1 = full replace, "
                   "0 = no effect)."),
    ]

    def apply(self, frames: List[bytes], *, width: int, height: int,
              fps: float, iterations: int = 4, shift_x: int = 3, shift_y: int = 0,
              swap: str = "rgb", decay: float = 0.6) -> List[bytes]:
        try:
            import numpy as np
        except Exception:
            return frames
        perm = list(_PERMS.get(swap, (0, 1, 2)))
        it = max(1, int(iterations))
        sx, sy = int(shift_x), int(shift_y)
        d = float(decay)
        out: List[bytes] = []
        for b in frames:
            cur = np.frombuffer(b, np.uint8).reshape(height, width, 3).astype(np.float32)
            for _ in range(it):
                r = np.roll(cur[..., 0], (sy, sx), axis=(0, 1))
                g = cur[..., 1]
                bb = np.roll(cur[..., 2], (-sy, -sx), axis=(0, 1))
                shifted = np.stack((r, g, bb), axis=-1)[..., perm]
                cur = cur * (1.0 - d) + shifted * d
            out.append(np.clip(cur, 0, 255).astype(np.uint8).tobytes())
        return out
