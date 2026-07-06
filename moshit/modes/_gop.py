"""Shared GOP / P-frame-run splitting for the codec-domain mosh modes.

Several modes carve a frame stream up the same two ways; keeping the split in
one place (with its own tests) means a mode reads as *what it does to the
groups*, not the bookkeeping of finding them.

* :func:`split_gops` -- ``(lead, blocks)`` where each block is one keyframe and
  the P-frames that follow it, and *lead* is any frames before the first
  keyframe. Used by whole-GOP reorderings (gop_scramble, pframe_reverse).
* :func:`map_pframe_runs` -- apply a function to each maximal run of consecutive
  P-frames, passing every non-P frame (I-frames, others) through untouched. The
  callback also gets the run's starting P-index and input index, so a mode can
  drive per-frame automation off them (pingpong, pframe_stutter).
"""
from __future__ import annotations

from typing import Callable, List, Tuple

from ..avi import Frame


def split_gops(frames: List[Frame]) -> Tuple[List[Frame], List[List[Frame]]]:
    """Split *frames* into a lead-in and keyframe-anchored blocks.

    ``lead`` is the frames before the first I-frame (empty when the stream opens
    on a keyframe); ``blocks`` is a list of ``[I, P, P, ...]`` runs, one per
    keyframe. Frames are referenced, not copied -- callers copy on emit.
    """
    lead: List[Frame] = []
    blocks: List[List[Frame]] = []
    for f in frames:
        if f.is_iframe:
            blocks.append([f])
        elif blocks:
            blocks[-1].append(f)
        else:
            lead.append(f)
    return lead, blocks


def map_pframe_runs(
    frames: List[Frame],
    fn: Callable[[List[Frame], int, int], List[Frame]],
) -> List[Frame]:
    """Rebuild *frames*, replacing each maximal run of consecutive P-frames with
    ``fn(run, p_start, i_start)`` and passing non-P frames through unchanged.

    ``p_start`` is the number of P-frames before the run; ``i_start`` is the
    run's first index in *frames*. Within a run both are contiguous (a run has
    no gaps by definition), so a callback can recover any member's indices as
    ``p_start + offset`` / ``i_start + offset``.
    """
    out: List[Frame] = []
    run: List[Frame] = []
    p_seen = 0
    run_p_start = 0
    run_i_start = 0
    for i, f in enumerate(frames):
        if f.is_pframe:
            if not run:
                run_p_start, run_i_start = p_seen, i
            run.append(f)
            p_seen += 1
            continue
        if run:
            out.extend(fn(run, run_p_start, run_i_start))
            run = []
        out.append(f)
    if run:
        out.extend(fn(run, run_p_start, run_i_start))
    return out
