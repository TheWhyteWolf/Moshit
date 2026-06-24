"""New corruption modes: P-frame stutter, motion gain, recursive RGB shift."""
import shutil

import pytest

from moshit.modes.raw import available as numpy_available

requires_ffmpeg = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
    reason="ffmpeg/ffprobe not on PATH")
requires_numpy = pytest.mark.skipif(not numpy_available(),
                                    reason="numpy not installed")
pytestmark = requires_ffmpeg


def test_pframe_stutter_repeats_grow_frames(project, engine, make_clip, tmp_path,
                                            probe):
    m = project.import_media(engine, make_clip("s.mp4"), role="main")
    c = project.add_clip(m.id, "main")
    project.add_mosh("pframe_stutter",
                     {"length": 3, "repeats": 3, "direction": "pingpong"}, c.id)
    out = tmp_path / "st.avi"
    r = project.render(engine, out)
    assert r["frames"] > 24 and probe.dims(out) == "160x120"   # repeats stretch it


def test_motion_gain_amplifies_and_reduces(project, engine, make_clip, tmp_path):
    import moshit.project as P

    def frames(gain, name):
        proj = P.Project(name=name, config=engine.config,
                         assets_dir=str(tmp_path / name))
        m = proj.import_media(engine, make_clip("s.mp4"), role="main")
        c = proj.add_clip(m.id, "main")
        proj.add_mosh("motion_gain", {"gain": gain}, c.id)
        return proj.render(engine, tmp_path / (name + ".avi"))["frames"]

    base = frames(1.0, "g1")
    assert base == 24                                  # gain 1.0 is identity
    assert frames(2.0, "g2") > base > frames(0.5, "ghalf")


@requires_numpy
def test_rgb_recurse_displaces_colour_preserves_geometry(project, engine,
                                                         make_clip, tmp_path, probe):
    import moshit.project as P
    plain = P.Project(name="plain", config=engine.config,
                      assets_dir=str(tmp_path / "plain"))
    mp = plain.import_media(engine, make_clip("s.mp4"), role="main")
    plain.add_clip(mp.id, "main")
    base = tmp_path / "base.avi"
    plain.render(engine, base)

    m = project.import_media(engine, make_clip("s.mp4"), role="main")
    c = project.add_clip(m.id, "main")
    c.raw_effects = [{"name": "rgb_recurse",
                      "params": {"iterations": 4, "shift_x": 6, "swap": "gbr",
                                 "decay": 0.8}}]
    out = tmp_path / "rr.avi"
    r = project.render(engine, out)
    assert r["frames"] == 24 and probe.dims(out) == "160x120"  # finish preserves both
    diff = sum(1 for x in range(0, 160, 8) for y in range(0, 120, 8)
               if max(abs(a - b) for a, b in
                      zip(probe.pixel(base, 6, x, y),
                          probe.pixel(out, 6, x, y))) > 12)
    assert diff > 0                                    # recursion visibly shifts colour
