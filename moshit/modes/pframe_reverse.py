"""P-Frame Reverse -- motion played back inverted.

Reverses the order in which P-frames (motion deltas) are applied. Because each
delta was encoded against the frame before it, replaying them backwards produces
a surreal un-warping that pulls the image inside-out rather than a clean reverse.
"""
from __future__ import annotations

from typing import List

from ..avi import Frame
from .base import MoshContext, MoshMode, Param


class PframeReverse(MoshMode):
    name = "pframe_reverse"
    description = "Reverse the order of P-frames so motion deltas replay inverted."
    params = [
        Param("per_gop", "bool", True, label="Per keyframe group",
              help="Reverse the P-frames within each keyframe group separately "
                   "(keeps anchors). Turn off to reverse across the whole region."),
    ]

    def apply(self, frames: List[Frame], ctx: MoshContext, *,
              per_gop: bool = True) -> List[Frame]:
        if not frames:
            return []

        if not per_gop:
            out = list(frames)
            positions = [i for i, f in enumerate(out) if f.is_pframe]
            reversed_p = [out[i] for i in positions][::-1]
            for pos, fr in zip(positions, reversed_p):
                out[pos] = fr
            return out

        # group into [I, P, P, ...] runs and reverse the P-tail of each
        groups: List[List[Frame]] = []
        current: List[Frame] = []
        for f in frames:
            if f.is_iframe and current:
                groups.append(current)
                current = [f]
            else:
                current.append(f)
        if current:
            groups.append(current)

        out: List[Frame] = []
        for g in groups:
            if g and g[0].is_iframe:
                out.append(g[0])
                out.extend(reversed(g[1:]))
            else:
                out.extend(reversed(g))
        return out
