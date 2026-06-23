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


@pytest.fixture
def ctl(qapp, tmp_path, monkeypatch):
    """A bare AppController (no MainWindow) for model/controller-level tests --
    fast, and free of the window's modal error dialogs."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    from moshit.gui.controller import AppController
    from moshit.engine import EngineConfig
    c = AppController(config=EngineConfig(width=64, height=48, fps=24.0, gop=8))
    yield c
    c.cleanup()


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
    waves = []
    c.preview_audio.connect(got.append)
    c.preview_waveform.connect(waves.append)
    run_op(lambda: c.refresh_preview())
    assert got and got[-1] and Path(got[-1]).exists()    # audio built from source
    assert waves and waves[-1] and max(waves[-1]) > 0     # waveform envelope built
    first = got[-1]
    run_op(lambda: c.refresh_preview())
    assert got[-1] == first                              # cached on unchanged plan
    c._preview_muted = True
    got.clear()
    waves.clear()
    run_op(lambda: c.refresh_preview())
    assert got and got[-1] is None                       # muted -> no audio
    assert waves and waves[-1] is None                   # muted -> no waveform
    c.cleanup()


def test_inspector_opacity_blend(qapp):
    from moshit.gui.widgets import InspectorPanel
    from moshit.project import Clip
    insp = InspectorPanel()
    got = []
    insp.clipPropsChanged.connect(got.append)
    insp._populate_clip_props(Clip(id="c", media_id="m", track="main",
                                   opacity=0.5, blend_mode="screen", gain=0.5))
    assert insp.opacity_spin.value() == 0.5
    assert insp.blend_combo.currentText() == "screen"
    assert insp.gain_spin.value() == 0.5
    insp.opacity_spin.setValue(0.25)                 # a control edit emits the props
    assert got and got[-1]["opacity"] == 0.25 and got[-1]["blend_mode"] == "screen"
    insp.gain_spin.setValue(2.0)
    assert got[-1]["gain"] == 2.0


def test_timeline_multitrack_lanes(qapp):
    from moshit.gui.widgets import TimelineWidget
    from moshit.project import (Project, Clip, MediaItem,
                                MAIN_TRACK_ID, MOTION_TRACK_ID)
    proj = Project()
    proj.media["m"] = MediaItem(id="m", source_path="x", label="m", role="main",
                                intermediate_path="x", nb_frames=20)
    proj.clips.append(Clip(id="a", media_id="m", track=MAIN_TRACK_ID))
    v2 = proj.add_track()
    proj.clips.append(Clip(id="c", media_id="m", track=v2.id,
                           opacity=0.5, blend_mode="screen"))
    proj.clips.append(Clip(id="mo", media_id="m", track=MOTION_TRACK_ID))
    tl = TimelineWidget()
    tl.set_sequence(proj.root_seq_id)
    tl.set_project(proj)
    # video tracks stack top-first; motion sits at the bottom
    assert [t.id for t in tl._lanes()] == [v2.id, MAIN_TRACK_ID, MOTION_TRACK_ID]
    from PySide6.QtGui import QPixmap          # paintEvent must not raise
    tl.resize(800, 240)
    tl.render(QPixmap(tl.size()))
    assert ("c", v2.id) in [(cid, tr) for _r, cid, tr in tl._hits]


def test_timeline_crossfade_overlap_layout(qapp):
    from moshit.gui.widgets import TimelineWidget
    from moshit.project import Project, Clip, MediaItem
    tl = TimelineWidget()
    proj = Project()
    proj.media["m"] = MediaItem(id="m", source_path="x", label="x", role="main",
                                intermediate_path="x", nb_frames=20)
    proj.clips.append(Clip(id="a", media_id="m", track="main"))
    proj.clips.append(Clip(id="b", media_id="m", track="main", start=20,
                           transition_in=8))           # butted up, legacy crossfade
    tl.set_project(proj)
    lay = [(c.id, start, length, trans) for c, start, length, trans
           in tl._main_layout()]
    assert lay == [("a", 0, 20, 0), ("b", 12, 20, 8)]   # b pulled back to overlap by 8
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


def test_undo_redo_round_trips(win):
    ctl = win.controller
    _seed_clip(ctl, "c")
    assert not ctl.can_undo
    ctl.add_effect("c", "bitrot", {"intensity": 0.4})
    assert [e["mode"] for e in ctl.clip_effects("c")] == ["bitrot"]
    assert ctl.can_undo and not ctl.can_redo
    ctl.undo()
    assert ctl.clip_effects("c") == [] and ctl.can_redo    # effect rolled back
    ctl.redo()
    assert [e["mode"] for e in ctl.clip_effects("c")] == ["bitrot"]


def test_split_clip_at_playhead(win):
    ctl = win.controller
    _seed_clip(ctl, "c")                                     # media nb_frames=20
    ctl.split_clip("c", 8)
    mains = ctl.project.main_clips()
    assert len(mains) == 2
    assert [ctl.project._clip_length(c) for c in mains] == [8, 12]
    assert ctl.can_undo
    ctl.undo()
    assert len(ctl.project.main_clips()) == 1                # back to one clip


def test_beat_positions_clip_to_span(win, monkeypatch):
    import moshit.beats as beats_mod
    ctl = win.controller
    _seed_clip(ctl, "a")
    _seed_clip(ctl, "b")                                 # a=[0,20], b=[20,40] frames
    ctl._audio_path_cache = "dummy.wav"                  # truthy: skip real audio
    fps = ctl.config.fps
    monkeypatch.setattr(beats_mod, "onsets",
                        lambda wav: [10 / fps, 30 / fps, 999.0])
    a, b = ctl.beat_positions("a"), ctl.beat_positions("b")
    assert len(a) == 1 and abs(a[0] - 0.5) < 1e-6        # the 10f onset, normalised
    assert len(b) == 1 and abs(b[0] - 0.5) < 1e-6        # the 30f onset (999s dropped)


def test_inspector_beat_fill(win):
    insp = win.inspector
    from moshit.gui.widgets import AutoParamWidget
    insp.set_beat_provider(lambda cid: [0.25, 0.5, 0.75])
    insp._clip_id = "c"
    insp.mode_combo.setCurrentText("pframe_duplicate")
    w = insp._param_widgets["factor"]
    assert isinstance(w, AutoParamWidget)
    insp._fill_beats(w)
    val = insp._getters["factor"]()
    assert isinstance(val, dict) and val["__auto__"] and val["interp"] == "hold"
    assert len([k for k in val["keys"] if len(k) >= 2]) >= 3   # a pulse per beat


def test_track_ops_and_undo(ctl):
    from moshit.project import MAIN_TRACK_ID
    _seed_clip(ctl, "c")                                  # media "m", clip on main
    root = ctl.project.root_seq_id
    t = ctl.add_video_track()
    assert [x.id for x in ctl.project.video_tracks(root)] == [MAIN_TRACK_ID, t.id]
    ctl.add_clip_for_media("m", t.id)
    new_clip = ctl.project.clips_for_track(t.id)[0]
    ctl.set_clip_props(new_clip.id, {"opacity": 0.5, "blend_mode": "screen"})
    c = ctl.project.clip(new_clip.id)
    assert c.opacity == 0.5 and c.blend_mode == "screen"

    ctl.undo()                                            # opacity/blend
    assert ctl.project.clip(new_clip.id).opacity == 1.0
    ctl.undo()                                            # add clip
    assert ctl.project.clips_for_track(t.id) == []
    ctl.undo()                                            # add track
    assert [x.id for x in ctl.project.video_tracks(root)] == [MAIN_TRACK_ID]


def test_precompose_and_sequence_switch(ctl):
    from moshit.project import Clip, MAIN_TRACK_ID
    _seed_clip(ctl, "c")                                  # media "m", clip on main
    ctl.project.clips.append(Clip(id="c2", media_id="m", track=MAIN_TRACK_ID,
                                  start=20))
    seq = ctl.precompose(["c2"], name="PC")
    assert seq is not None
    assert ctl.project.clip("c2").seq_id == seq.id and ctl.project.clip("c2").track \
        != MAIN_TRACK_ID
    # a precomp clip backed by the new sequence now sits on the root main track
    pc = [c for c in ctl.project.clips_for_track(MAIN_TRACK_ID)
          if ctl.project.media[c.media_id].sequence_id == seq.id]
    assert len(pc) == 1

    got = []
    ctl.sequence_changed.connect(lambda: got.append(ctl.current_seq_id))
    ctl.set_current_sequence(seq.id)
    assert ctl.current_seq_id == seq.id and got == [seq.id]

    ctl.set_current_sequence(ctl.project.root_seq_id)
    ctl.undo()                                            # undo the precompose
    assert ctl.project.clip("c2").track == MAIN_TRACK_ID


def test_move_clip_free_positioning(ctl):
    from moshit.project import MAIN_TRACK_ID
    _seed_clip(ctl, "c")                                  # clip "c" on main (0..20)
    second = ctl.add_clip_for_media("m", MAIN_TRACK_ID)   # butts up at 20
    assert second.start == 20
    ctl.move_clip(second.id, 30)                          # open a gap (free position)
    assert ctl.project.clip(second.id).start == 30
    ctl.undo()
    assert ctl.project.clip(second.id).start == 20
    # moving clears any legacy crossfade so the explicit position wins
    ctl.project.clip(second.id).transition_in = 5
    ctl.move_clip(second.id, 14)
    assert ctl.project.clip(second.id).start == 14
    assert ctl.project.clip(second.id).transition_in == 0


def test_cannot_remove_only_video_track(ctl):
    _seed_clip(ctl, "c")
    from moshit.project import MAIN_TRACK_ID
    ctl.remove_track(MAIN_TRACK_ID)                       # refused: it's the only one
    assert ctl.project.video_tracks(ctl.project.root_seq_id)
    t = ctl.add_video_track()
    ctl.remove_track(t.id)                                # now removable
    assert [x.id for x in ctl.project.video_tracks(ctl.project.root_seq_id)] \
        == [MAIN_TRACK_ID]


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
