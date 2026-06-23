"""Onset detection + beat-pulse curve (pure stdlib, no ffmpeg)."""
import array
import math
import wave

from moshit import beats


def _write_clicks(path, *, rate=48000, gap_s=0.25, count=8):
    """A WAV of short loud clicks every gap_s seconds over near-silence."""
    samples = array.array("h")
    total = int(gap_s * count * rate)
    click = int(0.01 * rate)                       # 10 ms burst
    for n in range(count):
        starts = int(n * gap_s * rate)
        while len(samples) < starts:
            samples.append(0)
        for i in range(click):
            samples.append(int(28000 * math.sin(2 * math.pi * 900 * i / rate)))
    while len(samples) < total:
        samples.append(0)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(samples.tobytes())


def test_onsets_finds_regular_clicks(tmp_path):
    wav = tmp_path / "clicks.wav"
    _write_clicks(wav, gap_s=0.25, count=8)
    times = beats.onsets(wav)
    assert 6 <= len(times) <= 10                   # ~8 clicks, allow slack
    gaps = [b - a for a, b in zip(times, times[1:])]
    assert all(abs(g - 0.25) < 0.06 for g in gaps)  # ~0.25 s apart


def test_onsets_empty_on_silence(tmp_path):
    wav = tmp_path / "silent.wav"
    _write_clicks(wav, count=0)                     # all zeros
    assert beats.onsets(wav) == []
    assert beats.onsets(tmp_path / "missing.wav") == []


def test_pulse_curve_spikes_on_each_beat():
    spec = beats.pulse_curve([0.25, 0.5, 0.75], low=1, high=4, is_int=True)
    assert spec["interp"] == "hold"
    keys = spec["keys"]
    assert keys[0] == [0.0, 1]                      # starts low before the first beat
    highs = [k for k in keys if k[1] == 4]
    assert [round(k[0], 2) for k in highs] == [0.25, 0.5, 0.75]
    assert all(isinstance(k[1], int) for k in keys)  # int param stays int
    assert beats.pulse_curve([], 1, 4) is None
