"""P-Frame Stutter -- repeat blocks of P-frames with direction control.

The directional, length-controlled cousin of :mod:`pframe_duplicate`: it chops
each run of P-frames into blocks of ``length`` and replays every block
``repeats`` times -- either *forward* (a hard stutter), *reverse* (each echo
replays the block's deltas backwards, un-warping the motion), or *pingpong* (out
and back). I-frames stay put as anchors so the clip still decodes.
"""
from __future__ import annotations

from typing import List, Tuple

from ..avi import Frame
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

        def flush(run: List[Tuple[Frame, int, int]]) -> List[Frame]:
            res: List[Frame] = []
            for i in range(0, len(run), length):
                chunk = run[i:i + length]
                block = [f for f, _p, _fi in chunk]
                p0, fi0 = chunk[0][1], chunk[0][2]
                if p0 < start:                 # before the start cursor: passthrough
                    res.extend(block)
                    continue
                rep = max(1, int(round(ctx.auto("repeats", fi0, repeats))))
                res.extend(self._emit(block, rep, direction))
            return res

        out: List[Frame] = []
        run: List[Tuple[Frame, int, int]] = []     # (frame, p_index, input_index)
        p_seen = 0
        for fi, f in enumerate(frames):
            if f.is_pframe:
                run.append((f, p_seen, fi))
                p_seen += 1
                continue
            if run:
                out.extend(flush(run))
                run = []
            out.append(f)
        if run:
            out.extend(flush(run))
        return out
