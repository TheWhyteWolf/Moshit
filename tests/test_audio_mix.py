"""Multi-track audio mixing and per-clip gain (ffmpeg-gated)."""
import re
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
    reason="ffmpeg/ffprobe not on PATH")


def _max_volume_db(path):
    """Peak level (dBFS) of *path*'s audio, via ffmpeg's volumedetect."""
    err = subprocess.run(
        ["ffmpeg", "-hide_banner", "-i", str(path), "-af", "volumedetect",
         "-f", "null", "-"], capture_output=True, text=True).stderr
    m = re.search(r"max_volume:\s*(-?[\d.]+) dB", err)
    return float(m.group(1)) if m else None


def test_multitrack_audio_mixes(engine, project, make_clip, tmp_path, probe):
    # two video tracks, each with its own tone -> one summed audio stream
    a = project.import_media(engine, make_clip("a.mp4", color="red", audio=220,
                                               dur=1.0), label="a", role="main")
    b = project.import_media(engine, make_clip("b.mp4", color="blue", audio=660,
                                               dur=1.0), label="b", role="main")
    project.add_clip(a.id, "main")
    v2 = project.add_track()
    project.add_clip(b.id, v2.id)
    out = tmp_path / "mix.mp4"
    r = project.render(engine, tmp_path / "r.avi", profile="h264_mp4",
                       export_path=str(out), audio=True)
    assert len(r["audio_plans"]) == 2                 # one plan per audible track
    assert "audio" in r and probe.has_audio(out)
    assert abs(probe.vdur(out) - probe.adur(out)) < 0.15


def test_silent_upper_track_keeps_lower_audio(engine, project, make_clip,
                                              tmp_path, probe):
    # bottom track has a tone, the upper layer is silent -> mix still has audio
    a = project.import_media(engine, make_clip("a.mp4", color="red", audio=440,
                                               dur=1.0), label="a", role="main")
    b = project.import_media(engine, make_clip("b.mp4", color="blue"),  # no audio
                             label="b", role="main")
    project.add_clip(a.id, "main")
    v2 = project.add_track()
    project.add_clip(b.id, v2.id)
    out = tmp_path / "s.mp4"
    r = project.render(engine, tmp_path / "r.avi", profile="h264_mp4",
                       export_path=str(out), audio=True)
    assert "audio" in r and probe.has_audio(out)


def test_clip_gain_lowers_level(engine, project, make_clip, tmp_path):
    # the same tone at gain 0.25 should peak markedly lower than at unity
    src = make_clip("a.mp4", color="red", audio=440, dur=1.0)

    def render_at(gain, name):
        proj = type(project)(name="g", config=engine.config,
                             assets_dir=str(tmp_path / f"assets_{name}"))
        m = proj.import_media(engine, src, label="a", role="main")
        c = proj.add_clip(m.id, "main")
        c.gain = gain
        out = tmp_path / f"{name}.mp4"
        proj.render(engine, tmp_path / f"{name}.avi", profile="h264_mp4",
                    export_path=str(out), audio=True)
        return _max_volume_db(out)

    loud = render_at(1.0, "loud")
    quiet = render_at(0.25, "quiet")
    assert loud is not None and quiet is not None
    assert loud - quiet > 6.0                          # ~12 dB down for 0.25x


def test_full_silence_writes_no_track(engine, project, make_clip, tmp_path,
                                      probe):
    # no track carries real audio -> no audio stream muxed
    a = project.import_media(engine, make_clip("a.mp4", color="red"),
                             label="a", role="main")
    b = project.import_media(engine, make_clip("b.mp4", color="blue"),
                             label="b", role="main")
    project.add_clip(a.id, "main")
    v2 = project.add_track()
    project.add_clip(b.id, v2.id)
    out = tmp_path / "n.mp4"
    r = project.render(engine, tmp_path / "r.avi", profile="h264_mp4",
                       export_path=str(out), audio=True)
    assert "audio" not in r and not probe.has_audio(out)
