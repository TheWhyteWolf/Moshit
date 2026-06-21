"""The dependency-light `cli selftest` (sections A-K) must stay green.

This covers the pure-Python logic -- AVI codec, modes, project model,
finishing math, automation, region, presets, pixel filter strings -- without
needing ffmpeg, so it runs everywhere.
"""
from moshit.cli import main


def test_selftest_passes():
    assert main(["selftest"]) == 0
