"""Automation reaches the newly-automatable mosh params (M4).

Each test drives a param with a position→value curve through MoshContext and
checks the per-frame effect actually changes; a constant curve reproduces the
static behaviour (so existing renders are unaffected).
"""
from moshit.avi import Frame
from moshit.modes import load_modes
from moshit.modes.base import MoshContext, get_mode

load_modes()


def _frames(pattern):
    return [Frame(data=bytes([i]) * 8, coding_type=t, source="b")
            for i, t in enumerate(pattern)]


def _ctx(frames, **curves):
    # curves: name -> callable(pos 0..1) -> value
    return MoshContext(fps=24.0, width=8, height=8, clips={},
                       automation=curves, n_frames=len(frames))


def _apply(name, frames, ctx, **params):
    mode = get_mode(name)
    return mode.apply(list(frames), ctx, **mode.resolve(params))


def test_pframe_drop_probability_automation_ramps():
    frames = _frames("I" + "P" * 40)
    # ramp 0 -> 1: the head is (almost) kept, the tail (almost) all dropped
    ramp = _apply("pframe_drop", frames,
                  _ctx(frames, probability=lambda pos: pos),
                  probability=0.0, seed=1)
    kept = [f.data[0] for f in ramp if f.is_pframe]
    head = [k for k in kept if k <= 20]
    tail = [k for k in kept if k > 20]
    assert len(head) > len(tail)                 # far more survive near the start
    # a constant 0.0 curve keeps everything (matches the static default path)
    none_dropped = _apply("pframe_drop", frames,
                          _ctx(frames, probability=lambda pos: 0.0),
                          probability=0.9, seed=1)
    assert sum(f.is_pframe for f in none_dropped) == 40


def test_iframe_pulse_period_automation_changes_cadence():
    frames = _frames("I" + "P" * 40)

    def pulses(**curves):
        out = _apply("iframe_pulse", frames, _ctx(frames, **curves),
                     period=8, hold=1)
        return len(out) - len(frames)            # extra keyframe copies = pulses

    # the automated curve value drives cadence: a tight period pulses often,
    # a wide one rarely — and both differ from the static period=8 (5 pulses)
    assert pulses(period=lambda pos: 4.0) == 10  # every 4 P-frames
    assert pulses(period=lambda pos: 20.0) == 2  # every 20
    assert pulses() == 5                          # no automation -> fixed period=8


def test_pframe_echo_copies_automation_thickens_trail():
    frames = _frames("I" + "P" * 30)
    # copies ramp 1 -> 6 over the clip: more echoes get scheduled later
    out = _apply("pframe_echo", frames,
                 _ctx(frames, copies=lambda pos: 1 + pos * 5),
                 stride=1, delay=1, copies=1)
    flat = _apply("pframe_echo", frames, _ctx(frames), stride=1, delay=1, copies=1)
    assert len(out) > len(flat)                  # automation adds echoes vs static


def test_new_automatable_flags_declared():
    expected = {
        "pframe_drop": {"probability"},
        "iframe_pulse": {"period"},
        "pframe_echo": {"delay", "copies"},
    }
    for mode_name, names in expected.items():
        auto = {p.name for p in get_mode(mode_name).params if p.automatable}
        assert names <= auto, f"{mode_name}: {names - auto} not marked automatable"
