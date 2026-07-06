"""Bitrot -- blocky compression glitches from corrupted bytes.

Unlike the reordering modes, this one corrupts bytes *inside* the compressed
P-frame payload. The decoder misreads the damaged macroblock data and smears
blocks of colour across the frame until the next start code resyncs it — the
classic "datamosh corruption" look.

Bytes at the very start of each frame (the VOP header) are left intact so the
frame is still recognised and the stream stays decodable; only the macroblock
data downstream is damaged. Keyframes are never touched, so corruption can't
poison the whole clip.
"""
from __future__ import annotations

import random
from typing import List

from ..avi import Frame
from .base import MoshContext, MoshMode, Param


class Bitrot(MoshMode):
    name = "bitrot"
    description = "Corrupt bytes inside P-frames for blocky compression artefacts."
    params = [
        Param("intensity", "float", 0.25, lo=0.0, hi=1.0, label="Affected frames",
              help="Fraction of P-frames that get corrupted (0–1).",
              automatable=True),
        Param("hits", "int", 6, lo=1, hi=512, label="Byte flips per frame",
              help="How many bytes to scramble in each affected frame."),
        Param("skip_header", "int", 32, lo=8, hi=256, label="Protect header bytes",
              help="Leading bytes left intact so the frame still parses."),
        Param("seed", "int", 0, lo=0, hi=1_000_000, label="Seed",
              help="Random seed for which bytes get flipped — keep it fixed for "
                   "a repeatable glitch, change it to roll a different one."),
    ]

    def apply(self, frames: List[Frame], ctx: MoshContext, *,
              intensity: float = 0.25, hits: int = 6, skip_header: int = 32,
              seed: int = 0) -> List[Frame]:
        base_intensity = max(0.0, min(1.0, float(intensity)))
        hits = max(1, int(hits))
        skip_header = max(8, int(skip_header))
        rng = random.Random(seed)
        out: List[Frame] = []
        for i, f in enumerate(frames):
            inten = max(0.0, min(1.0, float(ctx.auto("intensity", i, base_intensity))))
            if (f.is_pframe and len(f.data) > skip_header + 8
                    and rng.random() < inten):
                buf = bytearray(f.data)
                for _ in range(hits):
                    idx = rng.randint(skip_header, len(buf) - 1)
                    buf[idx] = rng.randint(0, 255)
                out.append(f.copy(data=bytes(buf)))
            else:
                out.append(f)
        return out
