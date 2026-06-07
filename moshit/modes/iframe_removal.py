"""I-Frame Removal -- the classic datamosh smear.

Drops keyframes inside a region so that the P-frames which follow apply their
motion to whatever frame was already on screen. Applied across a cut between two
clips (with ``keep_first`` off) it produces the signature transition where the
first clip melts into the motion of the second.
"""
from __future__ import annotations

from typing import List

from ..avi import Frame
from .base import MoshContext, MoshMode, Param


class IFrameRemoval(MoshMode):
    name = "iframe_removal"
    description = "Delete keyframes in the region so motion bleeds across cuts."
    params = [
        Param("keep_first", "bool", True, label="Keep first frame",
              help="Keep the region's opening frame so the clip stays decodable "
                   "on its own. Turn off for a smear that pulls from preceding "
                   "timeline content."),
        Param("keep_every", "int", 0, lo=0, hi=240, label="Keep every Nth keyframe",
              help="0 removes all (except the first if kept); 4 keeps every 4th "
                   "keyframe for a periodic re-bloom."),
    ]

    def apply(self, frames: List[Frame], ctx: MoshContext, *,
              keep_first: bool = True, keep_every: int = 0) -> List[Frame]:
        out: List[Frame] = []
        seen_iframes = 0
        for i, f in enumerate(frames):
            if f.is_iframe:
                keep = (keep_first and i == 0)
                if not keep and keep_every and seen_iframes % keep_every == 0:
                    keep = True
                seen_iframes += 1
                if not keep:
                    continue
            out.append(f)
        return out
