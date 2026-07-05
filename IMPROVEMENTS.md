# Moshit improvement backlog — performance & usability

Working notes from a full review (2026-07-03): three parallel audits over the render
pipeline, the GUI, and the modes/CLI/docs/tests surface, with the sharpest findings
verified by hand. Items carry stable IDs (B=bug, P=performance, U=usability, M=modes/
misc) so commits can reference them. Tick items off as they land.

---

## Verified correctness bugs

- [ ] **B1. `motion_weave` infinite loop** (`moshit/modes/motion_weave.py:45-60`).
  `base_run=0, motion_run≥1` passes the both-zero guard; `while bi < len(base_p)`
  never advances `bi` while appending motion frames forever → hang + unbounded
  memory. Reachable from the GUI (param `lo=0`).
- [ ] **B2. `motion_gain` automation can't cross 1.0** (`moshit/modes/motion_gain.py:35-54`).
  Amplify/reduce branch chosen once from the static `gain` (curve start value); the
  amplify branch never thins, the reduce branch clamps `g ≤ 1.0` so it never
  amplifies — an automated 0.5→2.0 curve silently half-works. Fix: one per-frame
  error-diffusion loop that both duplicates (acc ≥ 1) and drops (acc < 1).
- [ ] **B3. `pingpong` dead code + interior-I-frame loss** (`moshit/modes/pingpong.py:37-42`).
  `out = [f for f in frames if f.is_iframe]` is never used; only `frames[0]` survives
  as anchor in `per_gop=False` mode. Delete the dead line, pin the intended behavior
  with a test.
- [ ] **B4. ~11 mosh modes have zero behavioral tests** (`bitrot`, `gop_scramble`,
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
- [ ] U24: quick-save in place + Save As; project name in window title.
- [ ] U23/U34: recent projects, QSettings for window/splitter state, last-directory memory.
- [ ] U25: missing-media detection on open, offline badge, relink dialog.
- [ ] U7/U8: human-friendly errors; non-modal toast for auto-refresh failures.
- [ ] U6/U30: determinate progress (parse ffmpeg `-progress`) + preview "rendering…" overlay.

### Wave 4 — deeper perf restructurings
- [ ] P8: per-clip dependency-scoped seg-cache keys (stop global cache busts).
- [ ] P17: parallel per-clip segment rendering (first make `engine._tmp()` and the
  seg-cache OrderedDict thread-safe).
- [ ] P11: cache the folded finish output keyed on (ordered seg keys + layout).
- [ ] P13: stream flow/raw stages instead of ~3× whole-clip RAM.
- [ ] U5: cap preview QImage RAM / decode-on-demand.
- [ ] P12: bound `Project._parsed`.

### Wave 5 — polish backlog (pick opportunistically)
- [ ] U27: A/B compare (moshed vs original) in preview.
- [ ] U28/U29: preview zoom/1:1/pan; sub-range loop.
- [ ] U11/U12/U13: multi-select, snapping, copy/paste of clips + effect stacks.
- [ ] U15: undo survives bake/flow; undo labels.
- [ ] U1/U3/U4: beat detection off the UI thread; queue edits during renders; stop
  greying the whole inspector.
- [ ] M8/M4/M6: fill missing param help text; broaden `automatable`; CLI choice validation.
- [ ] M2/M1: shared `group_by_gop` helper; registry mixin for the three mode classes.
- [ ] M9/M7/M10: document `rgb_iterative_shift`; fix stale README effects table; correct
  "bundled CDP8" claim + document `$MOSHIT_CDP_DIR`; split numpy out of the `flow`
  extra; align `requires-python` with CI.
- [ ] M5: selftest `_FakeEngine` — derive from an ABC or cover composite/audio paths.

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
