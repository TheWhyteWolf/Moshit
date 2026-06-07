"""Motion Splice -- codec-domain motion transfer (the primary datamosh effect).

Holds the base region's opening frame, then substitutes the motion source's
P-frames for the base's own. The decoder applies the source's motion vectors to
the base's pixels, so e.g. the motion of fire warps the underlying footage.

Because a P-frame carries motion *and* a residual, a ghost of the source's
appearance bleeds through; this is characteristic of the look and not a bug. For
appearance-free motion transfer, a future pixel-domain optical-flow mode is the
right tool.
"""
from __future__ import annotations

from typing import List

from ..avi import Frame
from .base import MoshContext, MoshMode, Param


class MotionSplice(MoshMode):
    name = "motion_splice"
    description = ("Apply a motion-source clip's P-frames onto the held base "
                   "frame (codec-domain motion transfer).")
    params = [
        Param("source", "clip_ref", None, label="Motion source",
              help="Label of the clip on the motion track to take motion from."),
        Param("hold_base_iframe", "bool", True, label="Hold base keyframe",
              help="Keep the base region's first frame as the held pixels."),
        Param("match_base_length", "bool", True, label="Match base length",
              help="Trim/loop the motion run to the base region's length."),
        Param("loop_motion", "bool", False, label="Loop motion",
              help="If the motion run is shorter, repeat it to fill the region."),
    ]

    def apply(self, frames: List[Frame], ctx: MoshContext, *, source=None,
              hold_base_iframe: bool = True, match_base_length: bool = True,
              loop_motion: bool = False) -> List[Frame]:
        if source is None:
            raise ValueError("motion_splice requires a 'source' motion clip")
        if not frames:
            return []

        motion = [f for f in ctx.get_clip(source) if f.is_pframe]
        if not motion:
            ctx.log(f"motion source '{source}' has no P-frames; passing base through")
            return list(frames)

        anchor = [frames[0]] if hold_base_iframe else []
        budget = max(0, len(frames) - len(anchor)) if match_base_length else len(motion)

        if match_base_length:
            if len(motion) >= budget:
                run = motion[:budget]
            elif loop_motion and motion:
                run = [motion[i % len(motion)] for i in range(budget)]
            else:
                run = motion
        else:
            run = motion

        return anchor + [f.copy() for f in run]
