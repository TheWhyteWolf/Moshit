"""GOP Scramble -- shuffle keyframe-anchored blocks for jump-cut motion.

Splits the stream into GOPs (each an I-frame plus the P-frames that follow it)
and shuffles the block order. Because every block still starts on its own
keyframe, each chunk re-syncs cleanly -- the result is a stutter of hard
temporal jumps rather than a melt. Most effective on clips with a small GOP
(several keyframes); a single-keyframe clip has nothing to reorder.
"""
from __future__ import annotations

import random
from typing import List

from ..avi import Frame
from ._gop import split_gops
from .base import MoshContext, MoshMode, Param


class GopScramble(MoshMode):
    name = "gop_scramble"
    description = "Shuffle whole GOP blocks for hard temporal jump-cuts."
    params = [
        Param("seed", "int", 0, lo=0, hi=1_000_000, label="Seed",
              help="Shuffle seed (same seed = same order)."),
        Param("keep_first", "bool", True, label="Anchor first GOP",
              help="Leave the opening GOP in place so the clip starts clean."),
    ]

    def apply(self, frames: List[Frame], ctx: MoshContext, *, seed: int = 0,
              keep_first: bool = True) -> List[Frame]:
        if not frames:
            return []

        # Split into blocks that each begin with an I-frame. Anything before the
        # first keyframe is a lead-in that stays put.
        lead, blocks = split_gops(frames)

        if len(blocks) < 2:
            return list(frames)                      # nothing meaningful to shuffle

        rng = random.Random(seed)
        head = blocks[:1] if keep_first else []
        tail = blocks[1:] if keep_first else blocks
        rng.shuffle(tail)
        ordered = head + tail

        out: List[Frame] = list(lead)
        for block in ordered:
            out.extend(f.copy() for f in block)
        return out
