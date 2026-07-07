# Moshit improvement backlog — performance & usability

Working notes from a full review (2026-07-03): three parallel audits over the render
pipeline, the GUI, and the modes/CLI/docs/tests surface, with the sharpest findings
verified by hand. Items carry stable IDs (B=bug, P=performance, U=usability, M=modes/
misc) so commits can reference them. Tick items off as they land.

---

## Verified correctness bugs

*(All fixed in Wave 1 — see the roadmap below; boxes ticked for consistency.)*

- [x] **B1. `motion_weave` infinite loop** (`moshit/modes/motion_weave.py:45-60`).
  `base_run=0, motion_run≥1` passes the both-zero guard; `while bi < len(base_p)`
  never advances `bi` while appending motion frames forever → hang + unbounded
  memory. Reachable from the GUI (param `lo=0`).
- [x] **B2. `motion_gain` automation can't cross 1.0** (`moshit/modes/motion_gain.py:35-54`).
  Amplify/reduce branch chosen once from the static `gain` (curve start value); the
  amplify branch never thins, the reduce branch clamps `g ≤ 1.0` so it never
  amplifies — an automated 0.5→2.0 curve silently half-works. Fix: one per-frame
  error-diffusion loop that both duplicates (acc ≥ 1) and drops (acc < 1).
- [x] **B3. `pingpong` dead code + interior-I-frame loss** (`moshit/modes/pingpong.py:37-42`).
  `out = [f for f in frames if f.is_iframe]` is never used; only `frames[0]` survives
  as anchor in `per_gop=False` mode. Delete the dead line, pin the intended behavior
  with a test.
- [x] **B4. ~11 mosh modes have zero behavioral tests** (`bitrot`, `gop_scramble`,
  `iframe_pulse`, `momentum`, `motion_weave`, `pframe_drop`, `pframe_echo`,
  `pframe_reverse`, `pframe_shuffle`, `pingpong`, `surge`) — none of B1–B3 would be
  caught today. Add a parametrized synth-frames test that runs EVERY registered mosh
  mode with default + edge params and asserts termination + sane output.

## Roadmap

### Wave 1 — correctness + free speed (small diffs, high certainty) — DONE
- [x] Fix B1, B2, B3 + the all-modes behavioral test (B4).
- [x] P2: memoize `_media_signature()` once per render (O(clips×media) stat() today).
- [x] P1: lazy codec mosh inside `_clip_seg` miss branch (skip Python mosh on cache hits).
- [x] P3: hoist `_motion_frames()` out of the per-clip loop in the composite path.
- [x] P6: cache motion-source raw decode for flow, keyed (path, mtime_ns, size, geom),
      byte-bounded (~512 MB) so long full-res drivers can't pin multiple GB.
- [x] P9: memoize `beats.onsets`/`waveform.peaks` by path+mtime.
- [x] P14: compute matte once in `_raw_frames`, pass to blend helpers.
- [~] P7 (cheaper preview encoder settings): **dropped after adversarial review.** A
      runtime qscale bump on the shared preview engine poisoned full-quality precomp
      intermediates (the seg-cache key didn't capture quality) and degraded saved
      frames, for no measurable speed win in benchmarks. Revisit only with an isolated
      benchmark proving the encode is actually the bottleneck (e.g. 20+ clip timeline).

### Wave 2 — the two biggest user-facing wins
- [x] U18: **live parameter editing** — the param editor is now a non-modal window that
  re-renders the preview as you drag (debounced onto the existing auto-refresh; the seg
  cache means only the edited clip re-renders). Adding an effect opens the live editor on
  it immediately. A whole drag folds into **one** undo entry (coalesced live-edit session
  in the controller); Cancel reverts to the pre-edit state, Ok commits. Covers the **mosh
  effect stack**, the **pixel FX** and the **raw FX** panels — all three share one generic
  session core (keyed by op-id for mosh, by clip+index for pixel/raw) and one non-modal
  editor launcher; the old modal `_fx_dialog` is gone.
- [~] P4: **composite-path fusion** — **investigated, deferred (not cleanly safe).**
  Prototyped folding the per-clip `finish_chain` (reverse/speed/fades/pixel/fx_mask)
  into the single `composite_video` filter_complex so a composite is one ffmpeg
  process instead of N+1. A pixel-equivalence harness (multi-track project across
  opacity/blend/speed/reverse/fades/pixel-FX/fx_mask, tolerance-compared frame-by-
  frame against the two-pass baseline) confirmed the fused output is **pixel-identical
  for the common cases** (differing only by ~1/255 codec-quantisation noise — actually
  *less* loss, since it drops one encode generation). **But** compositions mixing a
  **non-normal blend mode** (screen/multiply/…) with a **retimed or frame-count-changing
  clip** (speed/reverse, or a P-frame-duplicating mosh op) get a different output
  *frame count*: the old path bakes each clip's retiming into a pre-encoded AVI and the
  compositor decodes a clean 0-based CFR stream, whereas an in-graph `setpts` leaves
  stream-duration metadata that ffmpeg's `blend` length semantics read differently
  (verified: pre-retimed input → 16 frames, identical in-graph retime → 24; `trim`+PTS-
  regen didn't reconcile them). Since the composite path isn't the hot editing path
  (the flat path is) and the win is modest, shipping a length-changing edge wasn't
  worth it. **Revisit** as a *hybrid*: fuse only `blend == normal` layers (proven
  equivalent — plain overlay clamps to the canvas) and keep the per-clip finish for
  non-normal-blend layers. Harness: `scratchpad/p4_equiv.py` (FEAT/SIMPLE toggles).

### Wave 3 — editing & safety fundamentals
- [x] U10: timeline zoom + horizontal scroll — `TimelinePane(QScrollArea)` hosts the
  timeline; zoom stretches the widget width so all coordinate math is untouched.
  Ctrl+wheel zooms at the cursor, wheel pans, Shift+wheel scrolls vertically;
  `=`/`-`/`0` shortcuts + tool-strip buttons; ruler ticks use 1/2/5×10^k steps;
  waveform/ruler paint is visible-region-bounded; lane labels stick to the view
  edge; playback pages the view when the playhead exits it.
- [x] U24: quick-save in place (Ctrl+S, no dialog once a path is known) + Save As
  (Ctrl+Shift+S); project name + modified marker in the window title; save-frame
  moved to Ctrl+Shift+F.
- [x] U23/U34: File → Open recent (8 entries, dedup, prune-on-missing, clear);
  QSettings persists window geometry + both splitters (saved on close); per-category
  last-directory memory for import / project / export / save-frame dialogs.
- [x] U25: missing-media handling — `Project.load` first auto-repairs stale absolute
  paths when the project folder was moved (sibling `_assets` dir wins); anything
  still missing gets an ⚠ offline badge in the library, an offer-to-relink prompt
  on open, and File → Relink offline media…; `Project.relink_media` re-normalizes
  the new source in place (same id, clips/effects stay attached).
- [x] U7/U8: `_friendly_error` maps FileNotFound / ffmpeg-missing-input / permission /
  disk-full to actionable messages (with relink hints); ALL error surfacing is now
  non-modal — first line to the status bar, full text on a click-to-dismiss toast
  (`_Toast`), so a failing auto-refresh can't spam dialogs.
- [x] U6/U30: determinate progress — `Project.render(progress=…)` reports per-clip /
  per-layer steps (gen-guarded so cancelled jobs go quiet), the decode phase then
  counts streamed frames against the known total; the preview shows a "Rendering…"
  badge over the stale frames while any job runs. (ffmpeg `-progress` parsing wasn't
  needed: step + frame-stream granularity already gives an honest bar.)

### Wave 4 — deeper perf restructurings
- [x] P8: per-clip dependency-scoped seg-cache keys — `_media_state()` (one stat per
  media, computed once per render) + `_clip_media_deps()` (own media + flow source +
  `clip_ref`'d op sources; unknown modes fall back to depend-on-everything). Importing
  or touching unrelated media no longer busts other clips' cached segments.
- [x] P17: parallel per-clip segment rendering — `_parallel_segments` fans clips out
  over `engine.seg_workers` (≤4) threads; `engine._tmp`/seg-cache/motion-RGB-cache
  locked; per-key in-flight locks in `_clip_seg` keep duplicate-clip dedupe; the
  GIL-bound flow/raw numpy stage is serialised behind `_pixel_stage_lock` (measured
  2× SLOWER when run concurrently, and it multiplies peak RAM); cancel now sets an
  ffmpeg abort flag so queued workers can't spawn post-cancel processes. Measured:
  parallel output is bit-identical; composite path ~4–8% faster (ffmpeg already
  saturates cores internally, so process-level wins are modest); no path regressed.
- [x] P11: finish-output caching, two layers — (1) composite per-clip *finished*
  segments cached on (seg key + finish meta + geom + enc), so repositioning /
  opacity/blend edits skip every unchanged layer's finish ffmpeg (move-only
  composite re-render: 0.91s → 0.52s); (2) the flat fold cached on (ordered seg
  keys + meta), so an unchanged re-render (undo/redo hop, audio-only edit, manual
  refresh) copies the AVI instead of re-encoding (0.34s → ~0s).
- [x] P13 (flow): flow-only clips now stream end-to-end — `transfer_raw_iter`
  consumes the live ffmpeg decode in lockstep and yields warped frames straight
  into the encoder, holding one frame + the motion driver instead of ~3 whole
  clips. Raw FX stay whole-clip *by contract* (CDP audio-bend modes treat the clip
  as one buffer; feedback modes carry temporal state) — documented, not streamable.
- [x] U5: preview frames are now held as JPEG bytes (encoded on the decoder worker
  thread at ~0.8 ms/frame, ~7–10× smaller than QImages) and decoded on demand at
  display time (~1.1 ms — trivial against a 42 ms frame budget). A 60 s 720p preview
  drops from ~1.3 GB of QImages to ~150 MB.
- [x] P12: `Project._parsed` is now a byte-budgeted LRU (~1 GB of coded frames,
  lock-guarded for the parallel workers); evicted media re-parse transparently and
  borrowed `AviVideo` references stay valid.

### Wave 5 — polish backlog (pick opportunistically)
- [x] U27: A/B compare — hold "Source" flashes the clean source frame under the
  playhead (`controller.source_frame_for` maps preview→source through the base
  track layout, trim/speed/reverse aware; `fetch_source_frame` decodes one frame
  on a dedicated FFmpeg, LRU-cached). Playback pauses while held.
- [x] U28/U29: preview zoom/1:1/pan (`_PreviewView` scroll area; Ctrl+wheel 0.25–8×,
  drag-pan, Fit/1:1 buttons; busy badge pinned to the viewport) + sub-range loop
  (I/O markers drawn on a `_LoopSlider`; `_advance` wraps the range; stale range
  cleared on a shorter re-render).
- [x] U11/U12/U13: snapping (`_snap_adjust`, 12px, Alt bypasses) with a ghost +
  snap-line, reused by the library→timeline drop; **multi-select** (Ctrl/Shift-
  click, primary keeps the brighter outline, batch delete via
  `controller.remove_clips`); **copy/paste** of clips *with their effect stacks*
  (`copy_clips`/`paste_clips`, offsets preserved, fresh ids, skips offline media).
- [x] **Drag & drop + timeline legibility** (new): drop video files anywhere to
  batch-import (`controller.import_media_batch`); drag library media onto a lane
  to place at a snapped frame (`place_clip_at`, Easy-mode aware, one undo step);
  clips show an ≋N mosh-op badge and an orange melt-junction notch.
  Follow-up (B3): OS file dropped *onto a lane* imports but doesn't auto-place
  (needs async import→place chaining); import-anywhere already works.
- [x] U21/U22/U31/U32/U17: preset **Apply ▾** (Replace/Append; `apply_preset` already
  took `replace=`); per-effect **🎲** randomise (`random_params` in modes/base.py +
  `controller.randomise_effect`, one undo step); effect/pixel/raw lists grow to fit
  (`_fit_list_height`); library empty-state hint + right-click menu (add/relink);
  `setAccessibleName` on the icon-only buttons.
- [x] U15: bake/flow are undoable (snapshot pre-op + commit on success; snapshots
  carry media + bake_records, imported footage preserved on restore) and every
  undo entry is labelled ("Undo Move clip" / "Redo Bake"). revert-bake still
  clears undo (it deletes the baked file on disk).
- [x] U1/U4: onset detection warmed on the audio worker thread (beat_positions
  is a cache hit on the UI thread); the inspector stays live during read-only
  preview renders (`busy_is_preview`), locking only for heavy ops. U3 is largely
  covered by the debounced auto-refresh retry (edits during a render land next pass).
- [x] M8/M4/M6: every mosh/pixel/raw param has help text (asserted by a test);
  `pframe_drop.probability`, `iframe_pulse.period`, `pframe_echo.copies/delay`
  are automatable; CLI `--param` validates numbers/choices/ranges.
- [x] M2/M1: `modes/_gop.py` (`split_gops` + `map_pframe_runs`) shared across four
  modes (golden-tested); one `RegisteredMode` base for the three mode families.
- [x] M9/M7/M10: `rgb_iterative_shift` documented; effects table completed
  (motion_gain/pframe_stutter); CDP "bundled" claim corrected + `$MOSHIT_CDP_DIR`;
  `[raw]` numpy extra split out; packages auto-discovered; CI tests 3.9–3.12.
- [x] M5: selftest `_FakeEngine` gained a `__getattr__` drift guard and now
  exercises the composite render + multi-track audio plan/mix.

---

## Detailed findings

### Performance (pipeline)

- **P1.** Codec mosh recomputed even on seg-cache hits — `_render_flat`
  (project.py:911-921) eagerly runs `engine.mosh(...)` per clip before `_clip_seg`
  checks the cache; frames discarded on hit.
- **P2.** `_media_signature()` recomputed once per clip (project.py:367-386 → 353-365,
  stats ALL media each call) → O(N_clips × M_media) stat() per render.
- **P3.** `_motion_frames()` rebuilt per clip in the composite path (project.py:884),
  N×M `exists()` stats.
- **P4.** Composite path = one full ffmpeg finish per clip (`_clip_segment` →
  `finish_clips([seg])` project.py:896) + `composite_video`; flat path folds all clips
  into ONE finish_video.
- **P5.** Audio assembly = one ffmpeg per plan segment + concat (ffmpeg.py:381-463);
  whole processes spawned just for `anullsrc` silence.
- **P6.** Flow motion source decoded fresh on every cache miss (engine.py:339, 264-265).
- **P7.** Preview intermediates use full-quality qscale/gop (ffmpeg.py:604, 793).
- **P8.** Global `media_sig` in every seg key (project.py:384): one media mtime change
  busts ALL clips' segments.
- **P9.** `beats.onsets` / `waveform.peaks` re-read + re-scan the WAV per call
  (beats.py:35, waveform.py:14; called in a comprehension at controller.py:359).
- **P10.** `has_alpha` not cached (ffmpeg.py:354) — minor, import-time only.
- **P11.** No cache of folded finish output — contiguity-preserving repositions still
  re-run finish_video.
- **P12.** `Project._parsed` unbounded (project.py:217).
- **P13.** Flow holds ~3× the decoded clip in RAM (flow.py:88-89; also magnify_raw
  flow.py:154) — could stream (prev gray + accumulator).
- **P14.** Matte recomputed inside each blend helper (raw.py:200-246).
- **P15.** beats/waveform pure-Python inner loops (beats.py:50, waveform.py:46) —
  numpy-if-available fast path.
- **P16.** `avi.parse_avi` allocates ~2× file size transiently (avi.py:235-273). Noted.
- **P17.** Per-clip segment rendering embarrassingly parallel (project.py:938-939,
  971-985); blockers: `engine._tmp()` counter, seg-cache OrderedDict thread safety.
- **P18.** Audio segment builds independent but serial (ffmpeg.py:404).
- **P19.** Tiny move breaking contiguity flips flat → composite = ~10× cost cliff
  (project.py:754-778). Largely dissolved by P4.
- **P20.** Every refresh re-decodes the ENTIRE preview.avi (preview.py:64) even for a
  tail-only change.

### Usability (GUI)

- **U1.** Beat detection on the UI thread (controller.py:343-360 via widgets.py:558-570).
- **U2.** Every edit re-renders + re-decodes the whole sequence; 350ms debounce bypassed
  by `immediate=True` everywhere (app.py:564-656).
- **U3.** No edit queuing during renders (controller.py:294, app.py:677-679).
- **U4.** Whole inspector disabled during every render (app.py:838).
- **U5.** Preview holds all frames as QImages, uncapped (preview.py:64-103).
- **U6.** No determinate progress anywhere (app.py:340-342). Cancel works well.
- **U7.** Raw exception strings in modal QMessageBox (app.py:845-847).
- **U8.** Failing render + auto-refresh = repeated modal error dialogs.
- **U9.** Silent failures: alpha-map extraction → None (project.py:493); silent KeyError
  returns across controller; QtMultimedia absence unexplained.
- **U10.** No timeline zoom/scroll (widgets.py:762-764).
- **U11.** No multi-select (widgets.py:668).
- **U12.** No snapping (widgets.py:1041-1043).
- **U13.** No copy/paste of clips or effect stacks (Ctrl+D only).
- **U14.** No numeric trim entry (drag-only).
- **U15.** Undo cleared by bake/bake-stack/flow/open/new (controller.py:404-988 passim);
  import not covered; no labels.
- **U16.** Shortcut gaps: import, add-to-timeline, zoom, next/prev clip, tool switch.
- **U17.** Media library: no context menu, no thumbnails, no empty-state hint.
- **U18.** Modal param dialogs, values only on OK, no live preview (widgets.py:455,
  517-537). Biggest iteration pain.
- **U19.** Two overlapping optical-flow paths, minimal signposting (widgets.py:1618 vs
  2019-2077).
- **U20.** Motion-source requirement (`clip_ref`) only errors at OK time.
- **U21.** Preset Apply replaces the whole stack, no append/confirm (controller.py:832-848).
- **U22.** Randomise buried inside the effect dialog.
- **U23.** No recent projects / autosave / crash recovery / QSettings.
- **U24.** Ctrl+S always re-prompts getSaveFileName (app.py:802-813); title fixed
  "Moshit[*]" (app.py:197).
- **U25.** Missing media on open = opaque parse_avi crash at first render; no offline
  state, no relink.
- **U26.** VERSION=1 written but never checked in from_dict (project.py:200, 1218-1238).
- **U27.** No A/B compare (moshed vs original).
- **U28.** No preview zoom/1:1/pan.
- **U29.** No sub-range loop.
- **U30.** No "rendering…" overlay on the preview itself.
- **U31.** Effect/pixel/raw lists at tiny fixed heights (widgets.py:1565, 1729, 1821).
- **U32.** Emoji-glyph buttons lack accessible names.
- **U33.** No export progress detail.
- **U34.** No last-directory memory in any file dialog.

### Modes / CLI / docs / tests / packaging

- **M1.** Three parallel registries (`MoshMode`/`PixelMode`/`RawMode`) duplicate the
  same `__init_subclass__`/defaults/resolve/registry surface (~90 lines).
- **M2.** GOP-run splitting reimplemented five times (gop_scramble, pframe_reverse,
  pingpong, pframe_stutter, iframe_pulse).
- **M3.** Reproducibility is GOOD — every randomized mode is seeded.
- **M4.** Only 5 params `automatable`; natural additions: `pframe_drop.probability`,
  `iframe_pulse.period`, `pframe_echo` knobs, `momentum.strength`,
  `pframe_duplicate.stride`.
- **M5.** `_FakeEngine` (cli.py:336-399) covers 9 of ~20 engine methods; composite/
  audio-mix paths never exercised by selftest; drift → raw AttributeError.
- **M6.** CLI `_coerce` doesn't validate `choice`/`clip_ref` values (cli.py:46-53).
- **M7.** CDP not actually bundled (gitignored, not packaged); README overstates;
  document `$MOSHIT_CDP_DIR`. Non-Linux: silent passthrough.
- **M8.** Help-text gaps: `bitrot`/`pframe_drop` seeds; `motion_weave.hold_base_iframe`;
  ALL PixelMode params; PixelSort `hi`/`by`/`order`.
- **M9.** README: `rgb_iterative_shift` undocumented; effects table omits `motion_gain`/
  `pframe_stutter` while presenting as a complete inventory.
- **M10.** `requires-python = ">=3.9"` vs CI 3.11-3.12 only; numpy gated behind the
  `flow` extra though raw FX/CDP need it without OpenCV; `packages` list hand-written.
- **M11.** Test gaps: no test_avi.py / test_engine.py / test_ffmpeg.py; preview.py
  essentially untested; ~11 mosh modes untested behaviorally (B4).
