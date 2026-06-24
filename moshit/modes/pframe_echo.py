"""P-Frame Echo -- motion trails / ghosting.

Re-inserts copies of chosen P-frames a little later in the stream, so their
motion is re-applied as a delayed echo layered over the ongoing motion. Lower
delays smear; higher delays leave rhythmic ghost trails.
"""
from __future__ import annotations

from typing import List

from ..avi import Frame
from .base import MoshContext, MoshMode, Param


class PFrameEcho(MoshMode):
    name = "pframe_echo"
    description = "Re-apply P-frames as delayed echoes for motion trails."
    params = [
        Param("stride", "int", 3, lo=1, hi=64, label="Stride",
              help="Echo every Nth P-frame."),
        Param("delay", "int", 2, lo=1, hi=240, label="Delay",
              help="How many frames later each echo lands."),
        Param("copies", "int", 1, lo=1, hi=64, label="Echoes",
              help="Number of trailing echoes per affected P-frame."),
    ]

    def apply(self, frames: List[Frame], ctx: MoshContext, *, stride: int = 3,
              delay: int = 2, copies: int = 1) -> List[Frame]:
        stride = max(1, int(stride))
        delay = max(1, int(delay))
        copies = max(1, int(copies))
        if not frames:
            return []

        out: List[Frame] = []
        scheduled: List[List] = []            # [frame, countdown]
        p_index = 0
        for f in frames:
            # drop in any echoes that have come due
            for item in [s for s in scheduled if s[1] <= 0]:
                out.append(item[0].copy())
                scheduled.remove(item)
            for item in scheduled:
                item[1] -= 1

            out.append(f)
            if f.is_pframe:
                if p_index % stride == 0:
                    for c in range(1, copies + 1):
                        scheduled.append([f, delay * c])
                p_index += 1

        for item in scheduled:                # flush any echoes left pending
            out.append(item[0].copy())
        return out
