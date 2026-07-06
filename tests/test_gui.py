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
