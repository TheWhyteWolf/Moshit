"""P-Frame Duplication -- the pulsing "bloom" / stretch effect.

Repeats P-frames so their motion vectors are re-applied, stretching and pushing
the image in the direction it was already moving.
"""
from __future__ import annotations

from typing import List

from ..avi import Frame
from .base import MoshContext, MoshMode, Param


class PFrameDuplicate(MoshMode):
    name = "pframe_duplicate"
    description = "Repeat P-frames to stretch motion into a bloom/pulse."
    params = [
        Param("factor", "int", 2, lo=1, hi=32, label="Repeat factor",
              help="Total copies of each affected P-frame (2 = one extra).",
              automatable=True),
        Param("stride", "int", 1, lo=1, hi=64, label="Stride",
              help="Affect every Nth P-frame (1 = all)."),
        Param("start", "int", 0, lo=0, hi=100000, label="Start P-frame",
              help="Index of the first P-frame eligible for duplication."),
    ]

    def apply(self, frames: List[Frame], ctx: MoshContext, *, factor: int = 2,
              stride: int = 1, start: int = 0) -> List[Frame]:
        base_factor = max(1, int(factor))
        stride = max(1, int(stride))
        out: List[Frame] = []
        p_index = 0
        for i, f in enumerate(frames):
            out.append(f)
            if f.is_pframe:
                if p_index >= start and (p_index - start) % stride == 0:
                    fac = max(1, int(round(ctx.auto("factor", i, base_factor))))
                    out.extend(f.copy() for _ in range(fac - 1))
                p_index += 1
        return out
