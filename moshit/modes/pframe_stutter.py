"""P-Frame Stutter -- repeat blocks of P-frames with direction control.

The directional, length-controlled cousin of :mod:`pframe_duplicate`: it chops
each run of P-frames into blocks of ``length`` and replays every block
``repeats`` times -- either *forward* (a hard stutter), *reverse* (each echo
replays the block's deltas backwards, un-warping the motion), or *pingpong* (out
and back). I-frames stay put as anchors so the clip still decodes.
"""
from __future__ import annotations

from typing import List

from ..avi import Frame
from ._gop import map_pframe_runs
from .base import MoshContext, MoshMode, Param


class PframeStutter(MoshMode):
    name = "pframe_stutter"
    description = "Repeat blocks of P-frames forward / reversed / ping-pong."
    params = [
        Param("length", "int", 4, lo=1, hi=240, label="Block length",
              help="P-frames per repeated block (the stutter's grain)."),
        Param("repeats", "int", 2, lo=1, hi=32, label="Repeats", automatable=True,
              help="How many times each block plays (1 = unchanged, 2 = one echo)."),
        Param("direction", "choice", "forward",
              choices=("forward", "reverse", "pingpong"), label="Direction",
              help="forward: a hard stutter; reverse: each echo plays the block "
                   "backwards; pingpong: out and back."),
        Param("start", "int", 0, lo=0, hi=100000, label="Start P-frame",
              help="P-frames before this index pass through un-stuttered."),
    ]

    @staticmethod
    def _unit(block: List[Frame], reverse: bool) -> List[Frame]:
        return [f.copy() for f in (block[::-1] if reverse else block)]

    def _emit(self, block: List[Frame], repeats: int, direction: str) -> List[Frame]:
        out: List[Frame] = []
        for r in range(max(1, repeats)):
            if direction == "reverse":
                out.extend(self._unit(block, True))
            elif direction == "pingpong":
                out.extend(self._unit(block, r % 2 == 1))
            elif r == 0:                       # forward: reuse originals once
                out.extend(block)
            else:
                out.extend(self._unit(block, False))
        return out

    def apply(self, frames: List[Frame], ctx: MoshContext, *, length: int = 4,
              repeats: int = 2, direction: str = "forward",
              start: int = 0) -> List[Frame]:
        if not frames:
            return []
        length = max(1, int(length))
        start = max(0, int(start))

        def stutter_run(run: List[Frame], p_start: int, i_start: int) -> List[Frame]:
            # A run is contiguous, so the j-th frame's P-index is p_start + j and
            # its input index (for automation) is i_start + j.
            res: List[Frame] = []
            for j in range(0, len(run), length):
                block = run[j:j + length]
                if p_start + j < start:        # before the start cursor: passthrough
                    res.extend(block)
                    continue
                rep = max(1, int(round(ctx.auto("repeats", i_start + j, repeats))))
                res.extend(self._emit(block, rep, direction))
            return res

        return map_pframe_runs(frames, stutter_run)
