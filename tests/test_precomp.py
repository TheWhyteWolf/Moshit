"""Nested sequences (precomps): rendered as cached, moshable media (ffmpeg-gated)."""
import shutil
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
    reason="ffmpeg/ffprobe not on PATH")


def _precomp_with(project, engine, media_id):
    """A precomp sequence holding one clip of *media_id*; returns the Sequence."""
    seq = project.add_sequence("inner")
    vt = project.video_tracks(seq.id)[0]
    project.add_clip(media_id, vt.id)
    return seq


def test_precomp_renders_as_a_clip(project, engine, make_clip, tmp_path, probe):
    mr = project.import_media(engine, make_clip("red.mp4", color="red"),
                              label="red", role="main")
    seq = _precomp_with(project, engine, mr.id)
    project.add_sequence_clip("main", seq.id)        # use the precomp on root
    out = tmp_path / "o.avi"
    r = project.render(engine, out)
    assert r["frames"] == 24                         # precomp is 1s @ 24
    assert probe.pixel(out, 12, 80, 60)[0] > 150     # its red shows through


def test_precomp_is_moshable(project, engine, make_clip, tmp_path, probe):
    # a mosh op on the precomp clip proves the precomp behaves like real media
    mr = project.import_media(engine, make_clip("red.mp4", color="red"),
                              label="red", role="main")
    seq = _precomp_with(project, engine, mr.id)
    clip = project.add_sequence_clip("main", seq.id)
    project.add_mosh("pframe_duplicate", {"factor": 2}, clip.id)
    out = tmp_path / "o.avi"
    r = project.render(engine, out)
    assert r["frames"] > 24                           # duplicated p-frames stretch it


def test_precomp_cache_invalidates_on_edit(project, engine, make_clip, tmp_path):
    mr = project.import_media(engine, make_clip("red.mp4", color="red"),
                              label="red", role="main")
    mb = project.import_media(engine, make_clip("blue.mp4", color="blue"),
                              label="blue", role="main")
    seq = _precomp_with(project, engine, mr.id)
    vt = project.video_tracks(seq.id)[0]
    project.add_sequence_clip("main", seq.id)
    out = tmp_path / "o.avi"
    project.render(engine, out)
    pcm = project.sequence_media(seq.id)
    d1, mtime = pcm.digest, Path(pcm.intermediate_path).stat().st_mtime_ns

    project.render(engine, out)                       # unchanged -> cache hit
    assert pcm.digest == d1
    assert Path(pcm.intermediate_path).stat().st_mtime_ns == mtime  # not rewritten

    project.add_clip(mb.id, vt.id)                    # edit the inner sequence
    project.render(engine, out)
    assert pcm.digest != d1                           # re-rendered
