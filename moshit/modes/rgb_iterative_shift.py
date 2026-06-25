"""RGB Iterative Shift -- the classic channel-shifting glitch.

A faithful numpy recreation of the Processing "ChannelShiftGlitch" sketch
(corruptabsolutely.com). Each iteration picks a random *source* colour channel
and a random *target* channel, then copies the source channel -- displaced by a
random horizontal and/or vertical offset, wrapping around the edges -- into the
target channel of the frame. Layering several such copies tears the red, green
and blue planes apart into the familiar drifting "ghost" registrations.

Two knobs go beyond the still-image original to make it behave on video:

* ``seed`` makes the random shifts reproducible, so a clip renders identically
  every time (and so the same treatment lands on every frame).
* ``animate`` re-rolls the shifts per frame (seeded by ``seed`` + frame index),
  turning the static glitch into a shimmering, churning one.

numpy is the only dependency (the optional ``flow`` extra); without it the
frames pass through untouched, mirroring the other raw effects.
"""
from __future__ import annotations

from typing import List, Tuple

from .base import Param
from .raw import RawMode


class RGBIterativeShift(RawMode):
    name = "rgb_iterative_shift"
    description = ("Channel-shifting glitch: copy random RGB channels to random "
                  "channels at random wrapped offsets, layered over N iterations.")
    params = [
        Param("iterations", "int", 5, lo=1, hi=64, label="Iterations",
              help="How many random channel copies to layer up. Past ~3 you want "
                   "Recursive on, or you just get three fixed ghost images."),
        Param("shift_horizontal", "bool", True, label="Shift horizontally",
              help="Allow channels to be displaced left/right (with wrap-around)."),
        Param("shift_vertical", "bool", False, label="Shift vertically",
              help="Allow channels to be displaced up/down (with wrap-around)."),
        Param("recursive", "bool", False, label="Recursive",
              help="Feed each pass's result back in as the source for the next, "
                   "so shifts compound. Best for high iteration counts."),
        Param("seed", "int", 1, lo=0, hi=99999, label="Seed",
              help="Random seed for the shift pattern -- change it to roll a "
                   "different glitch; keep it fixed for a repeatable one."),
        Param("animate", "bool", False, label="Animate",
              help="Re-roll the shifts every frame (shimmering) instead of "
                   "applying one fixed pattern to the whole clip."),
    ]

    def _plan(self, rng, n: int, W: int, H: int,
              horiz: bool, vert: bool) -> List[Tuple[int, int, int, int]]:
        """One iteration plan: ``(src_channel, tgt_channel, v_shift, h_shift)``."""
        ops = []
        for _ in range(n):
            sc = int(rng.integers(3))
            tc = int(rng.integers(3))
            hs = int(rng.integers(W)) if horiz and W > 0 else 0
            vs = int(rng.integers(H)) if vert and H > 0 else 0
            ops.append((sc, tc, vs, hs))
        return ops

    def apply(self, frames: List[bytes], *, width: int, height: int, fps: float,
              iterations: int = 5, shift_horizontal: bool = True,
              shift_vertical: bool = False, recursive: bool = False,
              seed: int = 1, animate: bool = False) -> List[bytes]:
        try:
            import numpy as np
        except Exception:
            return frames
        H, W = int(height), int(width)
        n = max(1, int(iterations))
        horiz, vert = bool(shift_horizontal), bool(shift_vertical)

        # A non-animated effect rolls one shift plan and reuses it on every frame,
        # so the channel registration stays put across the clip.
        fixed = None
        if not animate:
            fixed = self._plan(np.random.default_rng(int(seed)), n, W, H, horiz, vert)

        out: List[bytes] = []
        for fi, b in enumerate(frames):
            src = np.frombuffer(b, np.uint8).reshape(H, W, 3)
            tgt = src.copy()
            ops = fixed if fixed is not None else self._plan(
                np.random.default_rng(int(seed) + fi), n, W, H, horiz, vert)
            for sc, tc, vs, hs in ops:
                # target(x,y).channel[tc] = source(x+hs, y+vs).channel[sc], wrapped
                tgt[..., tc] = np.roll(src[..., sc], (-vs, -hs), axis=(0, 1))
                if recursive:
                    src = tgt.copy()
            out.append(np.ascontiguousarray(tgt).tobytes())
        return out
