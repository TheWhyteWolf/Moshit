"""Pixel-domain finishing + glitch integration: speed, reverse, fade, crossfade,
automation and region all through the real render (ffmpeg-gated)."""
import shutil

import pytest

pytestmark = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
    reason="ffmpeg/ffprobe not on PATH")


def _one_clip(engine, project, make_clip, **clip_attrs):
    src = make_clip("s.mp4", **{k: clip_attrs.pop(k)
                                for k in ("color", "audio", "dur")
                                if k in clip_attrs})
    media = project.import_media(engine, src, role="main")
    clip = project.add_clip(media.id, "main")
    for k, v in clip_attrs.items():
        setattr(clip, k, v)
    return clip


def test_speed_2x_halves_frames_and_audio(engine, project, make_clip, tmp_path,
                                          probe):
    clip = _one_clip(engine, project, make_clip, audio=440, dur=2.0, speed=2.0)
    out = tmp_path / "out.mp4"
    project.render(engine, tmp_path / "r.avi", profile="h264_mp4",
                   export_path=str(out), audio=True)
    assert 22 <= probe.nframes(out) <= 26          # 48 -> ~24
    assert abs(probe.vdur(out) - probe.adur(out)) < 0.2


def test_reverse_preserves_frame_count(engine, project, make_clip, tmp_path,
                                       probe):
    _one_clip(engine, project, make_clip, dur=2.0, reverse=True)
    out = tmp_path / "out.avi"
    project.render(engine, out)
    assert 46 <= probe.nframes(out) <= 50


def test_fade_in_darkens_first_frame(engine, project, make_clip, tmp_path,
                                     probe):
    _one_clip(engine, project, make_clip, color="red", fade_in=12)
    out = tmp_path / "out.avi"
    project.render(engine, out)
    assert sum(probe.pixel(out, 0, 80, 60)) < 60       # ~black
    assert probe.pixel(out, 18, 80, 60)[0] > 150       # red later


def test_crossfade_shortens_total_and_blends(engine, project, make_clip,
                                             tmp_path, probe):
    red = make_clip("red.mp4", color="red")
    blue = make_clip("blue.mp4", color="blue")
    m1 = project.import_media(engine, red, label="red", role="main")
    m2 = project.import_media(engine, blue, label="blue", role="main")
    project.add_clip(m1.id, "main")
    cb = project.add_clip(m2.id, "main")
    cb.transition_in = 12
    out = tmp_path / "out.avi"
    r = project.render(engine, out)
    assert r["frames"] == 36                            # 24 + 24 - 12
    seam = probe.pixel(out, 18, 80, 60)                 # mid crossfade (12..24)
    assert seam[0] > 30 and seam[2] > 30                # red+blue blend


def test_automated_factor_between_constant_ends(engine, project, make_clip,
                                                tmp_path):
    src = make_clip("s.mp4", dur=1.5)
    media = project.import_media(engine, src, role="main")
    clip = project.add_clip(media.id, "main")
    op = project.add_mosh("pframe_duplicate", {"factor": 1}, clip.id)
    n1 = project.render(engine, tmp_path / "a.avi")["frames"]
    op.params = {"factor": 3}
    n3 = project.render(engine, tmp_path / "b.avi")["frames"]
    op.params = {"factor": {"__auto__": True, "keys": [[0.0, 1], [1.0, 3]]}}
    nauto = project.render(engine, tmp_path / "c.avi")["frames"]
    assert n1 < nauto < n3


def test_region_limits_the_effect(engine, project, make_clip, tmp_path):
    src = make_clip("s.mp4", dur=1.5)
    media = project.import_media(engine, src, role="main")
    clip = project.add_clip(media.id, "main")
    op = project.add_mosh("pframe_duplicate", {"factor": 3}, clip.id)
    whole = project.render(engine, tmp_path / "w.avi")["frames"]
    op.region_start, op.region_end = 0, 10
    limited = project.render(engine, tmp_path / "l.avi")["frames"]
    assert media.nb_frames < limited < whole
