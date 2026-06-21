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
    from moshit.gui.widgets import AutoParamWidget
    insp = win.inspector
    insp.mode_combo.setCurrentText("pframe_duplicate")
    w = insp._param_widgets["factor"]
    assert isinstance(w, AutoParamWidget)
    w.auto_chk.setChecked(True)
    w.start.setValue(1)
    w.end.setValue(3)
    value = insp._getters["factor"]()
    assert isinstance(value, dict) and value["keys"] == [[0.0, 1], [1.0, 3]]
