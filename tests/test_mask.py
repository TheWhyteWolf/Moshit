"""Masking: layer mattes (compositor) and FX mattes (finish), ffmpeg-gated."""
import shutil

import pytest

pytestmark = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
    reason="ffmpeg/ffprobe not on PATH")


# -- layer matte (compositing) ---------------------------------------------- #

def test_layer_matte_reveals_lower_track(project, engine, make_clip, tmp_path,
                                         probe):
    # white clip over red, luma matte passing the bright (white) pixels -> white
    red = project.import_media(engine, make_clip("red.mp4", color="red"),
                               label="red", role="main")
    white = project.import_media(engine, make_clip("white.mp4", color="white"),
                                 label="white", role="main")
    project.add_clip(red.id, "main")
    v2 = project.add_track()
    cw = project.add_clip(white.id, v2.id)
    cw.layer_mask = {"source": "luma", "lo": 0.4, "hi": 0.6}
    out = tmp_path / "lm.avi"
    project.render(engine, out)
    px = probe.pixel(out, 12, 80, 60)
    assert px[0] > 200 and px[1] > 200 and px[2] > 200     # white shows


def test_layer_matte_invert_punches_to_lower_track(project, engine, make_clip,
                                                   tmp_path, probe):
    red = project.import_media(engine, make_clip("red.mp4", color="red"),
                               label="red", role="main")
    white = project.import_media(engine, make_clip("white.mp4", color="white"),
                                 label="white", role="main")
    project.add_clip(red.id, "main")
    v2 = project.add_track()
    cw = project.add_clip(white.id, v2.id)
    cw.layer_mask = {"source": "luma", "lo": 0.4, "hi": 0.6, "invert": True}
    out = tmp_path / "lmi.avi"
    project.render(engine, out)
    px = probe.pixel(out, 12, 80, 60)
    assert px[0] > 180 and px[1] < 80 and px[2] < 80       # red base revealed


def test_layer_mask_forces_composite_on_single_track(project, engine, make_clip,
                                                     tmp_path, probe):
    # a lone main-track clip with a matte must leave the fast path (compose to black)
    m = project.import_media(engine, make_clip("w.mp4", color="white"),
                             role="main")
    c = project.add_clip(m.id, "main")
    c.layer_mask = {"source": "luma", "lo": 0.9, "hi": 1.0, "invert": True}
    out = tmp_path / "solo.avi"
    project.render(engine, out)
    assert sum(probe.pixel(out, 12, 80, 60)) < 60          # masked out -> black


# -- FX matte (finish pass) ------------------------------------------------- #

def _fx_clip(project, engine, make_clip, mask):
    m = project.import_media(engine, make_clip("s.mp4"), role="main")
    c = project.add_clip(m.id, "main")
    c.pixel_effects = [{"name": "hue_rotate", "params": {"degrees": 180}}]
    c.fx_mask = mask
    return c


def test_fx_matte_white_equals_unmasked(project, engine, make_clip, tmp_path,
                                        probe):
    # an (almost) all-white matte applies the FX everywhere
    import moshit.project as P
    base = P.Project(name="b", config=engine.config,
                     assets_dir=str(tmp_path / "b"))
    mb = base.import_media(engine, make_clip("s.mp4"), role="main")
    cb = base.add_clip(mb.id, "main")
    cb.pixel_effects = [{"name": "hue_rotate", "params": {"degrees": 180}}]
    unmasked = tmp_path / "unmasked.avi"
    base.render(engine, unmasked)
    _fx_clip(project, engine, make_clip, {"source": "luma", "lo": 0.0, "hi": 0.0})
    white = tmp_path / "white.avi"
    project.render(engine, white)
    for x in (40, 120):
        a, b = probe.pixel(unmasked, 12, x, 60), probe.pixel(white, 12, x, 60)
        assert max(abs(p - q) for p, q in zip(a, b)) < 16


def test_fx_matte_black_equals_no_fx(project, engine, make_clip, tmp_path, probe):
    # an (almost) all-black matte applies no FX (matches the untouched clip)
    import moshit.project as P
    base = P.Project(name="p", config=engine.config,
                     assets_dir=str(tmp_path / "p"))
    mb = base.import_media(engine, make_clip("s.mp4"), role="main")
    base.add_clip(mb.id, "main")
    plain = tmp_path / "plain.avi"
    base.render(engine, plain)
    _fx_clip(project, engine, make_clip, {"source": "luma", "lo": 1.0, "hi": 1.0})
    black = tmp_path / "black.avi"
    project.render(engine, black)
    for x in (40, 120):
        a, b = probe.pixel(plain, 12, x, 60), probe.pixel(black, 12, x, 60)
        assert max(abs(p - q) for p, q in zip(a, b)) < 16


def test_fx_matte_preserves_geometry_and_count(project, engine, make_clip,
                                               tmp_path, probe):
    _fx_clip(project, engine, make_clip,
             {"source": "luma", "lo": 0.3, "hi": 0.7, "feather": 2})
    out = tmp_path / "g.avi"
    r = project.render(engine, out)
    assert r["frames"] == 24 and probe.dims(out) == "160x120"


def test_chroma_layer_matte_keys_color(project, engine, make_clip, tmp_path,
                                       probe):
    # green clip over red, chroma-key green -> green is removed, red base shows
    red = project.import_media(engine, make_clip("red.mp4", color="red"),
                               label="red", role="main")
    green = project.import_media(engine, make_clip("green.mp4", color="green"),
                                 label="green", role="main")
    project.add_clip(red.id, "main")
    v2 = project.add_track()
    cg = project.add_clip(green.id, v2.id)
    cg.layer_mask = {"source": "chroma", "key": "#008000", "lo": 0.2, "hi": 0.4}
    out = tmp_path / "ck.avi"
    project.render(engine, out)
    px = probe.pixel(out, 12, 80, 60)
    assert px[0] > 150 and px[1] < 90                  # red base revealed (green keyed)


def test_fx_matte_confine_vs_source_differ(project, engine, make_clip, tmp_path,
                                           probe):
    # a spreading FX through a partial matte differs between confine and source
    import moshit.project as P

    def render(mode, name):
        proj = P.Project(name=name, config=engine.config,
                         assets_dir=str(tmp_path / name))
        m = proj.import_media(engine, make_clip("s.mp4"), role="main")
        c = proj.add_clip(m.id, "main")
        c.pixel_effects = [{"name": "rgb_shift", "params": {"amount": 30}}]
        c.fx_mask = {"source": "luma", "lo": 0.35, "hi": 0.55, "mode": mode}
        out = tmp_path / (name + ".avi")
        proj.render(engine, out)
        return out

    confine, source = render("confine", "cf"), render("source", "sr")
    diff = sum(1 for x in range(0, 160, 4) for y in range(0, 120, 4)
               if max(abs(a - b) for a, b in
                      zip(probe.pixel(confine, 12, x, y),
                          probe.pixel(source, 12, x, y))) > 20)
    assert diff > 0                                    # overspill makes them differ


def test_raw_fx_source_mode_renders(engine, project, make_clip, tmp_path, probe):
    m = project.import_media(engine, make_clip("s.mp4"), role="main")
    c = project.add_clip(m.id, "main")
    c.raw_effects = [{"name": "pixel_sort", "params": {"lo": 0.0, "hi": 1.0}}]
    c.fx_mask = {"source": "luma", "lo": 0.3, "hi": 0.6, "mode": "source"}
    out = tmp_path / "rs.avi"
    r = project.render(engine, out)
    assert r["frames"] == 24 and probe.dims(out) == "160x120"


def test_motion_mask_renders(project, engine, make_clip, tmp_path, probe):
    # a motion-source matte exercises the tblend-difference path end to end
    m = project.import_media(engine, make_clip("s.mp4"), role="main")
    c = project.add_clip(m.id, "main")
    c.pixel_effects = [{"name": "rgb_shift", "params": {"amount": 8}}]
    c.fx_mask = {"source": "motion", "lo": 0.02, "hi": 0.3}
    out = tmp_path / "mo.avi"
    r = project.render(engine, out)
    assert r["frames"] == 24 and probe.dims(out) == "160x120"
