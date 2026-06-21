"""Momentum -- accelerate or decelerate the motion with a non-linear retime.

Resamples the P-frame stream along a power curve so the smear eases in (slow
start, snapping fast) or eases out (fast start, drifting to a halt), without
changing the overall length. The held opening frame anchors the pixels, like a
motion splice driven by the clip's own motion.
"""
from __future__ import annotations

from typing import List

from ..avi import Frame
from .base import MoshContext, MoshMode, Param


class Momentum(MoshMode):
    name = "momentum"
    description = "Ease the motion in or out by retiming the P-frames."
    params = [
        Param("mode", "choice", "decelerate",
              choices=["accelerate", "decelerate"], label="Curve",
              help="accelerate = slow start then rush; decelerate = rush then drift."),
        Param("strength", "float", 2.0, lo=1.0, hi=6.0, label="Strength",
              help="How pronounced the easing is (1 = linear)."),
        Param("hold_iframe", "bool", True, label="Hold base keyframe",
              help="Keep the opening frame as the held pixels."),
    ]

    def apply(self, frames: List[Frame], ctx: MoshContext, *,
              mode: str = "decelerate", strength: float = 2.0,
              hold_iframe: bool = True) -> List[Frame]:
        if not frames:
            return []
        pframes = [f for f in frames if f.is_pframe]
        if len(pframes) < 2:
            return list(frames)

        gamma = max(1.0, float(strength))
        if mode == "decelerate":
            gamma = 1.0 / gamma                      # fast start, slow finish

        n = len(pframes)
        out_run: List[Frame] = []
        for k in range(n):
            t = k / (n - 1)
            src = round((t ** gamma) * (n - 1))
            src = max(0, min(src, n - 1))
            out_run.append(pframes[src].copy())

        anchor = [frames[0]] if hold_iframe else []
        return anchor + out_run
