"""Pixel-domain effects through the real finish pass (ffmpeg-gated)."""
import shutil

import pytest

pytestmark = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
    reason="ffmpeg/ffprobe not on PATH")


@pytest.fixture
def pixel_engine(tmp_path):
    # 162x120: width not divisible by common block sizes, to exercise the
    # geometry-restore after size-changing pixel filters (e.g. pixelate).
    from moshit.engine import EngineConfig, MoshEngine
    from moshit.ffmpeg import FFmpeg
    from moshit.modes import load_modes
    load_modes()
    cfg = EngineConfig(width=162, height=120, fps=24.0, gop=8,
                       work_dir=str(tmp_path / "w"))
    eng = MoshEngine(cfg, FFmpeg())
    yield eng
    eng.cleanup()


def _clip(eng, make_clip, tmp_path, **attrs):
    from moshit.project import Project
    src = make_clip("s.mp4", size="162x120")
    p = Project(name="p", config=eng.config, assets_dir=str(tmp_path / "a"))
    m = p.import_media(eng, src, role="main")
    c = p.add_clip(m.id, "main")
    for k, v in attrs.items():
        setattr(c, k, v)
    return p, c


@pytest.mark.parametrize("name", ["rgb_shift", "hue_rotate", "pixelate",
                                  "noise", "echo", "trails",
                                  "zoom", "pan", "rotate", "shake"])
def test_pixel_effect_renders_preserving_geometry(name, pixel_engine, make_clip,
                                                  tmp_path, probe):
    p, c = _clip(pixel_engine, make_clip, tmp_path,
                 pixel_effects=[{"name": name, "params": {}}])
    out = tmp_path / f"{name}.avi"
    p.render(pixel_engine, out)
    assert probe.dims(out) == "162x120"          # geometry preserved
    assert probe.nframes(out) == 24              # 1s @ 24fps, count unchanged


@pytest.mark.parametrize("name,params", [
    ("zoom", {"start": 1.0, "end": 2.0}),
    ("pan", {"dx": 60, "dy": 0}),
    ("rotate", {"angle": 0.0, "spin": 90.0}),
    ("shake", {"amount": 20}),
])
def test_motion_injection_moves_pixels(name, params, pixel_engine, make_clip,
                                       tmp_path, probe):
    # a synthetic camera move should change the picture vs. the untouched clip
    p, c = _clip(pixel_engine, make_clip, tmp_path)
    plain = tmp_path / "plain.avi"
    p.render(pixel_engine, plain)
    c.pixel_effects = [{"name": name, "params": params}]
    moved = tmp_path / f"{name}.avi"
    p.render(pixel_engine, moved)
    assert probe.nframes(moved) == 24                       # length preserved
    # a frame partway through (animation has progressed) should differ
    assert (probe.pixel(plain, 18, 80, 60, w=162)
            != probe.pixel(moved, 18, 80, 60, w=162))


def test_rgb_shift_changes_pixels(pixel_engine, make_clip, tmp_path, probe):
    p, c = _clip(pixel_engine, make_clip, tmp_path)
    plain = tmp_path / "plain.avi"
    p.render(pixel_engine, plain)
    c.pixel_effects = [{"name": "rgb_shift", "params": {"amount": 12}}]
    shifted = tmp_path / "shift.avi"
    p.render(pixel_engine, shifted)
    assert (probe.pixel(plain, 10, 80, 60, w=162)
            != probe.pixel(shifted, 10, 80, 60, w=162))


def test_pixel_fx_composes_with_crossfade(pixel_engine, make_clip, tmp_path,
                                          probe):
    from moshit.project import Project
    src = make_clip("s.mp4", size="162x120")
    p = Project(name="p", config=pixel_engine.config,
                assets_dir=str(tmp_path / "a"))
    m1 = p.import_media(pixel_engine, src, label="a", role="main")
    m2 = p.import_media(pixel_engine, src, label="b", role="main")
    ca = p.add_clip(m1.id, "main")
    ca.pixel_effects = [{"name": "pixelate", "params": {"block": 10}}]
    cb = p.add_clip(m2.id, "main")
    cb.transition_in = 8
    out = tmp_path / "out.mp4"
    p.render(pixel_engine, tmp_path / "r.avi", profile="h264_mp4",
             export_path=str(out))
    assert probe.nframes(out) is not None        # the size-restore keeps xfade foldable
