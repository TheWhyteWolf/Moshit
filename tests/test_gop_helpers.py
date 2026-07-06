"""Characterization + unit tests for the shared GOP/P-run helpers (M2).

The golden signatures below were captured from the pre-refactor modes, so they
prove the shared helpers reproduce each mode's exact frame reordering (the
behavioral suite only checks termination and bounds, not exact output).
"""
from moshit.avi import Frame
from moshit.modes import load_modes
from moshit.modes.base import MoshContext, get_mode

load_modes()

PAT = "PIPPIPP"          # a lead P-frame, then two GOPs of different lengths


def _make(pattern):
    return [Frame(data=bytes([i]) * 8, coding_type=t, source="b")
            for i, t in enumerate(pattern)]


def _sig(frames):
    return " ".join(f"{f.coding_type}{f.data[0]}" for f in frames)


def _ctx(frames, auto=None):
    return MoshContext(fps=24.0, width=8, height=8, clips={},
                       automation=auto or {}, n_frames=len(frames))


def _run(name, overrides, pattern=PAT, auto=None):
    mode = get_mode(name)
    frames = _make(pattern)
    return _sig(mode.apply(list(frames), _ctx(frames, auto),
                           **mode.resolve(overrides)))


GOLDEN = [
    ("gop_scramble", {"seed": 3, "keep_first": True},
     "P0 I1 P2 P3 I4 P5 P6"),
    ("gop_scramble", {"seed": 3, "keep_first": False},
     "P0 I4 P5 P6 I1 P2 P3"),
    ("pframe_reverse", {"per_gop": True},
     "P0 I1 P3 P2 I4 P6 P5"),
    ("pframe_reverse", {"per_gop": False},
     "P6 I1 P5 P3 I4 P2 P0"),
    ("pingpong", {"per_gop": True, "tail_only": False},
     "P0 I1 P2 P3 P3 P2 I4 P5 P6 P6 P5"),
    ("pingpong", {"per_gop": True, "tail_only": True},
     "P0 I1 P2 P3 P2 I4 P5 P6 P5"),
    ("pingpong", {"per_gop": False, "tail_only": False},
     "P0 P2 P3 P5 P6 P6 P5 P3 P2 P0"),
    ("pframe_stutter", {"length": 2, "repeats": 2, "direction": "forward",
                        "start": 0}, "P0 P0 I1 P2 P3 P2 P3 I4 P5 P6 P5 P6"),
    ("pframe_stutter", {"length": 2, "repeats": 2, "direction": "reverse",
                        "start": 0}, "P0 P0 I1 P3 P2 P3 P2 I4 P6 P5 P6 P5"),
    ("pframe_stutter", {"length": 2, "repeats": 2, "direction": "pingpong",
                        "start": 0}, "P0 P0 I1 P2 P3 P3 P2 I4 P5 P6 P6 P5"),
    ("pframe_stutter", {"length": 3, "repeats": 2, "direction": "forward",
                        "start": 2}, "P0 I1 P2 P3 I4 P5 P6 P5 P6"),
]


import pytest


@pytest.mark.parametrize("name,overrides,expected", GOLDEN)
def test_mode_golden_output(name, overrides, expected):
    assert _run(name, overrides) == expected


def test_pframe_stutter_automation_still_ramps():
    # repeats automated 1 -> 3 across the clip (the per-frame ctx.auto path)
    out = _run("pframe_stutter",
               {"length": 1, "repeats": 1, "direction": "forward"},
               pattern="IPPPP",
               auto={"repeats": lambda pos: 1.0 + 2.0 * pos})
    assert out == "I0 P1 P1 P2 P2 P3 P3 P4 P4 P4"


# -- direct unit tests of the helpers themselves --------------------------- #

def test_split_gops():
    from moshit.modes._gop import split_gops
    lead, blocks = split_gops(_make("PIPPIPP"))
    assert _sig(lead) == "P0"
    assert [_sig(b) for b in blocks] == ["I1 P2 P3", "I4 P5 P6"]
    # no lead-in when the stream opens on a keyframe
    lead2, blocks2 = split_gops(_make("IPPIP"))
    assert lead2 == []
    assert [_sig(b) for b in blocks2] == ["I0 P1 P2", "I3 P4"]


def test_map_pframe_runs_passes_indices():
    from moshit.modes._gop import map_pframe_runs
    seen = []

    def fn(run, p_start, i_start):
        seen.append((_sig(run), p_start, i_start))
        return run
    out = map_pframe_runs(_make("PIPPIPP"), fn)
    assert _sig(out) == _sig(_make("PIPPIPP"))          # identity fn = unchanged
    assert seen == [("P0", 0, 0), ("P2 P3", 1, 2), ("P5 P6", 3, 5)]
