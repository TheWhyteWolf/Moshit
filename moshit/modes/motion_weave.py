"""Motion Weave -- braid two motions together.

Like motion_splice this pulls from a motion-source clip, but instead of fully
replacing the base's motion it *interleaves* the two: a few of the base clip's
P-frames, then a few of the source's, repeating. The result braids both motions
into one stream — the footage advances under its own motion while the source's
motion keeps shoving it sideways.
"""
from __future__ import annotations

from typing import List

from ..avi import Frame
from .base import MoshContext, MoshMode, Param


class MotionWeave(MoshMode):
    name = "motion_weave"
    description = ("Interleave the base clip's P-frames with a motion source's "
                   "P-frames to braid two motions.")
    params = [
        Param("source", "clip_ref", None, label="Motion source",
              help="Clip on the motion track to braid in."),
        Param("base_run", "int", 1, lo=0, hi=32, label="Base frames per cycle",
              help="How many base P-frames before switching to the source "
                   "(0 = the source's motion replaces the base's entirely)."),
        Param("motion_run", "int", 1, lo=0, hi=32, label="Source frames per cycle",
              help="How many source P-frames before switching back."),
        Param("hold_base_iframe", "bool", True, label="Hold base keyframe"),
    ]

    def apply(self, frames: List[Frame], ctx: MoshContext, *, source=None,
              base_run: int = 1, motion_run: int = 1,
              hold_base_iframe: bool = True) -> List[Frame]:
        if source is None:
            raise ValueError("motion_weave requires a 'source' motion clip")
        if not frames:
            return []

        motion = [f for f in ctx.get_clip(source) if f.is_pframe]
        base_p = [f for f in frames if f.is_pframe]
        if not motion:
            ctx.log(f"motion source '{source}' has no P-frames; passing base through")
            return list(frames)

        base_run = max(0, int(base_run))
        motion_run = max(0, int(motion_run))
        if base_run == 0 and motion_run == 0:
            base_run = motion_run = 1

        out: List[Frame] = [frames[0]] if hold_base_iframe else []
        bi = mi = 0
        # The base clip drives overall length; the source loops as needed.
        while bi < len(base_p):
            for _ in range(base_run):
                if bi < len(base_p):
                    out.append(base_p[bi])
                    bi += 1
            for _ in range(motion_run):
                out.append(motion[mi % len(motion)].copy())
                mi += 1
            if base_run == 0:
                # base_run=0 emits no base frames, so the source's frames must
                # stand in for base time or the loop never terminates.
                bi += motion_run
        return out
