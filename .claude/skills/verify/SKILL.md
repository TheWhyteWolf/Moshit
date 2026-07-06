---
name: verify
description: Drive the Moshit GUI/engine end to end (offscreen Qt) to verify a change at its real surface — launch MainWindow, click real widgets, render/export, inspect the coded AVI stream or exported pixels.
---

# Verifying Moshit changes

Moshit has three surfaces: the PySide6 GUI (`MainWindow`), the CLI
(`python -m moshit.cli`), and the engine/project API. GUI changes are
verified by driving the real window offscreen; engine changes by rendering
and inspecting output.

## Launch the GUI headless

```python
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["XDG_CONFIG_HOME"] = "<fresh tmp dir>"   # isolate QSettings; wipe between runs
from PySide6.QtWidgets import QApplication
from moshit.engine import EngineConfig
from moshit.gui.app import MainWindow
app = QApplication.instance() or QApplication([])
w = MainWindow(config=EngineConfig(width=320, height=240, fps=24.0, gop=12))
w.show()
```

A small config keeps imports/renders to ~1s each. `gop=12` on 1s clips gives
multiple keyframes per clip (useful for mosh-junction checks). Make sources
with `ffmpeg -f lavfi -i color=c=red:...` / `mandelbrot=...` — visually
distinct + real motion.

## Drive it with real events

- Pump manually — never `app.exec()`: `QTest.qWait(25)` in a loop, waiting
  until `not w.controller.is_busy and not w._refresh_timer.isActive()`
  (auto-refresh debounce is 350 ms; imports/renders/exports are async
  workers that finish via queued signals, so the loop must keep pumping).
- Toolbar buttons: `tb = w.findChildren(QToolBar)[0]`;
  `QTest.mouseClick(tb.widgetForAction(w.act_xxx), Qt.MouseButton.LeftButton)`.
- Library: `w.library.list.setCurrentRow(i)` then click the real
  "Add to main"/"Add to motion" `QPushButton`.
- Timeline click at a clip: compute x from `t._track_x()`, `t._ppf()`,
  `t._project.track_layout("main")`, y from `t._lane_y(t._lane_index("main"))`,
  then `QTest.mouseClick(t, LeftButton, pos=QPoint(x, y))`.
- Import/export without the file dialogs: `w.controller.import_media(path)`,
  `w.controller.export("h264_mp4", out, audio=False)` — these are exactly
  what the dialogs call.
- Screenshots work offscreen: `w.grab().save("shot.png")`.

## Gotchas (each cost real time)

- **Modal traps hang forever offscreen.** `w.close()` on a dirty project
  opens the save-changes QMessageBox → deadlock. Call `w._set_dirty(False)`
  first. Same for `_offer_relink` (missing media on open).
- QSettings persist across runs of the same XDG_CONFIG_HOME — wipe the dir
  at script start or a stale toggle breaks "default state" assertions.
- ffmpeg spam on stderr: filter `^\[mpeg4` / `^\[swscaler` lines.

## Observing results

- Coded-stream structure (keyframes etc.): `moshit.avi.parse_avi(out)` →
  `frames[i].is_iframe` / `.is_pframe`.
- Exported pixels: extract one frame raw and average it —
  `ffmpeg -i out.mp4 -vf select=eq(n\,IDX) -frames:v 1 -f rawvideo -pix_fmt rgb24 -`
  (no PIL needed).
- Frame counts: `ffprobe -count_frames -show_entries stream=nb_read_frames`.
