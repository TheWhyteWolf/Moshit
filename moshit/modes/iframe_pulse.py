"""I-Frame Pulse -- strobe the clean image back in on a beat.

Periodically re-inserts a copy of the most recent keyframe, snapping the picture
back to a clean state before the motion smears off it again. Tied to a musical
period it reads as a pulse or heartbeat punctuating the decay.
"""
from __future__ import annotations

from typing import List

from ..avi import Frame
from .base import MoshContext, MoshMode, Param


class IFramePulse(MoshMode):
    name = "iframe_pulse"
    description = "Re-inject the keyframe every N P-frames for a strobing pulse."
    params = [
        Param("period", "int", 8, lo=1, hi=240, label="Period",
              help="Insert a pulse every N P-frames.", automatable=True),
        Param("hold", "int", 1, lo=1, hi=16, label="Hold",
              help="Frames each pulse lasts (copies of the keyframe)."),
    ]

    def apply(self, frames: List[Frame], ctx: MoshContext, *, period: int = 8,
              hold: int = 1) -> List[Frame]:
        period = max(1, int(period))
        hold = max(1, int(hold))
        if not frames:
            return []

        last_key = frames[0] if frames[0].is_iframe else None
        out: List[Frame] = []
        # count P-frames since the last pulse rather than modulo a fixed period,
        # so an automated period (accelerating / slowing strobe) works cleanly
        since = 0
        for i, f in enumerate(frames):
            if f.is_iframe:
                last_key = f
            out.append(f)
            if f.is_pframe:
                since += 1
                period_i = max(1, int(round(ctx.auto("period", i, period))))
                if last_key is not None and since >= period_i:
                    out.extend(last_key.copy() for _ in range(hold))
                    since = 0
        return out
