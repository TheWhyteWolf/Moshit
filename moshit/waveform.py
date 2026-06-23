"""A dependency-light peak envelope for the preview's audio track.

The preview pipeline writes a 16-bit PCM WAV (`build_audio_track`); this reads
it back with the stdlib `wave` module and reduces it to a short list of
normalised peaks (0..1) the timeline can draw. No numpy/ffmpeg needed.
"""
from __future__ import annotations

import array
import wave
from typing import List, Optional


def peaks(wav_path, buckets: int = 600) -> Optional[List[float]]:
    """Return ~`buckets` normalised peak amplitudes (0..1) for *wav_path*.

    Reads the first channel of a 16-bit PCM WAV and takes the max absolute
    amplitude per bucket, normalised so the loudest bucket reaches 1.0. Returns
    None for an unreadable file, an empty track, or an unsupported sample width.
    """
    try:
        with wave.open(str(wav_path), "rb") as w:
            channels = w.getnchannels()
            width = w.getsampwidth()
            nframes = w.getnframes()
            raw = w.readframes(nframes)
    except (wave.Error, OSError, EOFError, ValueError):
        return None
    if width != 2 or not raw:
        return None                                  # only 16-bit PCM
    samples = array.array("h")
    samples.frombytes(raw)
    if channels > 1:
        samples = samples[::channels]                # first channel is enough
    total = len(samples)
    if not total:
        return None
    buckets = max(1, min(int(buckets), total))
    step = total / buckets
    out: List[int] = []
    top = 1
    for b in range(buckets):
        lo = int(b * step)
        hi = max(lo + 1, int((b + 1) * step))
        m = max((abs(s) for s in samples[lo:hi]), default=0)
        out.append(m)
        if m > top:
            top = m
    return [v / top for v in out]
