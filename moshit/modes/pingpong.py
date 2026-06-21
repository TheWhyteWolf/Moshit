"""Pingpong -- boomerang motion.

Replays each run of P-frames forward and then in reverse order, so the
accumulated motion lurches out and back. (P-frames always predict forward, so
the reversed run does not cleanly *undo* the motion -- it applies the earlier
deltas on top of the later state, which is the wobbling, tugging look.)
"""
from __future__ import annotations

from typing import List

from ..avi import Frame
from .base import MoshContext, MoshMode, Param


class Pingpong(MoshMode):
    name = "pingpong"
    description = "Replay P-frame runs forward then reversed (boomerang motion)."
    params = [
        Param("per_gop", "bool", True, label="Per GOP",
              help="Boomerang each keyframe's run separately (off = whole clip)."),
        Param("tail_only", "bool", False, label="Skip last frame",
              help="Drop the repeated turn-around frame for a snappier bounce."),
    ]

    def _bounce(self, run: List[Frame], tail_only: bool) -> List[Frame]:
        if len(run) < 2:
            return list(run)
        back = run[-2::-1] if tail_only else run[::-1]
        return list(run) + [f.copy() for f in back]

    def apply(self, frames: List[Frame], ctx: MoshContext, *, per_gop: bool = True,
              tail_only: bool = False) -> List[Frame]:
        if not frames:
            return []

        if not per_gop:
            out: List[Frame] = [f for f in frames if f.is_iframe]
            # keep the structural I-frames up front, then bounce the P-stream
            anchor = [frames[0]] if frames[0].is_iframe else []
            prun = [f for f in frames if f.is_pframe]
            return anchor + self._bounce(prun, tail_only)

        out = []
        run: List[Frame] = []
        for f in frames:
            if f.is_pframe:
                run.append(f)
                continue
            if run:
                out.extend(self._bounce(run, tail_only))
                run = []
            out.append(f)
        if run:
            out.extend(self._bounce(run, tail_only))
        return out
