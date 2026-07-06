"""Preview frame decoding for the GUI.

Decodes a moshed AVI by piping raw RGB frames out of ffmpeg, scaled to a
preview width. Frames are handed out as JPEG-encoded bytes (~7-10x smaller
than raw QImages, compressed here on the worker thread at <1ms/frame) so a
long preview no longer pins hundreds of MB of RAM; the preview widget decodes
the frame under the playhead on demand (~1ms). This avoids a PyAV dependency --
ffmpeg is already required by the engine. Decoding is linear (a moshed stream
has no reliable keyframes to seek to), and frames are streamed out in batches
so the UI can show the preview building up instead of blocking until complete.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Callable, List, Tuple

from PySide6.QtCore import QBuffer, QByteArray, QIODevice
from PySide6.QtGui import QImage

from ..avi import parse_avi

JPEG_QUALITY = 88


def encode_preview_frame(rgb: bytes, w: int, h: int) -> bytes:
    """Compress one raw RGB24 frame to JPEG bytes (worker-thread cheap)."""
    img = QImage(rgb, w, h, w * 3, QImage.Format.Format_RGB888)
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, "JPG", JPEG_QUALITY)
    buf.close()
    return bytes(ba)


class PreviewDecoder:
    def __init__(self, ffmpeg_bin: str = "ffmpeg"):
        self.ffmpeg = ffmpeg_bin
        self._proc: "subprocess.Popen | None" = None     # active decode, for cancel

    def terminate(self) -> None:
        """Kill the in-flight decode pipe (used to cancel a preview render)."""
        proc = self._proc
        if proc is not None:
            try:
                proc.kill()
            except Exception:
                pass

    def _dims(self, avi_path, max_width: int):
        info = parse_avi(avi_path)
        sw, sh, fps = info.width, info.height, info.fps or 30.0
        total = len(info.frames)
        if sw <= 0 or sh <= 0:
            return 0, 0, fps, total
        w = min(int(max_width), sw)
        h = max(2, round(w * sh / sw))
        if w % 2:
            w += 1
        if h % 2:
            h += 1
        return w, h, fps, total

    def decode(self, avi_path, max_width: int = 720
               ) -> Tuple[List[bytes], float, Tuple[int, int]]:
        """Decode the whole clip at once. Used for tests/synchronous callers."""
        frames: List[bytes] = []
        w = h = 0
        fps = 30.0

        def begin(_total, f):
            nonlocal fps
            fps = f

        self.decode_stream(avi_path, begin, frames.extend, max_width=max_width)
        # recover (w, h) for callers that want it
        w, h, _, _ = self._dims(avi_path, max_width)
        return frames, fps, (w, h)

    def decode_stream(self, avi_path, emit_begin: Callable[[int, float], None],
                      emit_batch: Callable[[List[bytes]], None],
                      max_width: int = 720, batch: int = 8) -> None:
        """Stream-decode *avi_path*.

        Calls ``emit_begin(total_frames, fps)`` once, then ``emit_batch(frames)``
        repeatedly with JPEG-encoded frames as they are decoded. Safe to call on
        a worker thread; the emit callbacks are expected to marshal to the UI
        thread.
        """
        w, h, fps, total = self._dims(avi_path, max_width)
        emit_begin(total, fps)
        if w <= 0 or h <= 0:
            return
        frame_bytes = w * h * 3

        proc = subprocess.Popen(
            [self.ffmpeg, "-hide_banner", "-loglevel", "error",
             "-i", str(avi_path), "-vf", f"scale={w}:{h}",
             "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self._proc = proc
        try:
            buf = b""
            pending: List[bytes] = []
            read_size = frame_bytes * batch
            while True:
                data = proc.stdout.read(read_size)
                if not data:
                    break
                buf += data
                while len(buf) >= frame_bytes:
                    chunk, buf = buf[:frame_bytes], buf[frame_bytes:]
                    pending.append(encode_preview_frame(chunk, w, h))
                if pending:
                    emit_batch(pending)
                    pending = []
            if pending:
                emit_batch(pending)
        finally:
            if proc.stdout:
                proc.stdout.close()
            proc.wait()
            self._proc = None
