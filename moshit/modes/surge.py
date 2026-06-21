"""Surge -- lurching bursts of motion.

Randomly seizes on P-frames and repeats them in short clusters, so the motion
comes in uneven surges and stalls rather than at a steady rate. Unlike
``pframe_duplicate`` (uniform repeats), the bursts are clustered and seeded.
"""
from __future__ import annotations

import random
from typing import List

from ..avi import Frame
from .base import MoshContext, MoshMode, Param


class Surge(MoshMode):
    name = "surge"
    description = "Randomly repeat P-frames in clusters for lurching surges."
    params = [
        Param("seed", "int", 0, lo=0, hi=1_000_000, label="Seed",
              help="Randomness seed (same seed = same surges)."),
        Param("intensity", "float", 0.2, lo=0.0, hi=1.0, label="Intensity",
              help="Chance each P-frame kicks off a surge.", automatable=True),
        Param("burst", "int", 4, lo=2, hi=32, label="Max burst",
              help="Largest number of repeats in a surge."),
    ]

    def apply(self, frames: List[Frame], ctx: MoshContext, *, seed: int = 0,
              intensity: float = 0.2, burst: int = 4) -> List[Frame]:
        base_intensity = max(0.0, min(1.0, float(intensity)))
        burst = max(2, int(burst))
        if not frames:
            return []

        rng = random.Random(seed)
        out: List[Frame] = []
        for i, f in enumerate(frames):
            out.append(f)
            inten = max(0.0, min(1.0, float(ctx.auto("intensity", i, base_intensity))))
            if f.is_pframe and rng.random() < inten:
                extra = rng.randint(1, burst - 1)
                out.extend(f.copy() for _ in range(extra))
        return out
