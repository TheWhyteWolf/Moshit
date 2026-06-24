"""Pixel sorting: numpy sort core (no ffmpeg) + full render path (ffmpeg-gated)."""
import shutil

import pytest

from moshit.modes.raw import available as numpy_available

requires_numpy = pytest.mark.skipif(not numpy_available(),
                                    reason="numpy not installed")
requires_ffmpeg = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
    reason="ffmpeg/ffprobe not on PATH")


# -- pure numpy core (dependency-light) ------------------------------------- #

@requires_numpy
def test_sort_orders_full_line_ascending():
    import numpy as np
    from moshit.modes.raw import _sort_frame
    vals = [200, 50, 120, 255, 10, 90]
    row = np.array([[[v, v, v] for v in vals]], np.uint8).tobytes()
    out = _sort_frame(row, 6, 1, vertical=False, by="brightness",
                      lo=0.0, hi=1.0, descending=False)
    got = list(np.frombuffer(out, np.uint8).reshape(1, 6, 3)[0, :, 0])
    assert got == sorted(vals)


@requires_numpy
def test_sort_descending():
    import numpy as np
    from moshit.modes.raw import _sort_frame
    vals = [200, 50, 120, 255, 10, 90]
    row = np.array([[[v, v, v] for v in vals]], np.uint8).tobytes()
    out = _sort_frame(row, 6, 1, vertical=False, by="brightness",
                      lo=0.0, hi=1.0, descending=True)
    got = list(np.frombuffer(out, np.uint8).reshape(1, 6, 3)[0, :, 0])
    assert got == sorted(vals, reverse=True)


@requires_numpy
def test_threshold_band_anchors_out_of_band_pixels():
    import numpy as np
    from moshit.modes.raw import _sort_frame
    vals = [200, 50, 120, 255, 10, 90]                  # 255 & 10 fall outside
    row = np.array([[[v, v, v] for v in vals]], np.uint8).tobytes()
    out = _sort_frame(row, 6, 1, vertical=False, by="brightness",
                      lo=40 / 255, hi=210 / 255, descending=False)
    got = list(np.frombuffer(out, np.uint8).reshape(1, 6, 3)[0, :, 0])
    # the [200,50,120] span sorts; 255 and 10 stay put; the lone 90 is its own span
    assert got == [50, 120, 200, 255, 10, 90]


@requires_numpy
def test_vertical_sorts_within_columns():
    import numpy as np
    from moshit.modes.raw import _sort_frame
    col = np.array([[[200, 200, 200]], [[10, 10, 10]], [[120, 120, 120]]],
                   np.uint8)                            # 3x1 column
    out = _sort_frame(col.tobytes(), 1, 3, vertical=True, by="brightness",
                      lo=0.0, hi=1.0, descending=False)
    got = list(np.frombuffer(out, np.uint8).reshape(3, 1, 3)[:, 0, 0])
    assert got == [10, 120, 200]


@requires_numpy
def test_mask_frames_luma_band():
    import numpy as np
    from moshit.modes.raw import mask_frames
    # a left-dark / right-bright frame; a luma matte should pass only the bright half
    img = np.zeros((4, 4, 3), np.uint8)
    img[:, 2:, :] = 255
    masks = mask_frames([img.tobytes()], 4, 4, {"source": "luma", "lo": 0.4, "hi": 0.6})
    m = masks[0]
    assert m[0, 0] < 0.1 and m[0, 3] > 0.9


@requires_numpy
def test_mask_frames_motion_and_alpha():
    import numpy as np
    from moshit.modes.raw import mask_frames
    a = np.zeros((4, 4, 3), np.uint8)
    b = np.full((4, 4, 3), 255, np.uint8)
    motion = mask_frames([a.tobytes(), b.tobytes()], 4, 4,
                         {"source": "motion", "lo": 0.05, "hi": 0.5})
    assert motion[1].mean() > 0.9                    # frame differs strongly -> hot
    # alpha on RGB frames is fully opaque (1.0 everywhere)
    alpha = mask_frames([a.tobytes()], 4, 4, {"source": "alpha"})
    assert float(alpha[0].min()) == 1.0


@requires_numpy
def test_chroma_mask_keys_the_color():
    import numpy as np
    from moshit.modes.raw import mask_frames, _parse_color
    assert list(_parse_color("#00ff00")) == [0.0, 1.0, 0.0]
    img = np.zeros((1, 2, 3), np.uint8)
    img[0, 0] = (0, 255, 0)                          # key color -> matte ~0
    img[0, 1] = (255, 0, 0)                          # far -> matte ~1
    m = mask_frames([img.tobytes()], 2, 1,
                    {"source": "chroma", "key": "#00ff00", "lo": 0.1, "hi": 0.5})[0]
    assert m[0, 0] < 0.1 and m[0, 1] > 0.9


@requires_numpy
def test_gate_island_blacks_outside_matte():
    import numpy as np
    from moshit.modes.raw import gate_island
    img = np.full((4, 4, 3), 200, np.uint8)
    img[:, 2:, :] = 0                                # right half dark -> luma ~0
    isl = gate_island([img.tobytes()], 4, 4, {"source": "luma", "lo": 0.3, "hi": 0.6})
    a = np.frombuffer(isl[0], np.uint8).reshape(4, 4, 3)
    assert a[0, 0].sum() > 300 and a[0, 3].sum() == 0  # bright kept, dark blacked


@requires_numpy
def test_overlay_spill_shows_spilled_content():
    import numpy as np
    from moshit.modes.raw import overlay_spill
    orig = np.full((4, 4, 3), 50, np.uint8)
    proc = np.zeros((4, 4, 3), np.uint8)
    proc[0, 3] = (240, 240, 240)                     # spilled bright pixel, mask dark there
    out = overlay_spill([orig.tobytes()], [proc.tobytes()], 4, 4,
                        {"source": "luma", "lo": 0.9, "hi": 1.0})  # ~no in-matte
    r = np.frombuffer(out[0], np.uint8).reshape(4, 4, 3)
    assert r[0, 3].sum() > 600                        # spilled content shows
    assert tuple(r[1, 1]) == (50, 50, 50)            # untouched stays original


@requires_numpy
def test_blend_masked_white_and_black():
    import numpy as np
    from moshit.modes.raw import blend_masked
    orig = np.full((4, 4, 3), 10, np.uint8).tobytes()
    proc = np.full((4, 4, 3), 200, np.uint8).tobytes()
    white = blend_masked([orig], [proc], 4, 4, {"source": "luma", "lo": 0.0, "hi": 0.0})
    black = blend_masked([orig], [proc], 4, 4, {"source": "luma", "lo": 1.0, "hi": 1.0})
    assert np.frombuffer(white[0], np.uint8)[0] > 190    # ~processed
    assert np.frombuffer(black[0], np.uint8)[0] < 20      # ~original


@requires_numpy
def test_sort_preserves_geometry_and_is_a_permutation():
    import numpy as np
    from moshit.modes.raw import _sort_frame
    rng = np.random.default_rng(0)
    img = rng.integers(0, 256, (8, 11, 3), dtype=np.uint8)
    out = _sort_frame(img.tobytes(), 11, 8, vertical=False, by="brightness",
                      lo=0.2, hi=0.9, descending=False)
    res = np.frombuffer(out, np.uint8).reshape(8, 11, 3)
    assert res.shape == img.shape
    # each row is a permutation of the original row's pixels (sorting only reorders)
    for r in range(8):
        a = {tuple(p) for p in img[r]}
        b = {tuple(p) for p in res[r]}
        assert a == b


# -- full render path ------------------------------------------------------- #

@requires_ffmpeg
@requires_numpy
def test_pixel_sort_renders_preserving_geometry(engine, project, make_clip,
                                                tmp_path, probe):
    src = make_clip("s.mp4")                            # testsrc, has detail
    m = project.import_media(engine, src, role="main")
    c = project.add_clip(m.id, "main")
    c.raw_effects = [{"name": "pixel_sort",
                      "params": {"axis": "vertical", "lo": 0.0, "hi": 1.0}}]
    assert c.has_finish()
    out = tmp_path / "sorted.avi"
    r = project.render(engine, out)
    assert r["frames"] == 24                            # count preserved
    assert probe.dims(out) == "160x120"                 # geometry preserved


@requires_ffmpeg
@requires_numpy
def test_pixel_sort_changes_pixels(engine, project, make_clip, tmp_path, probe):
    src = make_clip("s.mp4")
    m = project.import_media(engine, src, role="main")
    c = project.add_clip(m.id, "main")
    plain = tmp_path / "plain.avi"
    project.render(engine, plain)
    c.raw_effects = [{"name": "pixel_sort",
                      "params": {"axis": "horizontal", "lo": 0.0, "hi": 1.0}}]
    sorted_out = tmp_path / "sorted.avi"
    project.render(engine, sorted_out)
    assert (probe.pixel(plain, 10, 80, 60) != probe.pixel(sorted_out, 10, 80, 60))


@requires_ffmpeg
@requires_numpy
def test_pixel_sort_fx_matte_gates_the_sort(engine, project, make_clip, tmp_path,
                                            probe):
    # an fx_mask gates the raw sort too: black matte -> no sort, white -> full
    import moshit.project as P

    def render(mask, name, sort=True):
        proj = P.Project(name=name, config=engine.config,
                         assets_dir=str(tmp_path / ("a" + name)))
        m = proj.import_media(engine, make_clip("s.mp4"), role="main")
        c = proj.add_clip(m.id, "main")
        if sort:
            c.raw_effects = [{"name": "pixel_sort",
                              "params": {"axis": "vertical", "lo": 0.0, "hi": 1.0}}]
        c.fx_mask = mask
        out = tmp_path / (name + ".avi")
        proj.render(engine, out)
        return out

    def sample(p):
        return [probe.pixel(p, 12, x, y) for x in (40, 120) for y in (30, 90)]

    plain = render(None, "plain", sort=False)
    full = render(None, "full")
    black = render({"source": "luma", "lo": 1.0, "hi": 1.0}, "black")
    white = render({"source": "luma", "lo": 0.0, "hi": 0.0}, "white")

    def close(a, b):
        return all(max(abs(p - q) for p, q in zip(pa, pb)) < 24
                   for pa, pb in zip(sample(a), sample(b)))

    assert close(black, plain)                       # masked out -> untouched
    assert close(white, full)                        # masked in -> fully sorted
    assert not close(full, plain)                    # the sort really changes pixels


@requires_ffmpeg
@requires_numpy
def test_pixel_sort_composes_with_pixel_fx(engine, project, make_clip, tmp_path,
                                           probe):
    # raw sort (numpy) then an FFmpeg pixel filter in the same finish pass
    src = make_clip("s.mp4")
    m = project.import_media(engine, src, role="main")
    c = project.add_clip(m.id, "main")
    c.raw_effects = [{"name": "pixel_sort", "params": {"lo": 0.0, "hi": 1.0}}]
    c.pixel_effects = [{"name": "rgb_shift", "params": {"amount": 6}}]
    out = tmp_path / "out.mp4"
    r = project.render(engine, tmp_path / "r.avi", profile="h264_mp4",
                       export_path=str(out))
    assert probe.nframes(out) == 24 and probe.dims(out) == "160x120"
