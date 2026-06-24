"""Motion Gain -- amplify or reduce coded motion with one knob.

A single continuous control over the picture's motion energy. Above ``1.0`` it
re-applies P-frame motion (the deltas accumulate harder -> exaggerated, blooming
movement); below ``1.0`` it thins P-frames out (less motion, tending toward a
freeze at ``0``). It is the smooth, datamosh-native cousin of
:mod:`pframe_duplicate` (amplify) and :mod:`pframe_drop` (reduce) -- fractional
gains are realised by an error-diffusion accumulator, so e.g. ``1.5`` doubles
every other P-frame and ``0.5`` keeps every other one.
"""
from __future__ import annotations

from typing import List

from ..avi import Frame
from .base import MoshContext, MoshMode, Param


class MotionGain(MoshMode):
    name = "motion_gain"
    description = "Amplify (>1) or reduce (<1) coded motion by re/de-applying P-frames."
    params = [
        Param("gain", "float", 1.0, lo=0.0, hi=8.0, label="Motion gain",
              automatable=True,
              help=">1 re-applies P-frame motion (exaggerate); <1 thins P-frames "
                   "(reduce); 0 freezes on keyframes."),
    ]

    def apply(self, frames: List[Frame], ctx: MoshContext, *,
              gain: float = 1.0) -> List[Frame]:
        if not frames:
            return []
        out: List[Frame] = []
        acc = 0.0
        if float(gain) >= 1.0:                 # amplify: re-apply P-frame motion
            for i, f in enumerate(frames):
                out.append(f)
                if f.is_pframe:
                    g = max(0.0, float(ctx.auto("gain", i, gain)))
                    acc += g - 1.0
                    while acc >= 1.0 - 1e-9:
                        out.append(f.copy())
                        acc -= 1.0
        else:                                  # reduce: thin P-frames out
            for i, f in enumerate(frames):
                if f.is_iframe:
                    out.append(f)
                    continue
                g = min(1.0, max(0.0, float(ctx.auto("gain", i, gain))))
                acc += g
                if acc >= 1.0 - 1e-9:
                    out.append(f)
                    acc -= 1.0
                # else: drop this P-frame (its motion is removed)
        return out
