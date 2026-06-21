"""P-Frame Shuffle -- chaotic, stuttering motion.

Reorders the region's P-frames so each motion delta is applied to a frame it was
never meant for. Keyframes can be held in place (the default) so the result
stays anchored and decodable; the motion itself becomes a jittering scramble.
"""
from __future__ import annotations

import random
from typing import List

from ..avi import Frame
from .base import MoshContext, MoshMode, Param


class PframeShuffle(MoshMode):
    name = "pframe_shuffle"
    description = "Shuffle the order of P-frames for chaotic, jittering motion."
    params = [
        Param("seed", "int", 0, lo=0, hi=1_000_000, label="Seed",
              help="Same seed reproduces the same shuffle."),
        Param("keep_iframes", "bool", True, label="Keep keyframes in place",
              help="Hold I-frames at their positions and shuffle only the "
                   "P-frames between them. Turn off to scramble everything after "
                   "the first frame."),
    ]

    def apply(self, frames: List[Frame], ctx: MoshContext, *, seed: int = 0,
              keep_iframes: bool = True) -> List[Frame]:
        if len(frames) < 3:
            return list(frames)
        rng = random.Random(seed)
        out = list(frames)
        if keep_iframes:
            positions = [i for i, f in enumerate(out) if f.is_pframe]
            pframes = [out[i] for i in positions]
            rng.shuffle(pframes)
            for pos, fr in zip(positions, pframes):
                out[pos] = fr
            return out
        anchor, rest = out[:1], out[1:]
        rng.shuffle(rest)
        return anchor + rest
