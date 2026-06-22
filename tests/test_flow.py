"""Optical-flow motion transfer (needs the optional opencv+numpy `flow` extra;
the engine/project paths also need ffmpeg). All gated, so they skip cleanly."""
import os
import shutil

import pytest

cv2 = pytest.importorskip("cv2")
np = pytest.importorskip("numpy")

from moshit import flow  # noqa: E402

HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
requires_ffmpeg = pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg not on PATH")


def test_available_and_backend():
    assert flow.available()
    assert isinstance(flow.backend(), str) and flow.backend()


@pytest.mark.parametrize("use_opencl", [False, True])
def test_transfer_is_appearance_free(use_opencl):
    w, h = 64, 48
    base = np.zeros((h, w, 3), np.uint8)
    base[..., 0] = (np.arange(w) * 3 % 256).astype(np.uint8)        # red gradient
    motion = []
    for k in range(8):
        m = np.zeros((h, w, 3), np.uint8)
        m[12:36, 4 + k * 5:22 + k * 5, 2] = 255                     # blue block moving
        motion.append(m)

    out = flow.transfer_raw([base.tobytes(), base.tobytes()],
                            [m.tobytes() for m in motion], w, h,
                            strength=2.0, use_opencl=use_opencl)
    arrs = [np.frombuffer(b, np.uint8).reshape(h, w, 3) for b in out]
    assert len(arrs) == len(motion)
    assert (arrs[0] == base).all()                                  # frame 0 unwarped
    # the driver is pure blue; appearance-free => no green/blue in the output
    assert max(int(a[..., 1].max()) for a in arrs) == 0
    assert max(int(a[..., 2].max()) for a in arrs) == 0
    assert any((a != base).any() for a in arrs[1:])                 # warp happened


@requires_ffmpeg
def test_engine_optical_flow_transfer(engine, make_clip, tmp_path, probe):
    base = engine.normalize_clip(make_clip("base.mp4"), label="base")
    motion = engine.normalize_clip(make_clip("motion.mp4"), label="motion",
                                   single_keyframe=True)
    out = engine.optical_flow_transfer(base.source, motion.source,
                                       tmp_path / "w.avi", strength=1.5)
    assert out.exists() and probe.nframes(out) == len(motion.frames)


@requires_ffmpeg
def test_project_apply_and_revert(engine, project, make_clip):
    bm = project.import_media(engine, make_clip("base.mp4"), label="base",
                              role="main")
    mm = project.import_media(engine, make_clip("motion.mp4"), label="mot",
                              role="motion")
    clip = project.add_clip(bm.id, "main")
    rec = project.apply_optical_flow(engine, clip.id, mm.id, strength=1.5)
    assert project.clip(clip.id).archived
    assert project.media[rec.baked_media_id].derived
    assert len(project.main_clips()) == 1
    project.revert_bake(rec.id)
    assert project.clip(clip.id).enabled and rec.baked_media_id not in project.media


def test_flow_dialog_values():
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    try:
        from PySide6.QtWidgets import QApplication
        QApplication.instance() or QApplication([])
    except Exception as exc:
        pytest.skip(f"Qt unavailable: {exc}")
    from moshit.gui.app import FlowDialog
    dlg = FlowDialog(None, [("fire", "media_1"), ("base", "media_2")], "OpenCL: x")
    dlg.motion.setCurrentText("base")
    dlg.strength.setValue(2.0)
    dlg.hold.setChecked(False)
    media_id, params = dlg.values()
    assert media_id == "media_2"
    assert params["strength"] == 2.0 and params["hold"] is False
