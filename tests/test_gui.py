"""GUI wiring smoke tests (need ffmpeg for the controller and a usable Qt
platform; both gated, so they skip cleanly where unavailable)."""
import shutil

import pytest

pytestmark = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
    reason="ffmpeg/ffprobe not on PATH")

pytest.importorskip("PySide6")


@pytest.fixture(scope="session")
def qapp():
    try:
        from PySide6.QtWidgets import QApplication
        return QApplication.instance() or QApplication([])
    except Exception as exc:                       # offscreen platform missing, etc.
        pytest.skip(f"Qt platform unavailable: {exc}")


@pytest.fixture
def win(qapp, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))   # isolate presets
    from moshit.gui.app import MainWindow
    w = MainWindow()
    yield w
    w.controller.cleanup()


def _seed_clip(ctl, clip_id="c"):
    from moshit.project import Clip, MediaItem
    ctl.project.media.setdefault("m", MediaItem(
        id="m", source_path="x", label="x", role="main",
        intermediate_path="x", nb_frames=20))
    ctl.project.clips.append(Clip(id=clip_id, media_id="m", track="main"))


def test_mainwindow_constructs(win):
    assert win.preview and win.timeline and win.inspector


def test_preview_audio_builds_and_mutes(qapp, tmp_path, monkeypatch):
    from pathlib import Path
    from PySide6.QtCore import QEventLoop, QTimer
    from moshit.gui.controller import AppController
    from moshit.engine import EngineConfig
    from moshit.ffmpeg import FFmpeg

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    ff = FFmpeg()
    src = tmp_path / "a.mp4"
    ff._run(["-f", "lavfi", "-i", "testsrc=size=128x96:rate=24:duration=0.5",
             "-f", "lavfi", "-i", "sine=frequency=440:duration=0.5",
             "-pix_fmt", "yuv420p", "-shortest", "-y", str(src)], "mk")
    c = AppController(config=EngineConfig(width=128, height=96, fps=24.0, gop=12))

    def run_op(call):
        loop = QEventLoop()
        c.busy.connect(lambda busy, _m: loop.quit() if not busy else None)
        QTimer.singleShot(40000, loop.quit)
        call()
        loop.exec()

    run_op(lambda: c.import_media(str(src), "main"))
    c.add_clip_for_media(list(c.project.media)[0], "main")
    got = []
    c.preview_audio.connect(got.append)
    run_op(lambda: c.refresh_preview())
    assert got and got[-1] and Path(got[-1]).exists()    # audio built from source
    first = got[-1]
    run_op(lambda: c.refresh_preview())
    assert got[-1] == first                              # cached on unchanged plan
    c._preview_muted = True
    got.clear()
    run_op(lambda: c.refresh_preview())
    assert got and got[-1] is None                       # muted -> no audio
    c.cleanup()


def test_timeline_crossfade_overlap_layout(qapp):
    from moshit.gui.widgets import TimelineWidget
    from moshit.project import Project, Clip, MediaItem
    tl = TimelineWidget()
    proj = Project()
    proj.media["m"] = MediaItem(id="m", source_path="x", label="x", role="main",
                                intermediate_path="x", nb_frames=20)
    proj.clips.append(Clip(id="a", media_id="m", track="main"))
    proj.clips.append(Clip(id="b", media_id="m", track="main", transition_in=8))
    tl.set_project(proj)
    lay = [(c.id, start, length, trans) for c, start, length, trans
           in tl._main_layout()]
    assert lay == [("a", 0, 20, 0), ("b", 12, 20, 8)]   # b overlaps a's tail by 8
    assert tl._main_length() == 32                       # 20 + 20 - 8 (matches render)


def test_effect_stack_region_and_pixel_fx(win):
    ctl = win.controller
    _seed_clip(ctl, "c")
    ctl.add_effect("c", "bitrot", {"intensity": 0.4}, region=[5, 10])
    ctl.add_effect("c", "pframe_duplicate", {"factor": 2})
    eff = ctl.clip_effects("c")
    assert [e["mode"] for e in eff] == ["bitrot", "pframe_duplicate"]
    assert eff[0]["region"] == (5, 10)
    ctl.add_pixel_fx("c", "rgb_shift")
    assert ctl.clip_pixel_fx("c")[0]["name"] == "rgb_shift"
    assert ctl.project.clip("c").has_finish()


def test_presets_save_and_apply(win):
    ctl = win.controller
    _seed_clip(ctl, "c1")
    _seed_clip(ctl, "c2")
    ctl.add_effect("c1", "bitrot", {"intensity": 0.4})
    assert ctl.save_stack_as_preset("c1", "p") and "p" in ctl.preset_names()
    ctl.apply_preset("c2", "p")
    assert [e["mode"] for e in ctl.clip_effects("c2")] == ["bitrot"]


def test_inspector_automation_control(win):
    from moshit.gui.widgets import AutoParamWidget, KeyframeDialog
    insp = win.inspector
    insp.mode_combo.setCurrentText("pframe_duplicate")
    w = insp._param_widgets["factor"]
    assert isinstance(w, AutoParamWidget)
    # enable automation -> a 2-point ramp from the current value
    w.value.setValue(2)
    w.auto_chk.setChecked(True)
    value = insp._getters["factor"]()
    assert isinstance(value, dict) and value["__auto__"] and len(value["keys"]) == 2

    # the keyframe dialog round-trips a multi-point curve with easing
    from moshit.modes.base import get_mode
    factor = next(p for p in get_mode("pframe_duplicate").params if p.name == "factor")
    dlg = KeyframeDialog(None, factor,
                         {"__auto__": True, "interp": "smooth",
                          "keys": [[0.0, 1], [0.5, 4], [1.0, 2]]})
    spec = dlg.values()
    assert spec["interp"] == "smooth" and len(spec["keys"]) == 3
    w.set_value(spec)
    out = w.get_value()
    assert out["interp"] == "smooth" and len(out["keys"]) == 3
