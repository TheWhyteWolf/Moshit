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
              help="Probability each P-frame is dropped (0–1)."),
        Param("seed", "int", 0, lo=0, hi=1_000_000, label="Seed"),
    ]

    def apply(self, frames: List[Frame], ctx: MoshContext, *,
              probability: float = 0.3, seed: int = 0) -> List[Frame]:
        probability = max(0.0, min(1.0, float(probability)))
        rng = random.Random(seed)
        out: List[Frame] = []
        for f in frames:
            if f.is_pframe and rng.random() < probability:
                continue
            out.append(f)
        return out
