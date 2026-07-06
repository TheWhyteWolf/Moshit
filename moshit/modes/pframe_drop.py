"""P-Frame Drop -- stutter and skip.

Randomly discards P-frames. Each dropped delta is motion that never gets applied,
so the image lurches and skips ahead — a stutter that gets more aggressive as the
probability rises. Keyframes are always kept, so the clip stays decodable.
"""
from __future__ import annotations

import random
from typing import List

from ..avi import Frame
from .base import MoshContext, MoshMode, Param


class PframeDrop(MoshMode):
    name = "pframe_drop"
    description = "Randomly drop P-frames so motion stutters and skips forward."
    params = [
        Param("probability", "float", 0.3, lo=0.0, hi=1.0, label="Drop chance",
              help="Probability each P-frame is dropped (0–1).",
              automatable=True),
        Param("seed", "int", 0, lo=0, hi=1_000_000, label="Seed",
              help="Random seed for which P-frames drop — fix it for a "
                   "repeatable stutter, change it to reshuffle the skips."),
    ]

    def apply(self, frames: List[Frame], ctx: MoshContext, *,
              probability: float = 0.3, seed: int = 0) -> List[Frame]:
        probability = max(0.0, min(1.0, float(probability)))
        rng = random.Random(seed)
        out: List[Frame] = []
        for i, f in enumerate(frames):
            if f.is_pframe:
                # automatable: the drop chance can ramp across the clip so the
                # stutter builds or eases (constant value = unchanged behaviour)
                p = max(0.0, min(1.0, float(ctx.auto("probability", i, probability))))
                if rng.random() < p:
                    continue
            out.append(f)
        return out
