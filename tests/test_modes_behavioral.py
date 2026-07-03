"""Behavioral safety net over EVERY registered codec-domain mosh mode.

Every mode is run against synthetic frame lists with its default parameters and
with each parameter pushed to its lo/hi edge (booleans both ways, every choice),
under a watchdog so an accidental infinite loop fails the test instead of
hanging the suite. Pure Python — no ffmpeg needed.
"""
from __future__ import annotations

import signal
from contextlib import contextmanager

import pytest

from moshit.avi import Frame
from moshit.modes import load_modes
from moshit.modes.base import MoshContext, available_modes, get_mode, mode_class

load_modes()

# Only the built-in modes are our contract to keep green. A developer's personal
# user plugin (loaded from ~/.config/moshit/modes) may legitimately be slow or
# expansive; parametrizing over it would fail this suite on their machine while
# CI stays green. Built-ins live in the moshit.modes.* package; plugins load
# under a "moshit_usermode_*" module name, so filter by module.
BUILTIN_MODES = [n for n in available_modes()
                 if mode_class(n).__module__.startswith("moshit.modes.")]

# A generous runaway guard: no mode should blow a 10-frame input up past this.
MAX_OUTPUT_FRAMES = 200_000
APPLY_TIMEOUT_S = 15


@contextmanager
def watchdog(seconds=APPLY_TIMEOUT_S):
    """Fail (rather than hang) if a mode never terminates."""
    if hasattr(signal, "SIGALRM"):
        def handler(signum, frame):
            raise TimeoutError(f"mode.apply() did not finish within {seconds}s")
        old = signal.signal(signal.SIGALRM, handler)
        signal.alarm(seconds)
        try:
            yield
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old)
    else:  # non-Unix: still run, just without the timeout
        yield


def make_frames(pattern="IPPPPIPPPP", source="base"):
    """Frame list from a coding-type pattern, each with unique payload bytes."""
    return [Frame(data=bytes([i]) * 64, coding_type=t, source=source)
            for i, t in enumerate(pattern)]


def make_ctx(frames, motion=None, automation=None):
    clips = {"m": motion} if motion is not None else {}
    return MoshContext(fps=24.0, width=64, height=48, clips=clips,
                       automation=automation or {}, n_frames=len(frames))


def run_mode(mode, frames, ctx, overrides):
    values = mode.resolve(overrides)
    snapshot = [(f.data, f.coding_type) for f in frames]
    with watchdog():
        out = mode.apply(list(frames), ctx, **values)
    assert isinstance(out, list), f"{mode.name} returned {type(out).__name__}"
    assert len(out) <= MAX_OUTPUT_FRAMES, (
        f"{mode.name} exploded {len(frames)} frames into {len(out)}")
    for f in out:
        assert isinstance(f, Frame), f"{mode.name} emitted {type(f).__name__}"
    assert [(f.data, f.coding_type) for f in frames] == snapshot, (
        f"{mode.name} mutated its input frames")
    return out


def base_overrides(mode):
    """Every clip_ref param needs a real motion source to run at all."""
    return {p.name: "m" for p in mode.params if p.kind == "clip_ref"}


@pytest.mark.parametrize("name", BUILTIN_MODES)
def test_mode_defaults(name):
    mode = get_mode(name)
    frames = make_frames()
    motion = make_frames("IPPPPPP", source="motion")
    ctx = make_ctx(frames, motion=motion)
    out = run_mode(mode, frames, ctx, base_overrides(mode))
    assert out, f"{name} returned no frames for a 10-frame input at defaults"

    # Empty input must be a clean no-op for every mode.
    empty = run_mode(mode, [], make_ctx([], motion=motion), base_overrides(mode))
    assert empty == []


def edge_values(param):
    if param.kind in ("int", "float"):
        vals = []
        if param.lo is not None:
            vals.append(int(param.lo) if param.kind == "int" else float(param.lo))
        if param.hi is not None:
            vals.append(int(param.hi) if param.kind == "int" else float(param.hi))
        return vals
    if param.kind == "bool":
        return [True, False]
    if param.kind == "choice":
        return list(param.choices)
    return []


@pytest.mark.parametrize("name", BUILTIN_MODES)
def test_mode_param_edges(name):
    """Each param at its lo/hi (bools both ways, every choice), one at a time."""
    mode = get_mode(name)
    frames = make_frames()
    motion = make_frames("IPPPPPP", source="motion")
    ctx = make_ctx(frames, motion=motion)
    base = base_overrides(mode)
    for param in mode.params:
        for val in edge_values(param):
            overrides = dict(base)
            overrides[param.name] = val
            run_mode(mode, frames, ctx, overrides)  # must terminate, stay sane


def test_motion_gain_automation_crosses_unity():
    """A 0.5→2.0 gain curve must BOTH thin early frames and duplicate late ones
    (regression: the branch used to lock from the curve's start value)."""
    mode = get_mode("motion_gain")
    frames = make_frames("I" + "P" * 20)
    ctx = make_ctx(frames, automation={"gain": lambda pos: 0.5 + 1.5 * pos})
    out = run_mode(mode, frames, ctx, {"gain": 0.5})

    counts = {}
    for f in out:
        if f.is_pframe:
            counts[f.data] = counts.get(f.data, 0) + 1
    input_p = [f.data for f in frames if f.is_pframe]
    assert any(d not in counts for d in input_p), "low-gain frames were not thinned"
    assert any(c >= 2 for c in counts.values()), "high-gain frames were not duplicated"


def test_motion_weave_zero_base_run_terminates():
    """Regression: base_run=0 with motion_run>0 used to loop forever."""
    mode = get_mode("motion_weave")
    frames = make_frames("IPPPPPP")            # 6 base P-frames
    motion = make_frames("IPPP", source="motion")
    ctx = make_ctx(frames, motion=motion)
    out = run_mode(mode, frames, ctx,
                   {"source": "m", "base_run": 0, "motion_run": 2})
    assert out[0].is_iframe                     # held base keyframe
    body = out[1:]
    assert body and all(f.source == "motion" for f in body)
    assert len(body) == 6                       # source stands in for base time


def test_pingpong_whole_clip_anchors_on_first_iframe():
    mode = get_mode("pingpong")
    frames = make_frames("IPPIPP")
    ctx = make_ctx(frames)
    out = run_mode(mode, frames, ctx, {"per_gop": False, "tail_only": False})
    assert out[0].is_iframe and out[0].data == frames[0].data
    assert not any(f.is_iframe for f in out[1:])       # interior I dropped
    p = [f.data for f in frames if f.is_pframe]        # bounce: out and back
    assert [f.data for f in out[1:]] == p + p[::-1]
