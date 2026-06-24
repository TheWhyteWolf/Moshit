"""Motion Magnify -- amplify or reduce a clip's own motion (optical flow).

The pixel-domain counterpart to :mod:`motion_gain`: instead of re/de-applying
coded P-frames, it measures the picture's real motion with dense optical flow and
re-warps each frame by ``(factor - 1)`` times that displacement -- so movement
reads as ``factor`` times larger. ``factor`` above 1 exaggerates motion (the
"motion microscope"), 1 is identity, below 1 damps it, and 0 stabilises content
back toward the opening frame.

Needs the optional ``flow`` extra (OpenCV + numpy); without it the frames pass
through untouched, like the other raw effects.
"""
from __future__ import annotations

from typing import List

from .base import Param
from .raw import RawMode


class MotionMagnify(RawMode):
    name = "motion_magnify"
    description = "Scale a clip's own motion via optical flow (magnify / stabilise)."
    params = [
        Param("factor", "float", 2.0, lo=-4.0, hi=8.0, label="Motion factor",
              automatable=False,
              help=">1 exaggerates movement; 1 = identity; <1 damps; 0 stabilises "
                   "to the first frame; negatives push motion the other way."),
        Param("accumulate", "bool", True, label="Accumulate",
              help="Magnify motion accumulated from the first frame (the drifting "
                   "microscope look). Off = magnify instantaneous frame-to-frame "
                   "motion only (no drift)."),
        Param("preset", "choice", "fast",
              choices=("ultrafast", "fast", "medium"), label="Flow preset",
              help="Optical-flow quality/speed trade-off."),
    ]

    def apply(self, frames: List[bytes], *, width: int, height: int,
              fps: float, factor: float = 2.0, accumulate: bool = True,
              preset: str = "fast") -> List[bytes]:
        from .. import flow
        if not flow.available():
            return frames
        return flow.magnify_raw(frames, width, height, factor=float(factor),
                                accumulate=bool(accumulate), preset=str(preset))
