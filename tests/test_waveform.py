"""Peak-envelope extraction for the timeline waveform (pure stdlib, no ffmpeg)."""
import array
import math
import wave

from moshit import waveform


def _write_sine(path, *, seconds=0.5, rate=48000, channels=2, amp=20000):
    n = int(seconds * rate)
    samples = array.array("h")
    for i in range(n):
        s = int(amp * math.sin(2 * math.pi * 440 * i / rate))
        for _ in range(channels):
            samples.append(s)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(samples.tobytes())


def test_peaks_normalised_envelope(tmp_path):
    wav = tmp_path / "tone.wav"
    _write_sine(wav)
    env = waveform.peaks(wav, buckets=200)
    assert env is not None and len(env) == 200
    assert all(0.0 <= v <= 1.0 for v in env)
    assert max(env) > 0.9                       # a full-volume tone peaks near 1.0


def test_peaks_silence_and_missing(tmp_path):
    silent = tmp_path / "silent.wav"
    _write_sine(silent, amp=0)
    assert waveform.peaks(silent) == [0.0] * 600   # silence -> flat (top floored)
    assert waveform.peaks(tmp_path / "nope.wav") is None
