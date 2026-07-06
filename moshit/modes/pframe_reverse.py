"""P-Frame Reverse -- motion played back inverted.

Reverses the order in which P-frames (motion deltas) are applied. Because each
delta was encoded against the frame before it, replaying them backwards produces
a surreal un-warping that pulls the image inside-out rather than a clean reverse.
"""
from __future__ import annotations

from typing import List

from ..avi import Frame
from ._gop import split_gops
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

        # per GOP: keep each keyframe as an anchor and reverse only its P-tail;
        # any lead-in before the first keyframe is reversed as its own run.
        lead, blocks = split_gops(frames)
        out: List[Frame] = list(reversed(lead))
        for block in blocks:
            out.append(block[0])                     # the keyframe anchor
            out.extend(reversed(block[1:]))
        return out
