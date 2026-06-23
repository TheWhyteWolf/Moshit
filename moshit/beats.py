"""Dependency-light onset detection for beat-synced automation.

Reads the preview's 16-bit PCM WAV (`build_audio_track`) and finds onset times
with a time-domain energy-flux detector: frame the signal, take the rising edge
of its energy envelope, and peak-pick against a local average. No numpy/librosa.
`pulse_curve` then turns onset positions into a keyframe spec the automation
engine understands (a `hold` curve that spikes the parameter on each beat).
"""
from __future__ import annotations

import array
import math
import wave
from typing import Dict, List, Optional


def _read_mono(wav_path):
    try:
        with wave.open(str(wav_path), "rb") as w:
            channels = w.getnchannels()
            width = w.getsampwidth()
            rate = w.getframerate()
            raw = w.readframes(w.getnframes())
    except (wave.Error, OSError, EOFError, ValueError):
        return None, 0
    if width != 2 or not raw or rate <= 0:
        return None, 0
    samples = array.array("h")
    samples.frombytes(raw)
    if channels > 1:
        samples = samples[::channels]
    return samples, rate


def onsets(wav_path, *, hop: int = 512, win: int = 1024,
           sensitivity: float = 1.4, min_gap_s: float = 0.12) -> List[float]:
    """Return onset times (seconds) in *wav_path*.

    ``sensitivity`` scales the local-average threshold (higher = fewer onsets);
    ``min_gap_s`` is the shortest spacing between detected onsets.
    """
    samples, rate = _read_mono(wav_path)
    if not samples:
        return []
    n = len(samples)
    env: List[float] = []
    i = 0
    while i < n:
        chunk = samples[i:i + win]
        e = sum(s * s for s in chunk) / max(1, len(chunk))
        env.append(math.sqrt(e))
        i += hop
    if len(env) < 3:
        return []
    odf = [max(0.0, env[k] - env[k - 1]) for k in range(1, len(env))]
    out: List[float] = []
    span = 8                                          # frames each side for the local mean
    last = -1e9
    for k, v in enumerate(odf):
        lo = max(0, k - span)
        hi = min(len(odf), k + span + 1)
        local = sum(odf[lo:hi]) / (hi - lo)
        prev = odf[k - 1] if k > 0 else 0.0
        nxt = odf[k + 1] if k + 1 < len(odf) else 0.0
        t = (k + 1) * hop / rate
        if (v > local * sensitivity + 1e-6 and v >= prev and v >= nxt
                and t - last >= min_gap_s):
            out.append(round(t, 4))
            last = t
    return out


def pulse_curve(positions, low, high, *, is_int: bool = False
                ) -> Optional[Dict]:
    """A `hold` automation spec that pulses to *high* at each beat in *positions*.

    *positions* are normalised 0..1 offsets within the clip. Between beats the
    value drops back to *low* (at the midpoint), so each beat reads as a spike.
    Returns None if there are no beats.
    """
    pos = [max(0.0, min(1.0, float(p))) for p in positions]
    pos.sort()
    if not pos:
        return None
    coerce = (lambda v: int(round(v))) if is_int else (lambda v: float(v))
    lo = coerce(low)
    hi = coerce(high)
    keys: List[list] = []
    if pos[0] > 1e-4:
        keys.append([0.0, lo])
    for idx, p in enumerate(pos):
        nxt = pos[idx + 1] if idx + 1 < len(pos) else 1.0
        keys.append([round(p, 4), hi])
        mid = p + (nxt - p) * 0.5
        if mid < 1.0 - 1e-4:
            keys.append([round(mid, 4), lo])
    return {"__auto__": True, "interp": "hold", "keys": keys}
