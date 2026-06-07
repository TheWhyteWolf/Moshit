"""Preview frame decoding for the GUI.

Decodes a moshed AVI into a list of QImages by piping raw RGB frames out of
ffmpeg, scaled to a preview width to keep memory reasonable. This avoids a PyAV
dependency -- ffmpeg is already required by the engine. For v1 a short clip is
fully decoded into a frame cache, which makes scrubbing instant.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import List, Tuple

from PySide6.QtGui import QImage

from ..avi import parse_avi


class PreviewDecoder:
    def __init__(self, ffmpeg_bin: str = "ffmpeg"):
        self.ffmpeg = ffmpeg_bin

    def decode(self, avi_path, max_width: int = 720
               ) -> Tuple[List[QImage], float, Tuple[int, int]]:
        """Return (frames, fps, (w, h)). Empty list if decoding fails."""
        info = parse_avi(avi_path)
        sw, sh, fps = info.width, info.height, info.fps or 30.0
        if sw <= 0 or sh <= 0:
            return [], fps, (0, 0)

        w = min(int(max_width), sw)
        h = max(2, round(w * sh / sw))
        if w % 2:
            w += 1
        if h % 2:
            h += 1
        frame_bytes = w * h * 3

        proc = subprocess.run(
            [self.ffmpeg, "-hide_banner", "-loglevel", "error",
             "-i", str(avi_path), "-vf", f"scale={w}:{h}",
             "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
            capture_output=True)
        if proc.returncode != 0 or not proc.stdout:
            return [], fps, (w, h)

        raw = proc.stdout
        frames: List[QImage] = []
        for off in range(0, len(raw) - frame_bytes + 1, frame_bytes):
            chunk = raw[off:off + frame_bytes]
            # .copy() detaches from the temporary buffer so the QImage owns it
            img = QImage(chunk, w, h, w * 3, QImage.Format.Format_RGB888).copy()
            frames.append(img)
        return frames, fps, (w, h)
