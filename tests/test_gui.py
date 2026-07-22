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


def test_preview_skips_redecode_when_unchanged(qapp, tmp_path, monkeypatch):
    from PySide6.QtCore import QEventLoop, QTimer
    from moshit.gui.controller import AppController
    from moshit.engine import EngineConfig
    from moshit.ffmpeg import FFmpeg
    from moshit.project import MoshOp, _new_id

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    ff = FFmpeg()
    src = tmp_path / "s.mp4"
    ff._run(["-f", "lavfi", "-i", "testsrc=size=128x96:rate=24:duration=0.5",
             "-pix_fmt", "yuv420p", "-y", str(src)], "mk")
    c = AppController(config=EngineConfig(width=128, height=96, fps=24.0, gop=12))

    def run_op(call):
        loop = QEventLoop()
        c.busy.connect(lambda busy, _m: loop.quit() if not busy else None)
        QTimer.singleShot(40000, loop.quit)
        call()
        loop.exec()

    calls = {"n": 0}
    orig = c.decoder.decode_stream
    monkeypatch.setattr(c.decoder, "decode_stream",
                        lambda *a, **k: (calls.__setitem__("n", calls["n"] + 1),
                                         orig(*a, **k))[1])

    run_op(lambda: c.import_media(str(src), "main"))
    c.add_clip_for_media(list(c.project.media)[0], "main")

    run_op(lambda: c.refresh_preview())
    assert calls["n"] == 1                               # first render decodes
    run_op(lambda: c.refresh_preview())
    assert calls["n"] == 1                               # byte-identical -> decode skipped

    cid = c.project.main_clips()[0].id                   # change the video output
    c.project.mosh_ops.append(MoshOp(id=_new_id("op"), mode="pframe_duplicate",
                                     params={"factor": 2}, target_clip_id=cid))
    run_op(lambda: c.refresh_preview())
    assert calls["n"] == 2                               # changed render re-decodes
    c.cleanup()


def test_cancel_resets_preview_skip_guard(ctl):
    """A cancelled decode must not let the next identical render skip on top of
    partial frames -- cancel() clears the byte-signature guard."""
    ctl._busy = True
    ctl._last_preview_sig = "deadbeef"
    ctl.cancel()
    assert ctl._last_preview_sig is None


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


def test_inspector_body_blank_until_clip_selected(qapp):
    # the inspector body stays hidden (panel blank) until a clip is selected
    from moshit.gui.widgets import InspectorPanel
    from moshit.project import Clip
    insp = InspectorPanel()
    assert insp._body.isHidden()                     # blank at init, no clip
    insp.set_enabled_for_clip("c", "myclip",
                              clip=Clip(id="c", media_id="m", track="main"),
                              effects=[])
    assert not insp._body.isHidden()                 # shown for the selected clip
    insp.set_enabled_for_clip(None, None)
    assert insp._body.isHidden()                     # blank again after deselect


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


def _mouse_ev(etype, x, y, *, button=None, buttons=None, mods=None):
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtGui import QMouseEvent
    button = Qt.MouseButton.LeftButton if button is None else button
    buttons = button if buttons is None else buttons
    mods = Qt.KeyboardModifier.NoModifier if mods is None else mods
    return QMouseEvent(etype, QPointF(x, y), QPointF(x, y), button, buttons, mods)


def _snap_timeline():
    """An 800px timeline with a(0..100) and b(110..210) for snap tests."""
    from PySide6.QtGui import QPixmap
    from moshit.gui.widgets import TimelineWidget
    from moshit.project import Project, Clip, MediaItem
    proj = Project()
    proj.media["m"] = MediaItem(id="m", source_path="x", label="m", role="main",
                                intermediate_path="x", nb_frames=100)
    proj.clips.append(Clip(id="a", media_id="m", track="main"))
    proj.clips.append(Clip(id="b", media_id="m", track="main", start=110))
    tl = TimelineWidget()
    tl.resize(800, 240)
    tl.set_project(proj)
    tl.render(QPixmap(tl.size()))                  # populate hit rects
    return tl


def test_timeline_move_snaps_to_neighbor_end(qapp):
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QPixmap
    tl = _snap_timeline()
    x0, _ = tl._track_x()
    ppf = tl._ppf()
    y = tl._lane_y(tl._lane_index("main")) + tl.LANE_H // 2
    press_x = x0 + 160 * ppf                       # b's body
    got = []
    tl.moveRequested.connect(lambda cid, start: got.append((cid, start)))
    tl.mousePressEvent(_mouse_ev(QEvent.Type.MouseButtonPress, press_x, y))
    assert tl._drag and tl._drag["mode"] == "move" and tl._drag["start"] == 110
    move_x = press_x - 8 * ppf                     # left edge lands ~2f from 100
    tl.mouseMoveEvent(_mouse_ev(QEvent.Type.MouseMove, move_x, y,
                                button=Qt.MouseButton.NoButton))
    assert tl._snap_frame == 100                   # snap engaged on a's end
    tl.render(QPixmap(tl.size()))                  # ghost + snap-line paint path
    tl.mouseReleaseEvent(_mouse_ev(QEvent.Type.MouseButtonRelease, move_x, y,
                                   buttons=Qt.MouseButton.NoButton))
    assert got == [("b", 100)]                     # butted exactly: melt chains
    assert tl._snap_frame is None


def test_timeline_move_alt_disables_snap(qapp):
    from PySide6.QtCore import QEvent, Qt
    tl = _snap_timeline()
    x0, _ = tl._track_x()
    ppf = tl._ppf()
    y = tl._lane_y(tl._lane_index("main")) + tl.LANE_H // 2
    press_x = x0 + 160 * ppf
    got = []
    tl.moveRequested.connect(lambda cid, start: got.append((cid, start)))
    tl.mousePressEvent(_mouse_ev(QEvent.Type.MouseButtonPress, press_x, y))
    move_x = press_x - 8 * ppf
    alt = Qt.KeyboardModifier.AltModifier
    tl.mouseMoveEvent(_mouse_ev(QEvent.Type.MouseMove, move_x, y,
                                button=Qt.MouseButton.NoButton, mods=alt))
    assert tl._snap_frame is None                  # Alt: no snap feedback
    tl.mouseReleaseEvent(_mouse_ev(QEvent.Type.MouseButtonRelease, move_x, y,
                                   buttons=Qt.MouseButton.NoButton, mods=alt))
    assert got == [("b", 102)]                     # free placement kept the gap


def test_timeline_trim_snaps_to_playhead(qapp):
    from PySide6.QtCore import QEvent, Qt
    tl = _snap_timeline()
    tl.set_play_fraction(205 / 210)                # playhead at frame 205
    rect = next(r for r, cid, _t in tl._hits if cid == "b")
    ppf = tl._ppf()
    y = rect.center().y()
    press_x = rect.right() - 1
    got = []
    tl.trimRequested.connect(lambda cid, i, o: got.append((cid, i, o)))
    tl.mousePressEvent(_mouse_ev(QEvent.Type.MouseButtonPress, press_x, y))
    assert tl._drag and tl._drag["mode"] == "trim_r"
    move_x = press_x - 4 * ppf                     # edge lands 1f past the playhead
    tl.mouseMoveEvent(_mouse_ev(QEvent.Type.MouseMove, move_x, y,
                                button=Qt.MouseButton.NoButton))
    assert tl._snap_frame == 205
    tl.mouseReleaseEvent(_mouse_ev(QEvent.Type.MouseButtonRelease, move_x, y,
                                   buttons=Qt.MouseButton.NoButton))
    assert got == [("b", -1, 95)]                  # out 100 -> 95 (edge 210 -> 205)


def test_timeline_melt_marker_and_fx_badge_scan(qapp):
    from PySide6.QtGui import QPixmap
    from moshit.gui.widgets import TimelineWidget
    from moshit.project import Project, Clip, MediaItem, MoshOp
    proj = Project()
    proj.media["m"] = MediaItem(id="m", source_path="x", label="m", role="main",
                                intermediate_path="x", nb_frames=50)
    for cid, start in (("a", 0), ("b", 50), ("c", 100), ("d", 150)):
        proj.clips.append(Clip(id=cid, media_id="m", track="main", start=start))
    melt = {"keep_first": False, "keep_every": 0}
    proj.mosh_ops += [
        MoshOp(id="o1", mode="iframe_removal", params=dict(melt),
               target_clip_id="b", region_start=0, region_end=1),   # melt head
        MoshOp(id="o2", mode="iframe_removal",                      # periodic keeps
               params={"keep_first": False, "keep_every": 4}, target_clip_id="c"),
        MoshOp(id="o3", mode="bitrot", params={}, target_clip_id="c"),
        MoshOp(id="o4", mode="iframe_removal", params=dict(melt),   # not at the head
               target_clip_id="d", region_start=3, region_end=4),
        MoshOp(id="o5", mode="iframe_removal", params=dict(melt),   # disabled
               target_clip_id="a", enabled=False),
    ]
    tl = TimelineWidget()
    tl.resize(800, 240)
    tl.set_project(proj)
    assert tl._melt_heads == {"b"}                 # only the true melt junction
    assert tl._fx_count == {"b": 1, "c": 2, "d": 1}
    tl.render(QPixmap(tl.size()))                  # badge + zigzag paint path


# -- junction (transition-area) selection ----------------------------------- #

def _junction_timeline(b_start=100):
    """An 800px timeline with a(0..100) and b at *b_start* (100 = butted hard
    cut, <100 = positional overlap)."""
    from PySide6.QtGui import QPixmap
    from moshit.gui.widgets import TimelineWidget
    from moshit.project import Project, Clip, MediaItem
    proj = Project()
    proj.media["m"] = MediaItem(id="m", source_path="x", label="m", role="main",
                                intermediate_path="x", nb_frames=100)
    proj.clips.append(Clip(id="a", media_id="m", track="main"))
    proj.clips.append(Clip(id="b", media_id="m", track="main", start=b_start))
    tl = TimelineWidget()
    tl.resize(800, 240)
    tl.set_project(proj)
    tl.render(QPixmap(tl.size()))                  # populate hit rects
    return tl


def test_timeline_hard_cut_tab_selects_junction(qapp):
    from PySide6.QtCore import QEvent
    tl = _junction_timeline(b_start=100)
    jhits = [j for j in tl._junction_hits if (j[1], j[2]) == ("a", "b")]
    assert jhits                                   # the butt cut has a click target
    got = []
    tl.junctionSelected.connect(lambda l, r: got.append((l, r)))
    c = jhits[0][0].center()
    tl.mousePressEvent(_mouse_ev(QEvent.Type.MouseButtonPress, c.x(), c.y()))
    assert got == [("a", "b")]
    assert tl._selected_junction == ("a", "b")
    assert tl.selected_ids() == [] and tl._drag is None
    # clicking a clip body afterwards drops the junction selection
    rect = next(r for r, cid, _t in tl._hits if cid == "a")
    tl.mousePressEvent(_mouse_ev(QEvent.Type.MouseButtonPress,
                                 rect.center().x(), rect.center().y()))
    assert tl._selected_junction is None and tl.selected_ids() == ["a"]


def test_timeline_overlap_band_selects_junction(qapp):
    from PySide6.QtCore import QEvent, Qt
    tl = _junction_timeline(b_start=60)            # 40-frame positional overlap
    band = next(j[0] for j in tl._junction_hits if j[0].height() == tl.LANE_H)
    got = []
    tl.junctionSelected.connect(lambda l, r: got.append((l, r)))
    c = band.center()
    tl.mousePressEvent(_mouse_ev(QEvent.Type.MouseButtonPress, c.x(), c.y()))
    assert got == [("a", "b")] and tl._selected_junction == ("a", "b")
    # Ctrl+click is a clip multi-select gesture: the junction must not steal it
    tl.mousePressEvent(_mouse_ev(QEvent.Type.MouseButtonPress, c.x(), c.y(),
                                 mods=Qt.KeyboardModifier.ControlModifier))
    assert tl._selected_junction is None and "b" in tl.selected_ids()


def test_timeline_junction_selection_prunes_when_seam_goes(qapp):
    tl = _junction_timeline(b_start=100)
    tl._selected_junction = ("a", "b")
    tl.set_project(tl._project)                    # still butted: kept
    assert tl._selected_junction == ("a", "b")
    tl._project.clip("b").start = 150              # dragged apart: a gap, no seam
    tl.set_project(tl._project)
    assert tl._selected_junction is None


def _seed_pair(ctl, b_start=20):
    from moshit.project import Clip, MediaItem
    ctl.project.media["m"] = MediaItem(id="m", source_path="x", label="m",
                                       role="main", intermediate_path="x",
                                       nb_frames=20)
    ctl.project.clips += [
        Clip(id="a", media_id="m", track="main"),
        Clip(id="b", media_id="m", track="main", start=b_start)]


def test_junction_info_shapes(ctl):
    _seed_pair(ctl, b_start=20)                    # butted: a hard cut
    info = ctl.junction_info("a", "b")
    assert info and info["span"] == 0 and info["track_id"] == "main"
    assert info["left_label"] == info["right_label"] == "m"
    assert ctl.junction_info("b", "a") is None     # order matters
    assert ctl.junction_info("a", "zz") is None    # unknown clip
    ctl.project.clip("b").start = 15               # positional overlap
    assert ctl.junction_info("a", "b")["span"] == 5
    ctl.project.clip("b").start = 30               # a gap is not a junction
    assert ctl.junction_info("a", "b") is None


def test_junction_effect_add_scopes_region_to_the_seam(ctl):
    _seed_pair(ctl, b_start=15)                    # 5-frame overlap
    op = ctl.begin_junction_effect_add("a", "b", "pframe_duplicate")
    ctl.end_effect_edit(op.id, commit=True)
    assert op.target_clip_id == "b" and op.at_cut
    assert (op.region_start, op.region_end) == (0, 5)
    assert [e["id"] for e in ctl.junction_effects("a", "b")] == [op.id]
    # the op is real: absent from a's stack, present in b's own stack
    assert ctl.clip_effects("a") == []
    assert [e["id"] for e in ctl.clip_effects("b")] == [op.id]
    assert ctl.undo_label == "Add pframe_duplicate at cut"
    ctl.undo()
    assert ctl.junction_effects("a", "b") == []


def test_junction_effect_add_hard_cut_default_window(ctl):
    from moshit.project import Clip
    _seed_pair(ctl, b_start=20)                    # butted: no overlap to bound
    op = ctl.begin_junction_effect_add("a", "b", "pframe_drop")
    ctl.end_effect_edit(op.id, commit=True)
    assert (op.region_start, op.region_end) == (0, ctl.HARD_CUT_SPAN)
    # a short incoming clip clamps the window to its own length
    ctl.project.clips.append(Clip(id="c", media_id="m", track="main",
                                  start=40, out_point=4))
    op2 = ctl.begin_junction_effect_add("b", "c", "pframe_drop")
    ctl.end_effect_edit(op2.id, commit=True)
    assert op2.region_end == 4
    # and a seam that no longer exists refuses the add
    ctl.project.clip("c").start = 99
    assert ctl.begin_junction_effect_add("b", "c", "bitrot") is None


def test_easy_mode_melt_shows_in_junction_stack(ctl):
    from moshit.project import MediaItem
    ctl.project.media["m"] = MediaItem(id="m", source_path="x", label="m",
                                       role="main", intermediate_path="x",
                                       nb_frames=20)
    ctl.easy_mode = True
    a = ctl.add_clip_for_media("m")
    b = ctl.add_clip_for_media("m")
    assert ctl.junction_info(a.id, b.id)["span"] == 0
    assert [e["mode"] for e in ctl.junction_effects(a.id, b.id)] \
        == ["iframe_removal"]


def test_junction_ops_survive_duplicate_and_paste(ctl):
    _seed_pair(ctl, b_start=15)                    # 5-frame overlap
    op = ctl.begin_junction_effect_add("a", "b", "pframe_duplicate")
    ctl.end_effect_edit(op.id, commit=True)
    dup = ctl.project.duplicate_clip("b")          # regions used to be dropped
    cop = ctl.project.clip_ops(dup.id)[0]
    assert (cop.region_start, cop.region_end, cop.at_cut) == (0, 5, True)
    ctl.copy_clips(["b"])
    new = ctl.paste_clips(at_frame=60)
    pop = ctl.project.clip_ops(new[0].id)[0]
    assert pop.at_cut and (pop.region_start, pop.region_end) == (0, 5)


def test_inspector_junction_mode_shows_only_the_stack(qapp):
    from moshit.gui.widgets import InspectorPanel
    from moshit.project import Clip
    insp = InspectorPanel()
    insp.set_enabled_for_junction("A", "B", 5, [
        {"id": "o1", "mode": "bitrot", "params": {}, "enabled": True,
         "region": (0, 5)}], "b")
    assert not insp._body.isHidden()
    assert insp._sec_clip.isHidden() and insp._clip_actions.isHidden()
    assert insp._sec_pixel.isHidden() and insp._sec_flow.isHidden()
    assert not insp._sec_effects.isHidden()
    assert not insp.preset_apply_btn.isEnabled()   # presets are clip-stack tools
    assert "Transition" in insp.clip_lbl.text()
    assert insp.effect_list.count() == 1 and insp._clip_id == "b"
    # selecting a clip afterwards restores the clip-level groups
    insp.set_enabled_for_clip("c", "clip",
                              clip=Clip(id="c", media_id="m", track="main"),
                              effects=[])
    assert not insp._sec_clip.isHidden() and not insp._clip_actions.isHidden()
    assert insp.preset_apply_btn.isEnabled()


def test_app_junction_effect_add_flow(win):
    from moshit.project import Clip, MediaItem
    c = win.controller
    c.project.media["m"] = MediaItem(id="m", source_path="x", label="m",
                                     role="main", intermediate_path="x",
                                     nb_frames=20)
    c.project.clips += [Clip(id="a", media_id="m", track="main"),
                        Clip(id="b", media_id="m", track="main", start=20)]
    win._on_junction_selected("a", "b")
    assert win._selected_junction == ("a", "b")
    assert win.inspector._junction and win.inspector._clip_id == "b"
    win._on_effect_add_begin("pframe_drop")        # junction-scoped add
    dlg = win.inspector._live_dlg
    assert dlg is not None
    dlg.accept()                                   # commit the default params
    assert [e["mode"] for e in c.junction_effects("a", "b")] == ["pframe_drop"]
    op = c.project.clip_ops("b")[0]
    assert op.at_cut and (op.region_start, op.region_end) == (0, c.HARD_CUT_SPAN)
    # removing the left clip kills the seam; the project change clears the
    # selection and blanks the inspector instead of showing a stale stack
    c.remove_clip("a")
    assert win._selected_junction is None
    assert win.inspector._body.isHidden()


def _drain(qapp, c, timeout_s=90):
    """Pump the event loop until the controller's background job finishes."""
    import time
    t0 = time.time()
    while c.is_busy and time.time() - t0 < timeout_s:
        qapp.processEvents()
        time.sleep(0.01)
    qapp.processEvents()


def test_import_media_batch_two_files(qapp, ctl, make_clip):
    a, b = make_clip("a.mp4", color="red"), make_clip("b.mp4", color="blue")
    added, changed = [], []
    ctl.media_added.connect(added.append)
    ctl.project_changed.connect(lambda: changed.append(1))
    ctl.import_media_batch([a, b])
    _drain(qapp, ctl)
    assert [m.label for m in added] == ["a", "b"]
    assert len(ctl.project.media) == 2
    assert len(changed) == 1                       # one refresh for the batch


def test_import_media_batch_collects_errors(qapp, ctl, make_clip, tmp_path):
    good = make_clip("good.mp4", color="red")
    errors, added = [], []
    ctl.error.connect(errors.append)
    ctl.media_added.connect(added.append)
    ctl.import_media_batch([good, tmp_path / "missing.mp4"])
    _drain(qapp, ctl)
    assert [m.label for m in added] == ["good"]    # the good file still lands
    assert len(errors) == 1 and "missing.mp4" in errors[0]
    assert not ctl.is_busy                         # the job completed cleanly


def test_place_clip_at_video_track_and_undo(ctl):
    from moshit.project import MediaItem
    for mid in ("m1", "m2"):
        ctl.project.media[mid] = MediaItem(
            id=mid, source_path="x", label=mid, role="main",
            intermediate_path="x", nb_frames=20)
    first = ctl.add_clip_for_media("m1", "main")
    ctl.set_easy_mode(True)
    placed = ctl.place_clip_at("m2", "main", 37)
    assert placed.start == 37
    ops = ctl.project.clip_ops(placed.id)          # occupied track: melt op
    assert [o.mode for o in ops] == ["iframe_removal"]
    assert (ops[0].region_start, ops[0].region_end) == (0, 1)
    ctl.undo()                                     # clip + op = one undo step
    assert all(c.id != placed.id for c in ctl.project.clips)
    assert ctl.project.clip_ops(placed.id) == []
    assert any(c.id == first.id for c in ctl.project.clips)
    m = ctl.place_clip_at("m1", "motion", 55)      # motion pool: append, no melt
    assert m is not None and ctl.project.clip_ops(m.id) == []
    assert ctl.place_clip_at("m1", "no_such_track", 0) is None


def test_media_list_mime_payload(qapp):
    from moshit.gui.widgets import MEDIA_MIME, MediaLibrary
    from moshit.project import MediaItem
    lib = MediaLibrary()
    lib.add_media(MediaItem(id="med_1", source_path="x", label="clip",
                            role="main", intermediate_path="x", nb_frames=9))
    mime = lib.list.mimeData([lib.list.item(0)])
    assert bytes(mime.data(MEDIA_MIME)).decode() == "med_1"


def _media_drag_events(x, y):
    """(mime, enter, move, drop) carrying media id 'm' at widget pos (x, y).
    The caller must keep *mime* alive — the events only borrow it."""
    from PySide6.QtCore import QMimeData, QPoint, QPointF, Qt
    from PySide6.QtGui import QDragEnterEvent, QDragMoveEvent, QDropEvent
    from moshit.gui.widgets import MEDIA_MIME
    mime = QMimeData()
    mime.setData(MEDIA_MIME, b"m")
    args = (Qt.DropAction.CopyAction, mime, Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier)
    return (mime,
            QDragEnterEvent(QPoint(int(x), int(y)), *args),
            QDragMoveEvent(QPoint(int(x), int(y)), *args),
            QDropEvent(QPointF(x, y), *args))


def test_timeline_drop_places_at_snapped_frame(qapp):
    from PySide6.QtGui import QPixmap
    tl = _snap_timeline()
    x0, _ = tl._track_x()
    ppf = tl._ppf()
    y = tl._lane_y(tl._lane_index("main")) + 20
    x = x0 + 99 * ppf                              # 1f short of a's end
    mime, enter, move, drop = _media_drag_events(x, y)
    got = []
    tl.mediaDroppedOnTrack.connect(lambda m, t, f: got.append((m, t, f)))
    tl.dragEnterEvent(enter)
    assert enter.isAccepted()
    tl.dragMoveEvent(move)
    assert tl._drop_hover == ("main", 100, 100)    # snapped onto the junction
    assert tl._snap_frame == 100
    tl.render(QPixmap(tl.size()))                  # insertion-ghost paint path
    tl.dropEvent(drop)
    assert got == [("m", "main", 100)]
    assert tl._drop_hover is None and tl._snap_frame is None
    # hovering off every lane clears the ghost and rejects the position
    mime2, _enter2, move2, _drop2 = _media_drag_events(x, 2)   # in the ruler
    tl.dragMoveEvent(move2)
    assert tl._drop_hover is None and not move2.isAccepted()


def test_timeline_url_drag_accepts_and_emits_files(qapp, tmp_path):
    from PySide6.QtCore import QMimeData, QPoint, QPointF, Qt, QUrl
    from PySide6.QtGui import QDragEnterEvent, QDropEvent
    tl = _snap_timeline()
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(tmp_path / "x.mp4")),
                  QUrl.fromLocalFile(str(tmp_path / "notes.txt"))])
    args = (Qt.DropAction.CopyAction, mime, Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier)
    got = []
    tl.filesDropped.connect(got.append)
    enter = QDragEnterEvent(QPoint(100, 60), *args)
    tl.dragEnterEvent(enter)
    assert enter.isAccepted()                      # video files import from here
    tl.dropEvent(QDropEvent(QPointF(100, 60), *args))
    assert got == [[str(tmp_path / "x.mp4")]]      # non-video filtered out


def test_window_drop_imports_videos(win, monkeypatch, tmp_path):
    from PySide6.QtCore import QMimeData, QPoint, QPointF, Qt, QUrl
    from PySide6.QtGui import QDragEnterEvent, QDropEvent
    calls = []
    monkeypatch.setattr(win.controller, "import_media_batch",
                        lambda paths, role="any": calls.append(list(paths)))
    vids = QMimeData()
    vids.setUrls([QUrl.fromLocalFile(str(tmp_path / "a.mp4")),
                  QUrl.fromLocalFile(str(tmp_path / "b.txt"))])
    args = (Qt.DropAction.CopyAction, vids, Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier)
    enter = QDragEnterEvent(QPoint(50, 50), *args)
    win.dragEnterEvent(enter)
    assert enter.isAccepted()
    win.dropEvent(QDropEvent(QPointF(50, 50), *args))
    assert calls == [[str(tmp_path / "a.mp4")]]
    txt = QMimeData()
    txt.setUrls([QUrl.fromLocalFile(str(tmp_path / "b.txt"))])
    enter2 = QDragEnterEvent(QPoint(50, 50), Qt.DropAction.CopyAction, txt,
                             Qt.MouseButton.LeftButton,
                             Qt.KeyboardModifier.NoModifier)
    win.dragEnterEvent(enter2)
    assert not enter2.isAccepted()                 # nothing importable


def _timeline_in_pane(qapp, nb_frames=40):
    """A TimelineWidget with one clip, hosted in a shown TimelinePane."""
    from moshit.gui.widgets import TimelineWidget, TimelinePane
    from moshit.project import Project, Clip, MediaItem
    proj = Project()
    proj.media["m"] = MediaItem(id="m", source_path="x", label="m", role="main",
                                intermediate_path="x", nb_frames=nb_frames)
    proj.clips.append(Clip(id="a", media_id="m", track="main"))
    tl = TimelineWidget()
    tl.set_project(proj)
    pane = TimelinePane(tl)
    pane.resize(800, 240)
    pane.show()
    qapp.processEvents()
    return tl, pane


def test_timeline_pane_zoom_scroll_and_fit(qapp):
    from PySide6.QtGui import QPixmap
    tl, pane = _timeline_in_pane(qapp)
    ppf0 = tl._ppf()
    assert pane.zoom() == 1.0
    assert pane.horizontalScrollBar().maximum() == 0    # fit = nothing to scroll

    pane.set_zoom(4.0)
    qapp.processEvents()
    assert pane.zoom() == 4.0
    assert tl.width() >= pane.viewport().width() * 4 - 2
    assert tl._ppf() > 3 * ppf0            # gutter is fixed px, so not exactly 4x
    assert pane.horizontalScrollBar().maximum() > 0

    # zoomed + scrolled paint (visible-bounded ruler/waveform/sticky labels)
    tl.set_waveform([0.5] * 64)
    hbar = pane.horizontalScrollBar()
    hbar.setValue(hbar.maximum() // 2)
    tl.render(QPixmap(tl.size()))

    pane.set_zoom(0.25)                                 # clamped at both ends
    assert pane.zoom() == 1.0
    pane.set_zoom(10_000)
    assert pane.zoom() == pane.MAX_ZOOM

    pane.zoom_fit()
    qapp.processEvents()
    assert pane.zoom() == 1.0
    assert pane.horizontalScrollBar().value() == 0
    pane.hide()


def test_timeline_pane_zoom_anchor_keeps_frame(qapp):
    tl, pane = _timeline_in_pane(qapp)
    pane.set_zoom(2.0)
    qapp.processEvents()
    hbar = pane.horizontalScrollBar()
    x0 = tl.PAD + tl.LABEL_W
    anchor = hbar.value() + 300            # timeline x under viewport column 300
    f_before = (anchor - x0) / tl._ppf()
    pane.set_zoom(8.0, anchor_x=anchor)
    qapp.processEvents()
    f_after = (hbar.value() + 300 - x0) / tl._ppf()
    assert abs(f_before - f_after) <= 1.5  # same frame stays under the cursor
    pane.hide()


def test_timeline_wheel_zooms_and_pans(qapp):
    from PySide6.QtCore import QPoint, QPointF, Qt
    from PySide6.QtGui import QWheelEvent

    def wheel(widget, mods, dy):
        ev = QWheelEvent(QPointF(400, 30), QPointF(400, 30), QPoint(0, 0),
                         QPoint(0, dy), Qt.MouseButton.NoButton, mods,
                         Qt.ScrollPhase.NoScrollPhase, False)
        widget.wheelEvent(ev)

    tl, pane = _timeline_in_pane(qapp)
    wheel(tl, Qt.KeyboardModifier.ControlModifier, 120)   # ctrl+wheel = zoom in
    assert pane.zoom() == pytest.approx(1.25)
    wheel(tl, Qt.KeyboardModifier.ControlModifier, 120)
    qapp.processEvents()
    hbar = pane.horizontalScrollBar()
    assert hbar.maximum() > 0
    v0 = hbar.value()
    wheel(tl, Qt.KeyboardModifier.NoModifier, -120)       # plain wheel = pan right
    assert hbar.value() > v0 or v0 == hbar.maximum()
    wheel(tl, Qt.KeyboardModifier.ControlModifier, -120)  # ctrl+wheel down = out
    assert pane.zoom() == pytest.approx(1.25)
    pane.hide()


def test_timeline_pane_pages_to_follow_playhead(qapp):
    tl, pane = _timeline_in_pane(qapp)
    pane.set_zoom(8.0)
    qapp.processEvents()
    hbar = pane.horizontalScrollBar()
    hbar.setValue(0)
    tl.set_play_fraction(0.9)              # playback carries playhead off-screen
    assert hbar.value() > 0
    v = hbar.value()
    tl.set_play_fraction(0.9)              # still visible: no chase-scrolling
    assert hbar.value() == v
    pane.hide()


def test_mainwindow_timeline_zoom_controls(win, qapp):
    win.resize(1100, 700)
    qapp.processEvents()
    win.timeline_pane.zoom_in()
    assert win.timeline_pane.zoom() == pytest.approx(1.5)
    assert win.zoom_label.text() == "1.5×"
    win.timeline_pane.zoom_fit()
    assert win.timeline_pane.zoom() == 1.0
    assert win.zoom_label.text() == "1.0×"


def test_quick_save_and_title(win, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QFileDialog
    assert win.windowTitle() == "untitled[*] — Moshit"

    # never-saved: Ctrl+S falls through to Save As (one dialog, path recorded)
    p1 = tmp_path / "myproj.json"
    monkeypatch.setattr(QFileDialog, "getSaveFileName",
                        staticmethod(lambda *a, **k: (str(p1), "Project (*.json)")))
    win._set_dirty(True)
    assert win._save_project()
    assert p1.exists()
    assert win._project_path == str(p1)
    assert win.windowTitle() == "myproj[*] — Moshit"
    assert not win._dirty

    # saved once: quick save writes in place with NO dialog
    def _boom(*a, **k):
        raise AssertionError("quick save must not open a file dialog")
    monkeypatch.setattr(QFileDialog, "getSaveFileName", staticmethod(_boom))
    win._set_dirty(True)
    mtime = p1.stat().st_mtime_ns
    assert win._save_project()
    assert p1.stat().st_mtime_ns >= mtime and not win._dirty

    # Save As redirects the project path
    p2 = tmp_path / "renamed.json"
    monkeypatch.setattr(QFileDialog, "getSaveFileName",
                        staticmethod(lambda *a, **k: (str(p2), "Project (*.json)")))
    assert win._save_project_as()
    assert win._project_path == str(p2)
    assert win.windowTitle() == "renamed[*] — Moshit"


def test_open_project_sets_path_and_new_clears_it(win, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QFileDialog
    p = tmp_path / "roundtrip.json"
    win.controller.save_project(str(p))
    monkeypatch.setattr(QFileDialog, "getOpenFileName",
                        staticmethod(lambda *a, **k: (str(p), "Project (*.json)")))
    win._open_project()
    assert win._project_path == str(p)
    assert win.windowTitle() == "roundtrip[*] — Moshit"

    # New project forgets the path so the next Ctrl+S prompts again
    from PySide6.QtWidgets import QDialog
    from moshit.gui.app import ProjectSettingsDialog
    monkeypatch.setattr(ProjectSettingsDialog, "exec",
                        lambda self: QDialog.DialogCode.Accepted)
    win._new_project()
    assert win._project_path is None
    assert win.windowTitle() == "untitled[*] — Moshit"


def test_recent_projects_and_dir_memory(win, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QFileDialog, QMessageBox
    monkeypatch.setattr(QMessageBox, "warning",
                        staticmethod(lambda *a, **k: None))
    p1, p2 = tmp_path / "one.json", tmp_path / "two.json"
    monkeypatch.setattr(QFileDialog, "getSaveFileName",
                        staticmethod(lambda *a, **k: (str(p1), "")))
    assert win._save_project()
    monkeypatch.setattr(QFileDialog, "getSaveFileName",
                        staticmethod(lambda *a, **k: (str(p2), "")))
    assert win._save_project_as()
    assert win._recent_projects()[:2] == [str(p2), str(p1)]   # most recent first
    assert win._save_project()                                # re-save dedups
    assert win._recent_projects().count(str(p2)) == 1

    labels = [a.text() for a in win._recent_menu.actions()]
    assert "two.json" in labels and "one.json" in labels

    assert win._start_dir("project") == str(tmp_path)         # last-dir memory

    win._open_recent(str(p1))                                 # loads, no dialog
    assert win._project_path == str(p1)
    assert win._recent_projects()[0] == str(p1)

    p2.unlink()                                               # vanished → pruned
    win._open_recent(str(p2))
    assert str(p2) not in win._recent_projects()
    assert win._project_path == str(p1)                       # unchanged


def test_window_state_persists_across_sessions(qapp, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    from moshit.gui.app import MainWindow
    w1 = MainWindow()
    w1.resize(640, 480)
    w1._save_ui_state()
    w1.controller.cleanup()
    w2 = MainWindow()
    try:
        assert (w2.width(), w2.height()) == (640, 480)
    finally:
        w2.controller.cleanup()


def _seed_offline_media(win, mid="ghost"):
    from moshit.project import MediaItem
    win.controller.project.media[mid] = MediaItem(
        id=mid, source_path="x", label=mid, role="main",
        intermediate_path="/definitely/not/here.avi", nb_frames=10)
    return mid


def test_library_offline_badge(win):
    mid = _seed_offline_media(win)
    win._reload_library()
    texts = [win.library.list.item(i).text()
             for i in range(win.library.list.count())]
    assert any("offline" in t for t in texts)
    assert [m.id for m in win.controller.missing_media()] == [mid]


def test_relink_flow_and_open_prompt(win, monkeypatch, tmp_path):
    from PySide6.QtWidgets import QFileDialog, QMessageBox
    mid = _seed_offline_media(win)
    picked = tmp_path / "new.mp4"
    picked.write_bytes(b"x")
    monkeypatch.setattr(QFileDialog, "getOpenFileName",
                        staticmethod(lambda *a, **k: (str(picked), "")))
    calls = {}
    monkeypatch.setattr(win.controller, "relink_media",
                        lambda mapping: calls.update(mapping))

    # the on-open prompt routes into the relink flow on Yes
    monkeypatch.setattr(
        QMessageBox, "question",
        staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes))
    win._offer_relink()
    assert calls == {mid: str(picked)}

    # declining leaves everything untouched
    calls.clear()
    monkeypatch.setattr(
        QMessageBox, "question",
        staticmethod(lambda *a, **k: QMessageBox.StandardButton.No))
    win._offer_relink()
    assert calls == {}

    # no offline media → the menu action is a friendly no-op
    win.controller.project.media.clear()
    win._relink_offline_media()
    assert calls == {}


def test_friendly_error_mapping(qapp):
    from moshit.gui.controller import _friendly_error
    from moshit.ffmpeg import FFmpegError

    msg = _friendly_error(FileNotFoundError(2, "No such file", "media_1.avi"))
    assert "media_1.avi" in msg and "Relink" in msg

    hinted = _friendly_error(FFmpegError(
        "finish failed (exit 1):\n/x/gone.avi: No such file or directory"))
    assert hinted.startswith("finish failed") and "Relink" in hinted

    plain = _friendly_error(FFmpegError("encode failed (exit 1):\nbad option"))
    assert plain == "encode failed (exit 1):\nbad option"

    assert _friendly_error(ValueError("boom")) == "boom"


def test_error_shows_toast_not_dialog(win, monkeypatch, qapp):
    from PySide6.QtWidgets import QMessageBox

    def _boom(*a, **k):
        raise AssertionError("errors must not open a modal dialog")
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(_boom))

    win.show()
    win.controller.error.emit("render failed (exit 1):\nsome ffmpeg detail")
    assert win._toast.isVisible()
    assert "some ffmpeg detail" in win._toast.text()
    assert win.statusBar().currentMessage() == "render failed (exit 1):"
    win._toast.mousePressEvent(None)               # click dismisses
    assert not win._toast.isVisible()
    win.hide()


def test_progress_bar_and_rendering_badge(win, qapp):
    win.show()
    win._on_busy(True, "Rendering preview…")
    assert win.progress.isVisible() and win.progress.maximum() == 0   # indeterminate
    assert win.preview._busy_badge.isVisible()
    assert win.preview._busy_badge.text() == "Rendering preview…"

    win._on_progress(2, 5, "Rendering clip 3/5…")     # render steps arrive
    assert (win.progress.maximum(), win.progress.value()) == (5, 2)
    assert win.statusBar().currentMessage() == "Rendering clip 3/5…"

    win._on_stream_begin(100, 24.0)                   # decode phase takes over
    win._on_stream_batch([object()] * 30)
    win._on_stream_batch([object()] * 30)
    assert (win.progress.maximum(), win.progress.value()) == (100, 60)

    win._on_busy(False, "")
    assert not win.progress.isVisible()
    assert not win.preview._busy_badge.isVisible()
    win.hide()


def test_preview_frames_stored_compressed(qapp, tmp_path):
    import subprocess
    from moshit.gui.preview import PreviewDecoder
    from moshit.gui.widgets import PreviewWidget
    src = tmp_path / "t.avi"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", "testsrc=size=160x120:rate=12:duration=1",
                    "-c:v", "mpeg4", "-y", str(src)], check=True)
    dec = PreviewDecoder()
    frames, fps, (w, h) = dec.decode(src, max_width=160)
    assert frames and all(isinstance(f, bytes) for f in frames)
    assert all(f[:2] == b"\xff\xd8" for f in frames)       # JPEG magic
    raw_total = w * h * 3 * len(frames)
    assert sum(len(f) for f in frames) < 0.6 * raw_total   # actually smaller

    pv = PreviewWidget()                                   # decodes on demand
    pv.set_frames(frames, fps)
    img = pv.current_image()
    assert img is not None and (img.width(), img.height()) == (w, h)
    assert pv.frame_count() == len(frames)


def _preview_with_frames(n=10, w=8, h=8, fps=24.0):
    from moshit.gui.preview import encode_preview_frame
    from moshit.gui.widgets import PreviewWidget
    pv = PreviewWidget()
    pv.set_frames([encode_preview_frame(bytes([i * 20]) * (w * h * 3), w, h)
                   for i in range(n)], fps)
    return pv


def test_preview_loop_range_wraps(qapp):
    from PySide6.QtGui import QPixmap
    pv = _preview_with_frames(10)
    pv.seek_to(2)
    pv.set_loop_in()                                # I at frame 2 (open end)
    pv.seek_to(5)
    pv.set_loop_out()                               # O at frame 5
    assert pv.loop_range() == (2, 5)
    assert pv.loop_chk.isChecked()                  # a range implies looping
    pv.slider.resize(200, 20)                       # band paint path
    pv.slider.render(QPixmap(pv.slider.size()))
    pv.seek_to(5)
    pv._advance()
    assert pv.current_index() == 2                  # wrapped inside the range
    pv.loop_chk.setChecked(False)
    pv.seek_to(5)
    pv._advance()
    assert pv.current_index() == 6                  # loop off: plays through
    pv.loop_chk.setChecked(True)
    pv.clear_loop()
    pv.seek_to(9)
    pv._advance()
    assert pv.current_index() == 0                  # whole-clip wrap fallback
    pv.seek_to(8)
    pv.set_loop_in()                                # reversed marks normalize
    pv.seek_to(3)
    pv.set_loop_out()
    assert pv.loop_range() == (3, 8)
    pv.set_frames(pv._frames[:5], 24.0)             # shorter re-render
    assert pv.loop_range() is None                  # stale range dropped


def test_preview_loop_keys(win, qapp):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from moshit.gui.preview import encode_preview_frame
    win.show()                                     # window shortcuts need focus
    win.activateWindow()
    qapp.processEvents()
    frames = [encode_preview_frame(bytes(8 * 8 * 3), 8, 8) for _ in range(6)]
    win.preview.set_frames(frames, 24.0)
    win.preview.seek_to(1)
    QTest.keyClick(win, Qt.Key.Key_I)
    win.preview.seek_to(4)
    QTest.keyClick(win, Qt.Key.Key_O)
    assert win.preview.loop_range() == (1, 4)
    QTest.keyClick(win, Qt.Key.Key_I, Qt.KeyboardModifier.ShiftModifier)
    assert win.preview.loop_range() is None


def test_source_frame_mapping(ctl):
    from moshit.project import Clip, MediaItem
    assert ctl.source_frame_for(0) is None          # empty timeline
    ctl.project.media["m"] = MediaItem(id="m", source_path="x", label="m",
                                       role="main", intermediate_path="x",
                                       nb_frames=100)
    b = Clip(id="b", media_id="m", track="main", start=100,
             in_point=10, out_point=60)
    ctl.project.clips += [Clip(id="a", media_id="m", track="main"), b]
    assert ctl.source_frame_for(0) == ("m", 0)
    assert ctl.source_frame_for(99) == ("m", 99)
    assert ctl.source_frame_for(105) == ("m", 15)   # trim offsets the source
    b.speed = 2.0
    assert ctl.source_frame_for(110) == ("m", 30)   # 10 + 10×2
    b.speed, b.reverse = 1.0, True
    assert ctl.source_frame_for(105) == ("m", 54)   # 60-1-5
    b.reverse = False
    c = Clip(id="c", media_id="m", track="main", start=140)
    ctl.project.clips.append(c)
    assert ctl.source_frame_for(145) == ("m", 5)    # overlap: incoming clip wins
    assert ctl.source_frame_for(500) is None        # past the end


def test_fetch_source_frame_caches(qapp, ctl, make_clip):
    item = ctl._do_import(make_clip("src.mp4"), "main")
    calls = []
    orig = ctl._ab_ff.snapshot
    ctl._ab_ff.snapshot = lambda *a, **k: (calls.append(1), orig(*a, **k))[1]
    img1 = ctl.fetch_source_frame(item.id, 3)
    img2 = ctl.fetch_source_frame(item.id, 3)
    assert img1 is not None and not img1.isNull()
    assert img2 is img1 and calls == [1]            # second hit from the cache
    assert (img1.width(), img1.height()) == (64, 48)
    assert ctl.fetch_source_frame("nope", 0) is None


def test_preview_source_override(qapp):
    from PySide6.QtGui import QImage
    pv = _preview_with_frames(6)
    pv.resize(400, 300)
    pv.toggle()                                     # playing
    assert pv.timer.isActive()
    img = QImage(16, 16, QImage.Format.Format_RGB32)
    img.fill(0xFFFF0000)
    pv.show_override(img)
    assert not pv.timer.isActive()                  # comparing pauses playback
    before = pv.view.pixmap().cacheKey()
    pv._show(3)                                     # frame updates don't stomp it
    assert pv.view.pixmap().cacheKey() == before
    pv.clear_override()
    assert pv.view.pixmap().cacheKey() != before    # back to the moshed render


def test_preview_zoom_and_badge(qapp):
    from PySide6.QtCore import QPoint, QPointF, Qt
    from PySide6.QtGui import QWheelEvent
    pv = _preview_with_frames(4, w=32, h=24)
    pv.resize(500, 400)
    pv.show()
    qapp.processEvents()
    assert pv._scroll.zoom is None                  # fit by default
    pv.one_btn.click()                              # 1:1
    qapp.processEvents()
    assert pv._scroll.zoom == 1.0
    assert pv.view.width() == 32                    # label at native size
    pv._scroll.set_zoom(4.0)
    qapp.processEvents()
    assert pv.view.width() == 128
    ev = QWheelEvent(QPointF(50, 50), QPointF(50, 50), QPoint(0, 0),
                     QPoint(0, 120), Qt.MouseButton.NoButton,
                     Qt.KeyboardModifier.ControlModifier,
                     Qt.ScrollPhase.NoScrollPhase, False)
    pv._scroll.wheelEvent(ev)                       # Ctrl+wheel zooms in
    assert pv._scroll.zoom == 5.0                   # 4.0 × 1.25
    pv.set_rendering(True)                          # badge pinned to the viewport
    vp = pv._scroll.viewport()
    g = pv._busy_badge.geometry()
    assert pv._busy_badge.isVisible()
    assert g.right() <= vp.width() and g.top() >= 0
    pv.fit_btn.click()
    assert pv._scroll.zoom is None
    pv.hide()


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


def test_add_pixel_fx_with_params(ctl):
    _seed_clip(ctl, "c")
    ctl.add_pixel_fx("c", "rgb_shift", {"shift": 7})    # params carried from the pop-up
    assert ctl.clip_pixel_fx("c")[0]["params"]["shift"] == 7
    ctl.add_raw_fx("c", "pixel_sort")                   # no params -> mode defaults
    assert ctl.clip_raw_fx("c")[0]["params"]            # non-empty defaults


def test_raw_fx_add_update_remove_and_undo(ctl):
    _seed_clip(ctl, "c")
    ctl.add_raw_fx("c", "pixel_sort")
    assert ctl.clip_raw_fx("c")[0]["name"] == "pixel_sort"
    assert ctl.project.clip("c").has_finish()           # raw FX force the finish pass
    ctl.update_raw_fx("c", 0, {"axis": "vertical", "lo": 0.1, "hi": 0.7})
    assert ctl.clip_raw_fx("c")[0]["params"]["axis"] == "vertical"
    ctl.undo()                                          # roll back the params edit
    assert ctl.clip_raw_fx("c")[0]["params"].get("axis", "horizontal") != "vertical"
    ctl.remove_raw_fx("c", 0)
    assert ctl.clip_raw_fx("c") == []


def test_inspector_pixel_panel_live_round_trips(qapp):
    from PySide6.QtWidgets import QDialog
    from moshit.gui.widgets import InspectorPanel, EffectParamDialog
    insp = InspectorPanel()
    adds, begins, updates, ends = [], [], [], []
    insp.pixelFxAddBegin.connect(adds.append)
    insp.pixelFxEditBegin.connect(begins.append)
    insp.pixelFxLiveUpdate.connect(lambda i, p: updates.append((i, p)))
    insp.pixelFxEditEnd.connect(lambda i, ok: ends.append((i, ok)))
    insp._clip_id = "c"
    insp._add_pixel_fx("rgb_shift")                      # add asks the host to create
    assert adds == ["rgb_shift"]
    # edit an existing one through the non-modal live editor
    insp.set_clip_pixel_fx([{"name": "rgb_shift", "params": {"amount": 3}}])
    assert insp.pixel_list.count() == 1
    insp._edit_pixel_item(insp.pixel_list.item(0))
    assert begins == [0]
    dlg = insp._live_dlg
    assert isinstance(dlg, EffectParamDialog) and not dlg.isModal()
    dlg._param_widgets["amount"].setValue(9)            # a slider move -> live update
    assert updates and updates[-1] == (0, {"amount": 9})
    dlg.accept()
    assert ends == [(0, True)] and insp._live_dlg is None


def test_cancel_drops_stale_result_and_is_idle_safe(ctl):
    # a cancel supersedes the running job: its late finished callback is dropped
    called = []
    ctl._busy = True
    ctl._pending = lambda r: called.append(r)
    gen = ctl._job_gen
    ctl.cancel()
    assert not ctl._busy and ctl._job_gen != gen        # active flag cleared at once
    ctl._on_finished("late", gen)                       # stale generation -> ignored
    assert called == [] and not ctl.is_busy             # ...and the worker has drained
    # a fresh job still delivers its result
    ctl._busy = True
    ctl._pending = lambda r: called.append(r)
    ctl._on_finished("ok", ctl._job_gen)
    assert called == ["ok"]
    # cancelling while idle is a harmless no-op
    ctl.cancel()
    assert not ctl.is_busy


def test_cancel_serializes_until_worker_drains(ctl):
    # a cancelled (still-running) worker blocks a new job until it reports back,
    # so its in-thread state mutation can't race a fresh job
    ctl._busy = True
    gen = ctl._job_gen
    ctl.cancel()
    assert ctl._draining and ctl.is_busy        # draining counts as busy
    errs = []
    ctl.error.connect(errs.append)
    ctl._run(lambda: None, lambda r: None, "new job")
    assert errs and "moment" in errs[-1].lower()    # refused while draining
    ctl._on_finished("late", gen)                   # the zombie drains
    assert not ctl._draining and not ctl.is_busy


def test_inspector_raw_panel_live_round_trips(qapp):
    from moshit.gui.widgets import InspectorPanel, EffectParamDialog
    insp = InspectorPanel()
    adds, begins, updates, ends = [], [], [], []
    insp.rawFxAddBegin.connect(adds.append)
    insp.rawFxEditBegin.connect(begins.append)
    insp.rawFxLiveUpdate.connect(lambda i, p: updates.append((i, p)))
    insp.rawFxEditEnd.connect(lambda i, ok: ends.append((i, ok)))
    insp._clip_id = "c"
    insp._add_raw_fx("pixel_sort")                       # the "+ Add" flow
    assert adds == ["pixel_sort"]
    insp.set_clip_raw_fx([{"name": "pixel_sort", "params": {"axis": "horizontal"}}])
    assert insp.raw_list.count() == 1
    insp._edit_raw_item(insp.raw_list.item(0))
    assert begins == [0]
    dlg = insp._live_dlg
    assert isinstance(dlg, EffectParamDialog) and not dlg.isModal()
    dlg._param_widgets["axis"].setCurrentText("vertical")   # a control edit -> live
    assert updates and updates[-1][0] == 0
    assert updates[-1][1]["axis"] == "vertical"
    dlg.reject()                                            # Cancel
    assert ends == [(0, False)] and insp._live_dlg is None


def test_set_clip_mask_and_undo(ctl):
    _seed_clip(ctl, "c")
    ctl.set_clip_mask("c", "layer", {"source": "motion", "lo": 0.1, "hi": 0.5})
    assert ctl.project.clip("c").layer_mask["source"] == "motion"
    ctl.set_clip_mask("c", "fx", {"source": "luma", "lo": 0.3, "hi": 0.8})
    assert ctl.project.clip("c").fx_mask["lo"] == 0.3
    ctl.undo()                                          # roll back the fx matte
    assert ctl.project.clip("c").fx_mask is None
    assert ctl.project.clip("c").layer_mask is not None
    ctl.set_clip_mask("c", "layer", None)               # clearing it
    assert ctl.project.clip("c").layer_mask is None


def test_inspector_mask_editor_round_trips(qapp):
    from moshit.gui.widgets import InspectorPanel
    insp = InspectorPanel()
    emitted = []
    insp.maskChanged.connect(lambda k, s: emitted.append((k, s)))
    insp._clip_id = "c"
    insp.set_clip_masks({"source": "motion", "lo": 0.1, "hi": 0.5,
                         "invert": True, "feather": 3}, None)
    assert emitted == []                                # populating is silent
    ed = insp._mask_editors["layer"]
    assert ed["enable"].isChecked() and ed["source"].currentText() == "motion"
    assert ed["invert"].isChecked() and ed["feather"].value() == 3
    assert insp._read_mask("fx") is None                # disabled -> None
    insp._mask_editors["fx"]["enable"].setChecked(True)  # user enables it -> emit
    assert emitted and emitted[-1][0] == "fx" and emitted[-1][1] is not None


def test_inspector_mask_chroma_and_mode(qapp):
    from moshit.gui.widgets import InspectorPanel
    insp = InspectorPanel()
    emitted = []
    insp.maskChanged.connect(lambda k, s: emitted.append((k, s)))
    insp._clip_id = "c"
    # only the FX matte carries a confine/source mode
    assert insp._mask_editors["fx"]["mode"] is not None
    assert insp._mask_editors["layer"]["mode"] is None
    insp.set_clip_masks(None, {"source": "chroma", "key": "#112233",
                               "lo": 0.3, "hi": 0.5, "mode": "source"})
    ed = insp._mask_editors["fx"]
    assert ed["source"].currentText() == "chroma" and not ed["key_row"].isHidden()
    spec = insp._read_mask("fx")
    assert spec["key"] == "#112233" and spec["mode"] == "source"
    ed["source"].setCurrentText("luma")                 # non-chroma hides the key
    assert ed["key_row"].isHidden()
    assert emitted[-1][1]["source"] == "luma"


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


def test_undo_labels_name_the_action(win):
    ctl = win.controller
    _seed_clip(ctl, "c")
    ctl.add_effect("c", "bitrot", {"intensity": 0.4})
    assert ctl.undo_label == "Add effect"
    win._update_undo_labels()
    assert win.act_undo.text() == "&Undo Add effect"
    ctl.move_clip("c", 5)
    assert ctl.undo_label == "Move clip"
    ctl.undo()                                          # move rolled back
    assert ctl.redo_label == "Move clip" and ctl.undo_label == "Add effect"
    win._update_undo_labels()
    assert win.act_redo.text() == "&Redo Move clip"
    ctl.undo()                                          # effect rolled back
    assert not ctl.can_undo
    win._update_undo_labels()
    assert win.act_undo.text() == "&Undo"               # nothing to undo


def test_undo_survives_bake(qapp, tmp_path, monkeypatch, make_clip):
    # Baking used to wipe undo history; now it's a normal (undoable) step.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    from moshit.gui.controller import AppController
    from moshit.engine import EngineConfig
    c = AppController(config=EngineConfig(width=64, height=48, fps=24.0, gop=8))
    try:
        item = c._do_import(make_clip("b.mp4"), "main")
        clip = c.add_clip_for_media(item.id, "main")
        op = c.add_effect(clip.id, "pframe_duplicate", {"factor": 2})
        # mirror controller.bake(): snapshot pre-bake, bake, commit the entry
        pre = c._snapshot()
        c.project.bake_op(c.engine, op.id)               # synchronous bake path
        c._commit_undo(pre, "Bake")
        assert c.project.clip(clip.id).archived          # original archived
        assert any(m.derived for m in c.project.media.values())   # baked media added
        assert c.undo_label == "Bake"

        c.undo()                                         # undo the bake
        assert not c.project.clip(clip.id).archived      # original restored
        assert not c.project.op(op.id).archived          # its op restored
        assert not any(m.derived for m in c.project.media.values())
        assert c.project.bake_records == []
        assert item.id in c.project.media                # imported footage kept

        c.redo()                                         # redo re-bakes
        assert c.project.clip(clip.id).archived
        assert any(m.derived for m in c.project.media.values())
    finally:
        c.cleanup()


def test_inspector_stays_live_during_preview_render(win):
    c = win.controller
    c._busy_preview = True                              # a read-only preview render
    win._on_busy(True, "Rendering preview…")
    assert win.inspector.isEnabled()                   # editing stays available
    c._busy_preview = False                             # a heavy op (bake/export)
    win._on_busy(True, "Baking…")
    assert not win.inspector.isEnabled()               # locked while state changes
    win._on_busy(False, "")
    assert win.inspector.isEnabled()                   # re-enabled when idle


def test_beat_cache_warmed_off_thread(ctl, monkeypatch, tmp_path):
    from moshit import beats, waveform
    wav = tmp_path / "preview.wav"
    wav.write_bytes(b"")
    warmed = []
    monkeypatch.setattr(ctl.engine, "mix_audio", lambda *a, **k: wav)
    monkeypatch.setattr(waveform, "peaks", lambda *a, **k: [])
    monkeypatch.setattr(beats, "onsets", lambda p, **k: warmed.append(str(p)) or [])
    # _build_preview_audio runs on the audio worker thread; it should warm the
    # onset cache so the later main-thread beat_positions() call is instant.
    ctl._build_preview_audio([[{"source": "x", "silent": False, "duration": 1.0}]])
    assert warmed == [str(wav)]


def test_undo_preserves_media_imported_afterwards(ctl):
    from moshit.project import MediaItem
    ctl.project.media["m0"] = MediaItem(
        id="m0", source_path="x", label="m0", role="main",
        intermediate_path="x", nb_frames=20)
    clip = ctl.add_clip_for_media("m0", "main")          # snapshot captures {m0}
    # an import lands after that edit (imports aren't undoable)
    ctl.project.media["m1"] = MediaItem(
        id="m1", source_path="y", label="m1", role="main",
        intermediate_path="y", nb_frames=20)
    ctl.undo()                                           # roll back the add_clip
    assert all(x.id != clip.id for x in ctl.project.clips)
    assert "m0" in ctl.project.media                     # snapshot media restored
    assert "m1" in ctl.project.media                     # later import NOT dropped


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


def test_inspector_enabled_on_any_video_track(win):
    """The inspector edits clips on any video track, not just 'main'; a
    motion-pool clip still shows the disabled 'motion source' state."""
    from moshit.project import Clip, MOTION_TRACK_ID
    ctl = win.controller
    _seed_clip(ctl, "c")                                 # media "m", clip on main
    t = ctl.add_video_track()
    ctl.add_clip_for_media("m", t.id)
    sec = ctl.project.clips_for_track(t.id)[0]
    win._on_clip_selected(sec.id)
    assert win.inspector._clip_id == sec.id              # enabled on the 2nd video track

    ctl.project.clips.append(Clip(id="mo", media_id="m", track=MOTION_TRACK_ID))
    win._on_clip_selected("mo")
    assert win.inspector._clip_id is None                # motion pool stays disabled


def test_auto_refresh_predicate_covers_secondary_tracks(ctl):
    """has_video_clips() (the auto-refresh gate) sees clips on a secondary video
    track even when the legacy main_clips() is empty."""
    from moshit.project import MediaItem
    ctl.project.media.setdefault("m", MediaItem(
        id="m", source_path="x", label="x", role="main",
        intermediate_path="x", nb_frames=20))
    t = ctl.add_video_track()
    ctl.add_clip_for_media("m", t.id)                    # clip only on the 2nd track
    assert ctl.project.main_clips() == []                # nothing on the main track
    assert ctl.has_video_clips()                         # but the 2nd track has a clip


def test_beat_positions_on_secondary_track(win, monkeypatch):
    """beat_positions resolves a clip's span on its own track (was main-only)."""
    import moshit.beats as beats_mod
    ctl = win.controller
    _seed_clip(ctl, "a")                                 # media "m" on main
    t = ctl.add_video_track()
    ctl.add_clip_for_media("m", t.id)                    # 2nd track, span [0, 20]
    sec = ctl.project.clips_for_track(t.id)[0]
    ctl._audio_path_cache = "dummy.wav"                  # truthy: skip real audio
    fps = ctl.config.fps
    monkeypatch.setattr(beats_mod, "onsets", lambda wav: [10 / fps])
    got = ctl.beat_positions(sec.id)                     # [] before the fix
    assert len(got) == 1 and abs(got[0] - 0.5) < 1e-6


def test_inspector_beat_fill(win):
    # beats fill an automatable param inside the per-effect pop-up dialog
    from moshit.gui.widgets import AutoParamWidget, EffectParamDialog
    dlg = EffectParamDialog(win.inspector, kind="mosh",
                            mode_name="pframe_duplicate",
                            beat_provider=lambda cid: [0.25, 0.5, 0.75],
                            clip_id="c")
    w = dlg._param_widgets["factor"]
    assert isinstance(w, AutoParamWidget)
    dlg._fill_beats(w)
    val = dlg._getters["factor"]()
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


def test_easy_mode_adds_melt_junction_ops(ctl):
    from moshit.project import MediaItem
    for mid in ("m1", "m2", "m3"):
        ctl.project.media[mid] = MediaItem(
            id=mid, source_path="x", label=mid, role="main",
            intermediate_path="x", nb_frames=20)
    ctl.set_easy_mode(True)
    first = ctl.add_clip_for_media("m1", "main")
    assert ctl.project.clip_ops(first.id) == []       # nothing before it to melt from
    second = ctl.add_clip_for_media("m2", "main")
    ops = ctl.project.clip_ops(second.id)
    assert [(o.mode, o.params) for o in ops] == [
        ("iframe_removal", {"keep_first": False, "keep_every": 0})]
    assert (ops[0].region_start, ops[0].region_end) == (0, 1)   # just the cut
    third = ctl.add_clip_for_media("m3", "main")      # a row of three: every cut melts
    assert [o.mode for o in ctl.project.clip_ops(third.id)] == ["iframe_removal"]

    ctl.undo()                                        # clip + its op = one undo step
    assert all(c.id != third.id for c in ctl.project.clips)
    assert ctl.project.clip_ops(third.id) == []

    motion = ctl.add_clip_for_media("m1", "motion")   # motion sources never melt
    assert ctl.project.clip_ops(motion.id) == []
    ctl.set_easy_mode(False)                          # off: plain cuts again
    plain = ctl.add_clip_for_media("m1", "main")
    assert ctl.project.clip_ops(plain.id) == []


def test_easy_mode_render_melts_the_cut(ctl, make_clip, tmp_path):
    from moshit import avi
    ctl.set_easy_mode(True)
    a = ctl._do_import(make_clip("a.mp4", color="red"), "main")
    b = ctl._do_import(make_clip("b.mp4"), "main")
    ctl.add_clip_for_media(a.id, "main")
    ctl.add_clip_for_media(b.id, "main")
    out = tmp_path / "easy.avi"
    ctl.project.render(ctl.engine, out)
    parsed = avi.parse_avi(str(out))
    assert len(parsed.frames) == a.nb_frames + b.nb_frames - 1  # one keyframe gone
    assert parsed.frames[0].is_iframe                # the head still opens clean
    assert parsed.frames[a.nb_frames].is_pframe      # the cut lost its keyframe
    # only the junction keyframe is deleted: B's later ones survive (re-bloom)
    assert any(f.is_iframe for f in parsed.frames[a.nb_frames:])


def test_easy_mode_toolbar_persists(qapp, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    from moshit.gui.app import MainWindow
    w1 = MainWindow()
    try:
        assert not w1.controller.easy_mode           # off by default
        w1.act_easy.trigger()                        # toolbar toggle → controller
        assert w1.act_easy.isChecked() and w1.controller.easy_mode
    finally:
        w1.controller.cleanup()
    w2 = MainWindow()                                # next session: remembered
    try:
        assert w2.act_easy.isChecked() and w2.controller.easy_mode
    finally:
        w2.controller.cleanup()


def _seed_media(ctl, mid="m", n=20):
    from moshit.project import MediaItem
    ctl.project.media[mid] = MediaItem(
        id=mid, source_path="x", label=mid, role="main",
        intermediate_path="x", nb_frames=n)
    return mid


def test_copy_paste_clip_with_effects(ctl):
    _seed_media(ctl, "m", 20)
    c1 = ctl.add_clip_for_media("m", "main")             # 0..20
    ctl.add_effect(c1.id, "bitrot", {"intensity": 0.4})
    assert ctl.copy_clips([c1.id]) == 1 and ctl.can_paste
    depth = len(ctl._undo)
    new = ctl.paste_clips(at_frame=50)
    assert len(new) == 1
    n = new[0]
    assert n.id != c1.id and n.media_id == "m" and n.start == 50
    src_op = ctl.clip_effects(c1.id)[0]
    new_op = ctl.clip_effects(n.id)
    assert [e["mode"] for e in new_op] == ["bitrot"]
    assert new_op[0]["id"] != src_op["id"]               # independent op id
    assert new_op[0]["params"]["intensity"] == 0.4
    assert len(ctl._undo) == depth + 1                   # one undo step
    ctl.undo()
    assert all(x.id != n.id for x in ctl.project.clips)  # paste rolled back
    ctl.redo()
    assert any(x.start == 50 for x in ctl.project.clips)


def test_copy_multi_preserves_offsets(ctl):
    _seed_media(ctl, "m", 10)
    a = ctl.add_clip_for_media("m", "main")              # 0..10
    b = ctl.add_clip_for_media("m", "main")              # 10..20
    assert ctl.copy_clips([a.id, b.id]) == 2
    new = ctl.paste_clips(at_frame=100)
    assert sorted(x.start for x in new) == [100, 110]    # spacing kept


def test_paste_skips_missing_source_media(ctl):
    _seed_media(ctl, "m", 10)
    c = ctl.add_clip_for_media("m", "main")
    ctl.copy_clips([c.id])
    del ctl.project.media["m"]                            # source went offline
    depth = len(ctl._undo)
    assert ctl.paste_clips(at_frame=5) == []
    assert len(ctl._undo) == depth                        # no spurious undo entry


def test_remove_clips_batch_is_one_undo(ctl):
    _seed_media(ctl, "m", 10)
    a = ctl.add_clip_for_media("m", "main")
    b = ctl.add_clip_for_media("m", "main")
    ctl.add_effect(a.id, "bitrot", {})
    depth = len(ctl._undo)
    ctl.remove_clips([a.id, b.id])
    assert ctl.project.main_clips() == []                # both removed
    assert not any(o.target_clip_id == a.id for o in ctl.project.mosh_ops)
    assert len(ctl._undo) == depth + 1                    # single undo step
    ctl.undo()
    assert len(ctl.project.main_clips()) == 2


def _multi_timeline(nb=10):
    from PySide6.QtGui import QPixmap
    from moshit.gui.widgets import TimelineWidget
    from moshit.project import Project, Clip, MediaItem
    proj = Project()
    proj.media["m"] = MediaItem(id="m", source_path="x", label="m", role="main",
                                intermediate_path="x", nb_frames=nb)
    for i, cid in enumerate(("a", "b", "c")):
        proj.clips.append(Clip(id=cid, media_id="m", track="main", start=i * nb))
    tl = TimelineWidget()
    tl.resize(800, 240)
    tl.set_project(proj)
    tl.render(QPixmap(tl.size()))
    return tl


def test_timeline_multi_select_ctrl_and_shift(qapp):
    from PySide6.QtCore import Qt
    tl = _multi_timeline()
    none, ctrl, shift = (Qt.KeyboardModifier.NoModifier,
                         Qt.KeyboardModifier.ControlModifier,
                         Qt.KeyboardModifier.ShiftModifier)
    tl._update_selection("a", "main", none)
    assert tl._selected_ids == {"a"} and tl._selected == "a"
    tl._update_selection("c", "main", ctrl)              # ctrl adds
    assert tl._selected_ids == {"a", "c"} and tl._selected == "c"
    tl._update_selection("c", "main", ctrl)              # ctrl toggles off
    assert tl._selected_ids == {"a"}
    tl._update_selection("a", "main", none)              # reset primary to a
    tl._update_selection("c", "main", shift)             # shift range a..c
    assert tl._selected_ids == {"a", "b", "c"}
    assert tl.selected_ids()[0] == "c"                   # primary listed first
    tl._update_selection("b", "main", none)              # plain click collapses
    assert tl._selected_ids == {"b"}


def test_timeline_delete_emits_whole_selection(qapp):
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QKeyEvent
    from PySide6.QtCore import QEvent
    tl = _multi_timeline()
    tl._update_selection("a", "main", Qt.KeyboardModifier.NoModifier)
    tl._update_selection("b", "main", Qt.KeyboardModifier.ControlModifier)
    removed = []
    tl.removeManyRequested.connect(lambda ids: removed.append(sorted(ids)))
    tl.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Delete,
                              Qt.KeyboardModifier.NoModifier))
    assert removed == [["a", "b"]]


def test_timeline_leaves_copy_paste_keys_to_the_menu_actions(qapp):
    """Ctrl+C/Ctrl+V belong to the Edit-menu QActions (window-context
    shortcuts Qt dispatches before keyPressEvent). Handling them here too
    would be unreachable code carrying its own copy of the rules."""
    from PySide6.QtCore import Qt, QEvent
    from PySide6.QtGui import QKeyEvent
    tl = _multi_timeline()
    tl._update_selection("a", "main", Qt.KeyboardModifier.NoModifier)
    fired = []
    tl.copyRequested.connect(lambda ids: fired.append("copy"))
    tl.pasteRequested.connect(lambda: fired.append("paste"))
    for key in (Qt.Key.Key_C, Qt.Key.Key_V):
        tl.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, key,
                                   Qt.KeyboardModifier.ControlModifier))
    assert fired == []                       # the QActions are the sole owner


def test_clipboard_is_a_snapshot_not_a_live_alias(ctl):
    """to_dict() is a shallow __dict__.copy(), so copying without a deepcopy
    would leave the clipboard aliasing the clip's live effect lists."""
    _seed_media(ctl, "m", 20)
    c = ctl.add_clip_for_media("m", "main")
    c.pixel_effects.append({"name": "glow", "params": {"amount": 0.2}})
    ctl.copy_clips([c.id])
    c.pixel_effects[0]["params"]["amount"] = 0.9      # edit after copying
    c.pixel_effects.append({"name": "blur", "params": {}})
    new = ctl.paste_clips(at_frame=40)
    assert [e["name"] for e in new[0].pixel_effects] == ["glow"]   # not ["glow","blur"]
    assert new[0].pixel_effects[0]["params"]["amount"] == 0.2      # not 0.9


def test_paste_carries_every_clip_field_it_does_not_re_derive(ctl):
    """The carried-field set is derived from the Clip dataclass, so a field
    added to Clip rides along instead of being silently dropped."""
    import dataclasses
    from moshit.project import Clip
    from moshit.gui.controller import _PASTE_FIELDS, _PASTE_SKIP
    assert set(_PASTE_FIELDS) | _PASTE_SKIP == {f.name for f in
                                                dataclasses.fields(Clip)}
    assert "enabled" in _PASTE_FIELDS                 # was dropped by the old list
    _seed_media(ctl, "m", 20)
    c = ctl.add_clip_for_media("m", "main")
    c.enabled, c.gain, c.blend_mode = False, 0.25, "screen"
    ctl.copy_clips([c.id])
    new = ctl.paste_clips(at_frame=40)[0]
    assert (new.enabled, new.gain, new.blend_mode) == (False, 0.25, "screen")


def test_failed_paste_keeps_the_redo_stack(ctl):
    """_commit_undo clears redo, so a paste that lands nothing must not have
    pushed one -- popping the entry can't put redo back."""
    _seed_media(ctl, "m", 10)
    c = ctl.add_clip_for_media("m", "main")
    ctl.copy_clips([c.id])
    ctl.remove_clips([c.id])
    ctl.undo()                                        # something to redo
    assert ctl.can_redo
    del ctl.project.media["m"]                        # source went offline
    assert ctl.paste_clips(at_frame=5) == []
    assert ctl.can_redo                               # redo survived the no-op


def test_remove_effect_no_op_keeps_the_redo_stack(ctl):
    _seed_media(ctl, "m", 10)
    c = ctl.add_clip_for_media("m", "main")
    ctl.add_effect(c.id, "bitrot", {})
    ctl.undo()
    assert ctl.can_redo
    ctl.remove_effect("op_does_not_exist")            # removes nothing
    assert ctl.can_redo


def test_paste_lands_in_the_sequence_on_screen(ctl):
    """Pasting while inside a precomp must not drop clips into the root
    sequence, where the timeline would never show them."""
    _seed_media(ctl, "m", 10)
    c = ctl.add_clip_for_media("m", "main")
    ctl.copy_clips([c.id])
    seq = ctl.project.add_sequence("Precomp")
    ctl.current_seq_id = seq.id
    new = ctl.paste_clips(at_frame=0)
    assert len(new) == 1
    assert new[0].seq_id == seq.id
    assert new[0].track in {t.id for t in ctl.project.tracks_for(seq.id)}


def test_move_clips_shifts_the_group_in_one_undo_step(ctl):
    _seed_media(ctl, "m", 10)
    a = ctl.add_clip_for_media("m", "main")
    b = ctl.add_clip_for_media("m", "main")
    ctl.move_clip(a.id, 20)
    ctl.move_clip(b.id, 50)
    depth = len(ctl._undo)
    ctl.move_clips([a.id, b.id], 15)
    assert (a.start, b.start) == (35, 65)             # spacing preserved
    assert len(ctl._undo) == depth + 1                # single undo step
    ctl.undo()                                        # _restore rebuilds the clips
    assert (ctl.project.clip(a.id).start,
            ctl.project.clip(b.id).start) == (20, 50)


def test_move_clips_clamps_the_group_at_zero(ctl):
    """A drag past the start slides the group; it must not collapse clips
    onto frame 0 by clamping each one independently."""
    _seed_media(ctl, "m", 10)
    a = ctl.add_clip_for_media("m", "main")
    b = ctl.add_clip_for_media("m", "main")
    ctl.move_clip(a.id, 10)
    ctl.move_clip(b.id, 40)
    ctl.move_clips([a.id, b.id], -100)
    assert (a.start, b.start) == (0, 30)              # not (0, 0)


def test_undo_is_refused_while_a_heavy_job_runs(ctl):
    """bake/flow commit a pre-snapshot when they finish; an undo landing
    mid-job would be clobbered by that commit (and take redo with it)."""
    _seed_media(ctl, "m", 10)
    c = ctl.add_clip_for_media("m", "main")
    ctl.move_clip(c.id, 5)
    assert ctl.can_undo
    ctl._busy, ctl._busy_preview = True, False        # a bake is running
    assert ctl.edits_locked and not ctl.can_undo
    ctl.undo()
    assert ctl.project.clip(c.id).start == 5          # refused, state untouched
    ctl._busy_preview = True                          # a preview instead
    assert not ctl.edits_locked and ctl.can_undo
    ctl.undo()                                        # _restore rebuilds the clips
    assert ctl.project.clip(c.id).start == 0          # allowed
    ctl._busy = False


def test_render_view_detaches_the_model_from_the_worker(ctl):
    """A preview renders on a worker thread while the UI keeps editing, so the
    view must not share the lists the main thread mutates."""
    _seed_media(ctl, "m", 10)
    c = ctl.add_clip_for_media("m", "main")
    ctl.add_effect(c.id, "bitrot", {})
    view = ctl.project.render_view()
    n_clips, n_ops = len(view.clips), len(view.mosh_ops)
    ctl.remove_clips([c.id])                          # edit during the "render"
    d = ctl.add_clip_for_media("m", "main")
    ctl.add_effect(d.id, "bitrot", {})
    assert (len(view.clips), len(view.mosh_ops)) == (n_clips, n_ops)
    assert view.media is ctl.project.media            # caches/media stay shared


def test_ctrl_click_deselect_moves_the_primary(qapp):
    """Toggling a clip out of the set must not leave it as the primary -- it
    drives the inspector and the brightest outline."""
    from PySide6.QtCore import Qt
    tl = _multi_timeline()
    none, ctrl = Qt.KeyboardModifier.NoModifier, Qt.KeyboardModifier.ControlModifier
    tl._update_selection("a", "main", none)
    tl._update_selection("c", "main", ctrl)
    tl._update_selection("c", "main", ctrl)            # toggle "c" back out
    assert tl._selected_ids == {"a"}
    assert tl._selected == "a"                         # not the deselected "c"
    assert tl.selected_ids() == ["a"]
    tl._update_selection("a", "main", ctrl)            # now empty the set
    assert tl._selected_ids == set() and tl._selected is None


def test_selection_is_pruned_when_clips_disappear(qapp):
    """set_project is the one chokepoint every removal path goes through
    (delete, undo, bake, remove-track), so the selection can't go stale."""
    from PySide6.QtCore import Qt
    tl = _multi_timeline()
    tl._update_selection("a", "main", Qt.KeyboardModifier.NoModifier)
    tl._update_selection("b", "main", Qt.KeyboardModifier.ControlModifier)
    proj = tl._project
    proj.clips = [c for c in proj.clips if c.id != "b"]
    tl.set_project(proj)
    assert tl._selected_ids == {"a"} and tl._selected == "a"
    proj.clips = [c for c in proj.clips if c.id != "a"]
    tl.set_project(proj)
    assert tl._selected_ids == set() and tl._selected is None


def test_modifier_click_selects_without_arming_a_drag(qapp):
    """Ctrl/Shift click is a selection gesture; arming a move drag let a few
    pixels of jitter reposition a clip the user only meant to select."""
    from PySide6.QtCore import Qt, QPointF
    from PySide6.QtGui import QMouseEvent, QPointingDevice
    from PySide6.QtCore import QEvent
    tl = _multi_timeline()
    dev = QPointingDevice.primaryPointingDevice()

    def press(pos, mods):
        tl.mousePressEvent(QMouseEvent(
            QEvent.Type.MouseButtonPress, QPointF(pos), QPointF(pos),
            Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton, mods,
            dev))

    hit = tl._hits[0][0].center()
    press(hit, Qt.KeyboardModifier.NoModifier)
    assert tl._drag is not None                        # a plain click still drags
    tl._drag = None
    press(hit, Qt.KeyboardModifier.ControlModifier)
    assert tl._drag is None
    press(hit, Qt.KeyboardModifier.ShiftModifier)
    assert tl._drag is None


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


def test_random_params_respects_schema():
    import random
    from moshit.modes import get_mode, load_modes
    load_modes()
    from moshit.modes.base import random_params
    mode = get_mode("iframe_removal")               # bool + int-range params
    rng = random.Random(123)
    vals = random_params(mode, rng=rng)
    assert set(vals) == {p.name for p in mode.params}
    assert isinstance(vals["keep_first"], bool)
    assert 0 <= vals["keep_every"] <= 240           # its declared range
    # a clip_ref-style param without a range keeps the current value
    from moshit.modes.base import Param, MoshMode

    class _Ref(MoshMode):
        name = "_t_ref_mode"
        params = [Param("source", "clip_ref", "keep-me"),
                  Param("amt", "float", 0.0, lo=0.0, hi=1.0)]

        def apply(self, frames, ctx, **p):
            return frames
    got = random_params(get_mode("_t_ref_mode"), {"source": "keep-me"})
    assert got["source"] == "keep-me" and 0.0 <= got["amt"] <= 1.0
    # seeded determinism
    assert random_params(mode, rng=random.Random(7)) == \
        random_params(mode, rng=random.Random(7))


def test_randomise_effect_updates_and_undoes(ctl):
    _seed_clip(ctl, "c")
    op = ctl.add_effect("c", "iframe_removal",
                        {"keep_first": True, "keep_every": 0})
    op.region_start, op.region_end = 2, 5
    depth = len(ctl._undo)
    ctl.randomise_effect(op.id)
    params = ctl.clip_effects("c")[0]["params"]
    assert 0 <= params["keep_every"] <= 240
    assert (op.region_start, op.region_end) == (2, 5)   # region preserved
    assert len(ctl._undo) == depth + 1                  # one undo step
    ctl.undo()
    assert ctl.clip_effects("c")[0]["params"] == {"keep_first": True,
                                                  "keep_every": 0}


def test_preset_apply_menu_replace_and_append(win):
    ctl = win.controller
    _seed_clip(ctl, "c1")
    ctl.add_effect("c1", "bitrot", {"intensity": 0.4})
    assert ctl.save_stack_as_preset("c1", "p")
    win.inspector.set_presets(ctl.preset_names())
    _seed_clip(ctl, "c2")
    win._selected_clip = "c2"
    ctl.add_effect("c2", "pframe_drop", {})
    win.inspector.preset_combo.setCurrentText("p")
    win.inspector._apply_append_act.trigger()            # append: keep + add
    assert [e["mode"] for e in ctl.clip_effects("c2")] == ["pframe_drop", "bitrot"]
    win.inspector._apply_replace_act.trigger()           # replace: just the preset
    assert [e["mode"] for e in ctl.clip_effects("c2")] == ["bitrot"]


def test_effect_list_grows_with_content(qapp):
    from moshit.gui.widgets import InspectorPanel
    insp = InspectorPanel()
    insp.set_enabled_for_clip("c", "clip", effects=[])
    empty_h = insp.effect_list.height()
    one = [{"id": "o0", "mode": "bitrot", "params": {}, "enabled": True,
            "region": None}]
    insp.set_clip_effects(one)
    small_h = insp.effect_list.height()
    def rows(n):
        insp.set_clip_effects(
            [{"id": f"o{i}", "mode": "bitrot", "params": {}, "enabled": True,
              "region": None} for i in range(n)])
        return insp.effect_list.height()
    grown_h = rows(9)
    assert empty_h > 0 and grown_h > small_h            # grows with content
    assert rows(20) == rows(30)                         # capped at max_rows (12)
    assert rows(30) < grown_h * 3                        # bounded, not 30 rows tall


def test_library_context_menu_actions(qapp):
    from moshit.gui.widgets import MediaLibrary
    from moshit.project import MediaItem
    lib = MediaLibrary()                                # bare: no modal relink dialog
    lib.add_media(MediaItem(id="on1", source_path="x", label="online",
                            role="main", intermediate_path="x", nb_frames=5))
    menu = lib._menu_for(lib.list.item(0))
    labels = [a.text() for a in menu.actions() if a.text()]
    assert labels == ["Add to main track", "Add to motion track"]
    assert lib._act_relink is None                      # online: no relink
    placed = []
    lib.addToTrackRequested.connect(lambda mid, tr: placed.append((mid, tr)))
    lib._act_main.trigger()
    assert placed == [("on1", "main")]                  # menu action -> signal
    lib.add_media(MediaItem(id="off1", source_path="x", label="gone",
                            role="main", intermediate_path="/no/where.avi",
                            nb_frames=5), offline=True)
    menu2 = lib._menu_for(lib.list.item(1))
    assert any(a.text() == "Relink offline media…" for a in menu2.actions())
    got = []
    lib.relinkRequested.connect(lambda: got.append(1))
    lib._act_relink.trigger()
    assert got == [1]


def test_icon_buttons_have_accessible_names(win):
    pv = win.preview
    insp = win.inspector
    for b in (pv.mute_btn, pv.src_btn, pv.fit_btn, pv.one_btn,
              pv.start_btn, pv.prev_btn, pv.next_btn, pv.end_btn,
              insp.dice_btn, insp.up_btn, insp.down_btn, insp.preset_del_btn):
        assert b.accessibleName(), f"{b.objectName() or b.text()} has no a11y name"


def test_live_effect_edit_coalesces_undo(ctl):
    # A drag fires many live updates; they must fold into ONE undo entry.
    _seed_clip(ctl, "c")
    op = ctl.add_effect("c", "bitrot", {"intensity": 0.2})
    depth = len(ctl._undo)                               # 1: the add itself
    ctl.begin_effect_edit(op.id)
    for v in (0.3, 0.4, 0.5):
        ctl.live_update_effect(op.id, "bitrot", {"intensity": v})
    ctl.end_effect_edit(op.id, commit=True)
    assert len(ctl._undo) == depth + 1                   # one entry for the whole drag
    assert ctl.clip_effects("c")[0]["params"]["intensity"] == 0.5
    ctl.undo()                                           # a single undo -> pre-edit
    assert ctl.clip_effects("c")[0]["params"]["intensity"] == 0.2


def test_live_effect_edit_noop_commit_adds_no_history(ctl):
    # Opening the editor and committing without changing anything is not an edit.
    _seed_clip(ctl, "c")
    op = ctl.add_effect("c", "bitrot", {"intensity": 0.2})
    depth = len(ctl._undo)
    ctl.begin_effect_edit(op.id)
    ctl.live_update_effect(op.id, "bitrot", {"intensity": 0.2})   # same value
    ctl.end_effect_edit(op.id, commit=True)
    assert len(ctl._undo) == depth                       # no spurious undo entry


def test_live_effect_edit_cancel_reverts(ctl):
    _seed_clip(ctl, "c")
    op = ctl.add_effect("c", "bitrot", {"intensity": 0.2})
    depth = len(ctl._undo)
    ctl.begin_effect_edit(op.id)
    ctl.live_update_effect(op.id, "bitrot", {"intensity": 0.9})
    assert ctl.clip_effects("c")[0]["params"]["intensity"] == 0.9   # live-applied
    ctl.end_effect_edit(op.id, commit=False)                        # Cancel
    assert ctl.clip_effects("c")[0]["params"]["intensity"] == 0.2   # reverted
    assert len(ctl._undo) == depth                                   # left no trace


def test_live_effect_add_commit_and_cancel(ctl):
    _seed_clip(ctl, "c")
    assert not ctl.can_undo
    # a cancelled add leaves neither an op nor any history
    op = ctl.begin_effect_add("c", "bitrot")
    assert [e["mode"] for e in ctl.clip_effects("c")] == ["bitrot"]
    ctl.end_effect_edit(op.id, commit=False)
    assert ctl.clip_effects("c") == [] and not ctl.can_undo
    # a committed add (with a live tweak) is a single undo step back to empty
    op2 = ctl.begin_effect_add("c", "bitrot")
    ctl.live_update_effect(op2.id, "bitrot", {"intensity": 0.7})
    ctl.end_effect_edit(op2.id, commit=True)
    assert ctl.clip_effects("c")[0]["params"]["intensity"] == 0.7
    ctl.undo()
    assert ctl.clip_effects("c") == []


def test_live_update_without_session_falls_back_to_atomic(ctl):
    # Outside a session, live_update_effect behaves like the atomic update.
    _seed_clip(ctl, "c")
    op = ctl.add_effect("c", "bitrot", {"intensity": 0.2})
    depth = len(ctl._undo)
    ctl.live_update_effect(op.id, "bitrot", {"intensity": 0.8})
    assert ctl.clip_effects("c")[0]["params"]["intensity"] == 0.8
    assert len(ctl._undo) == depth + 1                   # atomic path pushed one


def test_live_pixel_fx_edit_coalesces_and_cancels(ctl):
    _seed_clip(ctl, "c")
    ctl.add_pixel_fx("c", "rgb_shift", {"amount": 3})
    depth = len(ctl._undo)
    ctl.begin_fx_edit("pixel", "c", 0)
    for v in (5, 7, 9):
        ctl.live_update_fx("pixel", "c", 0, {"amount": v})
    ctl.end_fx_edit("pixel", "c", 0, commit=True)
    assert len(ctl._undo) == depth + 1                   # one entry for the drag
    assert ctl.clip_pixel_fx("c")[0]["params"]["amount"] == 9
    # cancel reverts and leaves no history
    ctl.begin_fx_edit("pixel", "c", 0)
    ctl.live_update_fx("pixel", "c", 0, {"amount": 40})
    assert ctl.clip_pixel_fx("c")[0]["params"]["amount"] == 40
    ctl.end_fx_edit("pixel", "c", 0, commit=False)
    assert ctl.clip_pixel_fx("c")[0]["params"]["amount"] == 9
    assert len(ctl._undo) == depth + 1


def test_live_raw_fx_add_commit_and_cancel(ctl):
    _seed_clip(ctl, "c")
    assert not ctl.can_undo
    # cancelled add leaves neither the FX nor history
    i = ctl.begin_fx_add("raw", "c", "pixel_sort")
    assert i == 0 and len(ctl.clip_raw_fx("c")) == 1
    ctl.end_fx_edit("raw", "c", i, commit=False)
    assert ctl.clip_raw_fx("c") == [] and not ctl.can_undo
    # committed add with a live tweak is a single undo step
    i = ctl.begin_fx_add("raw", "c", "pixel_sort")
    ctl.live_update_fx("raw", "c", i, {"axis": "vertical"})
    ctl.end_fx_edit("raw", "c", i, commit=True)
    assert ctl.clip_raw_fx("c")[0]["params"]["axis"] == "vertical"
    ctl.undo()
    assert ctl.clip_raw_fx("c") == []


def test_inspector_live_editor_round_trips(qapp):
    from PySide6.QtWidgets import QDialog
    from moshit.gui.widgets import InspectorPanel, EffectParamDialog
    insp = InspectorPanel()
    begins, updates, ends = [], [], []
    insp.effectEditBegin.connect(begins.append)
    insp.effectLiveUpdate.connect(lambda oid, m, p, r: updates.append((oid, p)))
    insp.effectEditEnd.connect(lambda oid, ok: ends.append((oid, ok)))
    insp._clip_id = "c"
    insp._effects = [{"id": "op1", "mode": "bitrot",
                      "params": {"intensity": 0.2, "hits": 6},
                      "enabled": True, "region": None}]

    insp.open_live_editor("op1")
    assert begins == ["op1"]
    dlg = insp._live_dlg
    assert isinstance(dlg, EffectParamDialog) and not dlg.isModal()

    dlg._param_widgets["hits"].setValue(12)             # a slider move -> live update
    assert updates and updates[-1][0] == "op1"
    assert updates[-1][1]["hits"] == 12

    dlg.accept()                                        # Ok commits
    assert ends == [("op1", True)] and insp._live_dlg is None


def test_app_live_add_flow_commit_and_cancel(win):
    # End-to-end through app.py: add opens a live editor, tweaks apply live,
    # Ok commits as one undo step, and Cancel of an add removes the effect.
    ctl = win.controller
    _seed_clip(ctl, "c")
    win._selected_clip = "c"

    win._on_effect_add_begin("bitrot")
    assert win._live_editing and win.inspector._live_dlg is not None
    assert [e["mode"] for e in ctl.clip_effects("c")] == ["bitrot"]
    dlg = win.inspector._live_dlg
    dlg._param_widgets["hits"].setValue(20)              # live tweak re-applies
    assert ctl.clip_effects("c")[0]["params"]["hits"] == 20
    dlg.accept()                                         # Ok commits
    assert not win._live_editing and win.inspector._live_dlg is None
    ctl.undo()                                           # one step -> effect gone
    assert ctl.clip_effects("c") == []
    ctl.redo()

    # Cancel of a fresh add drops the effect entirely.
    win._on_effect_add_begin("bitrot")
    dlg = win.inspector._live_dlg
    before = [e["mode"] for e in ctl.clip_effects("c")]
    dlg.reject()                                         # Cancel
    assert [e["mode"] for e in ctl.clip_effects("c")] == before[:-1]
    assert not win._live_editing


def test_app_live_pixel_add_flow(win):
    # End-to-end through app.py: pixel add opens a live editor, a tweak applies
    # live, Ok commits as one undo step.
    ctl = win.controller
    _seed_clip(ctl, "c")
    win._selected_clip = "c"
    win._on_fx_add_begin("pixel", "rgb_shift")
    assert win._live_editing and win.inspector._live_dlg is not None
    assert ctl.clip_pixel_fx("c")[0]["name"] == "rgb_shift"
    dlg = win.inspector._live_dlg
    dlg._param_widgets["amount"].setValue(11)
    assert ctl.clip_pixel_fx("c")[0]["params"]["amount"] == 11
    dlg.accept()
    assert not win._live_editing and win.inspector._live_dlg is None
    ctl.undo()
    assert ctl.clip_pixel_fx("c") == []


def test_inspector_automation_control(win):
    from moshit.gui.widgets import AutoParamWidget, KeyframeDialog, EffectParamDialog
    dlg = EffectParamDialog(win.inspector, kind="mosh",
                            mode_name="pframe_duplicate")
    w = dlg._param_widgets["factor"]
    assert isinstance(w, AutoParamWidget)
    # enable automation -> a 2-point ramp from the current value
    w.value.setValue(2)
    w.auto_chk.setChecked(True)
    value = dlg._getters["factor"]()
    assert isinstance(value, dict) and value["__auto__"] and len(value["keys"]) == 2

    # the keyframe dialog round-trips a multi-point curve with easing
    from moshit.modes.base import get_mode
    factor = next(p for p in get_mode("pframe_duplicate").params if p.name == "factor")
    kdlg = KeyframeDialog(win.inspector, factor,
                          {"__auto__": True, "interp": "smooth",
                           "keys": [[0.0, 1], [0.5, 4], [1.0, 2]]})
    spec = kdlg.values()
    assert spec["interp"] == "smooth" and len(spec["keys"]) == 3
    w.set_value(spec)
    out = w.get_value()
    assert out["interp"] == "smooth" and len(out["keys"]) == 3


def test_help_menu_and_shortcut_sheet(win):
    """The Help menu exists and the cheat-sheet is generated from the live
    shortcut table + menu actions, so a new binding can't drift out of it."""
    titles = [a.text().replace("&", "") for a in win.menuBar().actions()]
    assert "Help" in titles
    keys = {k for k, _ in win._collect_shortcuts()}
    assert {"Space", "Ctrl+I", "Ctrl+E"} <= keys          # window table + menu action
    win._shortcut_table.append(("Ctrl+Alt+T", "Temp probe", lambda: None))
    assert any(k == "Ctrl+Alt+T" for k, _ in win._collect_shortcuts())   # no drift


def test_select_adjacent_clip_navigates(win):
    ctl = win.controller
    _seed_clip(ctl, "a")                                  # media "m", main [0, 20]
    ctl.add_clip_for_media("m", "main")                   # a second clip after it
    ids = [c.id for c in ctl.project.clips_for_track("main")]
    assert len(ids) == 2
    win._selected_clip = ids[0]
    win._select_adjacent_clip(1)
    assert win._selected_clip == ids[1]                   # -> next clip
    win._select_adjacent_clip(-1)
    assert win._selected_clip == ids[0]                   # -> previous clip
    win._select_adjacent_clip(-1)
    assert win._selected_clip == ids[0]                   # clamped at the start


def test_tool_switch_shortcut_slots(win):
    win.btn_cut.click()                                   # the 'B' shortcut target
    assert win.timeline._tool == "cut" and win.btn_cut.isChecked()
    win.btn_pointer.click()                               # the 'V' shortcut target
    assert win.timeline._tool == "pointer" and win.btn_pointer.isChecked()


def test_first_run_welcome_shows_once(qapp, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    from PySide6.QtCore import QSettings
    QSettings("moshit", "moshit").remove("ui/seen_welcome")   # clean slate for this test
    from moshit.gui.app import MainWindow
    w1 = MainWindow()
    assert w1._showed_welcome is True                     # first launch -> shown
    w1.controller.cleanup()
    w2 = MainWindow()
    assert w2._showed_welcome is False                    # flag persisted -> not again
    w2.controller.cleanup()
