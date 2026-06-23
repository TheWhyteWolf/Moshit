"""Multi-track compositing through the real pixel compositor (ffmpeg-gated)."""
import shutil

import pytest

pytestmark = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
    reason="ffmpeg/ffprobe not on PATH")


def _two_track(project, engine, make_clip, *, opacity=1.0, blend="normal",
               top_start=0):
    """Bottom track = full-frame red; a new top track = full-frame blue."""
    red = make_clip("red.mp4", color="red")
    blue = make_clip("blue.mp4", color="blue")
    mr = project.import_media(engine, red, label="red", role="main")
    mb = project.import_media(engine, blue, label="blue", role="main")
    project.add_clip(mr.id, "main")
    v2 = project.add_track()
    cb = project.add_clip(mb.id, v2.id)
    cb.start, cb.opacity, cb.blend_mode = top_start, opacity, blend
    return project


def test_opacity_blends_layers(project, engine, make_clip, tmp_path, probe):
    _two_track(project, engine, make_clip, opacity=0.5)
    out = tmp_path / "o.avi"
    r = project.render(engine, out)
    assert r["frames"] == 24                          # both clips 0..24
    px = probe.pixel(out, 12, 80, 60)
    assert px[0] > 80 and px[2] > 80 and px[1] < 70    # red+blue mix, low green


def test_screen_blend_brightens(project, engine, make_clip, tmp_path, probe):
    _two_track(project, engine, make_clip, blend="screen")
    out = tmp_path / "s.avi"
    project.render(engine, out)
    px = probe.pixel(out, 12, 80, 60)
    assert px[0] > 150 and px[2] > 150 and px[1] < 70  # screen(red,blue)=magenta


def test_gap_shows_lower_track(project, engine, make_clip, tmp_path, probe):
    _two_track(project, engine, make_clip, top_start=12)
    out = tmp_path / "g.avi"
    r = project.render(engine, out)
    assert r["frames"] == 36                           # blue (len 24) starts at 12
    early = probe.pixel(out, 2, 80, 60)
    late = probe.pixel(out, 20, 80, 60)
    assert early[0] > 150 and early[2] < 80            # red before the top clip
    assert late[2] > 150 and late[0] < 80              # blue once it starts


def test_intra_track_crossfade_dissolves(project, engine, make_clip, tmp_path, probe):
    # two clips on an upper track that overlap in time should cross-dissolve
    mr = project.import_media(engine, make_clip("red.mp4", color="red"),
                              label="red", role="main")
    mg = project.import_media(engine, make_clip("green.mp4", color="green"),
                              label="green", role="main")
    mb = project.import_media(engine, make_clip("blue.mp4", color="blue"),
                              label="blue", role="main")
    project.add_clip(mr.id, "main")                  # spine -> composite (2 tracks)
    v2 = project.add_track()
    project.add_clip(mg.id, v2.id)                   # green 0..24
    cb = project.add_clip(mb.id, v2.id)
    cb.start = 18                                     # overlap green's tail by 6
    out = tmp_path / "x.avi"
    project.render(engine, out)
    px = probe.pixel(out, 20, 80, 60)                # inside the 18..24 dissolve
    assert px[1] > 40 and px[2] > 60                 # green AND blue both present


def test_single_track_keeps_fast_path(project, engine, make_clip, tmp_path, probe):
    # one clean main-track clip -> flat/fast path, geometry + count unchanged
    src = make_clip("a.mp4")
    m = project.import_media(engine, src, role="main")
    project.add_clip(m.id, "main")
    out = tmp_path / "f.avi"
    r = project.render(engine, out)
    assert r["frames"] == 24 and probe.dims(out) == "160x120"
