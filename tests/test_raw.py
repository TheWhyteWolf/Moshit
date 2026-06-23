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
