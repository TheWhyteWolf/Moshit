"""End-to-end render/export and audio passthrough (ffmpeg-gated)."""
import shutil

import pytest

pytestmark = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
    reason="ffmpeg/ffprobe not on PATH")


def test_mosh_render_and_export(engine, project, make_clip, tmp_path, probe):
    src = make_clip("s.mp4")
    media = project.import_media(engine, src, role="main")
    clip = project.add_clip(media.id, "main")
    project.add_mosh("pframe_duplicate", {"factor": 2}, clip.id)
    out = tmp_path / "out.mp4"
    r = project.render(engine, tmp_path / "r.avi", profile="h264_mp4",
                       export_path=str(out))
    assert r["frames"] > 0
    assert out.exists() and probe.nframes(out) == r["frames"]


def test_export_includes_source_audio_in_sync(engine, project, make_clip,
                                               tmp_path, probe):
    src = make_clip("a.mp4", audio=440, dur=1.5)
    media = project.import_media(engine, src, role="main")
    project.add_clip(media.id, "main")
    out = tmp_path / "out.mp4"
    r = project.render(engine, tmp_path / "r.avi", profile="h264_mp4",
                       export_path=str(out), audio=True)
    assert "audio" in r
    v, a = probe.vdur(out), probe.adur(out)
    assert a is not None and abs(v - a) < 0.15


def test_trim_shrinks_audio_with_video(engine, project, make_clip, tmp_path,
                                       probe):
    src = make_clip("a.mp4", audio=440, dur=2.0)
    media = project.import_media(engine, src, role="main")
    clip = project.add_clip(media.id, "main")
    clip.in_point, clip.out_point = 12, 36          # ~1.0s at 24fps (gop=8 snap)
    out = tmp_path / "out.mp4"
    project.render(engine, tmp_path / "r.avi", profile="h264_mp4",
                   export_path=str(out), audio=True)
    assert 0.8 < probe.adur(out) < 1.2
    assert abs(probe.vdur(out) - probe.adur(out)) < 0.15


def test_source_without_audio_exports_no_audio_track(engine, project, make_clip,
                                                     tmp_path, probe):
    src = make_clip("v.mp4")                          # no audio
    media = project.import_media(engine, src, role="main")
    project.add_clip(media.id, "main")
    out = tmp_path / "out.mp4"
    r = project.render(engine, tmp_path / "r.avi", profile="h264_mp4",
                       export_path=str(out), audio=True)
    assert "audio" not in r and not probe.has_audio(out)
